"""
Client Gemini natif (Google AI Studio), en remplacement d'OpenRouter pour
sortir du pool gratuit partagé et utiliser ton propre quota dédié.

Utilise response_schema + response_mime_type="application/json" -- le
mécanisme natif de Gemini pour forcer une sortie JSON strictement typée,
équivalent au json_schema strict qu'on avait côté OpenRouter.
"""

import os
import time
import json
import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Flash-Lite: meilleur RPM du tier gratuit, largement suffisant pour de
# l'extraction structurée (pas besoin du raisonnement de Pro ici).
VISION_MODEL = os.environ.get("GEMINI_VISION_MODEL", "gemini-3.1-flash-lite")
REASONING_MODEL = os.environ.get("GEMINI_REASONING_MODEL", "gemini-3.1-flash-lite")

MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 5  # 15 RPM -> ~4s entre requêtes, backoff généreux sur 429


class GeminiError(Exception):
    pass


def _endpoint(model: str) -> str:
    if not GEMINI_API_KEY:
        raise GeminiError(
            "GEMINI_API_KEY absente. Crée un fichier .env avec ta clé "
            "(récupérable gratuitement sur https://aistudio.google.com/apikey)."
        )
    return f"{GEMINI_BASE_URL}/{model}:generateContent?key={GEMINI_API_KEY}"


def _post_with_retry(model: str, payload: dict) -> dict:
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(_endpoint(model), json=payload, timeout=120)
        except requests.RequestException as e:
            last_err = e
            time.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))
            continue

        if r.status_code == 429 or r.status_code >= 500:
            last_err = GeminiError(f"{r.status_code} - {r.text[:500]}")
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else BASE_BACKOFF_SECONDS * (2 ** attempt)
            time.sleep(wait)
            continue

        if not r.ok:
            raise GeminiError(f"{r.status_code} - {r.text[:500]}")

        return r.json()

    raise GeminiError(f"Échec après {MAX_RETRIES} tentatives: {last_err}")


def _to_gemini_schema(json_schema: dict) -> dict:
    """Convertit un JSON Schema standard (utilisé pour OpenRouter) vers le
    format que Gemini attend pour response_schema. Les deux sont proches
    (OpenAPI 3.0 subset), avec deux différences à gérer:
    - Gemini n'accepte pas "additionalProperties" -> on le retire.
    - Gemini n'accepte pas "type": ["number", "null"] (union JSON Schema) ->
      on convertit en "type": "number", "nullable": true.
    """
    if isinstance(json_schema, dict):
        cleaned = {k: v for k, v in json_schema.items() if k != "additionalProperties"}

        if isinstance(cleaned.get("type"), list):
            types = [t for t in cleaned["type"] if t != "null"]
            if "null" in cleaned["type"]:
                cleaned["nullable"] = True
            cleaned["type"] = types[0] if types else "string"

        for key in ("properties",):
            if key in cleaned:
                cleaned[key] = {k: _to_gemini_schema(v) for k, v in cleaned[key].items()}
        if "items" in cleaned:
            cleaned["items"] = _to_gemini_schema(cleaned["items"])
        return cleaned
    return json_schema


def _extract_json_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiError(f"Aucune réponse renvoyée par Gemini (probablement bloquée): {data}")

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")
    parts = candidate.get("content", {}).get("parts", [])

    if not parts:
        raise GeminiError(f"Réponse vide de Gemini, finishReason={finish_reason}: {data}")

    text = parts[0].get("text", "")

    if finish_reason == "MAX_TOKENS":
        raise GeminiError(
            "Réponse tronquée (MAX_TOKENS): le JSON est incomplet. "
            "Augmente maxOutputTokens ou réduis le volume de données envoyées en une fois."
        )
    if finish_reason not in ("STOP", None):
        raise GeminiError(f"Réponse interrompue (finishReason={finish_reason}): {data}")

    return text


def call_vision_json(image_bytes: bytes, prompt: str, json_schema: dict, mime="image/png") -> dict:
    import base64
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode("utf-8")}},
            ]
        }],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 6000,
            "responseMimeType": "application/json",
            "responseSchema": _to_gemini_schema(json_schema),
        },
    }
    data = _post_with_retry(VISION_MODEL, payload)
    text = _extract_json_text(data)
    return json.loads(text)


def call_reasoning_json(prompt: str, json_schema: dict) -> dict:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 8000,
            "responseMimeType": "application/json",
            "responseSchema": _to_gemini_schema(json_schema),
        },
    }
    data = _post_with_retry(REASONING_MODEL, payload)
    text = _extract_json_text(data)
    return json.loads(text)
