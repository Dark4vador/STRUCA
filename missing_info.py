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
from schemas import classify_title
from pipeline import extract_cartouche_title

# Chaque question : condition(bilan) -> bool décide si on la pose (inutile de
# demander une épaisseur de voile si aucun voile n'a été détecté).
QUESTION_SPECS = [
    {
        "key": "hauteur_soubassement_m",
        "question": ("Quelle est la hauteur de soubassement (du dessus des semelles/longrines "
                      "jusqu'au niveau du sol fini) en mètres ? Nécessaire pour chiffrer les "
                      "potelets et voiles en soubassement (postes 3.5 et 3.6)."),
        "default": None,
        "condition": lambda b: (
            bool(b.get("poteaux", {}).get("par_section"))
            or bool(b.get("poteaux", {}).get("total_legende_par_section"))
            or bool(b.get("poteaux", {}).get("total_legende_global"))
            or bool(b.get("voiles_par_type"))
        ),
    },
    {
        # v41 -- distincte de hauteur_soubassement_m: hauteur d'étage
        # courant (poteaux/voiles de superstructure, postes 3.11/3.13),
        # presque jamais la même valeur que le soubassement.
        "key": "hauteur_etage_courant_m",
        "question": (
            "Quelle est la hauteur d'étage courant (du sol fini d'un niveau au sol fini du niveau "
            "suivant) en mètres ? Nécessaire pour chiffrer les poteaux et voiles de superstructure "
            "détectés sur le(s) plan(s) de coffrage (postes 3.11 et 3.13). Valeur usuelle : 3.0m si "
            "un seul niveau de coffrage détecté (à ajuster si plusieurs étages de hauteurs différentes)."
        ),
        "default": None,
        "condition": lambda b: (
            bool(b.get("poteaux_coffrage_par_section"))
            or bool(b.get("voiles_coffrage_par_type"))
            or bool(b.get("raidisseurs_coffrage_par_section"))
        ),
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
        # v40 -- bug de dépendance circulaire corrigé: cette condition ne
        # se déclenchait QUE si la surface était DÉJÀ disponible au moment
        # où les questions sont générées (un seul lot, en début de
        # traitement -- voir server.py: _run_job). Or si la surface est
        # encore indisponible à ce moment précis, c'est justement PARCE QUE
        # la question surface_batiment_totale_m2 va être posée dans le
        # MÊME lot -- mais comme les deux conditions sont évaluées sur le
        # bilan d'AVANT toute réponse, epaisseur_dallage_cm ne pouvait
        # jamais apparaître dans ce cas précis, même une fois la surface
        # confirmée par l'utilisateur. Poser cette question systématiquement
        # dès qu'il y a une extraction infrastructure en cours (semelles
        # détectées) coûte rien si la surface ne se résout jamais (réponse
        # simplement inutilisée) et corrige ce blocage.
        "condition": lambda b: (
            bool(b.get("semelles"))
            or not b.get("surface_dallage", {}).get("donnee_indisponible", True)
        ),
    },
    {
        # v28 -- si la surface brute vient du repli "approximation rectangle"
        # (aucun plan architectural exploitable trouvé dans le document
        # principal), propose de joindre le vrai plan architectural (souvent
        # un fichier séparé, ex: APD) pour une surface bien plus fiable.
        # Optionnelle : si l'utilisateur ne répond rien, l'approximation
        # rectangle existante reste utilisée (voir condition de la question
        # epaisseur_dallage_cm ci-dessus, qui ne dépend pas de celle-ci).
        "key": "surface_batiment_totale_m2",
        "question": (
            "La surface du bâtiment n'a pas pu être calculée de façon fiable depuis les plans "
            "fournis (pas de plan architectural exploitable, et pas de cote totale exploitable non "
            "plus sur le plan de fondation). Indique la surface totale (m²) si tu la connais, ou "
            "joins le plan architectural (ex: plan d'aménagement RDC) pour une estimation "
            "automatique par somme des surfaces de pièces. Laisse vide si tu n'as pas cette info -- "
            "le poste 3.8 (dallage) restera à compléter manuellement dans ce cas."
        ),
        "default": None,
        "optional": True,
        "condition": lambda b: (
            "approximation rectangle" in (b.get("surface_dallage", {}).get("source") or "")
            or b.get("surface_dallage", {}).get("donnee_indisponible", True)
        ),
    },
    {
        "key": "epaisseur_voile_cm",
        "question": "Quelle épaisseur des voiles en soubassement (en cm) ? Valeur usuelle : 20cm.",
        "default": 20,
        "condition": lambda b: bool(b.get("voiles_par_type")),
    },
    {
        "key": "volume_beton_banche_m3",
        "question": ("As-tu des fondations filantes en béton banché ou cyclopéen (non armé), "
                      "distinctes des semelles isolées et des semelles filantes armées ? Si oui, indique "
                      "le volume total en m³ (0 si aucune -- notre extraction ne peut pas distinguer "
                      "ce type d'élément automatiquement, poste 3.2)."),
        "default": 0,
        "kind": "scalar",
        "condition": lambda b: bool(b.get("semelles")) or bool(b.get("longrines_par_section")) or bool(b.get("radiers")),
    },
    {
        # v37 -- catch-all manuel: la bèche d'escalier n'est presque jamais
        # dessinée/cotée sur un plan d'exécution BTP standard (c'est un
        # détail d'exécution, pas une cote structurelle) -- même logique que
        # volume_beton_banche_m3 ci-dessus: si l'utilisateur connaît le
        # volume (devis antérieur, bordereau fournisseur...), autant lui
        # permettre de le saisir directement plutôt que de laisser le poste
        # bloqué "à compléter manuellement" sans recours.
        "key": "volume_beche_escalier_m3",
        "question": (
            "La bèche d'escalier n'est pas cotée sur les plans trouvés (fréquent -- c'est un détail "
            "d'exécution rarement dessiné). Si tu connais son volume (m³), indique-le directement, "
            "sinon laisse vide et ce poste (3.10) restera à compléter manuellement."
        ),
        "default": None,
        "optional": True,
        "condition": lambda b: any(e.get("beche") is None or not (
            e["beche"].get("longueur_m") and e["beche"].get("largeur_cm") and e["beche"].get("hauteur_cm")
        ) for e in b.get("escaliers", [])),
    },
    {
        "key": "section_semelle_filante_cm",
        "question": ("Quelle est la section de la semelle filante sous les longrines (largeur x hauteur, "
                      "en cm, ex: 40x20) ? La longueur développée est déjà connue (= longueur totale des "
                      "longrines, poste 3.7) -- il ne manque que la section pour calculer le volume (poste 3.4)."),
        "default": "40x20",
        "kind": "dimension",
        "condition": lambda b: (
            bool(b.get("longrines_par_section"))
            or any(_normalise_type(t.get("type_designation")) not in EXCLUS_RESEAU_LONGRINES
                   for t in b.get("longrines_reseau_continu", []))
        ),
    },
    {
        # v37 -- volée(s) d'escalier détectées (nombre de marches connu)
        # mais cotes manquantes (giron/hauteur marche/largeur volée). Ces
        # dimensions sont généralement les MÊMES pour toutes les volées d'un
        # même escalier -- une seule question partagée plutôt qu'une par
        # volée. Valeurs usuelles pré-remplies (formule de Blondel:
        # 2×hauteur + giron ≈ 60-64cm de confort) -- à ajuster si besoin.
        "key": "giron_marche_cm",
        "question": "Quel est le giron des marches d'escalier (profondeur d'une marche, en cm) ? Valeur usuelle : 28cm.",
        "default": 28,
        "condition": lambda b: any(
            not (e.get("giron_cm") and e.get("hauteur_marche_cm") and e.get("largeur_volee_m"))
            for e in b.get("escaliers", [])
        ),
    },
    {
        "key": "hauteur_marche_cm",
        "question": "Quelle est la hauteur d'une marche d'escalier (en cm) ? Valeur usuelle : 17cm.",
        "default": 17,
        "condition": lambda b: any(
            not (e.get("giron_cm") and e.get("hauteur_marche_cm") and e.get("largeur_volee_m"))
            for e in b.get("escaliers", [])
        ),
    },
    {
        "key": "largeur_volee_m",
        "question": (
            "Quelle est la largeur de la volée d'escalier (en m) ? Varie beaucoup selon le projet -- "
            "pas de valeur usuelle proposée, indique la largeur réelle prévue."
        ),
        "default": None,
        "condition": lambda b: any(
            not (e.get("giron_cm") and e.get("hauteur_marche_cm") and e.get("largeur_volee_m"))
            for e in b.get("escaliers", [])
        ),
    },
    {
        # Repli v20 : aucun poteau comptable individuellement (grille trop
        # dense) mais un TOTAL global lu en légende (ex: "Total Poteaux :
        # 121") -- il manque la section représentative pour en tirer un
        # volume. Voir pipeline.py: bilan['poteaux']['total_legende_global'].
        "key": "section_poteaux_total_global_cm",
        "question": (
            "Un total global de poteaux a été lu en légende du plan de fondation/longrine "
            "(grille trop dense pour un comptage fiable poteau par poteau), mais la répartition "
            "par section n'est pas disponible. Quelle est la section représentative à utiliser "
            "pour ce total (ex: 25x25) ? Si plusieurs sections coexistent, indique la plus "
            "fréquente -- les autres seront à ajuster manuellement."
        ),
        "default": None,
        "kind": "dimension",
        "condition": lambda b: (
            bool(b.get("poteaux", {}).get("total_legende_global"))
            and not bool(b.get("poteaux", {}).get("par_section"))
            and not bool(b.get("poteaux", {}).get("total_legende_par_section"))
        ),
    },
]

