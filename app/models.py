from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


# --- SQLite Schema Models ---

class QuestionBankItem(BaseModel):
    id: str
    system: str
    level: str
    text: str
    rationale: str = ""
    jev_primitive: str = "Choice"
    trigger_condition: str = ""


class PatientSessionLog(BaseModel):
    session_id: str
    question_asked_id: str | None = None
    patient_response: str = ""
    jev_confidence_score: float
    timestamp: str


# --- Hierarchical Engine Routing Models ---

class RoutingRequest(BaseModel):
    session_id: str = Field(description="Unique encounter or session ID")
    chat_history: str = Field(description="Patient's conversational statement or complete chat transcript")
    current_system: str | None = Field(default=None, description="Clinical organ system being evaluated")
    current_level: str | None = Field(default=None, description="Active clinical level; inferred if omitted")
    patient_response: str | None = Field(default=None, description="Patient's immediate answer to the previous question")
    demographics: dict[str, Any] = Field(default_factory=dict)


class RoutingResponse(BaseModel):
    session_id: str
    current_system: str
    current_level: str
    is_abnormal: bool = False
    drilldown_active: bool = False
    abnormal_systems: list[str] = Field(default_factory=list)
    screened_systems: dict[str, str] = Field(default_factory=dict)
    gate_satisfied: bool = False
    next_question: QuestionBankItem | None = None
    questions: list[QuestionBankItem] = Field(default_factory=list, description="Top 3 recommended questions")
    red_flags: list[QuestionBankItem] = Field(default_factory=list, description="Any active red flag questions")
    confidence: float = 0.0
    rationale: str = ""
    is_red_flag: bool = False
    completed: bool = False
    latency_ms: int = 0


# --- Legacy Models for Backward Compatibility ---

class PatientInput(BaseModel):
    patient_statements: list[str] = Field(default_factory=list)
    demographics: dict[str, Any] = Field(default_factory=dict)
    asked: list[dict[str, Any]] = Field(default_factory=list)


class Suggestion(BaseModel):
    question_id: str
    text: str
    area: str
    rationale: str
    score: float = 0.0
    confidence: float = 0.0
    is_red_flag: bool = False
    label: str = ""


class SuggestResponse(BaseModel):
    suggestions: list[Suggestion]
    red_flags: list[Suggestion]
    low_confidence: bool
    degraded: bool
    latency_ms: int
