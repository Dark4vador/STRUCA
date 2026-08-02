DIAGNOSTIC DU 14/07/2026 -- suite au rapport "l'excel est encore vide, rigoles vide"
======================================================================================

CE QUE J'AI VERIFIE (pas juste relu le code, je l'ai vraiment execute) :

1. J'ai genere un vrai .xlsx avec generate_excel() en utilisant des donnees
   synthetiques mais realistes (semelles + longrines_par_section avec une
   section "20x30"), avec le code EXACT de ce zip (v14).

2. J'ai inspecte les formules ecrites -> toutes les references cross-feuille
   sont correctement quotees et orthographiees :
     'Paramètres'!$D$25, 'Bilan Éléments'!$E$34, etc.
   Plus de decalage d'accent entre le nom reel de la feuille et le nom
   utilise dans les formules (c'etait le bug corrige en v13/v14).

3. La colonne "Emprise" des fouilles rigoles (B35) pointe bien vers
   'Bilan Éléments'!$F$29, qui est Largeur x Longueur (m2) -- PAS le volume
   (colonne E). C'est la bonne grandeur pour une emprise de fouille.

4. IMPORTANT -- j'ai pousse le test plus loin : j'ai fait RECALCULER le
   fichier par un vrai moteur de tableur (LibreOffice headless, pas juste
   openpyxl qui n'evalue jamais les formules). Resultat avec mes donnees
   de test :

     2.3 Fouilles puits    : quantite=3.84 m3   PU=5000    montant=19200
     2.4 Fouilles rigoles  : quantite=13.5 m3   PU=3000    montant=40500
     Sous-total section II : 59700
     TOTAL infrastructure  : 1254780
     Semelles filantes (Bilan Elements D11) : 5.4 m3

   Donc avec des donnees d'entree presentes, TOUT se calcule correctement :
   prix inseres, rigoles non-vides, semelles filantes non-vides.

CONCLUSION
----------
Le fichier Excel genere par ce zip (v14) est fonctionnellement correct.
Si chez toi le fichier est encore vide/rigoles vide, la cause la plus
probable n'est PLUS dans generate_outputs.py, mais dans les DONNEES en
amont, c'est a dire :

  a) bilan["longrines_par_section"] est vide ou mal forme (le PDF n'a pas
     ete extrait avec les longrines, ou le LLM a renvoye un format de
     section que la regex ne reconnait pas) -> semelles filantes ET
     fouilles rigoles restent a 0, car les DEUX se basent sur cette meme
     liste (c'est normal et voulu : "rigoles suit le meme chemin que
     longrines").

  b) Ton logiciel n'a pas recalcule le fichier a l'ouverture (rare, le
     fichier force fullCalcOnLoad=1, mais si Excel est en mode de calcul
     "Manuel" chez toi -> Formules > Options de calcul > Automatique, ou
     appuie sur Ctrl+Alt+F9 apres ouverture).

COMMENT VERIFIER CHEZ TOI (2 minutes)
--------------------------------------
Lance, dans le dossier de ton VRAI projet (a cote du bilan.json reellement
extrait de ton PDF) :

    python diagnose_bilan.py bilan.json answers.json

(si tu n'as pas de answers.json separe, lance juste "python diagnose_bilan.py bilan.json")

Ca va t'afficher, sans passer par Excel :
  - combien d'entrees il y a dans bilan["semelles"] et
    bilan["longrines_par_section"]
  - si les sections ("20x30" etc.) sont bien reconnues
  - le volume de fouilles calcule pour 2.3 et 2.4

Colle-moi la sortie de ce script (avec ton vrai bilan.json) et je te dis
exactement ou ca casse cote extraction/pipeline, plutot que de continuer a
chercher un bug qui n'est probablement plus dans generate_outputs.py.