# v29 -- types de la légende "longrines" qui ne sont PAS des longrines --
# relèvent d'un autre poste du devis (bèche d'escalier = 3.10, béton
# banché/cyclopéen = 3.2) et ne doivent JAMAIS se voir poser une question de
# longueur développée pour le poste 3.7. Comparaison normalisée (accents/
# casse ignorés) pour tolérer les variantes d'écriture du modèle vision.
def _normalise_type(s: str) -> str:
    s = (s or "").lower().strip()
    for a, b in [("é", "e"), ("è", "e"), ("ê", "e"), ("à", "a"), ("ç", "c")]:
        s = s.replace(a, b)
    return s


EXCLUS_RESEAU_LONGRINES = {"beche", "beton banche", "beton cyclopeen"}


def _slug_type_designation(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _normalise_type(s)).strip("_") or "type"


def _section_max(items):
    """Renvoie l'entrée (dict avec 'section') dont l'aire de section (cm²)
    est la plus grande parmi une liste -- 'la plus grosse section' à
    appliquer à tout le réseau quand on ne distingue plus les types."""
    best, best_aire = None, -1
    for it in items:
        aire = _section_area_m2(it.get("section"))
        if aire is not None and aire > best_aire:
            best, best_aire = it, aire
    return best


def _questions_reseau_longrines(bilan: dict) -> list:
    """v31 -- UNE SEULE question de longueur développée totale pour tout le
    réseau continu (tous types confondus, hors Bèche/Béton banché -- autres
    postes). Simplification voulue: on ne cherche plus à distinguer quel
    tronçon appartient à quel type (impossible à faire de façon fiable
    depuis un plan architectural) -- on prend la longueur totale du réseau
    et on applique la SECTION LA PLUS GROSSE parmi les types détectés en
    légende pour calculer le volume (repli volontairement prudent/simple,
    à ajuster manuellement si la répartition réelle diffère beaucoup).

    Dérivation automatique : pré-remplie avec le calcul géométrique depuis
    la grille d'axes si disponible (voir pipeline.py:
    bilan['longueur_reseau_calculee_m']), sinon dérivable depuis une pièce
    jointe (plan architectural -- somme des murs porteurs)."""
    if bilan.get("longrines_par_section"):
        return []  # cas normal (tronçons individuellement désignés) -- pas de repli à couvrir ici

    types_valides = [
        item for item in (bilan.get("longrines_reseau_continu") or [])
        if _normalise_type(item.get("type_designation")) not in EXCLUS_RESEAU_LONGRINES
    ]
    if not types_valides:
        return []

    section_max = _section_max(types_valides)
    longueur_calculee = bilan.get("longueur_reseau_calculee_m")
    detail_calcul = bilan.get("longueur_reseau_calculee_detail") or {}
    types_str = ", ".join(f"{t['type_designation']} ({t['section']})" for t in types_valides)
    # v33 -- ne préremplir le champ avec le calcul automatique QUE s'il est
    # jugé cohérent (voir pipeline.py: garde-fou de plausibilité sur le
    # nombre de lignes de grille) -- sinon on le montre à titre indicatif
    # dans le texte de la question mais on laisse le champ vide plutôt que
    # de préremplir une valeur probablement fausse.
    calcul_fiable = longueur_calculee and detail_calcul.get("coherent", True)

    if calcul_fiable:
        question = (
            f"Réseau continu de longrines détecté en légende ({types_str}) -- longueur développée "
            f"totale (tous types confondus) calculée automatiquement à {longueur_calculee}m depuis "
            f"la grille d'axes du plan de fondation (page {detail_calcul.get('page')} -- "
            f"{detail_calcul.get('nombre_axes_y')} lignes × {detail_calcul.get('somme_cotes_x_m')}m + "
            f"{detail_calcul.get('nombre_axes_x')} lignes × {detail_calcul.get('somme_cotes_y_m')}m). "
            f"Hypothèse: chaque ligne de la grille porte une longrine sur toute sa longueur -- "
            f"vérifie/corrige si besoin. La section la plus grosse détectée "
            f"({section_max['section'] if section_max else '?'}) sera appliquée à l'ensemble du réseau."
        )
    elif longueur_calculee:
        # Calcul disponible mais jugé incohérent -- affiché à titre indicatif
        # seulement, champ laissé vide pour éviter de préremplir une valeur
        # probablement fausse.
        question = (
            f"Réseau continu de longrines détecté en légende ({types_str}) -- une estimation "
            f"géométrique automatique a été tentée ({longueur_calculee}m) mais jugée incohérente "
            f"({detail_calcul.get('avertissement', '')}), donc PAS préremplie. Quelle est la "
            "longueur développée totale de ce réseau (en m, tous types confondus) ? Tu peux joindre "
            "un plan architectural pour une estimation automatique par somme des murs porteurs, ou "
            "saisir la valeur directement si tu la connais. La section la plus grosse détectée "
            f"({section_max['section'] if section_max else '?'}) sera appliquée à l'ensemble du réseau."
        )
    else:
        question = (
            f"Réseau continu de longrines détecté en légende ({types_str}), sans tronçons "
            "individuellement désignés sur le plan -- quelle est la longueur développée TOTALE de "
            "ce réseau (en m, tous types confondus) ? Tu peux joindre un plan architectural (ex: "
            "plan de niveau RDC) pour une estimation automatique par somme des murs porteurs, ou "
            "saisir la valeur directement si tu la connais. La section la plus grosse détectée "
            f"({section_max['section'] if section_max else '?'}) sera appliquée à l'ensemble du réseau."
        )
    longueur_calculee = longueur_calculee if calcul_fiable else None

    return [{
        "key": "longueur_reseau_longrine_totale", "question": question,
        "default": longueur_calculee, "kind": "scalar",
        "optional": False, "allow_attachment": True,
    }]


