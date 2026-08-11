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
                    "quantite": {
                        "type": ["integer", "null"],
                        "description": (
                            "Si la légende a une colonne 'Quantité' (ou équivalent) donnant le "
                            "nombre d'exemplaires de CE type précis, reporte-la ici -- c'est la "
                            "source la PLUS FIABLE pour le nombre de poteaux de ce type, bien plus "
                            "que compter les symboles un par un sur une grille dense. Null si la "
                            "légende n'a pas cette colonne ou que la case est vide pour cette ligne."
                        ),
                    },
                },
                "required": ["designation", "a_cm", "b_cm", "quantite"],
            },
        },
        "raidisseurs_legende": {
            "type": "array",
            "description": (
                "Certaines légendes combinent poteaux ET raidisseurs dans le même tableau, "
                "distingués par une ligne récapitulative séparée (ex: 'Total Poteaux : 121' PUIS "
                "'Total Raidisseur : 87' plus bas dans le même tableau). Les lignes situées APRÈS "
                "'Total Poteaux' et AVANT/JUSQU'À 'Total Raidisseur' sont des raidisseurs, pas des "
                "poteaux -- reporte-les ICI, séparément de poteaux_legende, avec leur quantité si "
                "donnée. Un raidisseur se reconnaît aussi à son intitulé (souvent préfixé R0, R1, "
                "R2, R3...) ou à 'Plot'. Liste vide si aucun raidisseur distinct n'est listé."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "designation": {"type": "string"},
                    "a_cm": {"type": "string"},
                    "b_cm": {"type": "string"},
                    "quantite": {"type": ["integer", "null"]},
                },
                "required": ["designation", "a_cm", "b_cm", "quantite"],
            },
        },
        "poteaux_total_legende_global": {
            "type": ["integer", "null"],
            "description": (
                "Si un TOTAL global de poteaux est écrit explicitement en toutes lettres près de "
                "la légende poteaux (ex: 'Total Poteaux : 121'), reporte ce nombre ici -- "
                "INDÉPENDAMMENT de ce que tu comptes dans poteaux_instances. Sur une grille très "
                "dense (beaucoup de poteaux rapprochés, trame de calepinage serrée), ce total "
                "imprimé est plus fiable qu'un comptage visuel poteau par poteau -- remplis ce "
                "champ dans TOUS les cas où un tel total est visible, même si tu remplis aussi "
                "poteaux_instances par ailleurs. Null si aucun total de ce type n'est écrit."
            ),
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
        "longrines_reseau_continu": {
            "type": "array",
            "description": (
                "Cas DIFFÉRENT de 'longrines' ci-dessus. Certains plans (souvent les gros projets "
                "avec calepinage dense) ne désignent PAS chaque tronçon individuellement -- ils "
                "dessinent un RÉSEAU CONTINU qui court le long de (quasi) toutes les lignes de la "
                "grille de poteaux, avec un ou quelques types GÉNÉRIQUES donnés dans la légende "
                "(ex: 'Longrine-Type 20x40', parfois 'Bèche 20x40', 'Béton banché 40x40' à côté). "
                "Si tu observes ce cas -- aucune désignation individuelle du style LG1/LG2 à "
                "chaque tronçon, mais un type générique en légende s'appliquant à tout le réseau -- "
                "remplis CE champ (une entrée par type générique de la légende) et laisse "
                "'longrines' vide. N'invente JAMAIS des désignations de tronçon (LG1, LG2...) qui "
                "n'existent pas sur le plan juste pour remplir 'longrines' -- utilise ce champ à la "
                "place. Liste vide si le plan désigne bien chaque tronçon individuellement (dans "
                "ce cas utilise 'longrines' normalement, pas celui-ci)."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type_designation": {
                        "type": "string",
                        "description": "Libellé du type générique tel qu'écrit dans la légende, ex: 'Longrine-Type', 'Bèche', 'Béton banché'.",
                    },
                    "section": {"type": "string", "description": "Section en cm telle qu'écrite, ex: '20x40'."},
                },
                "required": ["type_designation", "section"],
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
        "grille_axes": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "Repli v30 -- pour calculer automatiquement la longueur développée d'un RÉSEAU "
                "CONTINU de longrines (voir longrines_reseau_continu) sans compter chaque tronçon "
                "un par un: lis la chaîne de cotes INDIVIDUELLES entre axes consécutifs (pas la "
                "cote cumulée totale) sur CHAQUE bord du plan où une grille de repères d'axes est "
                "visible (les cercles numérotés/lettrés en bordure, ex: '1', '2', '3'... ou 'A', "
                "'B', 'C'...). C'est exactement la même donnée qu'un humain utiliserait pour "
                "calculer cette longueur à la main: additionner les cotes entre axes, multiplier "
                "par le nombre de lignes de la grille dans l'autre sens. Remplis ce champ chaque "
                "fois qu'une grille d'axes avec ses cotes intermédiaires est visible, même si "
                "poteaux_total_legende_global ou longrines_reseau_continu ne sont pas remplis sur "
                "CETTE page précise (l'agrégation se fait ensuite au niveau du dossier entier)."
            ),
            "properties": {
                "cotes_intermediaires_x_m": {
                    "type": "array", "items": {"type": "number"},
                    "description": (
                        "Liste ORDONNÉE des cotes individuelles entre axes consécutifs le long de "
                        "la direction X (horizontale), dans l'ordre où elles apparaissent sur le "
                        "plan (ex: [5.20, 3.80, 4.00, 5.20]). PAS la cote cumulée totale -- les "
                        "valeurs intermédiaires uniquement. Liste vide si aucune chaîne de cotes X "
                        "n'est visible sur cette page."
                    ),
                },
                "nombre_axes_y": {
                    "type": ["integer", "null"],
                    "description": (
                        "Nombre de lignes de repères d'axes dans la direction Y (verticale) visibles "
                        "sur le plan (compte les cercles de repère le long d'un bord vertical, ex: "
                        "'A', 'B', 'C'... jusqu'à la dernière lettre/numéro utilisé) -- c'est le "
                        "nombre de lignes de grille horizontales que compte le réseau. Null si non "
                        "déterminable sur cette page."
                    ),
                },
                "cotes_intermediaires_y_m": {
                    "type": "array", "items": {"type": "number"},
                    "description": "Même principe que cotes_intermediaires_x_m, mais pour la direction Y (verticale).",
                },
                "nombre_axes_x": {
                    "type": ["integer", "null"],
                    "description": "Même principe que nombre_axes_y, mais nombre de lignes de repères d'axes dans la direction X.",
                },
            },
            "required": ["cotes_intermediaires_x_m", "nombre_axes_y", "cotes_intermediaires_y_m", "nombre_axes_x"],
        },
        "poutres_instances": {
            "type": "array",
            "description": (
                "v42 -- une entrée par poutre individuellement désignée et cotée visible sur un "
                "plan de coffrage/poutraison (ex: 'PT1 25x40', avec sa portée). Cherche un tableau "
                "LEGENDE POUTRES si présent (retranscris chaque ligne), sinon les désignations "
                "directement inline sur le dessin avec leur section et leur portée (longueur entre "
                "appuis). N'invente jamais une portée non cotée -- laisse longueur_m à null dans ce "
                "cas plutôt que de deviner. Liste vide si aucune poutre individuellement cotée n'est "
                "visible sur cette page."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "designation": {"type": "string"},
                    "section": {"type": "string", "description": "Section en cm, ex: '25x40'."},
                    "longueur_m": {"type": ["number", "null"], "description": "Portée (longueur entre appuis) en m, si cotée."},
                },
                "required": ["designation", "section", "longueur_m"],
            },
        },
        "chainage_legende": {
            "type": "array",
            "description": (
                "v43 -- si un chaînage (poste 3.15, béton armé courant le long des murs porteurs à "
                "un niveau donné) est identifié en légende sur cette page (plan de coffrage), avec "
                "une section (ex: '20x20'), retranscris-le ici -- une entrée par type/section. Comme "
                "pour les longrines en réseau continu, un chaînage court généralement sur tout le "
                "périmètre + refends porteurs -- n'invente pas de longueur, laisse la longueur au "
                "mécanisme de confirmation utilisateur en aval. Liste vide si aucun chaînage "
                "identifiable en légende sur cette page."
            ),
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "designation": {"type": "string"},
                    "section": {"type": "string", "description": "Section en cm, ex: '20x20'."},
                },
                "required": ["designation", "section"],
            },
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
            },
            "required": ["surface_totale_m2", "contour_m"],
        },
    },
    "required": [
        "niveau", "semelles", "poteaux_legende", "raidisseurs_legende", "poteaux_instances", "poteaux_total_legende_global",
        "longrines", "longrines_reseau_continu", "voiles_instances", "poutres_instances", "chainage_legende", "dimensions_generales_m", "grille_axes", "escaliers", "surface_archi",
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

2bis) GRILLE D'AXES (repli pour calculer une longueur de réseau continu
   sans compter chaque tronçon un par un -- voir point 5b plus loin) :
   à l'INVERSE du point 2 ci-dessus qui ne veut que la cote EXTÉRIEURE
   totale, ici c'est l'inverse -- lis la chaîne de cotes INTERMÉDIAIRES
   (chaque segment entre deux repères d'axes consécutifs, PAS le total
   cumulé) le long de chaque bord où une grille de repères d'axes
   (cercles numérotés/lettrés) est visible. Reporte cette liste ordonnée
   dans grille_axes.cotes_intermediaires_x_m (bord horizontal) et
   cotes_intermediaires_y_m (bord vertical). Compte aussi le nombre de
   lignes de repères d'axes dans l'autre direction (nombre_axes_y =
   nombre de repères le long d'un bord vertical, nombre_axes_x = nombre
   de repères le long d'un bord horizontal) -- ce sont les mêmes repères
   que ceux utilisés pour repere_grille des poteaux/longrines. Remplis ce
   champ chaque fois que cette grille est visible, indépendamment de ce
   que tu remplis ailleurs sur cette page.

3) SEMELLES: si un tableau "LEGENDE SEMELLES" est visible, retranscris-le
   exactement dans semelles. Sinon liste vide. N'invente aucune valeur.

