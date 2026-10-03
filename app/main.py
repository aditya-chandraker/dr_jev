from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.bank import load_bank
from app.engine import suggest_questions
from app.models import PatientInput, SuggestResponse


load_dotenv()
logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).resolve().parent
STATIC_INDEX = ROOT / "static" / "index.html"

app = FastAPI(title="Patient Interview Helper MVP")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/bank")
def api_bank() -> dict[str, object]:
    bank = load_bank()
    return {
        "drafted_by": bank.drafted_by,
        "questions": [question.__dict__ for question in bank.questions],
        "red_flags": [red_flag.__dict__ for red_flag in bank.red_flags],
        "fallback_checklist": bank.fallback_checklist,
    }


@app.post("/api/suggest", response_model=SuggestResponse)
async def api_suggest(payload: PatientInput) -> SuggestResponse:
    response = await suggest_questions(payload)
    logging.info("Suggestion latency_ms=%s degraded=%s low_confidence=%s", response.latency_ms, response.degraded, response.low_confidence)
    return response


@app.get("/")
def index() -> FileResponse:
    if not STATIC_INDEX.exists():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(STATIC_INDEX)