def _questions_chainage(bilan: dict) -> list:
    """v43 -- même principe que _questions_reseau_longrines: le chaînage
    court généralement sur tout le périmètre + refends porteurs à un
    niveau, longueur pas calculable fiablement sans plan dédié -- une
    question de longueur totale, section la plus grosse appliquée."""
    chainage_types = bilan.get("chainage_types") or []
    if not chainage_types:
        return []
    section_max = _section_max(chainage_types)
    types_str = ", ".join(f"{t['type_designation']} ({t['section']})" for t in chainage_types)
    question = (
        f"Chaînage détecté en légende sur plan(s) de coffrage ({types_str}) -- quelle est la "
        "longueur développée totale (en m, tous types confondus) ? Généralement proche du "
        "périmètre du bâtiment + refends porteurs à ce niveau. La section la plus grosse détectée "
        f"({section_max['section'] if section_max else '?'}) sera appliquée à l'ensemble."
    )
    return [{
        "key": "longueur_chainage_totale", "question": question,
        "default": None, "kind": "scalar", "optional": False, "allow_attachment": True,
    }]

ATTACHMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "valeur": {"type": ["number", "null"], "description": "La valeur demandée, dans l'unité précisée dans la question. Null si vraiment illisible sur ce document."},
        "note": {"type": ["string", "null"], "description": "Brève note si la lecture est incertaine, sinon null."},
    },
    "required": ["valeur", "note"],
}


def detect_missing_questions(bilan: dict) -> list:
    """Renvoie la liste des questions à poser, en fonction de ce qui a
    réellement été trouvé sur les plans."""
    questions = []
    for spec in QUESTION_SPECS:
        if spec["condition"](bilan):
            questions.append({
                "key": spec["key"], "question": spec["question"],
                "default": spec["default"], "kind": spec.get("kind", "scalar"),
                "optional": spec.get("optional", False),
                "allow_attachment": spec.get("allow_attachment", True),
            })
    # v29 -- une question par type de réseau continu de longrines (voir
    # _questions_reseau_longrines), plutôt qu'une seule question fourre-tout.
    questions.extend([
        {k: v for k, v in q.items() if not k.startswith("_")}
        for q in _questions_reseau_longrines(bilan)
    ])
    questions.extend(_questions_chainage(bilan))
    return questions