4) POTEAUX:
   a) Si un tableau "LEGENDE POTEAUX" (dimensions par désignation) est
      visible, retranscris-le dans poteaux_legende. Si ce tableau a une
      colonne "Quantité" (nombre d'exemplaires par type déjà chiffré,
      ex: "P1 | 25x25 | 57"), reporte cette quantité dans le champ
      "quantite" de chaque ligne -- c'est la source la PLUS FIABLE pour
      le nombre de poteaux par type, à privilégier sur un comptage visuel
      un par un. Si la légende combine poteaux ET raidisseurs dans le
      même tableau (reconnaissable à une ligne récapitulative "Total
      Poteaux : N" suivie plus bas d'une autre ligne "Total Raidisseur :
      M"), les lignes situées après "Total Poteaux" (souvent préfixées
      R0/R1/R2/R3, ou "Plot") vont dans raidisseurs_legende, PAS dans
      poteaux_legende -- ne les mélange jamais.
   b) NE CONFONDS JAMAIS deux symboles très différents qui coexistent
      souvent sur ces plans:
      - Les CERCLES numérotés/lettrés en bordure du plan (souvent en
        rouge, alignés sur le pourtour, avec un numéro ou une lettre
        comme "10", "12", "A", "B") sont des REPÈRES D'AXES DE GRILLE
        (convention de calepinage) -- ce ne sont PAS des poteaux, ne les
        compte jamais comme tels.
      - Les poteaux réels sont de petits symboles PLEINS (carré ou point
        noir/plein, parfois hachuré) situés à L'INTÉRIEUR du bâtiment --
        aux intersections des lignes de grille, aux angles de murs, au
        milieu des voiles. Ils sont souvent petits et peuvent se
        confondre visuellement avec la trame de grille dense qui les
        entoure -- regarde attentivement chaque intersection intérieure
        avant de conclure qu'aucun poteau n'y est dessiné.
   c) Compte et liste CHAQUE poteau (au sens du point b ci-dessus)
      individuellement visible sur le dessin dans poteaux_instances (une
      entrée par point/symbole dessiné avec son repère de grille), même
      si plusieurs partagent la même désignation. Si la section est
      écrite en clair à côté du poteau sur CE dessin (ex: "25x60"),
      reporte-la dans "section" -- sinon laisse section à null (elle
      sera résolue via poteaux_legende si besoin). Ce comptage individuel
      reste utile même quand la légende donne déjà une quantité par type
      (recoupement/audit) -- mais s'il est trop peu fiable sur une trame
      dense, la quantité de légende (point a) prime de toute façon.
      MÉTHODE pour une grille dense (beaucoup de poteaux rapprochés) : ne
      cherche PAS à voir tout le dessin d'un seul coup d'œil -- balaie-le
      MÉTHODIQUEMENT, zone par zone ou ligne d'axe par ligne d'axe (ex:
      toute la bande le long de l'axe "1", puis "2", puis "3"...),
      intersection par intersection, et note chaque poteau rencontré
      avant de passer à la bande suivante. C'est ce balayage systématique
      -- pas un survol global -- qui permet de ne pas en manquer sur une
      trame chargée.
   d) TOTAL GLOBAL (grilles très denses, SEULEMENT si la légende n'a PAS
      de colonne Quantité exploitable par type -- voir point a en
      priorité) : si un total est écrit en toutes lettres près de la
      priorité) : si un total est écrit en toutes lettres près de la
      légende (ex: "Total Poteaux : 121") sans répartition par type
      associée, reporte ce nombre dans poteaux_total_legende_global. Ne
      laisse jamais ce cas se traduire par des listes vides silencieuses.

