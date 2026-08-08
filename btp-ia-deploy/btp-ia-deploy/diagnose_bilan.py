"""
Script de diagnostic — a lancer sur TON bilan.json reel (celui produit par
pipeline.py pour ton vrai projet), pas sur des donnees de test.

Usage (PowerShell, depuis le dossier du projet, a cote de bilan.json) :
    python diagnose_bilan.py bilan.json answers.json

Si tu n'as pas de answers.json separe, le script prend des valeurs par
defaut plausibles pour pouvoir quand meme calculer les fouilles.

Ce script affiche EXACTEMENT ce que generate_excel() va voir et calculer,
sans passer par Excel -- si les nombres sont a 0 ou vides ICI, le probleme
est dans l'extraction PDF / le bilan, pas dans le fichier Excel genere.
"""
import json
import sys
import re

_SECTION_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[xX\u00d7*/-]\s*(\d+(?:[.,]\d+)?)")


def section_width_m(section):
    if not section:
        return None
    m = _SECTION_RE.search(str(section).strip())
    if not m:
        return None
    a = float(m.group(1).replace(",", "."))
    b = float(m.group(2).replace(",", "."))
    return min(a, b) / 100


def main():
    if len(sys.argv) < 2:
        print("Usage: python diagnose_bilan.py bilan.json [answers.json]")
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        bilan = json.load(f)

    answers = {}
    if len(sys.argv) > 2:
        with open(sys.argv[2], encoding="utf-8") as f:
            answers = json.load(f)

    profondeur = answers.get("profondeur_ancrage_m", 1.2)
    marge_pct = answers.get("marge_fouille_pct", 25)

    print("=" * 70)
    print("1) SEMELLES ISOLEES")
    print("=" * 70)
    semelles = bilan.get("semelles", [])
    print(f"Nombre d'entrees dans bilan['semelles'] : {len(semelles)}")
    for s in semelles:
        print(f"  {s}")
    if not semelles:
        print("  >>> VIDE. Le PDF n'a pas fourni de semelles isolees, ou la")
        print("      cle 'semelles' est absente/mal orthographiee dans le JSON.")

    print()
    print("=" * 70)
    print("2) LONGRINES PAR SECTION (utilisees pour semelles filantes + fouilles rigoles)")
    print("=" * 70)
    longrines = bilan.get("longrines_par_section", [])
    print(f"Nombre d'entrees dans bilan['longrines_par_section'] : {len(longrines)}")
    total_largeur_x_longueur = 0.0
    for item in longrines:
        section = item.get("section")
        longueur = item.get("longueur_totale_m")
        w = section_width_m(section)
        print(f"  section={section!r}  longueur_totale_m={longueur!r}  "
              f"-> largeur_extraite_m={w!r}")
        if w is None:
            print(f"    >>> PROBLEME : impossible d'extraire une largeur numerique "
                  f"depuis la section {section!r}. Verifie le format retourne par le LLM.")
        elif longueur is None:
            print(f"    >>> PROBLEME : 'longueur_totale_m' manquant pour cette section.")
        else:
            total_largeur_x_longueur += w * longueur

    if not longrines:
        print("  >>> VIDE. bilan['longrines_par_section'] est vide -> semelles filantes")
        print("      ET fouilles rigoles resteront a 0 dans l'Excel, car les deux se basent")
        print("      dessus (colonne F 'Largeur x Longueur' de la table Longrines).")

    print()
    print(f"Emprise totale rigoles (largeur x longueur, m2) = {round(total_largeur_x_longueur, 3)}")

    print()
    print("=" * 70)
    print("3) CALCUL DES FOUILLES (ce que generate_excel va ecrire comme formules)")
    print("=" * 70)
    print(f"profondeur_ancrage_m (answers) = {profondeur}")
    print(f"marge_fouille_pct (answers)    = {marge_pct}")
    marge = 1 + (marge_pct or 0) / 100

    surface_semelles = sum(s.get("a_m", 0) * s.get("b_m", 0) * s.get("nombre", 0) for s in semelles)
    vol_fouilles_puits = surface_semelles * profondeur * marge
    print(f"2.3 Fouilles puits (semelles isolees) : emprise={round(surface_semelles,3)} m2 "
          f"-> volume={round(vol_fouilles_puits,3)} m3")

    vol_fouilles_rigoles = total_largeur_x_longueur * profondeur * marge
    print(f"2.4 Fouilles rigoles (longrines)      : emprise={round(total_largeur_x_longueur,3)} m2 "
          f"-> volume={round(vol_fouilles_rigoles,3)} m3")

    if vol_fouilles_rigoles == 0:
        print()
        print(">>> Les fouilles rigoles seront a 0 dans l'Excel tant que")
        print("    bilan['longrines_par_section'] est vide ou que les sections ne")
        print("    contiennent pas un format 'NNxNN' reconnaissable (ex: '20x30').")
        print("    Ce n'est PAS un bug du fichier Excel : c'est une donnee manquante")
        print("    en amont, dans le JSON extrait du PDF.")

    print()
    print("=" * 70)
    print("4) PARAMETRES REELLEMENT UTILISES (answers.json)")
    print("=" * 70)
    for k in ["profondeur_ancrage_m", "marge_fouille_pct", "hauteur_soubassement_m",
              "epaisseur_beton_proprete_cm", "epaisseur_dallage_cm", "epaisseur_voile_cm",
              "largeur_semelle_filante_cm", "hauteur_semelle_filante_cm"]:
        print(f"  {k}: {answers.get(k, '(absent -> valeur par defaut utilisee dans le xlsx)')}")


if __name__ == "__main__":
    main()
