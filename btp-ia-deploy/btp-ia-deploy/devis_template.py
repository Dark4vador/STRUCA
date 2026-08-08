"""
Structure officielle du devis quantitatif, reprise telle quelle du canevas
fourni (Canevas_CADRE_DU_DEVIS_QUANTITATIF.xlsx). Sert de référence commune
pour la génération Excel et PDF, afin que la sortie ressemble exactement au
document officiel attendu par le client.

Le pipeline calcule automatiquement la section III en entier -- Infrastructures
(postes 3.1 à 3.10, depuis fondation/longrine/voiles soubassement) ET
Superstructures (postes 3.11 à 3.22, depuis la note de calcul + le plan
archi/coffrage -- voir build_volumes_beton dans pipeline.py) -- plus une
partie de la section II (terrassements liés aux fouilles). Le poste 3.18
(escaliers superstructure) reste volontairement toujours "à compléter" pour
éviter un double comptage avec 3.9/3.10 (voir pipeline.py). Les prix
unitaires 3.11 à 3.22 ne sont pas encore renseignés dans knowledge_base.json
-- les quantités s'affichent, le prix/montant restent vides jusqu'à leur
ajout. Tout le reste (généralités, maçonnerie, menuiseries, lots
techniques...) est hors périmètre de l'extraction plans -> on garde les
intitulés officiels mais on laisse quantité/prix vides, avec une note
explicite, plutôt que d'inventer des chiffres.
"""

SECTION_I_GENERALITES = {
    "numero": "I", "titre": "GENERALITES", "hors_perimetre": True,
    "note": "Postes d'installation de chantier / administratifs -- dépendent du contrat, non extractibles des plans. À compléter manuellement.",
    "lignes": [],
}

SECTION_II_TERRASSEMENT = {
    "numero": "II", "titre": "PREPARATIONS - DEMOLITIONS - TERRASSEMENTS",
    "hors_perimetre": False,
    "lignes_hors_perimetre": [
        {"code": "2.1", "designation": "Démolition, décapage, nettoyage et décharge publique à 3m minimum de l'emprise du bâtiment et de ses ouvrages annexes", "unite": "Ens"},
        {"code": "2.2", "designation": "Implantation générale de l'ensemble des ouvrages par un géomètre agréé", "unite": "Ens"},
    ],
    "codes_calcules": ["2.3", "2.4"],
    "codes_hors_perimetre_calcul": ["2.5", "2.6", "2.7", "2.8"],
}

SECTION_III_BETON = {
    "numero": "III", "titre": "BETON - BETON ARME", "hors_perimetre": False,
    "sous_sections": [
        {"titre": "Infrastructures", "codes": ["3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8", "3.9", "3.10"]},
        {"titre": "Superstructures", "hors_perimetre": False,
         "note": "Quantités calculées depuis la note de calcul et le plan archi/coffrage. Prix unitaires "
                 "3.11 à 3.22 pas encore renseignés dans la base de prix -- à compléter (voir knowledge_base.json). "
                 "Le poste 3.18 (escaliers) reste volontairement à compléter manuellement, pour éviter un "
                 "double comptage avec 3.9/3.10.",
         "codes": ["3.11", "3.12", "3.13", "3.14", "3.15", "3.16", "3.17", "3.18", "3.19", "3.20", "3.21", "3.22"]},
    ],
}

SECTIONS_HORS_PERIMETRE = [
    {"numero": "IV", "titre": "MACONNERIE"},
    {"numero": "V", "titre": "PLAQUETTES SIGNALETIQUES"},
    {"numero": "VI", "titre": "MENUISERIES SPECIFIQUES - ALUMINIUM - METALLIQUE ET BOIS"},
    {"numero": "VII", "titre": "PLOMBERIE"},
    {"numero": "VIII", "titre": "FAUX PLAFOND"},
    {"numero": "IX", "titre": "REVETEMENT - CARRELAGE - REVETEMENTS MURAUX FACADES"},
    {"numero": "X", "titre": "PEINTURE - REVETEMENTS MURAUX FACADES"},
    {"numero": "XI", "titre": "ELECTRICITE COURANT FORT - COURANT FAIBLE - CLIMATISATION - VENTILATION - ENERGIE SOLAIRE"},
]

# poste_key (tel que produit par pipeline.build_volumes_beton) -> code officiel du devis
POSTE_KEY_TO_CODE = {
    "beton_proprete_semelles": "3.1",
    "beton_banche_fondation_filante": "3.2",
    "semelles_isolees_beton_arme": "3.3",
    "radier_semelles_filantes": "3.4",
    "potelets": "3.5",
    "voiles_soubassement": "3.6",
    "longrines": "3.7",
    "dallage": "3.8",
    "marches_arrets_dallage_rampe": "3.9",
    "beche_escalier": "3.10",
    "fouilles_puits_semelles": "2.3",
    "fouilles_rigoles_fondations": "2.4",
    # Superstructure (postes 3.11 à 3.22) -- voir SUPERSTRUCTURE_TYPES et le
    # bloc surfaces_superstructure dans pipeline.build_volumes_beton.
    "poteaux_superstructure": "3.11",
    "raidisseurs_superstructure": "3.12",
    "voiles_superstructure": "3.13",
    "poutres_superstructure": "3.14",
    "chainages_superstructure": "3.15",
    "appuis_baies_superstructure": "3.16",
    "dalle_pleine_superstructure": "3.17",
    "escaliers_superstructure": "3.18",
    "elements_decoratifs_superstructure": "3.19",
    "rampes_acces_superstructure": "3.20",
    "plancher_corps_creux": "3.21",
    "table_compression": "3.22",
}