def extract_value_from_attachment(file_bytes: bytes, filename: str, question_text: str,
                                   key: str | None = None) -> float | None:
    """Rappelle la vision Gemini sur une pièce jointe (image ou PDF) pour en
    extraire la valeur demandée.

    v22 -- cas spécial 'longueur_totale_reseau_longrines_m': la pièce jointe
    peut être un PLAN ARCHITECTURAL entier (RDC, étages...), souvent un
    document de plusieurs dizaines de pages, plus lourd et plus chargé
    visuellement (mobilier, cotes de pièces, textes) qu'un plan structure --
    et la longueur développée n'y est JAMAIS écrite comme une cote directe:
    elle se déduit du tracé des murs porteurs/cloisons alignés sur la grille
    structurelle (les longrines courent dessous). Pour ce cas, on ne prend
    plus bêtement la première page du PDF joint -- on cherche la meilleure
    page (plan de niveau RDC de préférence) et on utilise un prompt dédié à
    la dérivation depuis un plan, pas à la lecture d'une cote déjà annotée.
    Pour tous les autres cas (coupe/élévation avec cote directement lisible),
    comportement inchangé: page 1 du document joint, prompt générique."""
    is_pdf = filename.lower().endswith(".pdf")
    # v28 -- même logique de repli de page que pour les longrines (voir
    # docstring ci-dessus) : la surface bâtie totale se lit aussi mieux sur
    # un plan de niveau RDC que sur la première page d'un dossier archi.
    # v29 -- clé dynamique par type (longueur_reseau_longrine__*).
    _is_longueur_reseau = key is not None and (key.startswith("longueur_reseau_longrine") or key == "longueur_chainage_totale")
    _needs_archi_page = _is_longueur_reseau or key == "surface_batiment_totale_m2"
    if is_pdf:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        if _needs_archi_page and len(doc) > 1:
            page = _pick_best_archi_page(doc)
        else:
            page = doc[0]
        zoom = 260 / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        image_bytes = pix.tobytes("png")
        doc.close()
        mime = "image/png"
    else:
        image_bytes = file_bytes
        mime = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"

    if _is_longueur_reseau:
        prompt = (
            "Tu analyses une pièce jointe fournie pour répondre à cette question précise: "
            f"{question_text}\n\n"
            "Cette pièce jointe est probablement un PLAN ARCHITECTURAL (plan de niveau, RDC ou "
            "étage) et non un plan de structure -- la longueur développée du réseau de longrines "
            "n'y sera JAMAIS écrite comme une cote directe. IMPORTANT: un plan architectural ne "
            "montre PAS la grille structurelle (axes de poteaux) -- ne cherche donc PAS à vérifier "
            "un alignement sur cette grille, c'est une information que ce document ne contient "
            "simplement pas. Concentre-toi plutôt sur le RÉSEAU DE MURS PORTEURS/CLOISONS visible "
            "sur CE plan: additionne la longueur du périmètre extérieur du bâtiment ET de tous les "
            "murs de refend intérieurs. Ces plans distinguent souvent visuellement les murs porteurs "
            "des cloisons simples: les murs porteurs sont généralement dessinés en double ligne "
            "épaisse, parfois hachurée ou remplie d'une couleur/trame spécifique (ex: saumon, rouge), "
            "tandis qu'une simple cloison de séparation est un trait fin. Base-toi sur cette "
            "distinction visuelle si elle est présente pour ne compter QUE les murs porteurs/refends "
            "structurels, pas les cloisons légères. Utilise les cotes annotées le long des bords du "
            "plan pour obtenir les longueurs. Ignore tout le bruit visuel qui n'aide pas à répondre "
            "(mobilier, sanitaires, textes de pièces, cotes de dimensions intérieures, hachures de "
            "revêtement, terrasses/jardins non couverts). Si "
            "le plan ne permet vraiment pas une estimation raisonnable (aucune cote exploitable), "
            "renvoie null plutôt que d'inventer un chiffre -- mais une estimation argumentée à "
            "partir de cotes réelles est préférable à un abandon, note tes hypothèses dans le champ "
            "'note'."
        )
    elif key == "surface_batiment_totale_m2":
        prompt = (
            "Tu analyses une pièce jointe fournie pour répondre à cette question précise: "
            f"{question_text}\n\n"
            "Cette pièce jointe est probablement un plan architectural de niveau (RDC ou étage). "
            "Cherche d'abord si une SURFACE TOTALE est déjà écrite en toutes lettres sur le plan "
            "(cartouche, tableau de surfaces, ou annotation du type 'Surface totale : XXX m²') -- "
            "si oui, utilise-la directement, c'est la source la plus fiable. Sinon, ADDITIONNE les "
            "surfaces de CHAQUE pièce couverte/fermée individuellement annotées sur le plan (ex: "
            "'44.28 m²', '88.28 m²' écrits à l'intérieur de chaque local) -- inclus les espaces "
            "couverts sous le même toit (chambres, salles, couloirs, sas, patios couverts), mais "
            "EXCLUS les terrasses ouvertes, jardins, rampes d'accès extérieures et espaces verts "
            "non couverts qui ne font pas partie de la dalle bâtie. Si des surfaces de pièces sont "
            "illisibles ou manquantes, note-le dans le champ 'note' plutôt que d'inventer un "
            "chiffre pour ces pièces-là -- mais fournis quand même la somme de ce qui est lisible."
        )
    else:
        prompt = (
            "Tu analyses un document technique BTP (coupe, élévation ou plan). "
            f"Question précise à résoudre: {question_text}\n"
            "Lis la cote correspondante directement sur le dessin si elle est annotée. "
            "N'invente jamais une valeur non explicitement lisible -- renvoie null si absente."
        )
    result = call_vision_json(image_bytes, prompt, ATTACHMENT_SCHEMA, mime=mime)
    return result.get("valeur")


