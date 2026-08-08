"""
Pipeline BoQ:

Passe 1 (gratuite, locale, PyMuPDF) : pour chaque page, extraction du texte
natif du cartouche -> classification du titre -> catégorie ou None
(catégorie sert juste à décider si la page est retenue, et plus tard à
choisir quelle page privilégier pour les totaux de poteaux -- elle ne
restreint plus ce qui est extrait sur la page).

Passe 2 (payante, ciblée) : pour chaque page retenue, UN SEUL appel vision
Gemini sur la page entière (pas de découpage en tuiles -- testé, ça
introduisait plus de sur-comptage qu'il n'en résolvait) avec UN SCHÉMA
UNIQUE (SCHEMA_PLAN_EXECUTION) qui cherche systématiquement tous les types
d'éléments (semelles, poteaux, longrines, voiles), peu importe le titre de
la page -- ça couvre le cas où plusieurs types de plans sont mélangés sur
une même feuille (ex: longrines dessinées directement sur le plan de
fondation).

Passe 3 (agrégation) : calcul 100% déterministe en Python (pas de LLM)
des totaux, avec détail par page, dédupliqué à travers les pages par
(désignation, repère début, repère fin) -- normalisé (espaces/casse) pour
éviter les faux doublons/faux distincts dus au bruit de lecture.

Multi-documents : run_pipeline() accepte une LISTE de PDF (ex: "Plan de
Fondation.pdf" + "Note de calcul.pdf" + "Plan de Coffrage R+1.pdf" envoyés
comme fichiers séparés plutôt qu'un seul PDF fusionné). Passe 1 et Passe 2
tournent sur l'ensemble des pages de TOUS les fichiers réunis dans un même
pool -- chaque page retenue porte son fichier d'origine ("fichier") en plus
de son numéro de page LOCAL à ce fichier ("page"), pour l'audit. Passe 3
agrège across-fichiers exactement comme elle agrège across-pages: aucune
distinction n'est faite entre "page 4 du même PDF" et "page 2 d'un PDF
séparé" -- c'est ce qui permet de lire les longrines sur le plan de
fondation (fichier A) même quand aucun "plan de longrine" dédié n'existe
dans le lot de fichiers envoyés (voir _is_valid_longrine_designation et
LONGRINE_CONTENT_MARKERS dans schemas.py pour le repli complémentaire par
contenu, indépendant du titre ET du fichier).
"""

import re
from pathlib import Path
import fitz  # PyMuPDF
from concurrent.futures import ThreadPoolExecutor, as_completed

from schemas import (
    classify_title, SCHEMA_PLAN_EXECUTION, PROMPT_PLAN_EXECUTION,
    SCHEMA_NOTE_CALCUL, PROMPT_NOTE_CALCUL,
)
from gemini_client import call_vision_json, GeminiError

MAX_WORKERS = 3  # ~15 RPM Gemini Flash-Lite -> 3 en parallele reste largement sous la limite
DPI = 260  # résolution plus haute pour bien distinguer les libellés à suffixes serrés (LG8.1, LG8.2...)


def extract_cartouche_title(page) -> str:
    """Renvoie tout le texte natif de la page, pour que classify_title()
    puisse chercher ses mots-clés n'importe où dedans -- pas seulement sur
    une ligne qui commencerait pile par 'PLAN DE'. L'ancienne heuristique
    (une seule ligne filtrée) ratait silencieusement tout titre qui ne
    commençait pas exactement par ce préfixe (ex: 'COUPE ESCALIER', 'PLAN
    ARCHITECTURE', 'DETAIL ESCALIER TYPE')."""
    return page.get_text()


def render_page_png(page, dpi=DPI) -> bytes:
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("png")


def process_page(source_file: str, page_index: int, category: str, image_bytes: bytes, on_log=None) -> dict:
    """Un seul appel vision sur la page entière, sans découpage en tuiles.

    Historique: on a essayé le découpage en tuiles (pour mieux lire les
    libellés serrés sur les gros plans), puis 3 passes de granularité
    combinées par union dédupliquée (pour ne pas sous-estimer). Les deux
    approches ont fini par introduire plus de sur-comptage qu'elles n'en
    résolvaient (le même élément relu plusieurs fois avec des repères
    légèrement différents d'une tuile/passe à l'autre est difficile à
    dédupliquer de façon fiable). Un seul appel sur la page complète est
    plus lent à égaliser en précision de lecture sur les tout petits
    libellés, mais ne peut structurellement pas dupliquer un élément.

    source_file identifie le PDF d'origine (nom de fichier tel qu'envoyé)
    -- utile uniquement pour l'audit multi-documents ; page_index reste le
    numéro de page LOCAL à ce fichier."""
    try:
        if category == "note_calcul":
            prompt, schema = PROMPT_NOTE_CALCUL, SCHEMA_NOTE_CALCUL
        else:
            prompt, schema = PROMPT_PLAN_EXECUTION, SCHEMA_PLAN_EXECUTION
        data = call_vision_json(image_bytes, prompt, schema)
        if on_log:
            on_log(f"{source_file} — page {page_index + 1} ({category}): OK")
        return {"fichier": source_file, "page": page_index + 1, "category": category, "data": data, "error": None}
    except GeminiError as e:
        if on_log:
            on_log(f"{source_file} — page {page_index + 1} ({category}): ÉCHEC - {e}")
        return {"fichier": source_file, "page": page_index + 1, "category": category, "data": None, "error": str(e)}


# ---------------------------------------------------------------------
# Agrégation déterministe (Python, pas de LLM)
# ---------------------------------------------------------------------

def _is_valid_longrine_designation(designation: str) -> bool:
    """Garde-fou indépendant du LLM: une vraie désignation de longrine
    contient des lettres (LG.., L..). Une cote de distance entre axes est
    un nombre seul (ex: "5.20") -- on l'exclut même si le modèle s'est
    trompé malgré le prompt."""
    return bool(re.search(r"[A-Za-z]", designation or ""))


def _aggregate_fondation(pages):
    """Semelles/radiers -- rassemblés depuis TOUTES les pages retenues
    (peuvent apparaître même sur une page dont le titre n'est pas
    'fondation' si le document mélange les contenus)."""
    per_page = []
    for r in pages:
        if r["data"] is None or not r["data"].get("semelles"):
            continue
        per_page.append({"fichier": r.get("fichier"), "page": r["page"], "semelles": r["data"]["semelles"]})
    return {"par_page": per_page}


