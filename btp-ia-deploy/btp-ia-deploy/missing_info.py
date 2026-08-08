"""
Gère la boucle "supposition -> question à l'utilisateur -> réponse (texte ou
pièce jointe) -> recalcul" pendant la phase de raisonnement.

Règle: on ne code plus AUCUNE constante par défaut en silence (épaisseur
béton de propreté, épaisseur dallage, épaisseur voile, marge de fouille,
hauteur de soubassement, profondeur d'ancrage). Chaque valeur a une question
associée. Certaines ont une valeur usuelle pré-remplie (modifiable) pour ne
pas être pénibles à valider ; d'autres (hauteur, profondeur) n'ont pas de
valeur par défaut car elles dépendent trop du projet pour en suggérer une.

Une réponse peut être un nombre saisi directement, ou une pièce jointe
(photo/PDF d'une coupe) -> on rappelle la vision Gemini pour en extraire la
valeur demandée.
"""

import math
import re
import fitz  # PyMuPDF
from gemini_client import call_vision_json, GeminiError

# Chaque question : condition(bilan) -> bool décide si on la pose (inutile de
# demander une épaisseur de voile si aucun voile n'a été détecté).
QUESTION_SPECS = [
    {
        "key": "hauteur_soubassement_m",
        "question": ("Quelle est la hauteur de soubassement (du dessus des semelles/longrines "
                      "jusqu'au niveau du sol fini) en mètres ? Nécessaire pour chiffrer les "
                      "potelets et voiles en soubassement (postes 3.5 et 3.6)."),
        "default": None,
        "condition": lambda b: bool(b.get("poteaux", {}).get("par_section")) or bool(b.get("voiles_par_type")),
    },
    {
        "key": "profondeur_ancrage_m",
        "question": ("Quelle est la profondeur d'ancrage moyenne des fondations (du terrain "
                      "naturel au fond de fouille) en mètres ? Nécessaire pour chiffrer les "
                      "fouilles en puits et en rigoles (postes 2.3 et 2.4)."),
        "default": None,
        "condition": lambda b: bool(b.get("semelles")) or bool(b.get("longrines_par_section")) or bool(b.get("radiers")),
    },
    {
        "key": "marge_fouille_pct",
        "question": ("Quelle marge de fouille (surprofondeur/surlargeur de terrassement, en %) "
                      "veux-tu appliquer au volume théorique des fouilles ? Valeur usuelle : 15%."),
        "default": 15,
        "condition": lambda b: bool(b.get("semelles")) or bool(b.get("longrines_par_section")) or bool(b.get("radiers")),
    },
    {
        "key": "epaisseur_beton_proprete_cm",
        "question": ("Quelle épaisseur de béton de propreté sous les semelles (en cm) ? "
                      "Valeur usuelle : 10cm."),
        "default": 10,
        "condition": lambda b: bool(b.get("semelles")),
    },
    {
        "key": "epaisseur_dallage_cm",
        "question": "Quelle épaisseur de dallage au sol (en cm) ? Valeur usuelle : 13cm.",
        "default": 13,
        "condition": lambda b: not b.get("surface_dallage", {}).get("donnee_indisponible", True),
    },
    {
        "key": "epaisseur_voile_cm",
        "question": "Quelle épaisseur des voiles en soubassement (en cm) ? Valeur usuelle : 20cm.",
        "default": 20,
        "condition": lambda b: bool(b.get("voiles_par_type")),
    },
    {
        "key": "a_beton_banche",
        "question": ("As-tu des fondations filantes en béton banché ou cyclopéen (non armé), "
                      "distinctes des semelles isolées et des semelles filantes armées ? Notre "
                      "extraction ne peut pas distinguer ce type d'élément automatiquement "
                      "(poste 3.2). Réponds oui ou non."),
        "default": "non",
        "kind": "boolean",
        "condition": lambda b: bool(b.get("semelles")) or bool(b.get("longrines_par_section")) or bool(b.get("radiers")),
    },
    {
        "key": "beton_banche_section_cm",
        "question": ("Quelle est la section du béton banché/cyclopéen (largeur x hauteur, en cm, "
                      "ex: 40x60) ?"),
        "default": None,
        "kind": "dimension",
        "condition": lambda b: False,  # ne s'affiche qu'au tour de suivi, voir condition_with_answers
        "condition_with_answers": lambda b, a: a.get("a_beton_banche") is True,
    },
    {
        "key": "beton_banche_longueur_developpee_m",
        "question": "Quelle est la longueur développée totale de ce béton banché/cyclopéen (en m) ?",
        "default": None,
        "kind": "scalar",
        "condition": lambda b: False,
        "condition_with_answers": lambda b, a: a.get("a_beton_banche") is True,
    },
    {
        "key": "epaisseur_dalle_pleine_cm",
        "question": ("Quelle épaisseur de dalle pleine d'étage (en cm) ? Valeur usuelle : 20cm. "
                      "Nécessaire pour chiffrer le poste 3.17 à partir de la surface de dalle pleine."),
        "default": 20,
        "condition": lambda b: not b.get("surfaces_superstructure", {}).get("donnee_indisponible", True)
                                and b.get("surfaces_superstructure", {}).get("surface_dalle_pleine_m2") is not None,
    },
    {
        "key": "section_semelle_filante_cm",
        "question": ("Quelle est la section de la semelle filante sous les longrines (largeur x hauteur, "
                      "en cm, ex: 40x20) ? La longueur développée est déjà connue (= longueur totale des "
                      "longrines, poste 3.7) -- il ne manque que la section pour calculer le volume (poste 3.4)."),
        "default": "40x20",
        "kind": "dimension",
        "condition": lambda b: bool(b.get("longrines_par_section")),
    },
]

ATTACHMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "valeur": {"type": ["number", "null"], "description": "La valeur demandée, dans l'unité précisée dans la question. Null si vraiment illisible sur ce document."},
        "note": {"type": ["string", "null"], "description": "Brève note si la lecture est incertaine, sinon null."},
    },
    "required": ["valeur", "note"],
}


def detect_missing_questions(bilan: dict, answers: dict = None) -> list:
    """Renvoie la liste des questions à poser, en fonction de ce qui a
    réellement été trouvé sur les plans (condition sur `bilan`) ET,
    éventuellement, des réponses déjà données (condition_with_answers) --
    ce qui permet un vrai tour de suivi (ex: une question ne s'affiche que
    si l'utilisateur a répondu 'oui' à une précédente)."""
    answers = answers or {}
    questions = []
    for spec in QUESTION_SPECS:
        applicable = spec["condition"](bilan)
        if "condition_with_answers" in spec:
            applicable = spec["condition_with_answers"](bilan, answers)
        if applicable:
            questions.append({
                "key": spec["key"], "question": spec["question"],
                "default": spec["default"], "kind": spec.get("kind", "scalar"),
            })
    return questions


def extract_value_from_attachment(file_bytes: bytes, filename: str, question_text: str) -> float | None:
    """Rappelle la vision Gemini sur une pièce jointe (image ou PDF, 1ère
    page) pour en extraire la valeur demandée."""
    is_pdf = filename.lower().endswith(".pdf")
    if is_pdf:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        page = doc[0]
        zoom = 260 / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        image_bytes = pix.tobytes("png")
        doc.close()
        mime = "image/png"
    else:
        image_bytes = file_bytes
        mime = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"

    prompt = (
        "Tu analyses un document technique BTP (coupe, élévation ou plan). "
        f"Question précise à résoudre: {question_text}\n"
        "Lis la cote correspondante directement sur le dessin si elle est annotée. "
        "N'invente jamais une valeur non explicitement lisible -- renvoie null si absente."
    )
    result = call_vision_json(image_bytes, prompt, ATTACHMENT_SCHEMA, mime=mime)
    return result.get("valeur")


DIMENSION_ATTACHMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "largeur_cm": {"type": ["number", "null"], "description": "Largeur de la section, en cm. Null si illisible."},
        "hauteur_cm": {"type": ["number", "null"], "description": "Hauteur de la section, en cm. Null si illisible."},
        "note": {"type": ["string", "null"], "description": "Brève note si la lecture est incertaine, sinon null."},
    },
    "required": ["largeur_cm", "hauteur_cm", "note"],
}

DIMENSION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*$")


def extract_dimension_from_attachment(file_bytes: bytes, filename: str, question_text: str):
    """Variante de extract_value_from_attachment pour une section (largeur x
    hauteur) au lieu d'une valeur scalaire unique. Renvoie 'LARGEURxHAUTEUR'
    (cm) ou None."""
    is_pdf = filename.lower().endswith(".pdf")
    if is_pdf:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        page = doc[0]
        zoom = 260 / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        image_bytes = pix.tobytes("png")
        doc.close()
        mime = "image/png"
    else:
        image_bytes = file_bytes
        mime = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"

    prompt = (
        "Tu analyses un document technique BTP (coupe, élévation ou plan). "
        f"Question précise à résoudre: {question_text}\n"
        "Lis les deux cotes (largeur et hauteur de la section) directement sur le dessin si "
        "annotées. N'invente jamais une valeur non explicitement lisible -- renvoie null si absente."
    )
    result = call_vision_json(image_bytes, prompt, DIMENSION_ATTACHMENT_SCHEMA, mime=mime)
    if result.get("largeur_cm") is not None and result.get("hauteur_cm") is not None:
        return f"{result['largeur_cm']}x{result['hauteur_cm']}"
    return None


