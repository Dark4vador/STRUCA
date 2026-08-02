"""
Construit le JSON du devis final: prend les volumes calculés dans
bilan['volumes_beton']['postes'] (100% Python, déterministe) et les
transforme en lignes de devis chiffrées avec les prix unitaires de la base
de connaissances. Aucun calcul de quantité n'est délégué au LLM -- Groq
n'intervient qu'en aval pour une relecture/synthèse (voir groq_client.py).
"""

from devis_template import (
    SECTION_I_GENERALITES, SECTION_II_TERRASSEMENT, SECTION_III_BETON,
    SECTIONS_HORS_PERIMETRE, POSTE_KEY_TO_CODE,
)


def _ligne_from_poste(code: str, poste: dict, kb_postes: dict) -> dict:
    kb = kb_postes.get(code, {})
    designation = kb.get("designation", poste.get("designation_devis", code))
    unite = kb.get("unite", poste.get("unite", "m3"))
    pu = kb.get("prix_unitaire_fcfa")

    if poste.get("donnee_indisponible") or poste.get("volume_m3") is None:
        return {
            "code": code, "designation": designation, "unite": unite,
            "quantite": None, "prix_unitaire_fcfa": pu, "montant_fcfa": None,
            "source": "indisponible",
            "note": poste.get("raison", "Donnée indisponible."),
        }

    qte = poste["volume_m3"]
    montant = round(qte * pu) if pu is not None else None
    source = poste.get("source_override") or "regle_locale"
    if poste.get("valeur_par_defaut_utilisee") and "source_override" not in poste:
        source = "hypothese_par_defaut"
    note = poste.get("raison")

    return {
        "code": code, "designation": designation, "unite": unite,
        "quantite": qte, "prix_unitaire_fcfa": pu, "montant_fcfa": montant,
        "source": source, "note": note,
    }


def build_devis(bilan: dict, knowledge_base: dict, project_name: str, location: str) -> dict:
    kb_postes = knowledge_base.get("postes", {})
    postes = bilan["volumes_beton"]["postes"]

    sections_out = []

    # ---- Section II (terrassements) : uniquement les codes calculés ----
    lignes_ii = []
    for poste_key, code in POSTE_KEY_TO_CODE.items():
        if code not in ("2.3", "2.4"):
            continue
        lignes_ii.append(_ligne_from_poste(code, postes[poste_key], kb_postes))
    sections_out.append({
        "numero": "II", "titre": SECTION_II_TERRASSEMENT["titre"],
        "lignes": lignes_ii,
        "note": "Seuls les postes 2.3/2.4 (fouilles) sont calculés depuis les plans structure. "
                "Les postes 2.1/2.2/2.5 à 2.8 dépendent de données hors périmètre (installation "
                "de chantier, remblais détaillés) -- à compléter manuellement.",
    })

    # ---- Section III (béton, infrastructure) ----
    lignes_iii = []
    for sous_section in SECTION_III_BETON["sous_sections"]:
        if sous_section.get("hors_perimetre"):
            continue
        for code in sous_section["codes"]:
            poste_key = next((k for k, c in POSTE_KEY_TO_CODE.items() if c == code), None)
            if poste_key is None:
                # Poste officiel du devis sans équivalent calculé par ce pipeline
                # (ex: 3.2 béton banché/cyclopéen fondation filante -- notre
                # extraction ne distingue pas ce type d'élément).
                kb = kb_postes.get(code, {})
                lignes_iii.append({
                    "code": code, "designation": kb.get("designation", code),
                    "unite": kb.get("unite", "m3"), "quantite": None,
                    "prix_unitaire_fcfa": kb.get("prix_unitaire_fcfa"), "montant_fcfa": None,
                    "source": "indisponible",
                    "note": "Non distingué par le schéma d'extraction actuel -- à compléter manuellement.",
                })
                continue
            lignes_iii.append(_ligne_from_poste(code, postes[poste_key], kb_postes))
    sections_out.append({
        "numero": "III", "titre": SECTION_III_BETON["titre"] + " (Infrastructures)",
        "lignes": lignes_iii,
        "note": "Superstructure (poteaux/poutres/dalles en élévation, postes 3.11 à 3.22) hors "
                "périmètre -- nécessite les plans de superstructure, non traités par ce pipeline.",
    })

    total_general = sum(l["montant_fcfa"] for s in sections_out for l in s["lignes"] if l["montant_fcfa"])

    a_confirmer = bilan["volumes_beton"].get("a_confirmer_ou_completer_en_aval", [])

    return {
        "projet": project_name,
        "localisation": location,
        "sections": sections_out,
        "sections_hors_perimetre": [s["titre"] for s in SECTIONS_HORS_PERIMETRE]
        + ["I. " + SECTION_I_GENERALITES["titre"]],
        "total_infrastructure_fcfa": total_general,
        "postes_a_completer_manuellement": a_confirmer,
        "avertissements": [],
    }