def _aggregate_poteaux(pages):
    """Total de poteaux avec une règle de priorité de source pour éviter
    le double comptage d'un même poteau physique vu sur deux plans
    différents (ex: même P1/P2/P3 dessinés à la fois sur le plan de
    fondation ET sur le plan de longrine):

    1) Priorité aux pages catégorie 'fondation'.
    2) Si aucune instance de poteau n'y est trouvée (page absente ou
       vide), repli automatique sur les pages catégorie 'longrine'.
    3) Le détail par page reste toujours affiché pour TOUTES les pages
       (fondation/longrine/coffrage), même si une seule sert de source
       pour le total -- pour audit/vérification.
    """
    legend_map = {}  # designation -> "a x b", construit à travers toutes les pages
    for r in pages:
        if r["data"] is None:
            continue
        for leg in r["data"].get("poteaux_legende", []):
            legend_map[leg["designation"]] = f'{leg["a_cm"]}x{leg["b_cm"]}'

    def _norm(v):
        return v.strip().upper() if isinstance(v, str) else v

    def build_detail(source_pages):
        """Déduplique par (désignation, repère de grille) à travers TOUTES
        les pages sources -- un même poteau physique (ex: 'P3' en A2) peut
        être dessiné à la fois sur un plan général et sur une vue zoomée
        classées toutes deux 'fondation' ; sans dédup inter-pages ici, il
        serait compté deux fois."""
        total_seen = {}  # (designation, repere_grille) -> section
        per_page = []
        for r in source_pages:
            instances = r["data"].get("poteaux_instances", [])
            page_counts = {}
            for inst in instances:
                section = inst.get("section") or legend_map.get(
                    inst["designation"], f"designation={inst['designation']} (dimensions inconnues)"
                )
                key = (_norm(inst.get("designation")), _norm(inst.get("repere_grille")))
                page_counts[section] = page_counts.get(section, 0) + 1  # audit par page, non dédupliqué
                if key not in total_seen:
                    total_seen[key] = section
            per_page.append({"fichier": r.get("fichier"), "page": r["page"], "nombre_poteaux": len(instances), "detail_par_section": page_counts})

        total_by_section = {}
        for section in total_seen.values():
            total_by_section[section] = total_by_section.get(section, 0) + 1
        total_list = [{"section": s, "nombre_total": n} for s, n in total_by_section.items()]
        return per_page, total_list

    fondation_pages = [r for r in pages if r["category"] == "fondation" and r["data"] is not None
                       and r["data"].get("poteaux_instances")]
    longrine_pages = [r for r in pages if r["category"] == "longrine" and r["data"] is not None
                      and r["data"].get("poteaux_instances")]
    coffrage_pages = [r for r in pages if r["category"] == "coffrage" and r["data"] is not None]

    if fondation_pages:
        source_used = "fondation"
        source_pages = fondation_pages
    elif longrine_pages:
        source_used = "longrine (repli: aucun poteau trouvé sur le plan de fondation)"
        source_pages = longrine_pages
    else:
        source_used = "aucune (ni fondation ni longrine n'ont donné de poteaux)"
        source_pages = []

    per_page_total, total_par_section = build_detail(source_pages) if source_pages else ([], [])

    # Détail informatif de TOUTES les pages, même si elles ne servent pas
    # de source officielle pour le total (pour comparaison/audit).
    detail_toutes_pages = {}
    for cat, cat_pages in (("fondation", fondation_pages), ("longrine", longrine_pages)):
        if cat_pages:
            pp, _ = build_detail(cat_pages)
            detail_toutes_pages[cat] = pp

    # Poteaux de coffrage: comptés séparément par nature (par niveau/étage,
    # ce ne sont pas les mêmes instances physiques qu'au niveau fondation).
    coffrage_per_page = []
    coffrage_total = {}
    for r in coffrage_pages:
        poteaux = r["data"].get("poteaux_instances", [])
        page_counts = {}
        for p in poteaux:
            section = p.get("section") or "section inconnue"
            page_counts[section] = page_counts.get(section, 0) + 1
            coffrage_total[section] = coffrage_total.get(section, 0) + 1
        coffrage_per_page.append({
            "fichier": r.get("fichier"), "page": r["page"], "niveau": r["data"].get("niveau"),
            "nombre_poteaux": len(poteaux), "detail_par_section": page_counts,
        })

    return {
        "source_utilisee_pour_total": source_used,
        "total_par_section": total_par_section,
        "par_page_source": per_page_total,
        "detail_toutes_pages_fondation_longrine": detail_toutes_pages,
        "poteaux_coffrage": {
            "par_page": coffrage_per_page,
            "total_par_section": [{"section": s, "nombre_total": n} for s, n in coffrage_total.items()],
            "note": "Comptage séparé (par niveau/étage) -- ce ne sont pas les mêmes instances physiques que le total fondation/longrine ci-dessus.",
        },
    }


def _aggregate_longrines(pages):
    """Rassemble les tronçons de longrines depuis TOUTES les pages retenues
    (peu importe la catégorie -- gère le cas où les longrines sont
    mélangées sur le plan de fondation), dédupliqués par
    (désignation, repère début, repère fin) -- PAS par désignation seule:
    en BTP une même désignation (ex: "LG1") est presque toujours réutilisée
    pour de nombreux tronçons physiques différents du même type/section, donc
    dédupliquer par désignation seule jetterait tous les tronçons sauf le
    premier vu et sous-estimerait massivement la longueur totale développée."""
    per_page = []
    total_seen = {}  # (designation, repere_debut, repere_fin) -> {"section":..., "longueur_m":...}

    for r in pages:
        if r["data"] is None:
            continue
        troncons_bruts = r["data"].get("longrines", [])
        if not troncons_bruts:
            continue

        troncons = [t for t in troncons_bruts if _is_valid_longrine_designation(t.get("designation", ""))]
        rejetes = len(troncons_bruts) - len(troncons)

        page_total_len = 0.0
        page_count = 0
        for t in troncons:
            key = (
                (t.get("designation") or "").strip().upper(),
                (t.get("repere_debut") or "").strip().upper() if isinstance(t.get("repere_debut"), str) else t.get("repere_debut"),
                (t.get("repere_fin") or "").strip().upper() if isinstance(t.get("repere_fin"), str) else t.get("repere_fin"),
            )
            if key not in total_seen:
                total_seen[key] = {"section": t["section"], "longueur_m": t.get("longueur_m")}
            if t.get("longueur_m"):
                page_total_len += t["longueur_m"]
            page_count += 1

        per_page.append({
            "fichier": r.get("fichier"), "page": r["page"], "category": r["category"], "nombre_troncons": page_count,
            "longueur_totale_m_page": round(page_total_len, 2),
            "troncons_sans_longueur": sum(1 for t in troncons if t.get("longueur_m") is None),
            "entrees_rejetees_cote_numerique": rejetes,
        })

    total_by_section = {}
    for (_, _, _), info in total_seen.items():
        entry = total_by_section.setdefault(info["section"], {"nombre_troncons": 0, "longueur_totale_m": 0.0})
        entry["nombre_troncons"] += 1
        if info["longueur_m"] is not None:
            entry["longueur_totale_m"] += info["longueur_m"]

    total_list = [
        {"section": s, "nombre_troncons": v["nombre_troncons"], "longueur_totale_m": round(v["longueur_totale_m"], 2)}
        for s, v in total_by_section.items()
    ]
    return {"par_page": per_page, "total_par_section": total_list}


