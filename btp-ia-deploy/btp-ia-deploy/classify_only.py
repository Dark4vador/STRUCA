#!/usr/bin/env python3
"""
Dry-run gratuit : classe chaque page du PDF (fondation/longrine/coffrage/
ignorée) SANS jamais appeler l'API vision. Sert à vérifier que le bon jeu
de pages sera sélectionné avant de lancer le pipeline complet (donc avant
de dépenser le moindre token).

Usage:
    python classify_only.py mon_document.pdf
"""

import sys
import fitz  # PyMuPDF

from schemas import classify_title
from pipeline import extract_cartouche_title


def main(pdf_path: str):
    doc = fitz.open(pdf_path)
    total = len(doc)

    retained = []
    for i in range(total):
        page = doc[i]
        title = extract_cartouche_title(page)
        category = classify_title(title)
        status = category if category else "ignorée"
        print(f"Page {i + 1:>4}/{total}  [{status:<10}]  titre détecté: {title or '(aucun)'}")
        if category:
            retained.append((i + 1, category))

    doc.close()

    print()
    print(f"=== {len(retained)}/{total} pages retenues pour la vision ===")
    from collections import Counter
    counts = Counter(cat for _, cat in retained)
    for cat, n in counts.items():
        print(f"  - {cat}: {n} page(s)")

    if not retained:
        print("\nAucune page retenue. Vérifie extract_cartouche_title() / TITLE_KEYWORDS "
              "dans schemas.py -- le titre de cartouche n'est peut-être pas détecté "
              "comme attendu sur ce document.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python classify_only.py mon_document.pdf")
        sys.exit(1)
    main(sys.argv[1])