5) LONGRINES:
   a) Cas normal -- tronçons individuellement désignés (LG1, LG2...) :
      liste CHAQUE tronçon individuellement dans longrines (jamais un
      total groupé). Deux pièges à éviter:
      - Les libellés utilisent souvent un suffixe numéroté serré (ex:
        LG8.1, LG8.2, LG8.3... jusqu'à LG8.11) -- vérifie bien CHAQUE
        suffixe individuellement, ne t'arrête pas au premier chiffre
        visible.
      - NE CONFONDS JAMAIS un libellé de longrine (désignation avec des
        lettres) avec une COTE DE DISTANCE entre axes (nombres seuls comme
        "5.20", "3.80" écrits le long des lignes de grille). Une
        désignation de longrine contient toujours des lettres, jamais un
        nombre seul.
      Pour longueur_m, lis la cote déjà annotée sur le plan entre les deux
      repères -- ne calcule ni n'invente une longueur non explicitement
      écrite. Si aucune cote fiable n'est visible pour un tronçon, mets
      longueur_m à null plutôt que de deviner.
   b) Cas RÉSEAU CONTINU (fréquent sur les gros projets à trame dense) :
      si les longrines ne sont PAS désignées tronçon par tronçon mais
      forment un réseau continu qui suit (quasi) toutes les lignes de la
      grille de poteaux, avec un ou quelques types génériques donnés en
      légende (ex: "Longrine-Type 20x40", éventuellement avec "Bèche
      20x40" et "Béton banché 40x40" séparés à côté) -- NE FORCE JAMAIS
      des désignations de tronçon inventées pour remplir "longrines".
      Utilise plutôt longrines_reseau_continu (une entrée par type
      générique de légende) et laisse "longrines" vide. Reconnaître ce
      cas : aucune étiquette individuelle du style "LG3" collée à un
      tronçon précis, mais un simple type + section en légende
      s'appliquant à l'ensemble du maillage rouge/fin visible sur tout
      le plan. IMPORTANT: dans ce cas, remplis SYSTÉMATIQUEMENT aussi
      grille_axes (point 2bis) sur cette même page si la grille de
      repères d'axes avec ses cotes intermédiaires y est visible -- c'est
      ce qui permet de calculer automatiquement la longueur développée
      totale du réseau (somme des cotes × nombre de lignes dans l'autre
      sens) sans avoir à compter chaque tronçon un par un.

6) VOILES: compte et liste CHAQUE côté de voile visible dans
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