def _aggregate_voiles(pages):
    """Agrège les voiles à travers TOUTES les pages retenues -- un même
    voile physique peut apparaître sur plusieurs plans, donc déduplication
    globale par (designation, repere_debut, repere_fin). Même mécanique
    que les longrines pour la longueur (déduite des cotes d'axe)."""
    per_page = []
    total_seen = {}  # (designation, repere_debut, repere_fin) -> longueur_m

    for r in pages:
        if r["data"] is None:
            continue
        voiles = r["data"].get("voiles_instances", [])
        if not voiles:
            continue
        page_total_len = 0.0
        for v in voiles:
            key = (
                (v.get("designation") or "").strip().upper(),
                (v.get("repere_debut") or "").strip().upper() if isinstance(v.get("repere_debut"), str) else v.get("repere_debut"),
                (v.get("repere_fin") or "").strip().upper() if isinstance(v.get("repere_fin"), str) else v.get("repere_fin"),
            )
            if key not in total_seen:
                total_seen[key] = v.get("longueur_m")
            if v.get("longueur_m"):
                page_total_len += v["longueur_m"]
        per_page.append({
            "fichier": r.get("fichier"), "page": r["page"], "category": r["category"],
            "nombre_voiles": len(voiles),
            "detail": [
                {"designation": v["designation"], "de": v.get("repere_debut"), "a": v.get("repere_fin"),
                 "longueur_m": v.get("longueur_m")}
                for v in voiles
            ],
            "longueur_totale_m_page": round(page_total_len, 2),
        })

    total_by_designation = {}
    for (designation, _, _), longueur in total_seen.items():
        entry = total_by_designation.setdefault(designation, {"nombre": 0, "longueur_totale_m": 0.0, "sans_longueur": 0})
        entry["nombre"] += 1
        if longueur:
            entry["longueur_totale_m"] += longueur
        else:
            entry["sans_longueur"] += 1

    total_par_designation = [
        {"designation": d, "nombre": v["nombre"], "longueur_totale_m": round(v["longueur_totale_m"], 2),
         "sans_longueur_annotee": v["sans_longueur"]}
        for d, v in total_by_designation.items()
    ]

    return {
        "par_page": per_page,
        "nombre_total": len(total_seen),
        "longueur_totale_m": round(sum(v for v in total_seen.values() if v), 2),
        "total_par_designation": total_par_designation,
        "voiles_sans_longueur_annotee": sum(1 for v in total_seen.values() if not v),
    }


def _aggregate_escaliers(pages):
    """Volées d'escalier -- rassemblées depuis toutes les pages retenues
    (typiquement catégorie 'escalier', mais peut apparaître ailleurs)."""
    escaliers = []
    for r in pages:
        if r["data"] is None or not r["data"].get("escaliers"):
            continue
        for e in r["data"]["escaliers"]:
            escaliers.append({**e, "fichier": r.get("fichier"), "page": r["page"]})
    return escaliers


def _aggregate_elements_structurels(pages):
    """Éléments de superstructure (poteaux, raidisseurs, voiles, poutres,
    chaînages, appuis de baies, éléments décoratifs, rampes d'accès) lus
    depuis les pages catégorie 'note_calcul' -- dédupliqués par
    (type, désignation, niveau). On ne lit QUE des sections/longueurs/
    nombres bruts ici (jamais un volume m3 déjà calculé) ; le volume est
    recalculé en Python dans build_volumes_beton, pour rester auditable."""
    total_seen = {}  # (type, designation, niveau) -> dernière entrée brute vue
    per_page = []

    for r in pages:
        if r["data"] is None:
            continue
        elements = r["data"].get("elements_structurels", [])
        if not elements:
            continue
        per_page.append({"fichier": r.get("fichier"), "page": r["page"], "nombre_lignes": len(elements)})
        for e in elements:
            niveau = e.get("niveau")
            key = (
                (e.get("type") or "").strip().upper(),
                (e.get("designation") or "").strip().upper(),
                niveau.strip().upper() if isinstance(niveau, str) else niveau,
            )
            total_seen[key] = e

    par_type = {}
    for (type_, _designation, _niveau), e in total_seen.items():
        par_type.setdefault(type_.lower(), []).append(e)

    return {"par_page": per_page, "par_type": par_type}


def build_boq(results: list) -> dict:
    """Agrégation 100% déterministe (aucun appel LLM)."""
    return {
        "fondation": _aggregate_fondation(results),
        "poteaux": _aggregate_poteaux(results),
        "longrines": _aggregate_longrines(results),
        "voiles": _aggregate_voiles(results),
        "escaliers": _aggregate_escaliers(results),
        "elements_structurels": _aggregate_elements_structurels(results),
    }


import re as _re

_SECTION_RE = _re.compile(r"(\d+(?:[.,]\d+)?)\s*[xX×*/-]\s*(\d+(?:[.,]\d+)?)")


def _parse_section_dims_cm(section: str):
    """Extrait (a_cm, b_cm) d'une section même mal formatée: '20x40',
    '20 X 40', '20×40cm', '20 x 40 cm', '20/40'... Renvoie (None, None) si
    aucun couple de nombres n'est trouvé."""
    if not section:
        return None, None
    section = section.strip().upper()
    m = _SECTION_RE.search(section)
    if not m:
        return None, None
    try:
        a = float(m.group(1).replace(",", "."))
        b = float(m.group(2).replace(",", "."))
        return a, b
    except ValueError:
        return None, None


