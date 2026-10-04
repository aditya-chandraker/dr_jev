from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from typesafe_sdk import AsyncTypeSafeClient

from app.bank import Bank, BankRegistry, load_registry
from app.engine import suggest_questions
from app.finchnode import FINCHNODE_SCENARIO, FinchNodeClient, FinchNodeError, summarize_records
from app.models import FinchNodeSessionRequest, PatientChart, PatientInput, SuggestResponse, TranscriptResponse
from app.transcribe import MAX_AUDIO_BYTES, TranscriptionError, transcribe_encounter


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
    finchnode = None
    try:
        if os.getenv("TYPESAFE_API_KEY"):
            client = AsyncTypeSafeClient()
        finchnode = FinchNodeClient.from_env()
        app.state.typesafe_client = client
        app.state.finchnode_client = finchnode
        yield
    finally:
        if client is not None:
            await client.aclose()
        if finchnode is not None:
            await finchnode.aclose()


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


@app.post("/api/transcribe", response_model=TranscriptResponse)
async def api_transcribe(audio: UploadFile = File(...)) -> TranscriptResponse:
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    try:
        transcript = await transcribe_encounter(data, audio.content_type)
    except TranscriptionError as exc:
        raise HTTPException(status_code=exc.status or 502, detail=exc.message) from exc
    logging.info(
        "Transcribed %d bytes into %d turns (%d patient) in %sms",
        len(data),
        len(transcript.turns),
        len(transcript.patient_statements),
        transcript.latency_ms,
    )
    return transcript


def _finchnode() -> FinchNodeClient:
    client = getattr(app.state, "finchnode_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="FINCHNODE_API_KEY is not configured")
    return client


def _finchnode_http_error(exc: FinchNodeError) -> HTTPException:
    status = exc.status if exc.status and 400 <= exc.status < 500 else 502
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


@app.post("/api/finchnode/sessions", status_code=201)
async def api_finchnode_create_session(payload: FinchNodeSessionRequest) -> dict[str, object]:
    client = _finchnode()
    try:
        session = await client.create_session(external_id=payload.external_id, categories=payload.categories)
        if client.is_sandbox:
            session = await client.simulate(session["id"], payload.scenario or FINCHNODE_SCENARIO)
    except FinchNodeError as exc:
        raise _finchnode_http_error(exc) from exc
    logging.info("FinchNode session %s created, simulation=%s", session.get("id"), session.get("simulation"))
    return session


@app.get("/api/finchnode/sessions/{session_id}")
async def api_finchnode_get_session(session_id: str) -> dict[str, object]:
    try:
        return await _finchnode().get_session(session_id)
    except FinchNodeError as exc:
        raise _finchnode_http_error(exc) from exc


@app.get("/api/finchnode/sessions/{session_id}/patient", response_model=PatientChart)
async def api_finchnode_patient(session_id: str) -> PatientChart:
    client = _finchnode()
    try:
        session = await client.wait_for_simulation(session_id)
        snapshot = await client.get_records(session["subject"])
    except FinchNodeError as exc:
        raise _finchnode_http_error(exc) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    summary = summarize_records(snapshot)
    return PatientChart(
        session_id=session_id,
        subject=session["subject"],
        scenario=(session.get("simulation") or {}).get("scenario"),
        organization=session.get("organization"),
        **summary,
    )


@app.get("/")
def index() -> FileResponse:
    if not STATIC_INDEX.exists():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(STATIC_INDEX)