def resolve_answer(key: str, question_text: str, text_answer: str | None,
                    file_bytes: bytes | None, filename: str | None, kind: str = "scalar"):
    """Renvoie (valeur_ou_None, source_str). Pour kind='dimension', la valeur
    est une chaîne 'LARGEURxHAUTEUR' (cm) plutôt qu'un float. Pour
    kind='boolean', la valeur est un bool (pas de pièce jointe possible)."""
    if kind == "boolean":
        if text_answer is None:
            return None, None
        normalized = text_answer.strip().lower()
        if normalized in ("oui", "o", "yes", "y", "true", "1"):
            return True, "utilisateur"
        if normalized in ("non", "n", "no", "false", "0"):
            return False, "utilisateur"
        return None, None

    if kind == "dimension":
        if file_bytes:
            try:
                val = extract_dimension_from_attachment(file_bytes, filename or "piece_jointe", question_text)
                if val is not None:
                    return val, f"pièce jointe ({filename})"
            except GeminiError:
                pass
        if text_answer and DIMENSION_RE.match(text_answer):
            m = DIMENSION_RE.match(text_answer.strip())
            return f"{m.group(1)}x{m.group(2)}", "utilisateur"
        return None, None

    if file_bytes:
        try:
            val = extract_value_from_attachment(file_bytes, filename or "piece_jointe", question_text)
            if val is not None:
                return float(val), f"pièce jointe ({filename})"
        except GeminiError:
            pass  # on retombe sur le texte si la lecture de la pièce jointe échoue

    if text_answer not in (None, ""):
        try:
            return float(str(text_answer).replace(",", ".").strip()), "utilisateur"
        except ValueError:
            return None, None

    return None, None


def _section_area_m2(section: str):
    section = (section or "").strip().upper()
    if section.startswith("D"):
        try:
            r_m = (float(section[1:]) / 100) / 2
            return math.pi * r_m ** 2
        except ValueError:
            return None
    if "X" in section:
        try:
            a, b = section.split("X")
            return (float(a) / 100) * (float(b) / 100)
        except ValueError:
            return None
    return None


_SECTION_RE_MI = re.compile(r"(\d+(?:[.,]\d+)?)\s*[xX×*/-]\s*(\d+(?:[.,]\d+)?)")

def _section_width_m(section: str):
    """Extrait la largeur (plus petite dimension) d'une section '20x40' (cm) -> m.
    Utilise la même regex robuste que pipeline.py pour gérer les variantes
    de format ('20x40', '20X40cm', '20 x 40', '20/40', etc.)."""
    if not section:
        return None
    m = _SECTION_RE_MI.search(section.strip())
    if not m:
        return None
    try:
        a = float(m.group(1).replace(",", "."))
        b = float(m.group(2).replace(",", "."))
        return min(a, b) / 100
    except ValueError:
        return None