def _parse_section_area_m2(section: str) -> float | None:
    """Parse une section du type '45x45' (cm) -> aire en m². Gère aussi
    'D25' (diamètre 25cm, poteau circulaire). Renvoie None si imparsable."""
    section = section.strip().upper()
    if section.startswith("D"):
        try:
            diam_cm = float(section[1:].rstrip("CM ").strip())
            r_m = (diam_cm / 100) / 2
            import math
            return math.pi * r_m ** 2
        except ValueError:
            return None
    a_cm, b_cm = _parse_section_dims_cm(section)
    if a_cm is None:
        return None
    return (a_cm / 100) * (b_cm / 100)


def _parse_section_width_m(section: str) -> float | None:
    """Pour une section de longrine/voile du type '20x40' (cm) -> largeur
    en plan = la plus petite des deux valeurs, en m."""
    a_cm, b_cm = _parse_section_dims_cm(section)
    if a_cm is None:
        return None
    return min(a_cm, b_cm) / 100


def _parse_section_height_m(section: str) -> float | None:
    """Même chose mais renvoie la plus grande des deux valeurs (hauteur de
    section, ex: '20x40' -> 0.40m)."""
    a_cm, b_cm = _parse_section_dims_cm(section)
    if a_cm is None:
        return None
    return max(a_cm, b_cm) / 100


def _volume_element_structurel(e: dict):
    """Calcule le volume (m3) d'UNE ligne d'élément de superstructure
    (issue de la note de calcul), à partir des sections/longueurs/nombres
    bruts. Renvoie (volume_m3, None) si calculable, ou (None, raison) si
    des données manquent -- ne devine jamais une dimension absente."""
    t = e.get("type")
    section = e.get("section")
    nombre = e.get("nombre")
    hauteur = e.get("hauteur_m")
    longueur = e.get("longueur_totale_m")
    epaisseur_cm = e.get("epaisseur_cm")

    if t in ("poteau", "raidisseur"):
        area = _parse_section_area_m2(section) if section else None
        if area is None or hauteur is None or nombre is None:
            return None, "section, hauteur ou nombre manquant"
        return area * hauteur * nombre, None

    if t in ("poutre", "chainage"):
        area = _parse_section_area_m2(section) if section else None
        if area is None or longueur is None:
            return None, "section ou longueur développée manquante"
        return area * longueur, None

    if t == "voile":
        if epaisseur_cm is None or hauteur is None or longueur is None:
            return None, "épaisseur, hauteur ou longueur manquante"
        return (epaisseur_cm / 100) * hauteur * longueur, None

    if t in ("appui_baie", "element_decoratif"):
        area = _parse_section_area_m2(section) if section else None
        if area is not None and longueur is not None:
            return area * longueur, None
        if area is not None and hauteur is not None and nombre is not None:
            return area * hauteur * nombre, None
        return None, "section incomplète avec ni longueur, ni (hauteur+nombre)"

    if t == "rampe_acces":
        rampe = e.get("rampe") or {}
        l, w, ep = rampe.get("longueur_m"), rampe.get("largeur_m"), rampe.get("epaisseur_m")
        if l is None or w is None or ep is None:
            return None, "dimensions de rampe (longueur/largeur/épaisseur) manquantes"
        return l * w * ep, None

    return None, f"type inconnu: {t}"


def compute_surfaces_superstructure(results: list) -> dict:
    """Somme des surfaces de dalle pleine et de plancher corps creux à
    travers tous les niveaux d'étage (pages catégorie 'archi'), lues
    directement si annotées sur les plans. Si aucune n'est disponible,
    donnee_indisponible=True -- saisie manuelle en aval (missing_info.py),
    conformément au repli demandé plutôt que d'inventer un chiffre."""
    dalle_pleine_total, corps_creux_total = 0.0, 0.0
    dalle_pleine_trouvee, corps_creux_trouvee = False, False
    par_niveau = []

    for r in results:
        if r["data"] is None or r["category"] != "archi":
            continue
        sa = r["data"].get("surface_archi") or {}
        dp = sa.get("surface_dalle_pleine_m2")
        cc = sa.get("surface_plancher_corps_creux_m2")
        if dp is None and cc is None:
            continue
        par_niveau.append({
            "fichier": r.get("fichier"), "page": r["page"], "niveau": r["data"].get("niveau"),
            "surface_dalle_pleine_m2": dp, "surface_plancher_corps_creux_m2": cc,
        })
        if dp is not None:
            dalle_pleine_total += dp
            dalle_pleine_trouvee = True
        if cc is not None:
            corps_creux_total += cc
            corps_creux_trouvee = True

    return {
        "par_niveau": par_niveau,
        "surface_dalle_pleine_m2": round(dalle_pleine_total, 2) if dalle_pleine_trouvee else None,
        "surface_plancher_corps_creux_m2": round(corps_creux_total, 2) if corps_creux_trouvee else None,
        "donnee_indisponible": not (dalle_pleine_trouvee or corps_creux_trouvee),
    }


# Valeurs par défaut couramment utilisées dans les devis BTP burkinabè type
# -- ne sont JAMAIS appliquées automatiquement ici. Elles ne servent que de
# suggestion affichée dans la question posée à l'utilisateur (voir
# missing_info.py) -- toute hypothèse doit être explicitement confirmée
# avant d'être utilisée dans un calcul.


