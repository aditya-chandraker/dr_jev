from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PatientInput(BaseModel):
    patient_statements: list[str] = Field(default_factory=list)
    demographics: dict[str, Any] = Field(default_factory=dict)
    asked: list[dict[str, Any]] = Field(default_factory=list)
    health_record: list[str] = Field(default_factory=list)


class TranscriptTurn(BaseModel):
    speaker: str
    text: str
    simplified: str = ""


class Exchange(BaseModel):
    question: str
    answer: str = ""


class TranscriptResponse(BaseModel):
    turns: list[TranscriptTurn] = Field(default_factory=list)
    patient_statements: list[str] = Field(default_factory=list)
    exchanges: list[Exchange] = Field(default_factory=list)
    model: str = ""
    latency_ms: int = 0


class FinchNodeSessionRequest(BaseModel):
    scenario: str | None = None
    categories: list[str] | None = None
    external_id: str | None = None


class PatientChart(BaseModel):
    session_id: str | None = None
    subject: str | None = None
    name: str | None = None
    scenario: str | None = None
    organization: str | None = None
    demographics: dict[str, Any] = Field(default_factory=dict)
    health_record: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class Suggestion(BaseModel):
    question_id: str
    bank: str = ""
    text: str
    area: str
    rationale: str
    score: float = 0.0
    confidence: float = 0.0
    is_red_flag: bool = False
    label: str = ""


class RoutedDomain(BaseModel):
    id: str
    label: str
    score: float


class RoutingInfo(BaseModel):
    primary: str | None = None
    domains: list[RoutedDomain] = Field(default_factory=list)
    unclear: bool = False
    method: str = "llm"


class SuggestResponse(BaseModel):
    suggestions: list[Suggestion]
    red_flags: list[Suggestion]
    low_confidence: bool
    degraded: bool
    latency_ms: int
    routing: RoutingInfo = Field(default_factory=RoutingInfo)