def apply_answers_to_bilan(bilan: dict, answers: dict) -> dict:
    """Recalcule en Python (pas de LLM) tous les postes qui dépendaient de
    suppositions, avec les valeurs désormais confirmées par l'utilisateur.
    Ne laisse plus aucune valeur par défaut silencieuse."""
    postes = bilan["volumes_beton"]["postes"]

    hauteur = answers.get("hauteur_soubassement_m")
    profondeur = answers.get("profondeur_ancrage_m")
    marge_pct = answers.get("marge_fouille_pct")
    ep_proprete_cm = answers.get("epaisseur_beton_proprete_cm")
    ep_dallage_cm = answers.get("epaisseur_dallage_cm")
    ep_voile_cm = answers.get("epaisseur_voile_cm")

    # ---- 3.1 Béton de propreté (épaisseur confirmée) ----
    if ep_proprete_cm is not None and bilan.get("semelles"):
        vol = sum(s["a_m"] * s["b_m"] * (ep_proprete_cm / 100) * s["nombre"] for s in bilan["semelles"])
        postes["beton_proprete_semelles"] = {
            "designation_devis": "3.1 Béton de propreté pour semelles isolées", "unite": "m3",
            "volume_m3": round(vol, 2), "donnee_indisponible": False,
            "raison": f"Épaisseur confirmée par l'utilisateur: {ep_proprete_cm}cm.",
        }

    # ---- 3.5 / 3.6 Potelets & voiles (hauteur + épaisseur voile confirmées) ----
    if hauteur is not None:
        vol_potelets = sum(
            (_section_area_m2(item["section"]) or 0) * item["nombre_total"] * hauteur
            for item in bilan["poteaux"]["par_section"]
        )
        postes["potelets"] = {
            "designation_devis": "3.5 Béton armé pour potelets", "unite": "m3",
            "volume_m3": round(vol_potelets, 2), "donnee_indisponible": False,
            "raison": f"Hauteur de soubassement confirmée: {hauteur}m.",
        }

        ep_voile_m = (ep_voile_cm or 20) / 100
        vol_voiles = sum(v["longueur_totale_m"] * ep_voile_m * hauteur for v in bilan.get("voiles_par_type", []))
        postes["voiles_soubassement"] = {
            "designation_devis": "3.6 Béton armé pour voiles en soubassement", "unite": "m3",
            "volume_m3": round(vol_voiles, 2), "donnee_indisponible": False,
            "raison": f"Hauteur de soubassement confirmée: {hauteur}m. Épaisseur voile confirmée: {ep_voile_cm}cm.",
        }

    # ---- 2.3 / 2.4 Fouilles (profondeur + marge confirmées) ----
    if profondeur is not None:
        marge = 1 + (marge_pct or 0) / 100
        surface_semelles = sum(s["a_m"] * s["b_m"] * s["nombre"] for s in bilan.get("semelles", []))
        vol_fouilles_puits = surface_semelles * profondeur * marge
        postes["fouilles_puits_semelles"] = {
            "designation_devis": "2.3 Fouilles en puits pour semelles isolées", "unite": "m3",
            "volume_m3": round(vol_fouilles_puits, 2), "donnee_indisponible": False,
            "raison": f"Profondeur d'ancrage confirmée: {profondeur}m. Marge de fouille confirmée: {marge_pct}%.",
        }

        largeur_longrines = sum(
            (_section_width_m(item["section"]) or 0) * item["longueur_totale_m"]
            for item in bilan.get("longrines_par_section", [])
        )
        vol_fouilles_rigoles = largeur_longrines * profondeur * marge
        postes["fouilles_rigoles_fondations"] = {
            "designation_devis": "2.4 Fouilles en rigoles pour fondations filantes", "unite": "m3",
            "volume_m3": round(vol_fouilles_rigoles, 2), "donnee_indisponible": False,
            "raison": f"Profondeur d'ancrage confirmée: {profondeur}m. Marge de fouille confirmée: {marge_pct}%.",
        }

    # ---- 3.8 Dallage (épaisseur dallage + épaisseur voile confirmées, la
    # surface nette dépend de l'épaisseur voile utilisée pour la déduction) ----
    if ep_dallage_cm is not None and not bilan.get("surface_dallage", {}).get("donnee_indisponible", True):
        sd = bilan["surface_dallage"]
        surface_brute = sd.get("surface_brute_m2", 0)
        deductions = sd.get("deductions_m2", {})
        deduction_poteaux = deductions.get("poteaux", 0)
        deduction_longrines = deductions.get("longrines", 0)
        # La déduction "voiles" d'origine utilisait l'ancienne épaisseur par défaut (20cm) ;
        # on la met à l'échelle de l'épaisseur voile confirmée si elle est connue.
        deduction_voiles_brute = deductions.get("voiles", 0)
        ep_voile_ref_cm = ep_voile_cm if ep_voile_cm is not None else 20
        deduction_voiles = deduction_voiles_brute * (ep_voile_ref_cm / 20) if deduction_voiles_brute else 0

        surface_nette = surface_brute - deduction_poteaux - deduction_longrines - deduction_voiles
        vol_dallage = surface_nette * (ep_dallage_cm / 100)
        postes["dallage"] = {
            "designation_devis": "3.8 Béton légèrement armé pour dallage au sol", "unite": "m3",
            "volume_m3": round(vol_dallage, 2), "donnee_indisponible": False,
            "raison": f"Épaisseur dallage confirmée: {ep_dallage_cm}cm. Surface nette recalculée: {round(surface_nette, 1)}m².",
        }

    # ---- 3.17 Dalle pleine (surface déjà connue depuis le plan archi,
    # épaisseur confirmée par l'utilisateur) ----
    ep_dalle_pleine_cm = answers.get("epaisseur_dalle_pleine_cm")
    surf_super = bilan.get("surfaces_superstructure", {})
    if ep_dalle_pleine_cm is not None and surf_super.get("surface_dalle_pleine_m2") is not None:
        vol_dp = surf_super["surface_dalle_pleine_m2"] * (ep_dalle_pleine_cm / 100)
        postes["dalle_pleine_superstructure"] = {
            "designation_devis": "3.17 Béton armé pour dalle pleine", "unite": "m3",
            "volume_m3": round(vol_dp, 2), "donnee_indisponible": False,
            "raison": f"Épaisseur confirmée par l'utilisateur: {ep_dalle_pleine_cm}cm.",
        }

    # ---- 3.2 Béton banché/cyclopéen -- l'extraction ne peut pas distinguer
    # structurellement ce type d'élément; on procède en 2 temps: d'abord un
    # oui/non, puis (si oui) section + longueur développée, recalculées en
    # Python comme partout ailleurs (jamais un volume donné directement). ----
    a_beton_banche = answers.get("a_beton_banche")
    if a_beton_banche is False:
        postes["beton_banche_fondation_filante"] = {
            "designation_devis": "3.2 Béton banché ou cyclopéen pour fondations filantes", "unite": "m3",
            "volume_m3": 0.0, "donnee_indisponible": False,
            "raison": "Confirmé par l'utilisateur : aucun béton banché/cyclopéen sur ce projet.",
        }
    elif a_beton_banche is True:
        section_bb = answers.get("beton_banche_section_cm")
        longueur_bb = answers.get("beton_banche_longueur_developpee_m")
        if section_bb and longueur_bb is not None:
            aire = _section_area_m2(section_bb)
            if aire is not None:
                vol_banche = aire * longueur_bb
                postes["beton_banche_fondation_filante"] = {
                    "designation_devis": "3.2 Béton banché ou cyclopéen pour fondations filantes", "unite": "m3",
                    "volume_m3": round(vol_banche, 2), "donnee_indisponible": False,
                    "source_override": "utilisateur",
                    "raison": (
                        f"Présence confirmée par l'utilisateur. Section: {section_bb}cm, longueur "
                        f"développée: {longueur_bb}m. Rappel : vérifie qu'il n'y a pas de double "
                        "comptage avec les semelles filantes armées (poste 3.4)."
                    ),
                }
            else:
                postes["beton_banche_fondation_filante"] = {
                    "designation_devis": "3.2 Béton banché ou cyclopéen pour fondations filantes", "unite": "m3",
                    "volume_m3": None, "donnee_indisponible": True,
                    "raison": f"Section '{section_bb}' non reconnue (format attendu: '40x60').",
                }
        else:
            postes["beton_banche_fondation_filante"] = {
                "designation_devis": "3.2 Béton banché ou cyclopéen pour fondations filantes", "unite": "m3",
                "volume_m3": None, "donnee_indisponible": True,
                "raison": "Présence confirmée mais section et/ou longueur développée pas encore renseignées.",
            }

    # ---- 3.4 Semelles filantes: longueur développée = longueur totale des
    # longrines (poste 3.7), section confirmée par l'utilisateur ----
    section_sf = answers.get("section_semelle_filante_cm")
    if section_sf:
        m = DIMENSION_RE.match(str(section_sf).strip())
        if m:
            largeur_sf_cm, hauteur_sf_cm = float(m.group(1)), float(m.group(2))
            # Cellules numériques dérivées, injectées dans `answers` pour que la
            # feuille Excel "Paramètres" les reçoive aussi (formules éditables).
            answers["largeur_semelle_filante_cm"] = largeur_sf_cm
            answers["hauteur_semelle_filante_cm"] = hauteur_sf_cm

            longueur_developpee = sum(
                item["longueur_totale_m"] for item in bilan.get("longrines_par_section", [])
            )
            vol_sf = (largeur_sf_cm / 100) * (hauteur_sf_cm / 100) * longueur_developpee
            postes["radier_semelles_filantes"] = {
                "designation_devis": "3.4 Béton armé pour semelles filantes et radier partiel", "unite": "m3",
                "volume_m3": round(vol_sf, 2), "donnee_indisponible": False,
                "source_override": "utilisateur",
                "raison": (
                    f"Section confirmée par l'utilisateur ({section_sf}cm). Longueur développée = "
                    f"longueur totale des longrines ({round(longueur_developpee, 2)}m, poste 3.7)."
                ),
            }

    bilan["volumes_beton"]["a_confirmer_ou_completer_en_aval"] = [
        {"poste": p["designation_devis"], "raison": p["raison"]}
        for p in postes.values() if p.get("donnee_indisponible")
    ]
    return bilan