def _pick_best_archi_page(doc):
    """Choisit la meilleure page d'un PDF joint (souvent un plan
    architectural complet, plusieurs dizaines de pages) pour en déduire la
    longueur développée des longrines: priorité à une page classée 'archi'
    dont le titre mentionne RDC/REZ-DE-CHAUSSEE (le niveau où courent les
    longrines de soubassement), sinon la première page classée 'archi'
    trouvée.

    Repli v24 -- si AUCUNE page n'est identifiable par mot-clé (exports CAD
    type ArchiCAD/GraphiSoft où le cartouche ne contient que des libellés
    génériques comme 'PLANCHE'/'ECHELLE', sans le vrai titre du plan sur une
    page repérable), on ne retombe plus bêtement sur la page 1 (souvent une
    page de garde sans aucun dessin technique) -- on choisit la page ayant
    le plus d'éléments vectoriels dessinés (get_drawings()), un bon indice
    qu'il s'agit d'un vrai plan technique dense plutôt que d'une page de
    garde/texte. Limité aux 60 premières pages pour rester rapide sur un
    gros document."""
    archi_pages = []
    for i in range(len(doc)):
        title = extract_cartouche_title(doc[i])
        if classify_title(title) == "archi":
            archi_pages.append((i, title.upper()))

    for i, title_upper in archi_pages:
        if "RDC" in title_upper or "REZ-DE-CHAUSSEE" in title_upper or "REZ DE CHAUSSEE" in title_upper:
            return doc[i]
    if archi_pages:
        return doc[archi_pages[0][0]]

    best_i, best_count = 0, -1
    for i in range(min(len(doc), 60)):
        try:
            n = len(doc[i].get_drawings())
        except Exception:
            n = 0
        if n > best_count:
            best_i, best_count = i, n
    return doc[best_i]


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
                    file_bytes: bytes | None, filename: str | None, kind: str = "scalar",
                    allow_attachment: bool = True):
    """Renvoie (valeur_ou_None, source_str, debug_note_ou_None). Pour
    kind='dimension', la valeur est une chaîne 'LARGEURxHAUTEUR' (cm) plutôt
    qu'un float. debug_note porte le message d'erreur Gemini réel si la
    pièce jointe a échoué à cause d'un problème technique (clé API, quota,
    réseau...) -- v23: avant, cette erreur était avalée silencieusement
    (`except GeminiError: pass`), ce qui affichait 'Vérifie la valeur saisie
    ou la pièce jointe' même quand le vrai problème était côté serveur
    (clé API absente/invalide, quota Gemini épuisé...), pas côté utilisateur.

    allow_attachment=False (v29) : ignore toute pièce jointe fournie et
    n'accepte qu'une réponse texte -- utilisé pour les types de longrine
    dont la longueur ne peut structurellement pas être dérivée d'un plan
    architectural (voir _questions_reseau_longrines)."""
    if kind == "dimension":
        if file_bytes and allow_attachment:
            try:
                val = extract_dimension_from_attachment(file_bytes, filename or "piece_jointe", question_text)
                if val is not None:
                    return val, f"pièce jointe ({filename})", None
            except GeminiError as e:
                if text_answer and DIMENSION_RE.match(text_answer):
                    m = DIMENSION_RE.match(text_answer.strip())
                    return f"{m.group(1)}x{m.group(2)}", "utilisateur", None
                return None, None, str(e)
        if text_answer and DIMENSION_RE.match(text_answer):
            m = DIMENSION_RE.match(text_answer.strip())
            return f"{m.group(1)}x{m.group(2)}", "utilisateur", None
        return None, None, None

    if file_bytes and allow_attachment:
        try:
            val = extract_value_from_attachment(file_bytes, filename or "piece_jointe", question_text, key=key)
            if val is not None:
                return float(val), f"pièce jointe ({filename})", None
            # Réponse Gemini bien reçue mais valeur=null (rien d'exploitable
            # trouvé sur le document) -- pas une erreur technique, mais on le
            # dit quand même explicitement plutôt que de laisser deviner.
            _attachment_debug_note = (
                "La pièce jointe a été lue mais aucune valeur exploitable n'y a été trouvée "
                "(Gemini a renvoyé null)."
            )
        except GeminiError as e:
            _attachment_debug_note = f"Erreur technique lors de la lecture de la pièce jointe: {e}"

        if text_answer not in (None, ""):
            try:
                return float(str(text_answer).replace(",", ".").strip()), "utilisateur", None
            except ValueError:
                return None, None, _attachment_debug_note
        return None, None, _attachment_debug_note

    if text_answer not in (None, ""):
        try:
            return float(str(text_answer).replace(",", ".").strip()), "utilisateur", None
        except ValueError:
            return None, None, None

    return None, None, None


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
        # v21 : priorité à la répartition exacte par section lue en légende
        # (colonne Quantité), plus fiable que le comptage individuel --
        # voir pipeline.py: bilan['poteaux']['total_legende_par_section'].
        _sections_pour_potelets = (
            bilan["poteaux"].get("total_legende_par_section")
            or bilan["poteaux"]["par_section"]
        )
        _source_potelets = (
            "légende (colonne Quantité par section)" if bilan["poteaux"].get("total_legende_par_section")
            else "comptage individuel sur plan"
        )
        vol_potelets = sum(
            (_section_area_m2(item["section"]) or 0) * item["nombre_total"] * hauteur
            for item in _sections_pour_potelets
        )
        postes["potelets"] = {
            "designation_devis": "3.5 Béton armé pour potelets", "unite": "m3",
            "volume_m3": round(vol_potelets, 2), "donnee_indisponible": False,
            "raison": f"Hauteur de soubassement confirmée: {hauteur}m. Répartition par section depuis: {_source_potelets}.",
        }

        # v37 -- 3.5bis Raidisseurs (poste ajouté, absent du canevas standard
        # -- voir pipeline.py: build_volumes_beton). Même hauteur que les
        # potelets ci-dessus.
        raidisseurs_legende = bilan.get("raidisseurs_legende_par_section") or []
        if raidisseurs_legende:
            vol_raidisseurs = sum(
                (_section_area_m2(item["section"]) or 0) * item["nombre_total"] * hauteur
                for item in raidisseurs_legende
            )
            postes["raidisseurs_soubassement"] = {
                "designation_devis": "3.5bis Béton armé pour raidisseurs (niveau soubassement -- poste ajouté, absent du canevas standard)",
                "unite": "m3", "volume_m3": round(vol_raidisseurs, 2), "donnee_indisponible": False,
                "raison": f"Hauteur de soubassement confirmée: {hauteur}m. Répartition par section depuis la légende du plan de fondation.",
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
        # v48 -- bug corrigé: cette largeur ne venait QUE de longrines_par_section
        # (le cas "tronçons individuellement désignés"), jamais du réseau
        # continu confirmé par l'utilisateur (longueur_reseau_longrine_totale
        # + section la plus grosse) -- le cas réel le plus fréquent. Résultat
        # avant ce correctif: poste 2.4 systématiquement à 0 dès que le
        # réseau continu était utilisé (poste 3.7), alors que la longueur
        # développée était pourtant déjà connue et confirmée.
        if not bilan.get("longrines_par_section"):
            _longueur_reseau_24 = answers.get("longueur_reseau_longrine_totale")
            _types_valides_24 = [
                t for t in (bilan.get("longrines_reseau_continu") or [])
                if _normalise_type(t.get("type_designation")) not in EXCLUS_RESEAU_LONGRINES
            ]
            if _longueur_reseau_24 and _types_valides_24:
                _section_max_24 = _section_max(_types_valides_24)
                _largeur_24 = _section_width_m(_section_max_24["section"]) if _section_max_24 else None
                if _largeur_24 is not None:
                    largeur_longrines = _largeur_24 * _longueur_reseau_24
        vol_fouilles_rigoles = largeur_longrines * profondeur * marge
        postes["fouilles_rigoles_fondations"] = {
            "designation_devis": "2.4 Fouilles en rigoles pour fondations filantes", "unite": "m3",
            "volume_m3": round(vol_fouilles_rigoles, 2), "donnee_indisponible": False,
            "raison": f"Profondeur d'ancrage confirmée: {profondeur}m. Marge de fouille confirmée: {marge_pct}%.",
        }

    # ---- v28 : surface bâtiment confirmée/dérivée par l'utilisateur (plan
    # architectural joint), remplace l'approximation rectangle si fournie.
    # Doit s'appliquer AVANT le calcul du poste 3.8 ci-dessous, qui lit
    # bilan["surface_dallage"]["surface_brute_m2"]. Les déductions
    # (poteaux/longrines/voiles) restent valables quelle que soit la source
    # de la surface brute -- elles ne dépendent pas de ce choix. ----
    # ---- v28/v32 : surface bâtiment confirmée/dérivée par l'utilisateur
    # (plan architectural joint), remplace l'approximation rectangle OU
    # construit bilan["surface_dallage"] depuis zéro s'il était totalement
    # indisponible (aucune cote exploitable nulle part -- c'est en fait le
    # cas le plus fréquent qui déclenche cette question, pas seulement le
    # repli rectangle). Doit s'appliquer AVANT le calcul du poste 3.8
    # ci-dessous. Les déductions (poteaux/longrines/voiles) sont
    # recalculées ici avec ce qui est disponible dans le bilan à ce stade
    # (y compris la répartition par légende pour les poteaux, et la
    # longueur de réseau continu tout juste confirmée pour les longrines). ----
    surface_confirmee = answers.get("surface_batiment_totale_m2")
    if surface_confirmee:
        sd_existant = bilan.get("surface_dallage") or {}
        etait_indisponible = sd_existant.get("donnee_indisponible", True)

        poteaux_area = 0.0
        for item in (bilan.get("poteaux", {}).get("par_section")
                     or bilan.get("poteaux", {}).get("total_legende_par_section") or []):
            aire = _section_area_m2(item["section"])
            if aire is not None:
                poteaux_area += aire * item["nombre_total"]

        longrines_area = 0.0
        for item in bilan.get("longrines_par_section") or []:
            largeur = _section_width_m(item["section"])
            if largeur is not None:
                longrines_area += largeur * item["longueur_totale_m"]
        # Repli réseau continu : utilise la longueur tout juste confirmée
        # (voir plus bas dans cette fonction) avec la section la plus grosse.
        _longueur_reseau_conf = answers.get("longueur_reseau_longrine_totale")
        _types_valides_surf = [t for t in (bilan.get("longrines_reseau_continu") or [])
                                if _normalise_type(t.get("type_designation")) not in EXCLUS_RESEAU_LONGRINES]
        if not bilan.get("longrines_par_section") and _longueur_reseau_conf and _types_valides_surf:
            _sm = _section_max(_types_valides_surf)
            _largeur = _section_width_m(_sm["section"]) if _sm else None
            if _largeur is not None:
                longrines_area += _largeur * _longueur_reseau_conf

        voiles_area = 0.0
        for item in bilan.get("voiles_par_type") or []:
            if item.get("longueur_totale_m"):
                voiles_area += item["longueur_totale_m"] * ((ep_voile_cm or 20) / 100)

        surface_nette = round(surface_confirmee - poteaux_area - longrines_area - voiles_area, 2)
        bilan["surface_dallage"] = {
            "donnee_indisponible": False,
            "source": "confirmée par l'utilisateur (plan architectural joint)" if etait_indisponible else (
                f"{sd_existant.get('source', '')} -- remplacée par une surface confirmée par "
                "l'utilisateur (plan architectural joint)"
            ),
            "surface_brute_m2": surface_confirmee,
            "deductions_m2": {
                "poteaux": round(poteaux_area, 2), "longrines": round(longrines_area, 2),
                "voiles": round(voiles_area, 2),
            },
            "surface_nette_dallage_m2": surface_nette,
            "avertissements": sd_existant.get("avertissements", []),
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

    # ---- 3.2 Béton banché/cyclopéen (valeur directe -- l'extraction ne peut
    # structurellement pas distinguer ce type d'élément) ----
    vol_banche = answers.get("volume_beton_banche_m3")
    if vol_banche is not None:
        postes["beton_banche_fondation_filante"] = {
            "designation_devis": "3.2 Béton banché ou cyclopéen pour fondations filantes", "unite": "m3",
            "volume_m3": round(vol_banche, 2), "donnee_indisponible": False,
            "source_override": "utilisateur",
            "raison": (
                f"Volume confirmé par l'utilisateur ({vol_banche}m³). Rappel : notre extraction ne "
                "distingue pas structurellement ce type d'élément des semelles filantes armées "
                "(poste 3.4) -- vérifie qu'il n'y a pas de double comptage avant validation finale."
            ),
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

    # ---- Repli v20 : 3.5 potelets depuis un total global de poteaux lu en
    # légende (grille trop dense), section représentative confirmée. Ne
    # s'applique que si aucune répartition par section n'existe déjà --
    # sinon le calcul normal ci-dessus prévaut. ----
    section_total_global = answers.get("section_poteaux_total_global_cm")
    total_legende_global = bilan.get("poteaux", {}).get("total_legende_global")
    if (hauteur is not None and section_total_global and total_legende_global
            and not bilan["poteaux"]["par_section"]):
        m = DIMENSION_RE.match(str(section_total_global).strip())
        if m:
            aire = _section_area_m2(f"{m.group(1)}x{m.group(2)}")
            if aire is not None:
                vol_potelets_global = aire * total_legende_global * hauteur
                postes["potelets"] = {
                    "designation_devis": "3.5 Béton armé pour potelets", "unite": "m3",
                    "volume_m3": round(vol_potelets_global, 2), "donnee_indisponible": False,
                    "source_override": "utilisateur",
                    "raison": (
                        f"Hauteur de soubassement confirmée: {hauteur}m. {total_legende_global} "
                        f"poteaux (total global lu en légende), section représentative confirmée "
                        f"par l'utilisateur: {section_total_global}cm."
                    ),
                }

    # ---- v43 : 3.15 Chaînage -- longueur totale confirmée, section la plus
    # grosse appliquée (même mécanisme que le réseau continu de longrines). ----
    chainage_types = bilan.get("chainage_types") or []
    longueur_chainage = answers.get("longueur_chainage_totale")
    if chainage_types and longueur_chainage is not None:
        section_max_chainage = _section_max(chainage_types)
        aire_chainage = _section_area_m2(section_max_chainage["section"]) if section_max_chainage else None
        if aire_chainage is not None:
            postes["chainage_superstructure"] = {
                "designation_devis": "3.15 Béton armé pour chaînages", "unite": "m3",
                "volume_m3": round(aire_chainage * longueur_chainage, 2), "donnee_indisponible": False,
                "raison": (
                    f"Longueur développée totale confirmée par l'utilisateur: {longueur_chainage}m, "
                    f"section la plus grosse appliquée ({section_max_chainage['section']})."
                ),
            }

    # ---- Repli v20/v31 : 3.7 longrines depuis un réseau continu (types en
    # légende), UNE SEULE longueur développée totale confirmée par
    # l'utilisateur (tous types confondus), appliquée à la section la PLUS
    # GROSSE parmi les types détectés en légende (hors Bèche/Béton banché,
    # autres postes: 3.10 / 3.2). Simplification volontaire -- on ne
    # distingue plus quel tronçon appartient à quel type précis. ----
    reseau_continu = bilan.get("longrines_reseau_continu") or []
    types_valides = [t for t in reseau_continu if _normalise_type(t.get("type_designation")) not in EXCLUS_RESEAU_LONGRINES]
    longueur_totale_confirmee = answers.get("longueur_reseau_longrine_totale")
    if longueur_totale_confirmee is not None and not bilan.get("longrines_par_section") and types_valides:
        section_max = _section_max(types_valides)
        aire = _section_area_m2(section_max["section"]) if section_max else None
        if aire is not None:
            vol_total_reseau = aire * longueur_totale_confirmee
            postes["longrines"] = {
                "designation_devis": "3.7 Béton armé pour longrines", "unite": "m3",
                "volume_m3": round(vol_total_reseau, 2), "donnee_indisponible": False,
                "source_override": "utilisateur",
                "raison": (
                    f"Réseau continu (légende: {', '.join(t['type_designation'] for t in types_valides)}) "
                    f"-- longueur développée totale confirmée par l'utilisateur: "
                    f"{longueur_totale_confirmee}m, section la plus grosse appliquée à l'ensemble du "
                    f"réseau ({section_max['section']})."
                ),
            }
            # Cascade sur 3.4 (semelles filantes) si la section a déjà été
            # confirmée -- même longueur développée totale que 3.7.
            section_sf_cascade = answers.get("section_semelle_filante_cm")
            if section_sf_cascade:
                m_sf = DIMENSION_RE.match(str(section_sf_cascade).strip())
                if m_sf:
                    largeur_sf_cm, hauteur_sf_cm = float(m_sf.group(1)), float(m_sf.group(2))
                    vol_sf = (largeur_sf_cm / 100) * (hauteur_sf_cm / 100) * longueur_totale_confirmee
                    postes["radier_semelles_filantes"] = {
                        "designation_devis": "3.4 Béton armé pour semelles filantes et radier partiel",
                        "unite": "m3", "volume_m3": round(vol_sf, 2), "donnee_indisponible": False,
                        "source_override": "utilisateur",
                        "raison": (
                            f"Section confirmée par l'utilisateur ({section_sf_cascade}cm). Longueur "
                            f"développée = longueur totale du réseau confirmée "
                            f"({longueur_totale_confirmee}m, poste 3.7)."
                        ),
                    }

    # ---- v41 : 3.11/3.13 poteaux et voiles de superstructure (hauteur
    # d'étage courant confirmée) -- même formule que potelets/voiles
    # soubassement, avec cette hauteur distincte. ----
    hauteur_etage = answers.get("hauteur_etage_courant_m")
    if hauteur_etage is not None:
        poteaux_coffrage = bilan.get("poteaux_coffrage_par_section") or []
        if poteaux_coffrage:
            vol_poteaux_sup = sum(
                (_section_area_m2(item["section"]) or 0) * item["nombre_total"] * hauteur_etage
                for item in poteaux_coffrage
            )
            postes["poteaux_superstructure"] = {
                "designation_devis": "3.11 Béton armé pour poteaux", "unite": "m3",
                "volume_m3": round(vol_poteaux_sup, 2), "donnee_indisponible": False,
                "raison": f"Hauteur d'étage courant confirmée: {hauteur_etage}m. Poteaux lus sur plan(s) de coffrage.",
            }

        raidisseurs_coffrage = bilan.get("raidisseurs_coffrage_par_section") or []
        if raidisseurs_coffrage:
            vol_raidisseurs_sup = sum(
                (_section_area_m2(item["section"]) or 0) * item["nombre_total"] * hauteur_etage
                for item in raidisseurs_coffrage
            )
            postes["raidisseurs_superstructure"] = {
                "designation_devis": "3.12 Béton armé pour raidisseurs", "unite": "m3",
                "volume_m3": round(vol_raidisseurs_sup, 2), "donnee_indisponible": False,
                "raison": f"Hauteur d'étage courant confirmée: {hauteur_etage}m. Raidisseurs lus sur plan(s) de coffrage.",
            }

        voiles_coffrage = bilan.get("voiles_coffrage_par_type") or []
        if voiles_coffrage and ep_voile_cm is not None:
            ep_voile_m_sup = ep_voile_cm / 100
            vol_voiles_sup = sum(
                v["longueur_totale_m"] * ep_voile_m_sup * hauteur_etage for v in voiles_coffrage
            )
            postes["voiles_superstructure"] = {
                "designation_devis": "3.13 Béton armé pour voiles", "unite": "m3",
                "volume_m3": round(vol_voiles_sup, 2), "donnee_indisponible": False,
                "raison": (
                    f"Hauteur d'étage courant confirmée: {hauteur_etage}m. Épaisseur voile confirmée: "
                    f"{ep_voile_cm}cm. Voiles lus sur plan(s) de coffrage."
                ),
            }

    # ---- v37 : 3.9 marches d'escalier -- giron/hauteur marche/largeur volée
    # confirmés par l'utilisateur, appliqués à TOUTES les volées qui en
    # manquaient (mêmes cotes en général pour un même escalier). Même
    # formule que pipeline.py:build_volumes_beton (prisme triangulaire par
    # marche × largeur de volée), recalculée ici avec les cotes complétées. ----
    giron_confirme = answers.get("giron_marche_cm")
    hauteur_marche_confirmee = answers.get("hauteur_marche_cm")
    largeur_volee_confirmee = answers.get("largeur_volee_m")
    if giron_confirme and hauteur_marche_confirmee and largeur_volee_confirmee:
        escaliers = bilan.get("escaliers") or []
        vol_marches = 0.0
        volees_completees = []
        for e in escaliers:
            g = e.get("giron_cm") or giron_confirme
            h = e.get("hauteur_marche_cm") or hauteur_marche_confirmee
            l = e.get("largeur_volee_m") or largeur_volee_confirmee
            n = e.get("nombre_marches")
            if n and g and h and l:
                vol_marches += (g / 100) * (h / 100) / 2 * l * n
                if not (e.get("giron_cm") and e.get("hauteur_marche_cm") and e.get("largeur_volee_m")):
                    volees_completees.append(e.get("designation", f"page {e.get('page')}"))
        if vol_marches > 0:
            postes["marches_arrets_dallage_rampe"] = {
                "designation_devis": "3.9 Béton armé pour marches, arrêts de dallage, rampe",
                "unite": "m3", "volume_m3": round(vol_marches, 2), "donnee_indisponible": False,
                "source_override": "utilisateur",
                "raison": (
                    f"Giron ({giron_confirme}cm), hauteur de marche ({hauteur_marche_confirmee}cm) et "
                    f"largeur de volée ({largeur_volee_confirmee}m) confirmés par l'utilisateur pour "
                    f"les volées incomplètes: {', '.join(volees_completees)}."
                    if volees_completees else
                    "Cotes confirmées par l'utilisateur."
                ),
            }

    # ---- v37 : 3.10 bèche escalier -- volume connu saisi directement par
    # l'utilisateur, faute de cote sur les plans (voir volume_beche_escalier_m3). ----
    vol_beche_confirme = answers.get("volume_beche_escalier_m3")
    if vol_beche_confirme is not None:
        postes["beche_escalier"] = {
            "designation_devis": "3.10 Béton armé pour bèche escalier", "unite": "m3",
            "volume_m3": vol_beche_confirme, "donnee_indisponible": False,
            "source_override": "utilisateur",
            "raison": "Volume confirmé directement par l'utilisateur (non coté sur les plans trouvés).",
        }

    bilan["volumes_beton"]["a_confirmer_ou_completer_en_aval"] = [
        {"poste": p["designation_devis"], "raison": p["raison"]}
        for p in postes.values() if p.get("donnee_indisponible")
    ]
    return bilan
