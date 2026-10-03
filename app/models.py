from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
