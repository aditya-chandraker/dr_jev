from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.engine import suggest_questions
from app.models import PatientInput


class MockClient:
    def __init__(self, answers: dict[str, object]) -> None:
        self._answers = answers

    async def system_one(self, state, questions, **kwargs):
        return SimpleNamespace(answers=self._answers)


def answer_noul(value: float) -> SimpleNamespace:
    return SimpleNamespace(type="noul", noul=value)


def answer_choice(choice: str, confidence: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(type="choice", choice=choice, confidence=confidence, probabilities={choice: 1.0})


def answer_score(score: float, confidence: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(type="score", score=score, confidence=confidence, probabilities={0: 0.1, 1: 0.2, 2: 0.7}, legend={0: "low", 1: "med", 2: "high"})


@pytest.mark.asyncio
async def test_ranking_excludes_already_asked_ids() -> None:
    answers = {
        "clarification": answer_choice("unclear_or_other", 0.95),
        "red_flag__rf_retention": answer_noul(0.1),
        "red_flag__rf_visible_blood": answer_noul(0.1),
        "red_flag__rf_fever_chills": answer_noul(0.1),
        "red_flag__rf_flank_pain": answer_noul(0.1),
        "red_flag__rf_back_neuro": answer_noul(0.1),
        "red_flag__rf_sudden_severe_pain": answer_noul(0.1),
        "score__q_dysuria": answer_score(2.0, 0.9),
        "score__q_frequency": answer_score(1.0, 0.9),
        "score__q_urgency": answer_score(0.9, 0.9),
        "score__q_duration": answer_score(0.8, 0.9),
    }
    client = MockClient(answers)
    result = await suggest_questions(
        PatientInput(patient_statements=["I have trouble peeing"], demographics={}, asked=[{"id": "q_dysuria"}]),
        client=client,
    )
    assert all(item.question_id != "q_dysuria" for item in result.suggestions)


@pytest.mark.asyncio
async def test_red_flag_above_threshold_is_first_and_labeled() -> None:
    answers = {
        "clarification": answer_choice("unclear_or_other", 0.4),
        "red_flag__rf_retention": answer_noul(0.9),
        "red_flag__rf_visible_blood": answer_noul(0.1),
        "red_flag__rf_fever_chills": answer_noul(0.1),
        "red_flag__rf_flank_pain": answer_noul(0.1),
        "red_flag__rf_back_neuro": answer_noul(0.1),
        "red_flag__rf_sudden_severe_pain": answer_noul(0.1),
        "score__q_dysuria": answer_score(0.2, 0.6),
        "score__q_frequency": answer_score(0.1, 0.6),
    }
    client = MockClient(answers)
    result = await suggest_questions(PatientInput(patient_statements=["I can't pee at all"], demographics={}, asked=[]), client=client)
    assert result.red_flags
    assert result.red_flags[0].label == "Red flag"


@pytest.mark.asyncio
async def test_diversity_cap_holds() -> None:
    answers = {
        "clarification": answer_choice("unclear_or_other", 0.95),
        "red_flag__rf_retention": answer_noul(0.1),
        "red_flag__rf_visible_blood": answer_noul(0.1),
        "red_flag__rf_fever_chills": answer_noul(0.1),
        "red_flag__rf_flank_pain": answer_noul(0.1),
        "red_flag__rf_back_neuro": answer_noul(0.1),
        "red_flag__rf_sudden_severe_pain": answer_noul(0.1),
        "score__q_dysuria": answer_score(2.0, 0.9),
        "score__q_frequency": answer_score(1.9, 0.9),
        "score__q_urgency": answer_score(1.8, 0.9),
        "score__q_nocturia": answer_score(1.7, 0.9),
        "score__q_duration": answer_score(1.6, 0.9),
    }
    client = MockClient(answers)
    result = await suggest_questions(PatientInput(patient_statements=["I pee a lot"], demographics={}, asked=[]), client=client)
    areas = [item.area for item in result.suggestions]
    assert max(areas.count(area) for area in set(areas)) <= 2


@pytest.mark.asyncio
async def test_low_confidence_returns_fallback_checklist_items() -> None:
    answers = {
        "clarification": answer_choice("unclear_or_other", 0.05),
        "red_flag__rf_retention": answer_noul(0.1),
        "red_flag__rf_visible_blood": answer_noul(0.1),
        "red_flag__rf_fever_chills": answer_noul(0.1),
        "red_flag__rf_flank_pain": answer_noul(0.1),
        "red_flag__rf_back_neuro": answer_noul(0.1),
        "red_flag__rf_sudden_severe_pain": answer_noul(0.1),
        "score__q_dysuria": answer_score(0.1, 0.05),
        "score__q_frequency": answer_score(0.1, 0.05),
    }
    client = MockClient(answers)
    result = await suggest_questions(PatientInput(patient_statements=["My knee hurts"], demographics={}, asked=[]), client=client)
    fallback_ids = {"q_clarify_trouble_peeing", "q_duration", "q_urine_color", "q_fever_chills", "q_flank_pain", "q_dysuria", "q_frequency", "q_medications"}
    assert result.low_confidence or result.degraded
    assert result.suggestions
    assert all(item.question_id in fallback_ids for item in result.suggestions)
