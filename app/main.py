from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.bank import load_bank
from app.database import (
    get_all_bank_questions,
    get_session_history,
    init_db,
    seed_database,
)
from app.engine import route_patient_hierarchical, suggest_questions
from app.models import (
    PatientInput,
    RoutingRequest,
    RoutingResponse,
    SuggestResponse,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
STATIC_INDEX = ROOT / "static" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifecycle context: Initialize and seed SQLite questions_bank on startup."""
    LOGGER.info("Starting up: initializing SQLite database and seeding questions_bank...")
    init_db()
    seed_count = seed_database()
    LOGGER.info("Startup complete. Seeded %d questions.", seed_count)
    yield
    LOGGER.info("Shutting down application.")


app = FastAPI(
    title="Clinical Intake Engine (TypeSafe Jev + Seed & Serve SQLite)",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --- Hierarchical SQLite Routing Endpoints ---

@app.post("/api/route", response_model=RoutingResponse)
async def api_route(payload: RoutingRequest) -> RoutingResponse:
    """Gated Hierarchical Clinical Routing across 4 clinical levels."""
    response = await route_patient_hierarchical(payload)
    LOGGER.info(
        "Routing session=%s level=%s next_q=%s confidence=%.2f red_flag=%s latency=%dms",
        response.session_id,
        response.current_level,
        response.next_question.id if response.next_question else "None",
        response.confidence,
        response.is_red_flag,
        response.latency_ms,
    )
    return response


@app.get("/api/sessions/{session_id}")
def api_session_history(session_id: str) -> dict[str, object]:
    """Retrieve audit history and Jev confidence scores for a patient session."""
    logs = get_session_history(session_id)
    return {"session_id": session_id, "logs": logs, "count": len(logs)}


@app.get("/api/bank/hierarchical")
def api_bank_hierarchical() -> dict[str, object]:
    """Fetch all questions loaded in SQLite questions_bank."""
    questions = get_all_bank_questions()
    return {"count": len(questions), "questions": questions}


# --- Hierarchical Bank Endpoints ---

@app.get("/api/bank")
def api_bank() -> dict[str, object]:
    """Returns the hierarchical question bank loaded in SQLite."""
    questions = get_all_bank_questions()
    by_level: dict[str, list[dict[str, object]]] = {}
    for q in questions:
        by_level.setdefault(q["level"], []).append(q)
    return {
        "source": "clinical_engine.db (SQLite questions_bank)",
        "count": len(questions),
        "questions": questions,
        "by_level": by_level,
    }


@app.post("/api/suggest", response_model=SuggestResponse)
async def api_suggest(payload: PatientInput) -> SuggestResponse:
    """Provides hierarchical suggestions matching the active gated clinical level."""
    chat_text = " ".join(payload.patient_statements)
    asked_ids = {str(item.get("id") or item.get("question_id")) for item in payload.asked if isinstance(item, dict)}
    routing_req = RoutingRequest(
        session_id="suggest-session",
        chat_history=chat_text or "Initial complaint",
        current_system="urinary",
    )
    routing_res = await route_patient_hierarchical(routing_req)

    suggestions: list[Suggestion] = []
    red_flags: list[Suggestion] = []

    if routing_res.next_question:
        sug = Suggestion(
            question_id=routing_res.next_question.id,
            text=routing_res.next_question.text,
            area=routing_res.next_question.level,
            rationale=routing_res.next_question.rationale,
            score=routing_res.confidence,
            confidence=routing_res.confidence,
            is_red_flag=routing_res.is_red_flag,
            label="Red flag" if routing_res.is_red_flag else routing_res.current_level,
        )
        if routing_res.is_red_flag:
            red_flags.append(sug)
        else:
            suggestions.append(sug)

    # Add other questions from the active level
    other_qs = get_questions_by_level("urinary", routing_res.current_level, exclude_ids=asked_ids)
    for q in other_qs:
        if routing_res.next_question and q["id"] == routing_res.next_question.id:
            continue
        suggestions.append(
            Suggestion(
                question_id=q["id"],
                text=q["text"],
                area=q["level"],
                rationale=q["rationale"],
                score=0.5,
                confidence=0.5,
                is_red_flag=(q["level"] == "3_Red_Flag"),
                label=q["level"],
            )
        )
        if len(suggestions) >= 3:
            break

    return SuggestResponse(
        suggestions=suggestions,
        red_flags=red_flags,
        low_confidence=False,
        degraded=False,
        latency_ms=routing_res.latency_ms,
    )


@app.get("/")
def index() -> FileResponse:
    if not STATIC_INDEX.exists():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(STATIC_INDEX)

