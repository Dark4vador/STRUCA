"""
Schéma JSON unique, utilisé pour TOUTE page retenue (fondation, longrine,
coffrage), peu importe ce que dit son titre.

Pourquoi un schéma unique et pas un schéma par titre de page: certains
documents mélangent plusieurs types de plans sur une même feuille (ex:
longrines dessinées directement sur le plan de fondation, sans page
dédiée "PLAN DE LONGRINE"). Un schéma par titre ratait alors totalement
les longrines sur cette page. Avec un schéma unique, chaque page retenue
est systématiquement analysée pour TOUS les types d'éléments possibles;
les listes restent simplement vides pour ce qui n'est pas présent.
"""

import re

SCHEMA_PLAN_EXECUTION = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "niveau": {
            "type": ["string", "null"],
            "description": "Niveau indiqué dans le cartouche si présent (ex: PHRDC, PHR+1), sinon null.",
        },
        "emprise_generale": {
            "type": "object",
            "additionalProperties": False,
            "description": "Dimensions globales du bâtiment, lues sur la cote TOTALE de la chaîne de cotation (la ligne de cote la plus extérieure/cumulée, PAS les segments individuels entre axes). Ex: si le haut du plan montre plusieurs lignes de cotes empilées (5.20, puis 12.80, puis 14.70 tout en haut), c'est 14.70 qui est la largeur totale, pas 5.20.",
            "properties": {
                "largeur_totale_m": {
                    "type": ["number", "null"],
                    "description": "Cote totale la plus extérieure le long de l'axe X (horizontal), sinon null si non identifiable avec certitude.",
                },
                "longueur_totale_m": {
                    "type": ["number", "null"],
                    "description": "Cote totale la plus extérieure le long de l'axe Y (vertical), sinon null si non identifiable avec certitude.",
                },
            },
            "required": ["largeur_totale_m", "longueur_totale_m"],
        },
        "semelles": {
            "type": "array",
            "description": "Contenu du tableau LEGENDE SEMELLES si présent sur cette page, sinon liste vide.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "designation": {"type": "string"},
                    "a_m": {"type": "number"},
                    "b_m": {"type": "number"},
                    "h_cm": {"type": "number"},
                    "nbre": {"type": "integer"},
                },
                "required": ["designation", "a_m", "b_m", "h_cm", "nbre"],
            },
        },
        "poteaux_legende": {
            "type": "array",
            "description": "Contenu du tableau LEGENDE POTEAUX si présent (dimensions par désignation), sinon liste vide.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "designation": {"type": "string"},
                    "a_cm": {"type": "string"},
                    "b_cm": {"type": "string"},
                },
                "required": ["designation", "a_cm", "b_cm"],
            },
        },
        "poteaux_instances": {
            "type": "array",
            "description": "Une entrée par poteau physiquement visible sur le dessin (pas la légende) -- même si plusieurs partagent la même désignation.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "designation": {"type": "string"},
                    "repere_grille": {"type": "string"},
                    "section": {
                        "type": ["string", "null"],
                        "description": "Section inline si écrite à côté du poteau sur CE dessin (ex: plan de coffrage: '25x60'). Laisse null si seule la légende séparée donne la dimension.",
                    },
                },
                "required": ["designation", "repere_grille", "section"],
            },
        },
        "longrines": {
            "type": "array",
            "description": "Une entrée PAR TRONÇON de longrine individuellement visible, si des longrines sont dessinées sur cette page (même mélangées à un plan de fondation ou de coffrage) -- sinon liste vide.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "designation": {"type": "string"},
                    "section": {"type": "string"},
                    "repere_debut": {"type": "string"},
                    "repere_fin": {"type": "string"},
                    "longueur_m": {
                        "type": ["number", "null"],
                        "description": "Longueur en mètres lue depuis une cote déjà annotée sur le plan entre repere_debut et repere_fin. Null si aucune cote fiable visible -- ne jamais deviner.",
                    },
                },
                "required": ["designation", "section", "repere_debut", "repere_fin", "longueur_m"],
            },
        },
        "voiles_instances": {
            "type": "array",
            "description": "Une entrée par voile (mur en béton armé) visible -- rectangle ALLONGÉ hachuré/grisé, souvent libellé V1, V2... Différent d'un poteau (carré/rond isolé) et d'une longrine (trait fin). Un même noyau/cage peut avoir plusieurs côtés avec des désignations différentes (V1 pour les côtés horizontaux, V2 pour les côtés verticaux par exemple) -- traite chaque côté comme une entrée séparée.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "designation": {"type": "string"},
                    "repere_debut": {"type": "string"},
                    "repere_fin": {"type": "string"},
                    "longueur_m": {
                        "type": ["number", "null"],
                        "description": "Longueur du voile en mètres, déduite des cotes d'axe déjà annotées sur le plan entre repere_debut et repere_fin (même technique que pour les longrines) -- PAS besoin d'une cote écrite directement à côté du voile lui-même. Null seulement si aucune cote d'axe fiable ne permet de la déduire.",
                    },
                    "epaisseur_cm": {"type": ["number", "null"]},
                },
                "required": ["designation", "repere_debut", "repere_fin", "longueur_m", "epaisseur_cm"],
            },
        },
        "dimensions_generales_m": {
            "type": "object",
            "additionalProperties": False,
            "description": "Dimensions extérieures totales du bâtiment, lues sur la cote CUMULÉE la plus externe du plan (ex: le total '14.70' en haut, le total '29.20' sur le côté) -- PAS une cote intermédiaire entre deux axes. Uniquement sur les plans où cette cote totale est visible (souvent fondation/radier), sinon tous les champs à null.",
            "properties": {
                "longueur_x_m": {"type": ["number", "null"]},
                "largeur_y_m": {"type": ["number", "null"]},
            },
            "required": ["longueur_x_m", "largeur_y_m"],
        },
        "escaliers": {
            "type": "array",
            "description": "Une entrée par volée/cage d'escalier visible sur cette page (typiquement une coupe d'escalier), sinon liste vide.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "designation": {"type": "string"},
                    "nombre_marches": {
                        "type": ["integer", "null"],
                        "description": "Nombre de marches de la volée, si annoté ou comptable directement sur le dessin.",
                    },
                    "giron_cm": {
                        "type": ["number", "null"],
                        "description": "Giron (profondeur d'une marche, en cm), si annoté.",
                    },
                    "hauteur_marche_cm": {
                        "type": ["number", "null"],
                        "description": "Hauteur d'une marche/contremarche (en cm), si annotée.",
                    },
                    "largeur_volee_m": {
                        "type": ["number", "null"],
                        "description": "Largeur de la volée (perpendiculaire au sens de la montée, en m), si annotée.",
                    },
                    "beche": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "description": "Bèche d'escalier (petit massif/mur en pied de volée qui reçoit la première marche), si dessinée et cotée sur cette page. Sinon null.",
                        "properties": {
                            "longueur_m": {"type": ["number", "null"]},
                            "largeur_cm": {"type": ["number", "null"]},
                            "hauteur_cm": {"type": ["number", "null"]},
                        },
                        "required": ["longueur_m", "largeur_cm", "hauteur_cm"],
                    },
                },
                "required": ["designation", "nombre_marches", "giron_cm", "hauteur_marche_cm",
                             "largeur_volee_m", "beche"],
            },
        },
        "surface_archi": {
            "type": "object",
            "additionalProperties": False,
            "description": "Uniquement pertinent sur un plan architectural (PLAN ARCHITECTURE / PLAN DE MASSE / PLAN DE NIVEAU). Deux façons de donner la surface, à essayer dans cet ordre.",
            "properties": {
                "surface_totale_m2": {
                    "type": ["number", "null"],
                    "description": "Si un tableau des surfaces est visible sur le plan (surface habitable, emprise au sol, surface plancher...) donnant un chiffre déjà calculé en m², reporte-le ici tel quel. Sinon null.",
                },
                "contour_m": {
                    "type": ["array", "null"],
                    "description": "Si aucun chiffre de surface n'est écrit mais que le contour extérieur du bâtiment est clairement visible avec ses cotes, liste les sommets du contour dans l'ordre (coordonnées en mètres, origine arbitraire cohérente entre les points) pour permettre un calcul d'aire. Sinon null -- ne devine jamais des coordonnées non déductibles des cotes visibles.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"x_m": {"type": "number"}, "y_m": {"type": "number"}},
                        "required": ["x_m", "y_m"],
                    },
                },
                "surface_dalle_pleine_m2": {
                    "type": ["number", "null"],
                    "description": "UNIQUEMENT pour un plan de NIVEAU D'ÉTAGE (pas le rez-de-chaussée/dallage sol): si un tableau de surfaces distingue une zone en 'dalle pleine' (souvent balcons, cages d'escalier/ascenseur, loggias) avec un chiffre m² déjà calculé, reporte-le ici. Sert au poste 3.17. Null si non distingué sur cette page.",
                },
                "surface_plancher_corps_creux_m2": {
                    "type": ["number", "null"],
                    "description": "UNIQUEMENT pour un plan de NIVEAU D'ÉTAGE: surface du plancher en corps creux/poutrelles-hourdis de ce niveau, si un chiffre m² déjà calculé est visible (tableau de surfaces ou mention). Sert aux postes 3.21 (plancher corps creux) et 3.22 (table de compression, même surface). Null si non disponible sur cette page.",
                },
            },
            "required": ["surface_totale_m2", "contour_m", "surface_dalle_pleine_m2", "surface_plancher_corps_creux_m2"],
        },
    },
    "required": [
        "niveau", "semelles", "poteaux_legende", "poteaux_instances",
        "longrines", "voiles_instances", "dimensions_generales_m", "escaliers", "surface_archi",
    ],
}