def build_volumes_beton(bilan: dict, boq: dict) -> dict:
    """Calcule les volumes de béton par poste du devis (section III
    'BETON - BETON ARME', sous-partie Infrastructures), avec un flag
    donnee_indisponible=True explicite pour CHAQUE poste qui repose sur une
    hypothèse non confirmée (épaisseur, hauteur, profondeur...) -- aucune
    valeur par défaut n'est appliquée silencieusement ici. Les postes
    concernés exposent les ingrédients bruts nécessaires (footprint_m2,
    surface_nette_m2...) pour que missing_info.py puisse calculer le volume
    final une fois l'hypothèse confirmée par l'utilisateur."""

    postes = {}

    # ---- 3.3 Semelles isolées (béton armé) ----
    vol_semelles = 0.0
    for s in bilan["semelles"]:
        vol_semelles += s["a_m"] * s["b_m"] * (s["h_cm"] / 100) * s["nombre"]
    postes["semelles_isolees_beton_arme"] = {
        "designation_devis": "3.3 Béton armé pour semelles isolées",
        "unite": "m3",
        "volume_m3": round(vol_semelles, 2) if bilan["semelles"] else 0.0,
        "donnee_indisponible": False,
    }

    # ---- 3.1 Béton de propreté (semelles isolées, épaisseur à confirmer) ----
    if bilan["semelles"]:
        footprint_m2 = sum(s["a_m"] * s["b_m"] for s in bilan["semelles"])
        postes["beton_proprete_semelles"] = {
            "designation_devis": "3.1 Béton de propreté pour semelles isolées",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "footprint_m2": round(footprint_m2, 2),
            "raison": "Épaisseur du béton de propreté non confirmée -- nécessaire pour calculer le volume.",
        }
    else:
        postes["beton_proprete_semelles"] = {
            "designation_devis": "3.1 Béton de propreté pour semelles isolées",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": "Aucune semelle isolée détectée.",
        }

    # ---- 3.4 Semelles filantes: longueur développée = longueur totale des
    # longrines (même donnée que le poste 3.7), section à confirmer par
    # l'utilisateur -- calculé en aval dans missing_info.apply_answers_to_bilan. ----
    postes["radier_semelles_filantes"] = {
        "designation_devis": "3.4 Béton armé pour semelles filantes et radier partiel",
        "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
        "raison": ("Longueur développée = longueur totale des longrines (poste 3.7) -- section de la "
                    "semelle filante à confirmer pour calculer le volume."),
    }

    # ---- 3.5 Potelets (poteaux d'infrastructure) -- hauteur indisponible ----
    postes["potelets"] = {
        "designation_devis": "3.5 Béton armé pour potelets",
        "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
        "raison": (
            "Hauteur des potelets (du dessus de semelle au niveau du sol) non "
            "disponible depuis un plan vu de dessus -- nécessite une coupe/élévation "
            "ou une confirmation utilisateur de la hauteur de soubassement."
        ),
        "donnees_disponibles_en_attente": {
            "sections_poteaux": bilan["poteaux"]["par_section"],
        },
    }

    # ---- 3.6 Voiles en soubassement -- hauteur indisponible ----
    postes["voiles_soubassement"] = {
        "designation_devis": "3.6 Béton armé pour voiles en soubassement",
        "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
        "raison": (
            "Hauteur des voiles non disponible depuis un plan vu de dessus -- "
            "nécessite une coupe/élévation ou une confirmation utilisateur."
        ),
        "donnees_disponibles_en_attente": {
            "voiles_par_type": bilan["voiles_par_type"],
        },
    }

    # ---- 3.7 Longrines ----
    vol_longrines = 0.0
    longrines_manquantes = []
    for item in bilan["longrines_par_section"]:
        width = _parse_section_width_m(item["section"])
        height = _parse_section_height_m(item["section"])
        if width is None or height is None:
            longrines_manquantes.append(item["section"])
            continue
        vol_longrines += width * height * item["longueur_totale_m"]

    if not bilan["longrines_par_section"]:
        postes["longrines"] = {
            "designation_devis": "3.7 Béton armé pour longrines", "unite": "m3",
            "volume_m3": None, "donnee_indisponible": True,
            "raison": "Aucune longrine détectée.",
        }
    elif longrines_manquantes and len(longrines_manquantes) == len(bilan["longrines_par_section"]):
        # Toutes les sections trouvées existent mais AUCUNE n'a pu être parsée
        # (format inattendu) -- on ne doit surtout pas afficher 0 silencieusement.
        postes["longrines"] = {
            "designation_devis": "3.7 Béton armé pour longrines", "unite": "m3",
            "volume_m3": None, "donnee_indisponible": True,
            "raison": f"Sections non reconnues (format inattendu): {longrines_manquantes}. "
                      "Vérifie le libellé de section sur le plan et corrige si besoin.",
        }
    else:
        raison = None
        if longrines_manquantes:
            raison = (f"Volume partiel -- sections non reconnues ignorées: {longrines_manquantes} "
                       "(à ajouter manuellement si besoin).")
        postes["longrines"] = {
            "designation_devis": "3.7 Béton armé pour longrines", "unite": "m3",
            "volume_m3": round(vol_longrines, 2), "donnee_indisponible": False,
            "raison": raison, "sections_non_calculees": longrines_manquantes or None,
        }

    # ---- 3.8 Dallage au sol -- épaisseur à confirmer ----
    surface = bilan.get("surface_dallage", {})
    if surface.get("donnee_indisponible"):
        postes["dallage"] = {
            "designation_devis": "3.8 Béton légèrement armé pour dallage au sol",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": "Surface de dallage elle-même indisponible (voir bilan.surface_dallage.raison).",
        }
    else:
        postes["dallage"] = {
            "designation_devis": "3.8 Béton légèrement armé pour dallage au sol",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "surface_nette_m2": surface["surface_nette_dallage_m2"],
            "raison": "Épaisseur du dallage non confirmée -- nécessaire pour calculer le volume.",
        }

    # ---- 3.9 Marches (volées d'escalier) et 3.10 Bèche escalier ----
    escaliers = bilan.get("escaliers", [])
    if not escaliers:
        postes["marches_arrets_dallage_rampe"] = {
            "designation_devis": "3.9 Béton armé pour marches, arrêts de dallage, rampe",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": "Aucune volée d'escalier détectée (pas de coupe/détail escalier trouvé dans le PDF).",
        }
        postes["beche_escalier"] = {
            "designation_devis": "3.10 Béton armé pour bèche escalier",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": "Aucune volée d'escalier détectée (pas de coupe/détail escalier trouvé dans le PDF).",
        }
    else:
        vol_marches = 0.0
        volees_incompletes = []
        for e in escaliers:
            if e.get("nombre_marches") and e.get("giron_cm") and e.get("hauteur_marche_cm") and e.get("largeur_volee_m"):
                # Volume d'une marche = prisme triangulaire (giron x hauteur / 2) x largeur de la volée.
                vol_marches += (
                    e["nombre_marches"]
                    * (e["giron_cm"] / 100) * (e["hauteur_marche_cm"] / 100) / 2
                    * e["largeur_volee_m"]
                )
            else:
                volees_incompletes.append(e.get("designation", f"page {e.get('page')}"))

        if vol_marches > 0:
            postes["marches_arrets_dallage_rampe"] = {
                "designation_devis": "3.9 Béton armé pour marches, arrêts de dallage, rampe",
                "unite": "m3", "volume_m3": round(vol_marches, 2), "donnee_indisponible": False,
                "raison": (f"Volée(s) incomplète(s) non comptée(s) (giron/hauteur marche/largeur manquants): "
                           f"{', '.join(volees_incompletes)}." if volees_incompletes else None),
            }
        else:
            postes["marches_arrets_dallage_rampe"] = {
                "designation_devis": "3.9 Béton armé pour marches, arrêts de dallage, rampe",
                "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
                "raison": ("Volée(s) d'escalier détectée(s) mais cotes incomplètes (giron, hauteur de "
                            "marche ou largeur de volée non annotés) : "
                            + ", ".join(volees_incompletes)),
            }

        vol_beche = 0.0
        beches_absentes = []
        for e in escaliers:
            b = e.get("beche")
            if b and b.get("longueur_m") and b.get("largeur_cm") and b.get("hauteur_cm"):
                vol_beche += (b["largeur_cm"] / 100) * (b["hauteur_cm"] / 100) * b["longueur_m"]
            else:
                beches_absentes.append(e.get("designation", f"page {e.get('page')}"))

        if vol_beche > 0:
            postes["beche_escalier"] = {
                "designation_devis": "3.10 Béton armé pour bèche escalier",
                "unite": "m3", "volume_m3": round(vol_beche, 2), "donnee_indisponible": False,
                "raison": (f"Bèche non cotée pour: {', '.join(beches_absentes)}." if beches_absentes else None),
            }
        else:
            postes["beche_escalier"] = {
                "designation_devis": "3.10 Béton armé pour bèche escalier",
                "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
                "raison": "Bèche d'escalier non dessinée/cotée sur les coupes trouvées -- à compléter manuellement.",
            }

    # ---- 3.11-3.16, 3.19, 3.20 Superstructure (poteaux, raidisseurs,
    # voiles, poutres, chaînages, appuis de baies, éléments décoratifs,
    # rampes d'accès) -- lus depuis la note de calcul (sections/longueurs/
    # nombres bruts), volume recalculé en Python (voir
    # _volume_element_structurel). Priorité donnée à la note de calcul par
    # construction: ce bloc ne dépend d'aucune donnée du plan archi. ----
    SUPERSTRUCTURE_TYPES = [
        ("poteau", "poteaux_superstructure", "3.11 Béton armé pour poteaux"),
        ("raidisseur", "raidisseurs_superstructure", "3.12 Béton armé pour raidisseurs"),
        ("voile", "voiles_superstructure", "3.13 Béton armé pour voiles"),
        ("poutre", "poutres_superstructure", "3.14 Béton armé pour poutres"),
        ("chainage", "chainages_superstructure", "3.15 Béton armé pour chaînages"),
        ("appui_baie", "appuis_baies_superstructure", "3.16 Béton armé pour appuis de baies"),
        ("element_decoratif", "elements_decoratifs_superstructure", "3.19 Béton armé pour élément décoratif"),
        ("rampe_acces", "rampes_acces_superstructure", "3.20 Béton armé pour rampes d'accès niveau supérieur"),
    ]
    elements_par_type = bilan.get("elements_structurels_par_type", {})
    for type_key, poste_key, designation in SUPERSTRUCTURE_TYPES:
        items = elements_par_type.get(type_key, [])
        if not items:
            postes[poste_key] = {
                "designation_devis": designation, "unite": "m3",
                "volume_m3": None, "donnee_indisponible": True,
                "raison": "Aucune ligne de ce type détectée dans la note de calcul.",
            }
            continue

        total_vol = 0.0
        lignes_incompletes = []
        for e in items:
            vol, raison_manquante = _volume_element_structurel(e)
            if vol is None:
                lignes_incompletes.append(f'{e.get("designation", "?")} ({raison_manquante})')
                continue
            total_vol += vol

        if total_vol > 0:
            postes[poste_key] = {
                "designation_devis": designation, "unite": "m3",
                "volume_m3": round(total_vol, 2), "donnee_indisponible": False,
                "raison": (f"Ligne(s) ignorée(s) (données incomplètes dans la note de calcul): "
                           f"{', '.join(lignes_incompletes)}." if lignes_incompletes else None),
            }
        else:
            postes[poste_key] = {
                "designation_devis": designation, "unite": "m3",
                "volume_m3": None, "donnee_indisponible": True,
                "raison": f"Ligne(s) trouvée(s) mais données incomplètes: {', '.join(lignes_incompletes)}.",
            }

    # ---- 3.17 / 3.21 / 3.22 Surfaces de plancher d'étage (dalle pleine,
    # corps creux, table de compression) -- lues sur le plan architectural
    # si distinguées, sinon saisie manuelle (voir missing_info.py). ----
    surf = bilan.get("surfaces_superstructure", {})

    if surf.get("surface_dalle_pleine_m2") is not None:
        postes["dalle_pleine_superstructure"] = {
            "designation_devis": "3.17 Béton armé pour dalle pleine", "unite": "m3",
            "volume_m3": None, "donnee_indisponible": True,
            "surface_m2": surf["surface_dalle_pleine_m2"],
            "raison": "Épaisseur de dalle pleine non confirmée -- nécessaire pour calculer le volume.",
        }
    else:
        postes["dalle_pleine_superstructure"] = {
            "designation_devis": "3.17 Béton armé pour dalle pleine", "unite": "m3",
            "volume_m3": None, "donnee_indisponible": True,
            "raison": "Surface de dalle pleine non distinguée sur le(s) plan(s) archi -- saisie manuelle requise (m² puis épaisseur).",
        }

    if surf.get("surface_plancher_corps_creux_m2") is not None:
        cc_m2 = surf["surface_plancher_corps_creux_m2"]
        postes["plancher_corps_creux"] = {
            "designation_devis": "3.21 Plancher corps creux en poutrelles hourdis",
            "unite": "m2", "quantite_m2": cc_m2, "donnee_indisponible": False,
            "raison": None,
        }
        postes["table_compression"] = {
            "designation_devis": "3.22 Béton armé pour table de compression du plancher en poutrelles hourdis",
            "unite": "m2", "quantite_m2": cc_m2, "donnee_indisponible": False,
            "raison": "Même surface que le plancher corps creux (3.21) -- la table de compression recouvre le même footprint.",
        }
    else:
        for key, designation in [
            ("plancher_corps_creux", "3.21 Plancher corps creux en poutrelles hourdis"),
            ("table_compression", "3.22 Béton armé pour table de compression du plancher en poutrelles hourdis"),
        ]:
            postes[key] = {
                "designation_devis": designation, "unite": "m2",
                "quantite_m2": None, "donnee_indisponible": True,
                "raison": "Surface de plancher corps creux non distinguée sur le(s) plan(s) archi -- saisie manuelle requise.",
            }

    # ---- Postes non couverts du tout par notre extraction actuelle ----
    for key, designation, raison in [
        ("beton_banche_fondation_filante", "3.2 Béton banché ou cyclopéen pour fondations filantes",
         "Notre extraction ne distingue pas structurellement les fondations filantes en béton "
         "banché/cyclopéen (non armé) des semelles filantes armées (poste 3.4) -- volume à confirmer "
         "directement par l'utilisateur."),
        ("fouilles_puits_semelles", "2.3 Fouilles en puits pour semelles isolées",
         "Nécessite la profondeur d'ancrage (donnée géotechnique/coupe), non extraite des plans en vue de dessus."),
        ("fouilles_rigoles_fondations", "2.4 Fouilles en rigoles pour fondations filantes",
         "Nécessite la profondeur d'ancrage, non extraite des plans en vue de dessus."),
        ("remblais", "2.5-2.7 Remblais (sans/avec apport, hydraulique)",
         "Nécessite des hauteurs/profondeurs verticales, non extraites des plans en vue de dessus."),
        ("escaliers_superstructure", "3.18 Béton armé pour escaliers",
         "Volontairement non mappé sur les volées déjà comptées en 3.9/3.10 (infrastructure) -- "
         "à confirmer si ce poste 3.18 est bien un élément distinct (ex: volées d'étage) avant de "
         "réutiliser le même calcul, pour éviter un double comptage."),
    ]:
        postes[key] = {
            "designation_devis": designation,
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": raison,
        }

    # ---- Liste consolidée de ce qui nécessite une confirmation utilisateur
    # ou un traitement par le LLM de raisonnement en aval ----
    a_confirmer = [
        {"poste": p["designation_devis"], "raison": p["raison"]}
        for p in postes.values() if p.get("donnee_indisponible") or p.get("valeur_par_defaut_utilisee")
    ]

    return {"postes": postes, "a_confirmer_ou_completer_en_aval": a_confirmer}


