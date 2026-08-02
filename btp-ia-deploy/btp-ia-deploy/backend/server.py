import os
import uuid
import shutil
import threading
import json
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pipeline import run_pipeline
from missing_info import detect_missing_questions, resolve_answer, apply_answers_to_bilan
from devis_builder import build_devis
from groq_client import review_devis
from generate_outputs import generate_excel, generate_pdf

BASE_DIR = Path(__file__).parent
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUTS_DIR = BASE_DIR / "outputs"
UPLOADS_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

KNOWLEDGE_BASE = json.loads((BASE_DIR / "knowledge_base.json").read_text(encoding="utf-8"))

app = FastAPI()

# CORS : le frontend (Netlify) et le backend (Render) sont sur des domaines
# différents. ALLOWED_ORIGINS = liste séparée par des virgules, ex:
# "https://ton-site.netlify.app,http://localhost:5500"
# Si la variable n'est pas définie, on autorise tout (pratique pour tester,
# mais à restreindre une fois l'URL Netlify connue).
_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _origins_env:
    _allowed_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]
else:
    _allowed_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")

# job_id -> {
#   "phase": "extraction"|"needs_input"|"reasoning"|"generation"|"done"|"error",
#   "progress": int, "stage": str, "done": bool, "error": str|None, "files": [...],
#   "missing_info": [...], "bilan": {...}, "boq": {...}, "answers": {...},
#   "project_name": str, "location": str,
# }
JOBS = {}


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


def _set(job_id, **kwargs):
    JOBS[job_id].update(kwargs)


def _merge_pages_analysees(boq: dict) -> list:
    """Fusionne les listes 'par_page' de chaque agrégation (fondation,
    longrines, voiles, escaliers, éléments structurels) en une seule liste
    triée par page, pour l'onglet 'Pages analysées' de l'explorateur.
    Certaines agrégations (ex: escaliers) renvoient directement une liste
    d'éléments -- chaque élément y porte déjà sa propre clé 'page'."""
    pages = {}
    for section_key, section in (boq or {}).items():
        if isinstance(section, dict):
            items = section.get("par_page") or []
        elif isinstance(section, list):
            items = section
        else:
            items = []
        for p in items:
            if not isinstance(p, dict):
                continue
            page_num = p.get("page")
            if page_num is None:
                continue
            entry = pages.setdefault(page_num, {"page": page_num, "categories": [], "detail": []})
            cat = p.get("category") or section_key
            if cat not in entry["categories"]:
                entry["categories"].append(cat)
            detail = {k: v for k, v in p.items() if k not in ("page", "category")}
            if detail:
                entry["detail"].append({"source": section_key, **detail})
    return sorted(pages.values(), key=lambda e: e["page"])