PROMPT_PLAN_EXECUTION = """Tu analyses une page de plan d'exécution BTP (français). Cette
page peut mélanger plusieurs types de contenu (fondation, longrines,
coffrage) sur un même dessin, ou n'en contenir qu'un seul -- analyse tout
ce qui est réellement présent, et laisse une liste vide pour ce qui ne
l'est pas. Ne suppose jamais qu'un type de contenu est absent juste parce
que le titre du plan suggère autre chose.

1) NIVEAU: si un niveau est indiqué dans le cartouche (ex: PHRDC, PHR+1),
   reporte-le dans niveau, sinon null.

2) EMPRISE GÉNÉRALE: si le plan affiche une chaîne de cotes empilées le
   long des axes (plusieurs lignes de cotes, chacune donnant un total
   cumulé plus grand que la précédente), identifie la cote LA PLUS
   EXTÉRIEURE de chaque côté -- c'est la dimension totale du bâtiment,
   pas les segments intermédiaires entre axes voisins. Reporte-la dans
   emprise_generale.largeur_totale_m (axe X) et longueur_totale_m (axe
   Y). Si tu n'es pas sûr laquelle est la cote totale, laisse à null
   plutôt que de deviner.

3) SEMELLES: si un tableau "LEGENDE SEMELLES" est visible, retranscris-le
   exactement dans semelles. Sinon liste vide. N'invente aucune valeur.

4) POTEAUX:
   a) Si un tableau "LEGENDE POTEAUX" (dimensions par désignation) est
      visible, retranscris-le dans poteaux_legende.
   b) Compte et liste CHAQUE poteau individuellement visible sur le
      dessin dans poteaux_instances (une entrée par point/symbole
      dessiné avec son repère de grille), même si plusieurs partagent la
      même désignation. Si la section est écrite en clair à côté du
      poteau sur CE dessin (ex: "25x60"), reporte-la dans "section" --
      sinon laisse section à null (elle sera résolue via poteaux_legende
      si besoin).

4) LONGRINES: si des tronçons de longrine sont dessinés sur cette page
   (avec ou sans page dédiée), liste CHAQUE tronçon individuellement
   dans longrines (jamais un total groupé). Deux pièges à éviter:
   a) Les libellés utilisent souvent un suffixe numéroté serré (ex:
      LG8.1, LG8.2, LG8.3... jusqu'à LG8.11) -- vérifie bien CHAQUE
      suffixe individuellement, ne t'arrête pas au premier chiffre
      visible.
   b) NE CONFONDS JAMAIS un libellé de longrine (désignation avec des
      lettres) avec une COTE DE DISTANCE entre axes (nombres seuls comme
      "5.20", "3.80" écrits le long des lignes de grille). Une
      désignation de longrine contient toujours des lettres, jamais un
      nombre seul.
   Pour longueur_m, lis la cote déjà annotée sur le plan entre les deux
   repères -- ne calcule ni n'invente une longueur non explicitement
   écrite. Si aucune cote fiable n'est visible pour un tronçon, mets
   longueur_m à null plutôt que de deviner.

5) VOILES: compte et liste CHAQUE côté de voile visible dans
   voiles_instances. Un voile se reconnaît à sa forme: un rectangle
   ALLONGÉ hachuré/grisé (souvent libellé V1, V2...), différent d'un
   poteau (carré/rond isolé, plus petit) et d'une longrine (simple trait
   fin). Points importants:
   a) Un même noyau/cage (souvent dessiné comme un carré ou rectangle
      hachuré) peut regrouper PLUSIEURS voiles avec des désignations
      différentes selon l'orientation (ex: V1 pour les côtés
      horizontaux, V2 pour les côtés verticaux du même carré) -- liste
      CHAQUE côté/segment comme une entrée séparée avec sa propre
      désignation et son propre repere_debut/repere_fin, même s'ils
      appartiennent visuellement à la même forme.
   b) Les voiles apparaissent parfois PAR PAIRE SYMÉTRIQUE (ex: aux deux
      extrémités d'une cage d'escalier), mais le libellé n'est écrit
      qu'une seule fois pour toute la paire -- repère aussi les
      occurrences non labellisées visuellement identiques et
      attribue-leur la désignation du voile labellisé le plus proche.
   c) Pour longueur_m: DÉDUIS la longueur depuis les cotes d'axe déjà
      annotées sur le plan entre repere_debut et repere_fin -- exactement
      la même technique que pour les longrines (les distances du type
      "5.20", "2.20", "2.42" affichées le long des axes X/Y). Tu n'as PAS
      besoin d'une cote écrite directement à côté du voile: utilise les
      cotes de grille qui bornent le voile. Mets null seulement si aucune
      cote d'axe fiable ne permet de la déterminer.
   d) epaisseur_cm: uniquement si explicitement annotée (légende ou cote
      d'épaisseur), sinon null.

7) ESCALIER: si une coupe ou un détail d'escalier est visible sur cette
   page (souvent un profil en marches d'un côté, avec des cotes de giron/
   hauteur de marche), liste chaque volée dans escaliers:
   a) nombre_marches: compte les marches visibles, ou reporte le nombre
      annoté si écrit (ex: "12 marches").
   b) giron_cm / hauteur_marche_cm: lis les cotes annotées à côté d'une
      marche typique (souvent "giron" ou "g=" et "hauteur"/"contremarche"
      ou "h="). Ne déduis jamais ces valeurs d'une autre cote -- laisse
      null si non explicitement annoté.
   c) largeur_volee_m: largeur de la volée perpendiculairement au sens de
      la montée, si cotée (souvent visible en plan plutôt qu'en coupe --
      utilise la cote si présente sur cette page, sinon null).
   d) beche: la bèche d'escalier est un petit massif en béton en pied de
      volée qui reçoit la première marche -- si elle est dessinée et cotée
      (longueur, largeur, hauteur), reporte ses dimensions, sinon laisse
      beche à null (ne pas confondre avec une simple ligne de contour non
      cotée).
   Si aucune coupe/détail d'escalier n'est présent sur cette page, laisse
   escaliers à liste vide -- n'invente jamais une volée non dessinée.

8) DIMENSIONS GÉNÉRALES: si la cote TOTALE cumulée du bâtiment est visible
   (le nombre le plus grand tout en haut du plan pour l'axe X, et le plus
   grand sur le côté pour l'axe Y -- PAS une cote intermédiaire entre deux
   axes voisins), reporte-la dans dimensions_generales_m. Sinon laisse
   longueur_x_m/largeur_y_m à null.

9) SURFACE ARCHITECTURALE (uniquement si cette page est un plan
   architectural / plan de masse / plan de niveau): cherche en priorité
   un tableau ou une mention donnant une surface déjà calculée en m²
   (surface habitable, emprise au sol, surface plancher...) et reporte-la
   dans surface_archi.surface_totale_m2. Si aucun chiffre n'est écrit
   mais que le contour extérieur du bâtiment est visible avec ses cotes,
   donne ses sommets dans surface_archi.contour_m. Si ni l'un ni l'autre
   n'est déductible avec certitude, laisse les deux champs à null --
   n'invente jamais de coordonnées ou de surface.

10) SURFACES DE PLANCHER D'ÉTAGE (uniquement sur un plan de NIVEAU
    D'ÉTAGE, pas le rez-de-chaussée/dallage sol): si un tableau de
    surfaces sur cette page distingue explicitement une zone "dalle
    pleine" (souvent balcons, cages d'escalier/ascenseur, loggias) avec
    un chiffre m² déjà calculé, reporte-le dans
    surface_archi.surface_dalle_pleine_m2. Si le même tableau (ou une
    autre mention) donne la surface du plancher en corps creux/poutrelles
    hourdis de ce niveau, reporte-la dans
    surface_archi.surface_plancher_corps_creux_m2. Laisse à null si non
    distingué explicitement sur cette page -- ne déduis jamais ces deux
    surfaces l'une de l'autre ni de surface_totale_m2.

N'invente jamais une valeur non présente sur l'image."""


