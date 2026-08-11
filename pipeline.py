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
"""

import re
import fitz  # PyMuPDF
from concurrent.futures import ThreadPoolExecutor, as_completed

from schemas import classify_title, SCHEMA_PLAN_EXECUTION, PROMPT_PLAN_EXECUTION
from gemini_client import call_vision_json, GeminiError

MAX_WORKERS = 3  # ~15 RPM Gemini Flash-Lite -> 3 en parallele reste largement sous la limite
DPI = 260  # résolution plus haute pour bien distinguer les libellés à suffixes serrés (LG8.1, LG8.2...)
# v22 -- résolution renforcée pour les catégories les plus sujettes aux
# grilles denses (beaucoup de poteaux rapprochés) : fondation et longrine.
# 'coffrage' et 'archi' restent à DPI standard (moins souvent aussi denses,
# et coffrage est de toute façon désactivé par défaut -- voir TITLE_KEYWORDS).
DPI_DENSE = 340
_DENSE_CATEGORIES = {"fondation", "longrine"}

# Libellé "métier" par catégorie de page, pour que les messages de
# progression parlent d'ÉLÉMENTS BTP (ce que l'utilisateur reconnaît et
# attend) plutôt que d'un seul numéro de page. Le schéma d'extraction est
# unique (SCHEMA_PLAN_EXECUTION) et cherche systématiquement TOUS ces
# éléments sur chaque page retenue, quelle que soit sa catégorie -- ce
# libellé reste indicatif de ce qu'on s'attend à trouver le plus souvent.
CATEGORY_ELEMENTS_LABEL = {
    "fondation": "semelles, poteaux et longrines (3.1-3.5, 3.7)",
    "longrine": "longrines et semelles filantes (3.4, 3.7)",
    "archi": "surfaces architecturales et dallage",
    "escalier": "marches et bèche d'escalier (3.9-3.10)",
    "coffrage": "poteaux de superstructure (3.11)",
}


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


def process_page(page_index: int, category: str, image_bytes: bytes, on_log=None) -> dict:
    """Un seul appel vision sur la page entière, sans découpage en tuiles.

    Historique: on a essayé le découpage en tuiles (pour mieux lire les
    libellés serrés sur les gros plans), puis 3 passes de granularité
    combinées par union dédupliquée (pour ne pas sous-estimer). Les deux
    approches ont fini par introduire plus de sur-comptage qu'elles n'en
    résolvaient (le même élément relu plusieurs fois avec des repères
    légèrement différents d'une tuile/passe à l'autre est difficile à
    dédupliquer de façon fiable). Un seul appel sur la page complète est
    plus lent à égaliser en précision de lecture sur les tout petits
    libellés, mais ne peut structurellement pas dupliquer un élément."""
    label = f"Extraction : {CATEGORY_ELEMENTS_LABEL.get(category, category)} — p.{page_index + 1} ({category})"
    if on_log:
        on_log(f"{label}...")
    try:
        data = call_vision_json(image_bytes, PROMPT_PLAN_EXECUTION, SCHEMA_PLAN_EXECUTION)
        if on_log:
            on_log(f"{label}: OK")
        return {"page": page_index + 1, "category": category, "data": data, "error": None}
    except GeminiError as e:
        if on_log:
            on_log(f"{label}: échec — aucun élément n'a pu être extrait de cette page ({e})")
        return {"page": page_index + 1, "category": category, "data": None, "error": str(e)}


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
        per_page.append({"page": r["page"], "semelles": r["data"]["semelles"]})
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
    legend_quantites = {}  # designation -> (section, quantite) -- v21: colonne Quantité de la légende
    for r in pages:
        if r["data"] is None:
            continue
        for leg in r["data"].get("poteaux_legende", []):
            section = f'{leg["a_cm"]}x{leg["b_cm"]}'
            legend_map[leg["designation"]] = section
            if leg.get("quantite"):
                legend_quantites[leg["designation"]] = (section, leg["quantite"])

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
            per_page.append({"page": r["page"], "nombre_poteaux": len(instances), "detail_par_section": page_counts})

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
    # v46 -- bug corrigé: contrairement à build_detail() ci-dessus (utilisé
    # pour fondation/longrine), cette boucle ne retombait JAMAIS sur
    # legend_map quand la section n'était pas écrite en clair sur CE plan
    # de coffrage précis -- alors qu'un plan de coffrage désigne presque
    # toujours ses poteaux par leur seul repère (ex: "P1"), en comptant sur
    # la légende du plan de fondation pour la section, exactement comme les
    # instances fondation/longrine. Résultat avant ce correctif: "section
    # inconnue" systématique dès que la section n'était pas répétée sur le
    # plan de coffrage lui-même (le cas normal), même quand legend_map
    # avait déjà la bonne réponse depuis la légende lue ailleurs.
    coffrage_per_page = []
    coffrage_total = {}
    for r in coffrage_pages:
        poteaux = r["data"].get("poteaux_instances", [])
        page_counts = {}
        for p in poteaux:
            section = p.get("section") or legend_map.get(p.get("designation"), "section inconnue")
            page_counts[section] = page_counts.get(section, 0) + 1
            coffrage_total[section] = coffrage_total.get(section, 0) + 1
        coffrage_per_page.append({
            "page": r["page"], "niveau": r["data"].get("niveau"),
            "nombre_poteaux": len(poteaux), "detail_par_section": page_counts,
        })

    # ---- Repli v20 : total global écrit en légende (ex: "Total Poteaux :
    # 121"), utilisé quand aucun poteau n'a pu être compté fiablement un
    # par un (grille/calepinage trop dense pour un comptage visuel sûr --
    # voir PROMPT_PLAN_EXECUTION §4c). Sans ce repli, ces plans
    # retournaient purement et simplement zéro poteau, silencieusement.
    total_legende_global = None
    total_legende_global_page = None
    for r in pages:
        if r["data"] is None:
            continue
        n = r["data"].get("poteaux_total_legende_global")
        if n:
            total_legende_global = n
            total_legende_global_page = r["page"]
            break  # une seule légende globale attendue par dossier -- le premier trouvé suffit

    # ---- v21 : répartition par section EXACTE quand la légende a une
    # colonne Quantité par type (voir PROMPT_PLAN_EXECUTION §4a) -- la
    # source la plus fiable qui existe, elle rend inutile à la fois le
    # comptage individuel ET la question "section représentative" posée
    # pour le simple total global. Priorité maximale si présente.
    total_legende_par_section = [
        {"designation": d, "section": s, "nombre_total": n}
        for d, (s, n) in legend_quantites.items()
    ] or None

    # ---- v21/v43 : raidisseurs listés dans une légende (souvent celle des
    # poteaux, distingués par la ligne "Total Raidisseur" -- voir
    # schemas.py). Deux postes différents selon la catégorie de la page:
    # fondation/longrine -> soubassement (3.5bis), coffrage -> superstructure
    # (3.12) -- jamais mélangés, même bug que les voiles avant séparation.
    raidisseurs_legende_total = {}
    raidisseurs_coffrage_total = {}
    for r in pages:
        if r["data"] is None:
            continue
        target = raidisseurs_coffrage_total if r["category"] == "coffrage" else raidisseurs_legende_total
        for item in r["data"].get("raidisseurs_legende", []) or []:
            section = f'{item["a_cm"]}x{item["b_cm"]}'
            if item.get("quantite"):
                key = (item["designation"], section)
                target[key] = item["quantite"]

    return {
        "source_utilisee_pour_total": source_used,
        "total_par_section": total_par_section,
        "par_page_source": per_page_total,
        "detail_toutes_pages_fondation_longrine": detail_toutes_pages,
        "total_legende_global": total_legende_global,
        "total_legende_global_page": total_legende_global_page,
        "total_legende_par_section": total_legende_par_section,
        "raidisseurs_legende_par_section": [
            {"designation": d, "section": s, "nombre_total": n}
            for (d, s), n in raidisseurs_legende_total.items()
        ],
        "raidisseurs_coffrage_par_section": [
            {"designation": d, "section": s, "nombre_total": n}
            for (d, s), n in raidisseurs_coffrage_total.items()
        ],
        "sections_possibles_legende": [
            {"designation": leg, "section": sec} for leg, sec in legend_map.items()
        ],
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
            "page": r["page"], "category": r["category"], "nombre_troncons": page_count,
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

    # ---- Repli v20 : réseau continu (voir PROMPT_PLAN_EXECUTION §5b) --
    # aucun tronçon désigné individuellement, mais un ou plusieurs types
    # génériques trouvés en légende (ex: "Longrine-Type 20x40").
    reseau_continu = {}  # type_designation -> section
    reseau_continu_pages = []
    for r in pages:
        if r["data"] is None:
            continue
        for item in r["data"].get("longrines_reseau_continu", []) or []:
            reseau_continu[item["type_designation"]] = item["section"]
        if r["data"].get("longrines_reseau_continu"):
            reseau_continu_pages.append(r["page"])

    # ---- v30 : calcul automatique de la longueur développée du réseau
    # continu à partir de la grille d'axes (voir PROMPT_PLAN_EXECUTION
    # §2bis) -- exactement le calcul qu'un humain ferait à la main (somme
    # des cotes entre axes × nombre de lignes dans l'autre sens), au lieu
    # de demander à l'utilisateur de le mesurer lui-même. Suppose que
    # CHAQUE ligne de la grille porte une longrine sur toute sa longueur
    # (vrai pour un bâtiment simple/rectangulaire à trame régulière -- à
    # vérifier si le bâtiment a une forme plus complexe, d'où le champ
    # "detail" exposé pour audit plutôt qu'un chiffre opaque).
    longueur_reseau_calculee_m = None
    longueur_reseau_calculee_detail = None
    for r in pages:
        if r["data"] is None:
            continue
        ga = r["data"].get("grille_axes") or {}
        cotes_x = ga.get("cotes_intermediaires_x_m") or []
        cotes_y = ga.get("cotes_intermediaires_y_m") or []
        nb_axes_y = ga.get("nombre_axes_y")
        nb_axes_x = ga.get("nombre_axes_x")
        if cotes_x and nb_axes_y and cotes_y and nb_axes_x:
            somme_x, somme_y = sum(cotes_x), sum(cotes_y)
            longueur_reseau_calculee_m = round(somme_x * nb_axes_y + somme_y * nb_axes_x, 2)
            # v33 -- garde-fou de plausibilité : nombre_axes_y devrait être
            # proche de len(cotes_x)+1 (le nombre de lignes de grille = le
            # nombre de segments de cote + 1 sur la dimension qu'elles
            # couvrent), et symétriquement pour nombre_axes_x/cotes_y. Un
            # grand écart signale presque toujours une mauvaise lecture du
            # nombre de lignes (ex: confusion avec un autre repère sur le
            # plan) plutôt qu'une vraie grille à 70+ lignes -- fréquent sur
            # les plans très denses. On ne bloque pas le calcul (il reste
            # utile comme point de départ) mais on le signale clairement
            # plutôt que de le présenter comme fiable par défaut.
            attendu_axes_y = len(cotes_x) + 1
            attendu_axes_x = len(cotes_y) + 1
            incoherent = (
                abs(nb_axes_y - attendu_axes_y) > max(2, attendu_axes_y * 0.5)
                or abs(nb_axes_x - attendu_axes_x) > max(2, attendu_axes_x * 0.5)
            )
            longueur_reseau_calculee_detail = {
                "page": r["page"],
                "somme_cotes_x_m": round(somme_x, 2), "nombre_axes_y": nb_axes_y,
                "somme_cotes_y_m": round(somme_y, 2), "nombre_axes_x": nb_axes_x,
                "hypothese": "chaque ligne de la grille porte une longrine sur toute sa longueur -- à vérifier si le bâtiment n'est pas de forme simple/régulière",
                "coherent": not incoherent,
            }
            if incoherent:
                longueur_reseau_calculee_detail["avertissement"] = (
                    f"Nombre de lignes de grille incohérent avec le nombre de cotes lues "
                    f"(attendu ~{attendu_axes_y} lignes Y depuis {len(cotes_x)} cotes X, lu "
                    f"{nb_axes_y} ; attendu ~{attendu_axes_x} lignes X depuis {len(cotes_y)} cotes Y, "
                    f"lu {nb_axes_x}) -- ce calcul est probablement faux (mauvaise lecture du nombre "
                    "de lignes sur une grille dense), à vérifier avant utilisation."
                )
            break  # la grille est propre au bâtiment entier -- la première page complète suffit

    return {
        "par_page": per_page, "total_par_section": total_list,
        "reseau_continu_types": [{"type_designation": t, "section": s} for t, s in reseau_continu.items()],
        "reseau_continu_pages": reseau_continu_pages,
        "longueur_reseau_calculee_m": longueur_reseau_calculee_m,
        "longueur_reseau_calculee_detail": longueur_reseau_calculee_detail,
    }


def _aggregate_voiles(pages):
    """Agrège les voiles à travers TOUTES les pages retenues -- un même
    voile physique peut apparaître sur plusieurs plans, donc déduplication
    globale par (designation, repere_debut, repere_fin). Même mécanique
    que les longrines pour la longueur (déduite des cotes d'axe).

    v41 -- sépare les voiles de SOUBASSEMENT (pages fondation/longrine --
    poste 3.6) des voiles de SUPERSTRUCTURE (pages coffrage, par étage --
    poste 3.13, futur). Avant cette séparation, activer la catégorie
    'coffrage' aurait mélangé silencieusement les deux dans le même total
    -- un voile d'étage aurait gonflé le poste 3.6 (soubassement) à tort.
    Même principe déjà en place pour les poteaux (poteaux_coffrage,
    compté séparément du total fondation/longrine)."""
    per_page = []
    total_seen = {}  # (designation, repere_debut, repere_fin) -> longueur_m
    coffrage_total_seen = {}  # même clé, mais uniquement pages coffrage (par étage)
    coffrage_per_page = []

    for r in pages:
        if r["data"] is None:
            continue
        voiles = r["data"].get("voiles_instances", [])
        if not voiles:
            continue
        is_coffrage = r["category"] == "coffrage"
        page_total_len = 0.0
        for v in voiles:
            key = (
                (v.get("designation") or "").strip().upper(),
                (v.get("repere_debut") or "").strip().upper() if isinstance(v.get("repere_debut"), str) else v.get("repere_debut"),
                (v.get("repere_fin") or "").strip().upper() if isinstance(v.get("repere_fin"), str) else v.get("repere_fin"),
            )
            target_seen = coffrage_total_seen if is_coffrage else total_seen
            if key not in target_seen:
                target_seen[key] = v.get("longueur_m")
            if v.get("longueur_m"):
                page_total_len += v["longueur_m"]
        entry = {
            "page": r["page"], "category": r["category"],
            "nombre_voiles": len(voiles),
            "detail": [
                {"designation": v["designation"], "de": v.get("repere_debut"), "a": v.get("repere_fin"),
                 "longueur_m": v.get("longueur_m")}
                for v in voiles
            ],
            "longueur_totale_m_page": round(page_total_len, 2),
        }
        (coffrage_per_page if is_coffrage else per_page).append(entry)

    def _totaliser(seen):
        by_designation = {}
        for (designation, _, _), longueur in seen.items():
            e = by_designation.setdefault(designation, {"nombre": 0, "longueur_totale_m": 0.0, "sans_longueur": 0})
            e["nombre"] += 1
            if longueur:
                e["longueur_totale_m"] += longueur
            else:
                e["sans_longueur"] += 1
        return [
            {"designation": d, "nombre": v["nombre"], "longueur_totale_m": round(v["longueur_totale_m"], 2),
             "sans_longueur_annotee": v["sans_longueur"]}
            for d, v in by_designation.items()
        ]

    total_par_designation = _totaliser(total_seen)
    coffrage_par_designation = _totaliser(coffrage_total_seen)

    return {
        "par_page": per_page,
        "nombre_total": len(total_seen),
        "longueur_totale_m": round(sum(v for v in total_seen.values() if v), 2),
        "total_par_designation": total_par_designation,
        "voiles_sans_longueur_annotee": sum(1 for v in total_seen.values() if not v),
        "voiles_coffrage": {
            "par_page": coffrage_per_page,
            "total_par_designation": coffrage_par_designation,
            "note": "Comptage séparé (par étage) -- ce ne sont pas les mêmes voiles physiques que le total soubassement (fondation/longrine) ci-dessus.",
        },
    }


def _aggregate_escaliers(pages):
    """Volées d'escalier -- rassemblées depuis toutes les pages retenues
    (typiquement catégorie 'escalier', mais peut apparaître ailleurs)."""
    escaliers = []
    for r in pages:
        if r["data"] is None or not r["data"].get("escaliers"):
            continue
        for e in r["data"]["escaliers"]:
            escaliers.append({**e, "page": r["page"]})
    return escaliers


def _aggregate_poutres(pages):
    """v42 -- poutres de superstructure (poste 3.14), lues sur les pages
    coffrage/poutraison uniquement (mêmes catégories que les poteaux/voiles
    de superstructure). Déduplication par (designation, section) à travers
    les pages -- une même poutre peut apparaître sur plusieurs vues."""
    seen = {}  # (designation, section) -> longueur_m
    per_page = []
    for r in pages:
        if r["data"] is None or r["category"] != "coffrage":
            continue
        poutres = r["data"].get("poutres_instances", [])
        if not poutres:
            continue
        for p in poutres:
            key = ((p.get("designation") or "").strip().upper(), (p.get("section") or "").strip().upper())
            if key not in seen:
                seen[key] = p.get("longueur_m")
        per_page.append({"page": r["page"], "nombre_poutres": len(poutres)})

    total_par_section = {}
    for (designation, section), longueur in seen.items():
        e = total_par_section.setdefault(section, {"nombre_troncons": 0, "longueur_totale_m": 0.0})
        e["nombre_troncons"] += 1
        if longueur:
            e["longueur_totale_m"] += longueur

    return {
        "par_page": per_page,
        "total_par_section": [
            {"section": s, "nombre_troncons": v["nombre_troncons"], "longueur_totale_m": round(v["longueur_totale_m"], 2)}
            for s, v in total_par_section.items()
        ],
    }


def _aggregate_chainage(pages):
    """v43 -- même principe que longrines_reseau_continu: type/section
    trouvés en légende sur les plans de coffrage, longueur confirmée en
    aval par l'utilisateur (impossible à calculer fiablement sans
    reconstruire la géométrie complète du périmètre + refends porteurs)."""
    seen = {}  # designation -> section
    for r in pages:
        if r["data"] is None or r["category"] != "coffrage":
            continue
        for item in r["data"].get("chainage_legende", []) or []:
            seen[item["designation"]] = item["section"]
    return {"types": [{"type_designation": d, "section": s} for d, s in seen.items()]}


def build_boq(results: list) -> dict:
    """Agrégation 100% déterministe (aucun appel LLM)."""
    return {
        "fondation": _aggregate_fondation(results),
        "poteaux": _aggregate_poteaux(results),
        "longrines": _aggregate_longrines(results),
        "voiles": _aggregate_voiles(results),
        "poutres": _aggregate_poutres(results),
        "chainage": _aggregate_chainage(results),
        "escaliers": _aggregate_escaliers(results),
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
    _total_legende_par_section = bilan["poteaux"].get("total_legende_par_section")
    _total_legende_global = bilan["poteaux"].get("total_legende_global")
    if _total_legende_par_section:
        # v21 : répartition EXACTE par section lue dans la colonne Quantité
        # de la légende (voir PROMPT_PLAN_EXECUTION §4a) -- la source la
        # plus fiable, prioritaire sur le comptage individuel ET sur le
        # simple total global. Il ne manque plus que la hauteur.
        postes["potelets"] = {
            "designation_devis": "3.5 Béton armé pour potelets",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": (
                "Répartition par section connue avec certitude (colonne Quantité de la légende "
                "poteaux) -- seule la hauteur de soubassement reste à confirmer pour calculer le "
                "volume."
            ),
            "donnees_disponibles_en_attente": {
                "sections_poteaux": _total_legende_par_section,
            },
        }
    elif not bilan["poteaux"]["par_section"] and _total_legende_global:
        # Repli v20 : ni comptage par instance ni répartition par section en
        # légende, seulement un total imprimé global (grille trop dense --
        # voir PROMPT_PLAN_EXECUTION §4d). Deux inconnues à lever plutôt
        # qu'une : la hauteur (comme d'habitude) ET la répartition par
        # section (le total ne dit pas combien de chaque type).
        postes["potelets"] = {
            "designation_devis": "3.5 Béton armé pour potelets",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": (
                f"{_total_legende_global} poteaux comptés globalement via un total imprimé en "
                "légende (grille trop dense pour un comptage fiable poteau par poteau, et la "
                "légende n'a pas de colonne Quantité par type) -- répartition par section ET "
                "hauteur de soubassement à confirmer pour calculer le volume."
            ),
            "donnees_disponibles_en_attente": {
                "total_legende_global": _total_legende_global,
                "sections_possibles_legende": bilan["poteaux"].get("sections_possibles_legende", []),
            },
        }
    else:
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

    # ---- 3.5bis Raidisseurs (niveau soubassement) -- poste AJOUTÉ, absent
    # de la nomenclature standard du canevas de référence (qui ne prévoit
    # des raidisseurs qu'en 3.12, au niveau superstructure). Ceux-ci sont
    # détectés directement sur la légende du plan de fondation (voir
    # pipeline.py: bilan['raidisseurs_legende_par_section']) -- des éléments
    # de soubassement distincts, pas les mêmes que 3.12. Même logique de
    # calcul que les potelets (3.5): même hauteur de soubassement. ----
    raidisseurs_legende = bilan.get("raidisseurs_legende_par_section") or []
    if raidisseurs_legende:
        postes["raidisseurs_soubassement"] = {
            "designation_devis": "3.5bis Béton armé pour raidisseurs (niveau soubassement -- poste ajouté, absent du canevas standard)",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": (
                "Raidisseurs détectés sur la légende du plan de fondation (distincts des poteaux) "
                "-- hauteur de soubassement à confirmer pour calculer le volume (même hauteur que "
                "pour les potelets, poste 3.5)."
            ),
            "donnees_disponibles_en_attente": {"sections_raidisseurs": raidisseurs_legende},
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

    # ---- v41 -- 3.11/3.13 Poteaux et voiles de SUPERSTRUCTURE (par étage,
    # lus sur les plans de coffrage -- catégorie longtemps désactivée). Même
    # logique que potelets/voiles soubassement, mais avec une hauteur
    # d'étage courant distincte de la hauteur de soubassement (les deux ne
    # valent presque jamais la même chose). ----
    poteaux_coffrage = bilan.get("poteaux_coffrage_par_section") or []
    if poteaux_coffrage:
        postes["poteaux_superstructure"] = {
            "designation_devis": "3.11 Béton armé pour poteaux",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": "Poteaux détectés sur plan(s) de coffrage -- hauteur d'étage courant à confirmer pour calculer le volume.",
            "donnees_disponibles_en_attente": {"sections_poteaux_coffrage": poteaux_coffrage},
        }

    # ---- v43 : 3.12 Raidisseurs superstructure (légende trouvée sur un
    # plan de coffrage -- distincts des raidisseurs de soubassement 3.5bis). ----
    raidisseurs_coffrage = bilan.get("raidisseurs_coffrage_par_section") or []
    if raidisseurs_coffrage:
        postes["raidisseurs_superstructure"] = {
            "designation_devis": "3.12 Béton armé pour raidisseurs",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": "Raidisseurs détectés sur plan(s) de coffrage -- hauteur d'étage courant à confirmer pour calculer le volume.",
            "donnees_disponibles_en_attente": {"sections_raidisseurs_coffrage": raidisseurs_coffrage},
        }

    voiles_coffrage = bilan.get("voiles_coffrage_par_type") or []
    if voiles_coffrage:
        postes["voiles_superstructure"] = {
            "designation_devis": "3.13 Béton armé pour voiles",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": "Voiles détectés sur plan(s) de coffrage -- hauteur d'étage courant et épaisseur voile à confirmer pour calculer le volume.",
            "donnees_disponibles_en_attente": {"voiles_coffrage_par_type": voiles_coffrage},
        }

    # ---- v42 : 3.14 Poutres -- section × portée déjà connues (pas besoin
    # de hauteur d'étage), calculable directement, sans attendre de
    # confirmation utilisateur -- comme les semelles isolées (3.3). ----
    poutres = bilan.get("poutres_par_section") or []
    if poutres:
        vol_poutres = 0.0
        poutres_sans_portee = []
        for item in poutres:
            aire = _parse_section_area_m2(item["section"])
            if aire is not None and item["longueur_totale_m"]:
                vol_poutres += aire * item["longueur_totale_m"]
            else:
                poutres_sans_portee.append(item["section"])
        postes["poutres_superstructure"] = {
            "designation_devis": "3.14 Béton armé pour poutres",
            "unite": "m3", "volume_m3": round(vol_poutres, 2), "donnee_indisponible": False,
            "raison": (
                f"Portée(s) non cotée(s) pour: {', '.join(poutres_sans_portee)} -- non comptée(s)."
                if poutres_sans_portee else None
            ),
        }

    # ---- v43 : 3.15 Chaînage -- type/section trouvés en légende sur plan
    # de coffrage, longueur à confirmer par l'utilisateur (identique au
    # mécanisme réseau continu des longrines, réutilisé tel quel). ----
    chainage_types = bilan.get("chainage_types") or []
    if chainage_types:
        postes["chainage_superstructure"] = {
            "designation_devis": "3.15 Béton armé pour chaînages",
            "unite": "m3", "volume_m3": None, "donnee_indisponible": True,
            "raison": "Chaînage détecté en légende sur plan(s) de coffrage -- longueur développée totale à confirmer pour calculer le volume.",
            "donnees_disponibles_en_attente": {"chainage_types": chainage_types},
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
        _reseau = bilan.get("longrines_reseau_continu") or []
        if _reseau:
            # Repli v20 : réseau continu détecté (types génériques en légende,
            # pas de tronçons désignés individuellement -- voir
            # PROMPT_PLAN_EXECUTION §5b). On ne calcule PAS de longueur ici
            # (reconstruire la géométrie de la grille de façon fiable n'est
            # pas possible depuis ce qu'on extrait) -- longueur totale à
            # confirmer par l'utilisateur, section déjà connue.
            _types_str = ", ".join(f"{t['type_designation']} {t['section']}" for t in _reseau)
            postes["longrines"] = {
                "designation_devis": "3.7 Béton armé pour longrines", "unite": "m3",
                "volume_m3": None, "donnee_indisponible": True,
                "raison": (
                    "Réseau continu de longrines détecté (pas de tronçons individuellement "
                    f"désignés, mais type(s) générique(s) en légende: {_types_str}) -- "
                    "longueur développée totale à confirmer (reconstruire la géométrie exacte "
                    "de la grille depuis le plan n'est pas fiable automatiquement)."
                ),
                "donnees_disponibles_en_attente": {"reseau_continu_types": _reseau},
            }
        else:
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


def build_bilan(boq: dict, surface_dallage: dict) -> dict:
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
            "total_legende_global": boq["poteaux"].get("total_legende_global"),
            "total_legende_par_section": boq["poteaux"].get("total_legende_par_section"),
            "sections_possibles_legende": boq["poteaux"].get("sections_possibles_legende") or [],
        },
        "poteaux_coffrage_par_section": boq["poteaux"]["poteaux_coffrage"]["total_par_section"],
        "raidisseurs_legende_par_section": boq["poteaux"].get("raidisseurs_legende_par_section") or [],
        "raidisseurs_coffrage_par_section": boq["poteaux"].get("raidisseurs_coffrage_par_section") or [],
        "semelles": [_format_element(d, s) for d, s in semelles_seen.items()],
        "radiers": [_format_element(d, s) for d, s in radiers_seen.items()],
        "voiles_par_type": boq["voiles"]["total_par_designation"],
        "voiles_coffrage_par_type": boq["voiles"]["voiles_coffrage"]["total_par_designation"],
        "poutres_par_section": boq["poutres"]["total_par_section"],
        "chainage_types": boq["chainage"]["types"],
        "longrines_par_section": boq["longrines"]["total_par_section"],
        "longrines_reseau_continu": boq["longrines"].get("reseau_continu_types") or [],
        "longueur_reseau_calculee_m": boq["longrines"].get("longueur_reseau_calculee_m"),
        "longueur_reseau_calculee_detail": boq["longrines"].get("longueur_reseau_calculee_detail"),
        "escaliers": boq["escaliers"],
        "surface_dallage": surface_dallage,
    }


def run_pipeline(pdf_path: str, on_log=None) -> dict:
    doc = fitz.open(pdf_path)
    total_pages = len(doc)

    # ---- Passe 1: classification gratuite ----
    routed_pages = []  # [(page_index, category)]
    for i in range(total_pages):
        page = doc[i]
        title = extract_cartouche_title(page)
        category = classify_title(title)
        if category:
            routed_pages.append((i, category))

    if on_log:
        on_log(f"Passe 1 terminée: {len(routed_pages)}/{total_pages} pages pertinentes "
               f"({', '.join(sorted(set(c for _, c in routed_pages)))})")

    if not routed_pages:
        doc.close()
        return {"pages_analysees": [], "boq": None,
                "avertissement": "Aucune page pertinente détectée (fondation/longrine/coffrage)."}

    # ---- Passe 2: vision ciblée, en parallèle ----
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for i, category in routed_pages:
            dpi = DPI_DENSE if category in _DENSE_CATEGORIES else DPI
            image_bytes = render_page_png(doc[i], dpi=dpi)
            futures[executor.submit(process_page, i, category, image_bytes, on_log)] = i

        for future in as_completed(futures):
            results.append(future.result())

    doc.close()
    results.sort(key=lambda r: r["page"])

    # ---- Passe 3: agrégation déterministe (Python, pas de LLM) ----
    boq = build_boq(results)
    surface_dallage = compute_surface_dallage(boq, results)
    bilan = build_bilan(boq, surface_dallage)
    bilan["volumes_beton"] = build_volumes_beton(bilan, boq)

    return {
        "bilan": bilan,
        "boq": boq,
        "pages_analysees": results,
    }