def _finalize(job_id: str):
    """Devis déterministe + relecture Groq + génération Excel/PDF. Appelée
    une fois toutes les questions bloquantes résolues (ou s'il n'y en avait
    aucune)."""
    job = JOBS[job_id]
    try:
        _set(job_id, phase="reasoning", stage="Construction du devis chiffré...", progress=75)
        devis = build_devis(job["bilan"], KNOWLEDGE_BASE, job["project_name"], job["location"])

        _set(job_id, stage="Relecture IA (Groq)...", progress=85)
        devis["avertissements"] = review_devis(devis)

        _set(job_id, phase="generation", stage="Génération du fichier Excel...", progress=92)
        xlsx_path = OUTPUTS_DIR / f"{job_id}_devis.xlsx"
        generate_excel(job["bilan"], job["answers"], KNOWLEDGE_BASE, job["project_name"],
                        job["location"], devis["avertissements"], str(xlsx_path))

        _set(job_id, stage="Génération du PDF bilan...", progress=97)
        pdf_path = OUTPUTS_DIR / f"{job_id}_bilan.pdf"
        generate_pdf(devis, str(pdf_path))

        json_path = OUTPUTS_DIR / f"{job_id}_devis.json"
        json_path.write_text(json.dumps({
            "devis": devis,
            "bilan": job["bilan"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        _set(job_id, phase="done", progress=100, stage="Terminé.", done=True,
             files=[xlsx_path.name, pdf_path.name, json_path.name],
             devis=devis, pages_analysees=_merge_pages_analysees(job.get("boq")))
    except Exception as e:
        _set(job_id, phase="error", error=str(e), done=True)


def _run_job(job_id: str, pdf_path: str):
    def on_log(msg: str):
        JOBS[job_id]["stage"] = msg
        JOBS[job_id]["progress"] = min(60, JOBS[job_id]["progress"] + 2)

    try:
        _set(job_id, phase="extraction", stage="Analyse du document...", progress=2)
        result = run_pipeline(pdf_path, on_log=on_log)

        if not result.get("boq"):
            _set(job_id, phase="error", done=True,
                 error=result.get("avertissement", "Aucune donnée exploitable extraite."))
            return

        _set(job_id, bilan=result["bilan"], boq=result["boq"], progress=65,
             stage="Vérification des données manquantes...")

        questions = detect_missing_questions(result["bilan"])
        unanswered = [q for q in questions if q["key"] not in JOBS[job_id]["answers"]]
        if unanswered:
            _set(job_id, phase="needs_input", missing_info=unanswered,
                 stage="Informations manquantes -- complète les champs pour continuer.", progress=70)
            return

        _finalize(job_id)
    except Exception as e:
        _set(job_id, phase="error", error=str(e), done=True)


@app.post("/api/run")
async def api_run(files: list[UploadFile] = File(...),
                   project_name: str = Form("Projet BTP"),
                   location: str = Form("")):
    if not files:
        return JSONResponse({"error": "Aucun fichier reçu."}, status_code=400)

    job_id = str(uuid.uuid4())
    pdf_file = files[0]
    pdf_path = UPLOADS_DIR / f"{job_id}_{pdf_file.filename}"
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(pdf_file.file, f)

    JOBS[job_id] = {
        "phase": "extraction", "progress": 0, "stage": "En file d'attente...",
        "done": False, "error": None, "files": [], "missing_info": [],
        "bilan": None, "boq": None, "answers": {}, "devis": None, "pages_analysees": [],
        "project_name": project_name, "location": location,
    }
    threading.Thread(target=_run_job, args=(job_id, str(pdf_path)), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/complete/{job_id}")
async def api_complete(job_id: str, request: Request):
    """Reçoit les réponses de l'utilisateur (texte et/ou pièces jointes,
    multipart/form-data) aux questions d'info manquante et relance le
    pipeline. Champs attendus par question de clé K : 'text__K' (optionnel)
    et 'file__K' (optionnel, fichier)."""
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "Job introuvable."}, status_code=404)
    if job["phase"] != "needs_input":
        return JSONResponse({"error": "Ce job n'attend pas de complément d'info."}, status_code=400)

    form = await request.form()
    resolved = {}
    unresolved_keys = []

    for q in job["missing_info"]:
        key = q["key"]
        text_answer = form.get(f"text__{key}")
        upload = form.get(f"file__{key}")
        file_bytes, filename = None, None
        if upload is not None and hasattr(upload, "read"):
            file_bytes = await upload.read()
            filename = upload.filename

        value, source = resolve_answer(key, q["question"], text_answer, file_bytes, filename,
                                        kind=q.get("kind", "scalar"))
        if value is None:
            unresolved_keys.append(key)
        else:
            resolved[key] = value

    if unresolved_keys:
        return JSONResponse(
            {"error": f"Impossible de résoudre: {', '.join(unresolved_keys)}. "
                      f"Vérifie la valeur saisie ou la pièce jointe."},
            status_code=400,
        )

    job["answers"].update(resolved)
    job["bilan"] = apply_answers_to_bilan(job["bilan"], job["answers"])

    # Un tour de réponses peut faire apparaître de nouvelles questions de
    # suivi (ex: section/longueur du béton banché, qui ne s'affichent que
    # si l'utilisateur vient de répondre "oui" à la question précédente).
    next_questions = detect_missing_questions(job["bilan"], job["answers"])
    unanswered = [q for q in next_questions if q["key"] not in job["answers"]]
    if unanswered:
        job["phase"] = "needs_input"
        job["missing_info"] = unanswered
        job["stage"] = "Informations complémentaires -- complète les champs pour continuer."
        return {"ok": True, "needs_more_input": True}

    job["done"] = False
    job["missing_info"] = []

    threading.Thread(target=_finalize, args=(job_id,), daemon=True).start()
    return {"ok": True}


@app.get("/api/status/{job_id}")
def api_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "Job introuvable."}, status_code=404)
    return {k: v for k, v in job.items() if k not in ("bilan", "boq")}


@app.get("/api/explore/{job_id}")
def api_explore(job_id: str):
    """Tout ce dont l'explorateur en-app a besoin: devis chiffré ligne par
    ligne, éléments bruts extraits des plans, et pages analysées -- sans
    passer par le fichier Excel. Disponible une fois le job terminé."""
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "Job introuvable."}, status_code=404)
    if job["phase"] != "done" or not job.get("devis"):
        return JSONResponse({"error": "Le job n'est pas encore terminé."}, status_code=400)

    bilan = job.get("bilan") or {}
    return {
        "project_name": job["project_name"], "location": job["location"],
        "devis": job["devis"],
        "pages_analysees": job.get("pages_analysees") or [],
        "elements": {
            "semelles": bilan.get("semelles", []),
            "radiers": bilan.get("radiers", []),
            "poteaux": bilan.get("poteaux", {}),
            "voiles_par_type": bilan.get("voiles_par_type", []),
            "longrines_par_section": bilan.get("longrines_par_section", []),
            "escaliers": bilan.get("escaliers", []),
            "elements_structurels_par_type": bilan.get("elements_structurels_par_type", {}),
            "surfaces_superstructure": bilan.get("surfaces_superstructure", {}),
            "surface_dallage": bilan.get("surface_dallage", {}),
        },
        "answers": job.get("answers", {}),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