# ---------------------------------------------------------------------
# Note de calcul de structure (récap déjà chiffré par l'ingénieur BA) --
# utilisée pour couvrir la Superstructure (postes 3.11 à 3.20). On lit
# les SECTIONS + LONGUEURS/NOMBRES bruts, PAS un volume m3 déjà calculé:
# le volume est ensuite recalculé en Python (même philosophie que pour
# les longrines/voiles d'infrastructure), pour rester auditable et éviter
# une erreur d'arithmétique du LLM.
# ---------------------------------------------------------------------

SCHEMA_NOTE_CALCUL = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "elements_structurels": {
            "type": "array",
            "description": "Une entrée par ligne du tableau récapitulatif de structure (poteaux, raidisseurs, voiles, poutres, chaînages, appuis de baies, éléments décoratifs, rampes d'accès), telle que présentée dans la note de calcul. Une ligne par section/désignation distincte -- pas de total groupé entre types différents.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["poteau", "raidisseur", "voile", "poutre", "chainage",
                                 "appui_baie", "element_decoratif", "rampe_acces"],
                        "description": "Type structurel de la ligne, déduit de son intitulé dans le tableau.",
                    },
                    "designation": {"type": "string"},
                    "niveau": {
                        "type": ["string", "null"],
                        "description": "Étage/niveau concerné si précisé dans le tableau (ex: RDC, R+1), sinon null.",
                    },
                    "section": {
                        "type": ["string", "null"],
                        "description": "Section en cm (ex: '20x20', '15x60'), ou 'D25' pour une section circulaire. Null si non applicable (ex: rampe d'accès).",
                    },
                    "nombre": {
                        "type": ["integer", "null"],
                        "description": "Nombre d'exemplaires de cette ligne -- pour les éléments comptés à l'unité (poteau, raidisseur, appui_baie, element_decoratif). Null pour les éléments linéaires continus (poutre, chainage, voile) où seule longueur_totale_m compte.",
                    },
                    "hauteur_m": {
                        "type": ["number", "null"],
                        "description": "Hauteur d'un exemplaire (poteau, raidisseur) ou hauteur du voile, en mètres, telle qu'écrite dans la note. Null si non applicable/non écrite.",
                    },
                    "longueur_totale_m": {
                        "type": ["number", "null"],
                        "description": "Longueur développée totale déjà annotée dans la note (poutre, chainage, voile, appui_baie filant). Null si non applicable/non écrite -- ne déduis jamais une longueur non explicitement chiffrée dans la note.",
                    },
                    "epaisseur_cm": {
                        "type": ["number", "null"],
                        "description": "Épaisseur, uniquement pour un voile. Null sinon.",
                    },
                    "rampe": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "description": "UNIQUEMENT si type == 'rampe_acces': dimensions de la rampe (longueur x largeur x épaisseur), si chiffrées dans la note. Sinon null.",
                        "properties": {
                            "longueur_m": {"type": ["number", "null"]},
                            "largeur_m": {"type": ["number", "null"]},
                            "epaisseur_m": {"type": ["number", "null"]},
                        },
                        "required": ["longueur_m", "largeur_m", "epaisseur_m"],
                    },
                },
                "required": ["type", "designation", "niveau", "section", "nombre",
                             "hauteur_m", "longueur_totale_m", "epaisseur_cm", "rampe"],
            },
        },
    },
    "required": ["elements_structurels"],
}