6bis) POUTRES (v42, plans de coffrage/poutraison uniquement -- ex: "POUTRES
   BLOC B", "POUTRES PH-RDC") : si un tableau "LEGENDE POUTRES" est
   visible, retranscris-le dans poutres_instances. Sinon, si les poutres
   sont désignées individuellement sur le dessin (ex: "PT1 25x40" écrit
   le long d'une ligne de poutre), liste chaque poutre avec sa section.
   Pour longueur_m (portée) : lis la cote déjà annotée entre les deux
   appuis (poteaux/voiles) si elle existe -- comme pour les voiles, tu
   peux utiliser les cotes de grille qui bornent la poutre si aucune cote
   directe n'est écrite dessus. Null si vraiment aucune cote fiable.
   N'invente jamais une portée.

6ter) CHAÎNAGE (v43, plans de coffrage uniquement) : si un chaînage est
   identifié en légende avec sa section (ex: "Chaînage 20x20"), retranscris-
   le dans chainage_legende (type + section, comme pour un réseau continu
   de longrines). Ne compte JAMAIS une longueur ici -- ce champ ne sert
   qu'à signaler le type/section trouvé, la longueur sera confirmée en aval.

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

N'invente jamais une valeur non présente sur l'image."""


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
    "archi": [
        "PLAN ARCHITECTURE", "PLAN DE MASSE", "PLAN DE NIVEAU", "PLAN ARCHI",
        "PLAN AMENAGEMENT", "PLAN AMENAGE", "AMENAGEMENT DU REZ",
        "REZ DE CHAUSSEE", "REZ-DE-CHAUSSEE",
    ],
    # v41 -- réactivé: infrastructure validée (Phase 1 superstructure --
    # poteaux/voiles par étage, postes 3.11/3.13). Les plans de coffrage
    # donnent poteaux + niveau -- déjà agrégés par pipeline.py
    # (poteaux_coffrage_par_section) mais jamais utilisés tant que cette
    # catégorie restait désactivée.
    # v42 -- élargi: "POUTRES [ZONE]" (ex: "POUTRES BLOC B", "POUTRES PH-
    # SALLE DE REUNION", "POUTRES PH-RDC") est un intitulé de plan de
    # coffrage tout aussi courant que "PLAN DE COFFRAGE" -- confirmé en
    # trouvant plusieurs pages de ce type dans un vrai document (poste
    # 3.14, jusqu'ici invisible faute de matcher ce filtre). "POUTRES "
    # (pluriel, espace final) plutôt que "POUTRE" seul: un plan archi
    # mentionne parfois "Poutre ou chaînage..." en passant (singulier),
    # ce qui ne doit PAS le faire classer comme coffrage.
    "coffrage": ["PLAN DE COFFRAGE", "POUTRES ", "COFFRAGE POUTRES", "PLAN DE POUTRAISON"],
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
    "NOTE DE CALCUL", "SOMMAIRE", "GEOTECHNIQUE", "DEVIS",
    "PAGE DE GARDE", "GENERALITES", "MEMOIRE", "RAPPORT",
]


def classify_title(raw_title: str) -> str | None:
    """Renvoie une catégorie normalisée ou None si la page doit être
    ignorée (par défaut: tout ce qui n'est pas un plan d'exécution listé
    dans TITLE_KEYWORDS)."""
    upper = raw_title.upper()

    if any(kw in upper for kw in EXCLUDE_KEYWORDS):
        return None

    for category, keywords in TITLE_KEYWORDS.items():
        if any(kw in upper for kw in keywords):
            return category
    return None
