from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from typesafe_sdk import AsyncTypeSafeClient

from app.bank import Bank, BankRegistry, load_registry
from app.engine import suggest_questions
from app.models import PatientInput, SuggestResponse


load_dotenv()
logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).resolve().parent
STATIC_INDEX = ROOT / "static" / "index.html"


def _question_payload(question) -> dict[str, object]:
    return {
        "id": question.id,
        "qid": question.qid,
        "bank_id": question.bank_id,
        "text": question.text,
        "area": question.area,
        "rationale": question.rationale,
        "tags": question.tags,
        "status": question.status,
    }


def _red_flag_payload(red_flag) -> dict[str, object]:
    return {
        "id": red_flag.id,
        "qid": red_flag.qid,
        "bank_id": red_flag.bank_id,
        "label": red_flag.label,
        "detection_instructions": red_flag.detection_instructions,
        "prompt_question_id": red_flag.prompt_question_id,
        "prompt_qid": red_flag.prompt_qid,
    }


def _bank_payload(bank: Bank) -> dict[str, object]:
    return {
        "bank_id": bank.bank_id,
        "drafted_by": bank.drafted_by,
        "questions": [_question_payload(question) for question in bank.questions],
        "red_flags": [_red_flag_payload(red_flag) for red_flag in bank.red_flags],
        "fallback_checklist": bank.fallback_checklist,
    }


def _registry_payload(registry: BankRegistry) -> dict[str, object]:
    return {
        "version": 1,
        "domains": [
            {
                "id": domain.id,
                "label": domain.label,
                "file": domain.file,
                "router_description": domain.router_description,
                "keywords": domain.keywords,
            }
            for domain in registry.domains.values()
        ],
        "general_bank": registry.general.bank_id,
        "banks": {bank_id: _bank_payload(bank) for bank_id, bank in registry.banks.items()},
        "general": _bank_payload(registry.general),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = None
    try:
        if os.getenv("TYPESAFE_API_KEY"):
            client = AsyncTypeSafeClient()
        app.state.typesafe_client = client
        yield
    finally:
        if client is not None:
            await client.aclose()


app = FastAPI(title="Patient Interview Helper MVP", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/bank")
def api_bank() -> dict[str, object]:
    registry = load_registry()
    return _registry_payload(registry)


@app.post("/api/suggest", response_model=SuggestResponse)
async def api_suggest(payload: PatientInput) -> SuggestResponse:
    response = await suggest_questions(payload, client=getattr(app.state, "typesafe_client", None))
    logging.info(
        "Suggestion latency_ms=%s degraded=%s low_confidence=%s",
        response.latency_ms,
        response.degraded,
        response.low_confidence,
    )
    return response


@app.get("/")
def index() -> FileResponse:
    if not STATIC_INDEX.exists():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(STATIC_INDEX)