def _shoelace_area(points: list) -> float:
    """Formule du lacet -- aire d'un polygone à partir de ses sommets ordonnés."""
    n = len(points)
    area = 0.0
    for i in range(n):
        x1, y1 = points[i]["x_m"], points[i]["y_m"]
        x2, y2 = points[(i + 1) % n]["x_m"], points[(i + 1) % n]["y_m"]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2


def compute_surface_dallage(boq: dict, results: list) -> dict:
    """Calcule la surface nette de dallage: surface brute du bâtiment moins
    l'emprise au sol des poteaux, longrines et voiles. Priorité de source
    pour la surface brute:
    1) Surface déjà écrite sur un plan architectural (la plus fiable).
    2) Contour du plan architectural (formule du lacet).
    3) Repli: rectangle approximatif depuis les cotes cumulées du plan de
       fondation (moins fiable si le bâtiment n'est pas rectangulaire).
    4) Si rien de tout ça n'est disponible: donnee_indisponible=True,
       aucun chiffre inventé.
    """
    avertissements = []
    surface_brute = None
    source = None

    # ---- 1) Surface écrite sur le plan archi ----
    for r in results:
        if r["data"] is None or r["category"] != "archi":
            continue
        sa = r["data"].get("surface_archi") or {}
        if sa.get("surface_totale_m2"):
            surface_brute = sa["surface_totale_m2"]
            source = "plan architectural (surface déjà écrite sur le plan)"
            break

    # ---- 2) Contour du plan archi ----
    if surface_brute is None:
        for r in results:
            if r["data"] is None or r["category"] != "archi":
                continue
            sa = r["data"].get("surface_archi") or {}
            contour = sa.get("contour_m")
            if contour and len(contour) >= 3:
                surface_brute = round(_shoelace_area(contour), 2)
                source = "plan architectural (contour extérieur, formule du lacet)"
                break

    # ---- 3) Repli: rectangle depuis le plan de fondation ----
    if surface_brute is None:
        for r in results:
            if r["data"] is None:
                continue
            dims = r["data"].get("dimensions_generales_m") or {}
            if dims.get("longueur_x_m") and dims.get("largeur_y_m"):
                surface_brute = round(dims["longueur_x_m"] * dims["largeur_y_m"], 2)
                source = "plan de fondation (approximation rectangle -- aucun plan architectural exploitable trouvé)"
                avertissements.append(
                    "Aucun plan architectural exploitable (ni surface écrite, ni contour) -- "
                    "repli sur une approximation rectangle depuis le plan de fondation. Fiable "
                    "uniquement si le bâtiment est effectivement de forme rectangulaire simple."
                )
                break

    # ---- 4) Rien de disponible ----
    if surface_brute is None:
        return {
            "donnee_indisponible": True,
            "raison": (
                "Aucune donnée exploitable pour calculer la surface de dallage: pas de plan "
                "architectural avec surface ou contour, et pas de cote cumulée totale sur le "
                "plan de fondation."
            ),
        }

    # ---- Déductions (poteaux, longrines, voiles) ----
    poteaux_area = 0.0
    for item in boq["poteaux"]["total_par_section"]:
        area = _parse_section_area_m2(item["section"])
        if area is None:
            avertissements.append(f"Section de poteau '{item['section']}' non reconnue, exclue du calcul.")
            continue
        poteaux_area += area * item["nombre_total"]
    poteaux_area = round(poteaux_area, 2)

    longrines_area = 0.0
    for item in boq["longrines"]["total_par_section"]:
        width = _parse_section_width_m(item["section"])
        if width is None:
            avertissements.append(f"Section de longrine '{item['section']}' non reconnue, exclue du calcul.")
            continue
        longrines_area += width * item["longueur_totale_m"]
    longrines_area = round(longrines_area, 2)

    voiles_area = 0.0
    voiles_epaisseur_par_defaut_utilisee = False
    for item in boq["voiles"]["total_par_designation"]:
        if not item.get("longueur_totale_m"):
            continue
        voiles_area += item["longueur_totale_m"] * 0.20  # épaisseur par défaut si non annotée
        voiles_epaisseur_par_defaut_utilisee = True
    voiles_area = round(voiles_area, 2)
    if voiles_epaisseur_par_defaut_utilisee:
        avertissements.append(
            "Épaisseur des voiles jamais annotée -- 20cm utilisé par défaut pour le calcul "
            "(à corriger si l'épaisseur réelle diffère)."
        )

    surface_nette = round(surface_brute - poteaux_area - longrines_area - voiles_area, 2)

    return {
        "donnee_indisponible": False,
        "source": source,
        "surface_brute_m2": surface_brute,
        "deductions_m2": {
            "poteaux": poteaux_area,
            "longrines": longrines_area,
            "voiles": voiles_area,
        },
        "surface_nette_dallage_m2": surface_nette,
        "avertissements": avertissements,
    }