PROMPT_NOTE_CALCUL = """Tu analyses une page de NOTE DE CALCUL de structure béton armé
(français). Cette page contient typiquement un ou plusieurs tableaux
récapitulatifs listant les éléments de superstructure avec leurs sections,
longueurs ou nombres.

Pour CHAQUE ligne de CHAQUE tableau pertinent, crée une entrée dans
elements_structurels avec:
- type: déduis-le de l'intitulé de la ligne/du tableau (poteau, raidisseur,
  voile, poutre, chainage, appui_baie, element_decoratif, rampe_acces).
- section: la section telle qu'écrite (ex: "20x20"), si applicable.
- nombre: UNIQUEMENT si la ligne compte des exemplaires individuels
  (poteaux, raidisseurs, appuis de baie ponctuels, éléments décoratifs).
- hauteur_m: hauteur d'un exemplaire ou d'étage, si écrite (poteaux,
  raidisseurs, voiles).
- longueur_totale_m: longueur développée déjà chiffrée dans la note
  (poutres, chaînages, voiles, appuis de baie filants).
- epaisseur_cm: uniquement pour un voile.
- rampe: uniquement pour une rampe d'accès, avec ses 3 dimensions si
  chiffrées.

RÈGLES IMPORTANTES:
a) Lis les valeurs BRUTES (sections, longueurs, nombres, hauteurs) telles
   qu'écrites dans la note -- NE calcule PAS toi-même un volume m3, même
   si tu en serais capable. Laisse le champ correspondant à null si
   l'information n'est pas explicitement écrite; le volume sera recalculé
   en aval de façon déterministe.
b) Ne mélange jamais deux types différents dans une même entrée.
c) Si un même type de ligne apparaît pour plusieurs niveaux différents
   (ex: poteaux RDC et poteaux R+1 avec des sections/hauteurs
   différentes), crée une entrée SÉPARÉE par niveau -- ne les fusionne
   jamais en une seule ligne.
d) N'invente jamais une valeur non présente sur l'image."""


