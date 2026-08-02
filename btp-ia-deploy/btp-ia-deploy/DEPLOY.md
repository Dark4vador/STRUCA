# Déploiement BTP-IA : Render (backend) + Netlify (frontend)

Deux dossiers dans ce zip :
- `backend/` → à déployer sur **Render** (FastAPI)
- `frontend/` → à déployer sur **Netlify** (le fichier `index.html` + `config.js`)

## 1. Backend sur Render

1. Pousse le contenu de `backend/` dans un repo GitHub (ex: `btp-pdf-gui2`).
2. Sur [render.com](https://render.com) → **New +** → **Web Service** → connecte le repo.
3. Render détecte `render.yaml` automatiquement (sinon règle à la main) :
   - Build command : `pip install -r requirements.txt`
   - Start command : `uvicorn server:app --host 0.0.0.0 --port $PORT`
4. Dans **Environment**, ajoute tes clés (elles ne sont jamais dans le repo) :
   - `GEMINI_API_KEY`
   - `GROQ_API_KEY`
   - `ALLOWED_ORIGINS` → l'URL de ton site Netlify une fois créé (ex: `https://btp-ia.netlify.app`), sans slash final. Tu peux mettre plusieurs origines séparées par des virgules.
5. Déploie. Note l'URL Render générée, du type `https://btp-pdf-gui2.onrender.com`.

⚠️ Plan gratuit Render : le service s'endort après 15 min d'inactivité (premier appel après veille = 30-60s de latence) et le disque est éphémère (les fichiers dans `uploads/`/`outputs/` disparaissent au redéploiement — normal ici puisque chaque job régénère ses fichiers).

## 2. Frontend sur Netlify

1. Ouvre `frontend/config.js` et remplace l'URL par celle de ton backend Render :
   ```js
   window.API_BASE = "https://btp-pdf-gui2.onrender.com";
   ```
2. Pousse le dossier `frontend/` dans un repo GitHub (séparé, ou un sous-dossier du même repo).
3. Sur [netlify.com](https://app.netlify.com) → **Add new site** → **Import an existing project** → connecte le repo.
   - Si `frontend/` est un sous-dossier du même repo backend : mets **Base directory** = `frontend`.
   - Publish directory : `.` (déjà réglé dans `netlify.toml`).
   - Pas de build command nécessaire (site statique).
4. Déploie. Netlify te donne une URL du type `https://btp-ia.netlify.app`.
5. Retourne sur Render et mets à jour `ALLOWED_ORIGINS` avec cette URL exacte, puis redéploie le backend (sinon erreurs CORS dans la console navigateur).

## 3. Vérification

- Ouvre le site Netlify, lance une génération de devis.
- Si tu vois une erreur CORS dans la console (F12) : vérifie que `ALLOWED_ORIGINS` sur Render correspond **exactement** à l'URL Netlify (https, pas de slash final).
- Si "Erreur de connexion au serveur local" apparaît : vérifie que `config.js` pointe bien vers ton URL Render et que le service Render est bien "Live" (pas endormi/en échec de build).