def build_bilan(boq: dict, surface_dallage: dict, surfaces_superstructure: dict = None) -> dict:
    """Résumé plat et propre, pensé pour être consommé par un LLM de
    raisonnement en aval sans risque de confusion -- pas de détail par
    page, juste les totaux par type d'élément. Le détail complet reste
    disponible dans 'boq' et 'pages_analysees' pour l'audit humain."""

    # Semelles vs radiers: distingués par mot-clé dans la désignation,
    # dédupliqués par désignation (un même élément ne doit compter qu'une
    # fois même s'il apparaît sur plusieurs pages).
    semelles_seen = {}
    radiers_seen = {}
    for page in boq["fondation"]["par_page"]:
        for s in page["semelles"]:
            target = radiers_seen if "RADIER" in s["designation"].upper() else semelles_seen
            target.setdefault(s["designation"], s)

    def _format_element(designation, s):
        return {
            "designation": designation,
            "dimensions": f'{s["a_m"]}m x {s["b_m"]}m x {s["h_cm"]}cm',
            "a_m": s["a_m"], "b_m": s["b_m"], "h_cm": s["h_cm"],
            "nombre": s["nbre"],
        }

    return {
        "poteaux": {
            "source": boq["poteaux"]["source_utilisee_pour_total"],
            "par_section": boq["poteaux"]["total_par_section"],
        },
        "poteaux_coffrage_par_section": boq["poteaux"]["poteaux_coffrage"]["total_par_section"],
        "semelles": [_format_element(d, s) for d, s in semelles_seen.items()],
        "radiers": [_format_element(d, s) for d, s in radiers_seen.items()],
        "voiles_par_type": boq["voiles"]["total_par_designation"],
        "longrines_par_section": boq["longrines"]["total_par_section"],
        "escaliers": boq["escaliers"],
        "surface_dallage": surface_dallage,
        "elements_structurels_par_type": boq["elements_structurels"]["par_type"],
        "surfaces_superstructure": surfaces_superstructure or {"donnee_indisponible": True},
    }