# ---------- Classification du titre de page (texte natif -> catégorie) ----------
#
# LISTE BLANCHE STRICTE : seules les catégories listées ici déclenchent un
# appel vision (donc un coût). Tout le reste (notes de calcul, géotechnique,
# sommaire, devis, page de garde, généralités...) est ignoré PAR DÉFAUT,
# même si son titre contient un mot qui ressemble à une catégorie ci-dessous.
#
# La catégorie sert uniquement à savoir QUELLE PAGE GARDER et, pour les
# totaux de poteaux, quelle page privilégier comme source de référence
# (voir pipeline.py) -- elle ne limite plus ce qui est extrait sur la
# page (schéma unique désormais, voir SCHEMA_PLAN_EXECUTION ci-dessus).

TITLE_KEYWORDS = {
    "fondation": ["PLAN DE FONDATION", "PLAN DE RADIER", "FONDATION - RADIER"],
    "longrine": ["PLAN DE LONGRINE"],
    "archi": ["PLAN ARCHITECTURE", "PLAN DE MASSE", "PLAN DE NIVEAU", "PLAN ARCHI"],
    # Slot dédié à la note de calcul de structure (récap déjà chiffré des
    # sections/longueurs/nombres de poteaux, raidisseurs, voiles, poutres,
    # chaînages, appuis de baies... par l'ingénieur BA) -- utilisé pour
    # couvrir la Superstructure (postes 3.11-3.20). Avant, "NOTE DE CALCUL"
    # était dans EXCLUDE_KEYWORDS et la page était purement ignorée.
    "note_calcul": ["NOTE DE CALCUL", "MEMOIRE DE CALCUL"],
    # Réactivé pour couvrir la Superstructure (poteaux/voiles par étage,
    # postes 3.11+) -- était volontairement désactivé le temps de valider
    # l'infrastructure seule. `_aggregate_poteaux` sait déjà agréger ces
    # pages séparément (poteaux_coffrage, par niveau).
    "coffrage": ["PLAN DE COFFRAGE"],
    # Décommente/complète si ces plans existent dans tes dossiers et sont
    # indispensables au comptage (à valider au cas par cas, pas par défaut):
    # "ferraillage": ["PLAN DE FERRAILLAGE", "DETAIL FERRAILLAGE"],
    # "plancher": ["PLAN DE PLANCHER"],
    "escalier": ["ESCALIER"],
}

