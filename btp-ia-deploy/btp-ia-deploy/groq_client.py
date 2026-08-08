"""
Passe de raisonnement Groq. Les quantités et montants sont déjà calculés de
façon déterministe (devis_builder.py) -- Groq n'est PAS appelé pour faire
les calculs (peu fiable sur l'arithmétique, et inutile puisque Python le
fait déjà correctement). Son rôle ici se limite à une relecture experte :
repérer les incohérences visibles (ex: un ratio béton/emprise anormal),
rédiger des avertissements clairs pour le maître d'ouvrage, et suggérer
(sans jamais l'imposer) un ordre de grandeur de prix pour un poste où la
base de connaissances n'a pas de référence -- toujours étiqueté comme tel.
"""

import os
import time
import json
import requests
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_REASONING_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 4


class GroqError(Exception):
    pass


def _post_with_retry(payload: dict) -> dict:
    if not GROQ_API_KEY:
        raise GroqError(
            "GROQ_API_KEY absente. Crée un fichier .env avec ta clé "
            "(gratuite sur https://console.groq.com/keys)."
        )
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=90)
        except requests.RequestException as e:
            last_err = e
            time.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last_err = GroqError(f"{r.status_code} - {r.text[:500]}")
            retry_after = r.headers.get("retry-after")
            time.sleep(float(retry_after) if retry_after else BASE_BACKOFF_SECONDS * (2 ** attempt))
            continue
        if not r.ok:
            raise GroqError(f"{r.status_code} - {r.text[:500]}")
        return r.json()
    raise GroqError(f"Échec après {MAX_RETRIES} tentatives: {last_err}")


SYSTEM_PROMPT = """Tu es un métreur BTP expérimenté au Burkina Faso qui relit un devis quantitatif
d'infrastructure déjà chiffré par des calculs Python déterministes (fiables, ne les remets pas en
question). Ta seule tâche : rédiger une liste courte d'observations utiles pour le maître d'ouvrage
(avertissements) -- incohérences visibles entre postes (ex: volume de béton de propreté largement
supérieur au volume de semelles correspondant), postes marqués "indisponible" à ne pas oublier de
traiter, ou remarques générales de bon sens sur un devis d'infrastructure BTP. Ne propose JAMAIS de
nouvelle quantité ni de nouveau prix unitaire pour les postes déjà chiffrés.
Réponds UNIQUEMENT en JSON valide : {"avertissements": ["...", "..."]}. Pas de texte hors JSON."""


def review_devis(devis: dict) -> list:
    """Renvoie une liste d'avertissements/observations. En cas d'échec Groq
    (quota, réseau...), renvoie une liste vide plutôt que de bloquer la
    génération des livrables -- ce n'est qu'une relecture, pas une étape
    critique."""
    payload = {
        "model": GROQ_MODEL, "temperature": 0, "max_tokens": 1500,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Voici le devis à relire (JSON) :\n\n" + json.dumps(devis, ensure_ascii=False, indent=2)},
        ],
    }
    try:
        data = _post_with_retry(payload)
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return parsed.get("avertissements", [])
    except (GroqError, KeyError, IndexError, json.JSONDecodeError):
        return []