def run_pipeline(pdf_paths, on_log=None) -> dict:
    """pdf_paths: un chemin (str, rétrocompatible) OU une liste de chemins
    -- un par document envoyé (fondation, longrine, coffrage, note de
    calcul... peuvent être des fichiers séparés). Toutes les pages de tous
    les documents sont classifiées puis analysées dans un même pool: la
    Passe 3 agrège across-fichiers exactement comme elle agrège
    across-pages, donc un plan de fondation envoyé comme fichier séparé
    du reste sert de repli pour les longrines de la même façon qu'une page
    de fondation à l'intérieur d'un seul PDF fusionné (voir docstring en
    tête de fichier)."""
    if isinstance(pdf_paths, (str, Path)):
        pdf_paths = [pdf_paths]

    docs = {}  # label (nom de fichier) -> fitz.Document, dans l'ordre d'envoi
    for path in pdf_paths:
        label = Path(path).name
        if label in docs:
            # Deux fichiers envoyés avec le même nom -- on désambiguïse
            # plutôt que d'écraser silencieusement l'un des deux documents.
            base, dot, ext = label.rpartition(".")
            label = f"{base or label} ({sum(1 for k in docs if k.startswith(base or label))+1}){dot}{ext}"
        docs[label] = fitz.open(path)

    total_pages_all = sum(len(d) for d in docs.values())

    # ---- Passe 1: classification gratuite, sur toutes les pages de tous les fichiers ----
    routed_pages = []  # [(label, page_index, category)]
    for label, doc in docs.items():
        for i in range(len(doc)):
            title = extract_cartouche_title(doc[i])
            category = classify_title(title)
            if category:
                routed_pages.append((label, i, category))

    if on_log:
        noms = ", ".join(docs.keys())
        on_log(f"Passe 1 terminée: {len(routed_pages)}/{total_pages_all} pages pertinentes "
               f"sur {len(docs)} fichier(s) [{noms}] "
               f"({', '.join(sorted(set(c for _, _, c in routed_pages)))})")

    if not routed_pages:
        for doc in docs.values():
            doc.close()
        return {"pages_analysees": [], "boq": None,
                "avertissement": "Aucune page pertinente détectée (fondation/longrine/coffrage) "
                                  f"dans {len(docs)} fichier(s) envoyé(s)."}

    # ---- Passe 2: vision ciblée, en parallèle sur l'ensemble des pages retenues ----
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for label, i, category in routed_pages:
            image_bytes = render_page_png(docs[label][i])
            futures.append(executor.submit(process_page, label, i, category, image_bytes, on_log))

        for future in as_completed(futures):
            results.append(future.result())

    for doc in docs.values():
        doc.close()
    results.sort(key=lambda r: (r.get("fichier") or "", r["page"]))

    # ---- Passe 3: agrégation déterministe (Python, pas de LLM) ----
    boq = build_boq(results)
    surface_dallage = compute_surface_dallage(boq, results)
    surfaces_superstructure = compute_surfaces_superstructure(results)
    bilan = build_bilan(boq, surface_dallage, surfaces_superstructure)
    bilan["volumes_beton"] = build_volumes_beton(bilan, boq)

    return {
        "bilan": bilan,
        "boq": boq,
        "pages_analysees": results,
    }