# Titres qu'on NE VEUT JAMAIS traiter, même en cas de faux positif sur les
# mots-clés ci-dessus (ex: une note de calcul qui mentionne "fondation" dans
# son texte sans être un plan). Vérifié en premier, prioritaire sur le match.
EXCLUDE_KEYWORDS = [
    "SOMMAIRE", "GEOTECHNIQUE", "DEVIS",
    "PAGE DE GARDE", "GENERALITES", "MEMOIRE", "RAPPORT",
]


def _header_line(raw_text: str) -> str:
    """Première ligne non vide du texte natif de la page -- c'est là que
    vit quasi toujours le titre/l'intitulé réel de la page, avant tout
    paragraphe de corps de texte (hypothèses, renvois à d'autres
    documents, etc.)."""
    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


# Repli de contenu (indépendant du titre de la page) : capte le cas où des
# longrines sont dessinées sur une page dont l'intitulé officiel ne
# correspond à AUCUNE catégorie de TITLE_KEYWORDS -- ex. "PLAN DE COFFRAGE
# NIVEAU RDC", "PLAN DE CHAINAGE", ou tout intitulé maison non standard.
# Sans ce repli, une telle page est purement et simplement ignorée en
# Passe 1 et n'atteint jamais l'appel vision -- les longrines qu'elle
# contient sont alors invisibles pour tout le reste du pipeline (le
# schéma unique SCHEMA_PLAN_EXECUTION ne sert à rien si la page n'est
# jamais envoyée). On cherche soit le mot "LONGRINE" employé seul (sans
# le "PLAN DE" devant), soit des désignations typiques (LG1, LG8.2...).
LONGRINE_CONTENT_MARKERS = [
    re.compile(r"LONGRINE"),
    re.compile(r"\bLG\s?-?\d{1,3}(\.\d+)?\b"),
]


