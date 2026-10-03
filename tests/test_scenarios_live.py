from __future__ import annotations

import os

import pytest

from app.engine import suggest_questions
from app.models import PatientInput


pytestmark = pytest.mark.slow


def _has_key() -> bool:
    return bool(os.getenv("TYPESAFE_API_KEY"))


@pytest.mark.asyncio
async def test_live_smoke_scenario_1_trouble_peeing() -> None:
    if not _has_key():
        pytest.skip("TYPESAFE_API_KEY not set")
    result = await suggest_questions(PatientInput(patient_statements=["I'm having trouble peeing"], demographics={"age": 58, "sex": "male"}, asked=[]))
    assert result.suggestions
    assert result.suggestions[0].question_id == "q_clarify_trouble_peeing"


@pytest.mark.asyncio
async def test_live_smoke_scenario_2_pink_urine() -> None:
    if not _has_key():
        pytest.skip("TYPESAFE_API_KEY not set")
    result = await suggest_questions(PatientInput(patient_statements=["Weak stream, and I noticed it looked pink"], demographics={}, asked=[]))
    assert any(flag.question_id == "q_urine_color" for flag in result.red_flags)


@pytest.mark.asyncio
async def test_live_smoke_scenario_3_retention() -> None:
    if not _has_key():
        pytest.skip("TYPESAFE_API_KEY not set")
    result = await suggest_questions(PatientInput(patient_statements=["I can't pee at all and my belly hurts"], demographics={}, asked=[]))
    assert result.red_flags
    assert result.red_flags[0].question_id == "q_unable_to_void"


@pytest.mark.asyncio
async def test_live_smoke_scenario_4_fever_dysuria() -> None:
    if not _has_key():
        pytest.skip("TYPESAFE_API_KEY not set")
    result = await suggest_questions(PatientInput(patient_statements=["It burns when I pee and I have a fever"], demographics={}, asked=[]))
    assert any(flag.question_id == "q_fever_chills" for flag in result.red_flags)


@pytest.mark.asyncio
async def test_live_smoke_scenario_7_unrelated_complaint_falls_back() -> None:
    if not _has_key():
        pytest.skip("TYPESAFE_API_KEY not set")
    result = await suggest_questions(PatientInput(patient_statements=["My knee hurts when I run"], demographics={}, asked=[]))
    assert result.low_confidence
    assert result.suggestions