def _page_mentions_longrine(upper_text: str) -> bool:
    return any(pat.search(upper_text) for pat in LONGRINE_CONTENT_MARKERS)


def classify_title(raw_title: str) -> str | None:
    """Renvoie une catégorie normalisée ou None si la page doit être
    ignorée (par défaut: tout ce qui n'est pas un plan d'exécution listé
    dans TITLE_KEYWORDS).

    Ordre de vérification (important -- voir le commentaire détaillé
    au-dessus d'EXCLUDE_KEYWORDS pour le pourquoi) :
    1) un mot-clé TITLE_KEYWORDS trouvé sur la ligne d'en-tête (le titre
       réel de la page, presque toujours en première ligne) l'emporte
       toujours, même si EXCLUDE_KEYWORDS apparaît plus loin dans le
       corps de la page ;
    2) sinon, si l'en-tête lui-même annonce un type de page qu'on ne veut
       jamais traiter (sommaire, rapport géotechnique, devis...), on
       exclut ;
    3) sinon, repli : le titre n'est pas toujours sur la toute première
       ligne (ex: légende avant l'intitulé) -- on le cherche dans le
       texte entier ;
    4) dernier repli : marqueurs de contenu longrine, indépendants du
       titre.
    """
    upper = raw_title.upper()
    header = _header_line(raw_title).upper()

    for category, keywords in TITLE_KEYWORDS.items():
        if any(kw in header for kw in keywords):
            return category

    if any(kw in header for kw in EXCLUDE_KEYWORDS):
        return None

    for category, keywords in TITLE_KEYWORDS.items():
        if any(kw in upper for kw in keywords):
            return category

    # Aucun titre reconnu -- dernier repli avant d'ignorer la page : si des
    # marqueurs de longrine apparaissent quand même dans le texte natif de
    # la page (plan mixte avec un intitulé non standard), on la retient
    # sous la catégorie "longrine" pour qu'elle passe en Passe 2.
    if _page_mentions_longrine(upper):
        return "longrine"

    return None
