from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx2 import Headers
from typesafe_sdk import TypeSafeAPIError

from app.bank import load_registry
from app import engine
from app.engine import suggest_questions
from app.models import PatientInput
from tests.fakes import FakeClient


REGISTRY = load_registry()


def noul(value: float) -> SimpleNamespace:
    return SimpleNamespace(type="noul", noul=value)


def choice(value: str, confidence: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(type="choice", choice=value, confidence=confidence, probabilities={value: 1.0})


def score(value: float, confidence: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(
        type="score",
        score=value,
        confidence=confidence,
        legend={0: "low", 1: "medium", 2: "high"},
        probabilities={0: 0.0, 1: 0.0, 2: 1.0},
    )


def domain_answers(**overrides: float) -> dict[str, object]:
    answers = {f"domain_signal__{domain_id}": noul(0.05) for domain_id in REGISTRY.domains}
    for domain_id, value in overrides.items():
        answers[f"domain_signal__{domain_id}"] = noul(value)
    return answers


def score_answers(bank_id: str, mapping: dict[str, float], confidence: float = 0.95) -> dict[str, object]:
    return {f"score__{bank_id}__{qid}": score(value, confidence) for qid, value in mapping.items()}


def red_flag_answer(bank_id: str, rf_id: str, value: float) -> tuple[str, object]:
    return f"red_flag__{bank_id}__{rf_id}", noul(value)


def _patient(text: str | list[str], asked: list[dict[str, object]] | None = None) -> PatientInput:
    statements = [text] if isinstance(text, str) else text
    return PatientInput(patient_statements=statements, demographics={}, asked=asked or [])


def _question_ids(response) -> list[str]:
    return [item.question_id for item in response.suggestions]


def _all_question_keys(client: FakeClient) -> list[str]:
    return [key for call in client.calls for key in call["question_keys"]]


def _score_keys(client: FakeClient) -> list[str]:
    return [key for key in _all_question_keys(client) if key.startswith("score__")]


@pytest.mark.asyncio
async def test_single_domain_route_uses_one_stage_two_bank() -> None:
    answers = domain_answers(respiratory=0.9)
    answers.update(score_answers("respiratory", {q.id: 2.0 for q in REGISTRY.banks["respiratory"].questions[:3]}))
    client = FakeClient(answers)

    response = await suggest_questions(_patient("I've had a cough for three weeks and I get winded climbing stairs"), client=client)

    assert _score_keys(client)
    assert all(key.startswith("score__respiratory__") for key in _score_keys(client))
    assert response.routing.primary == "respiratory"
    assert not response.routing.unclear
    assert {item.bank for item in response.suggestions} == {"respiratory"}


@pytest.mark.asyncio
async def test_multi_domain_activates_both_banks() -> None:
    answers = domain_answers(cardiovascular=0.8, urinary=0.6)
    answers.update(score_answers("cardiovascular", {q.id: 2.0 for q in REGISTRY.banks["cardiovascular"].questions[:2]}))
    answers.update(score_answers("urinary", {q.id: 1.8 for q in REGISTRY.banks["urinary"].questions[:2]}))
    client = FakeClient(answers)

    response = await suggest_questions(_patient("I have chest pain and I also can't pee"), client=client)

    stage_two_keys = _score_keys(client)
    assert any(key.startswith("score__cardiovascular__") for key in stage_two_keys)
    assert any(key.startswith("score__urinary__") for key in stage_two_keys)
    assert response.routing.primary == "cardiovascular"
    assert {item.bank for item in response.suggestions}.issubset({"cardiovascular", "urinary"})


@pytest.mark.asyncio
async def test_router_max_banks_caps_active_banks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "ROUTER_MAX_BANKS", 1)
    answers = domain_answers(cardiovascular=0.8, urinary=0.6)
    answers.update(score_answers("cardiovascular", {q.id: 2.0 for q in REGISTRY.banks["cardiovascular"].questions[:2]}))
    answers.update(score_answers("urinary", {q.id: 1.8 for q in REGISTRY.banks["urinary"].questions[:2]}))
    client = FakeClient(answers)

    await suggest_questions(_patient("I have chest pain and I also can't pee"), client=client)

    stage_two_keys = _score_keys(client)
    assert any(key.startswith("score__cardiovascular__") for key in stage_two_keys)
    assert not any(key.startswith("score__urinary__") for key in stage_two_keys)


@pytest.mark.asyncio
async def test_unclear_route_returns_general_fallback_without_stage_two() -> None:
    client = FakeClient(domain_answers())

    response = await suggest_questions(_patient("I feel off"), client=client)

    assert not _score_keys(client)
    assert response.routing.unclear
    assert response.low_confidence
    assert response.suggestions
    assert response.suggestions[0].question_id == "general:q_clarify_main_complaint"


@pytest.mark.asyncio
async def test_red_flag_cross_bank_surfaces_despite_routing() -> None:
    answers = domain_answers(digestive=0.9)
    answers["red_flag__cardiovascular__rf_ongoing_chest_pain"] = noul(0.7)
    answers.update(score_answers("digestive", {q.id: 2.0 for q in REGISTRY.banks["digestive"].questions[:3]}))
    client = FakeClient(answers)

    response = await suggest_questions(_patient("I've been vomiting blood since this morning"), client=client)

    assert any(flag.bank == "cardiovascular" for flag in response.red_flags)
    assert any(flag.question_id == "cardiovascular:q_chest_pain_ongoing" for flag in response.red_flags)
    assert {item.bank for item in response.suggestions} == {"digestive"}


@pytest.mark.asyncio
async def test_red_flag_while_unclear_uses_general_fallback() -> None:
    answers = domain_answers()
    answers["red_flag__urinary__rf_retention"] = noul(0.8)
    client = FakeClient(answers)

    response = await suggest_questions(_patient("I feel strange"), client=client)

    assert response.routing.unclear
    assert response.low_confidence
    assert any(flag.question_id == "urinary:q_unable_to_void" for flag in response.red_flags)
    assert response.suggestions[0].question_id == "general:q_clarify_main_complaint"


@pytest.mark.asyncio
async def test_red_flag_max_limits_to_three_results() -> None:
    answers = domain_answers()
    red_flags = [
        red_flag_answer("urinary", "rf_retention", 0.95),
        red_flag_answer("respiratory", "rf_hemoptysis", 0.88),
        red_flag_answer("cardiovascular", "rf_ongoing_chest_pain", 0.81),
        red_flag_answer("neurological", "rf_stroke_signs", 0.76),
        red_flag_answer("dermatological", "rf_anaphylaxis", 0.7),
    ]
    for key, value in red_flags:
        answers[key] = value
    client = FakeClient(answers)

    response = await suggest_questions(_patient("I feel off"), client=client)

    assert len(response.red_flags) == 3
    assert [item.score for item in response.red_flags] == sorted([item.score for item in response.red_flags], reverse=True)


@pytest.mark.asyncio
async def test_asked_handling_supports_qualified_and_legacy_ids() -> None:
    answers = domain_answers(urinary=0.9)
    answers.update(
        score_answers(
            "urinary",
            {
                "q_dysuria": 2.0,
                "q_frequency": 1.9,
                "q_duration": 1.8,
            },
        )
    )
    client = FakeClient(answers)

    response = await suggest_questions(
        _patient(
            "It burns when I pee and I'm going every hour",
            asked=[{"id": "urinary:q_dysuria"}, {"id": "q_frequency"}, {"question_id": "unknown:bogus"}],
        ),
        client=client,
    )

    assert "urinary:q_dysuria" not in _question_ids(response)
    assert "urinary:q_frequency" not in _question_ids(response)


@pytest.mark.asyncio
async def test_cross_bank_dedupe_keeps_the_highest_scoring_copy() -> None:
    answers = domain_answers(respiratory=0.8, urinary=0.7)
    answers["score__respiratory__q_duration"] = score(2.0, 0.95)
    answers["score__urinary__q_duration"] = score(1.0, 0.95)
    answers.update(score_answers("respiratory", {q.id: 0.0 for q in REGISTRY.banks["respiratory"].questions[:3]}))
    answers.update(score_answers("urinary", {q.id: 0.0 for q in REGISTRY.banks["urinary"].questions[:3]}))
    client = FakeClient(answers)

    response = await suggest_questions(_patient("I have chest pain and I also can't pee"), client=client)

    duration_items = [item for item in response.suggestions if item.question_id.endswith(":q_duration")]
    assert len(duration_items) == 1


@pytest.mark.asyncio
async def test_diversity_cap_holds() -> None:
    answers = domain_answers(urinary=0.9)
    answers.update(
        score_answers(
            "urinary",
            {
                "q_weak_stream": 2.0,
                "q_hesitancy": 1.95,
                "q_straining": 1.9,
                "q_frequency": 1.85,
                "q_urgency": 1.8,
            },
        )
    )
    client = FakeClient(answers)

    response = await suggest_questions(_patient("I'm having trouble peeing"), client=client)

    counts: dict[tuple[str, str], int] = {}
    for item in response.suggestions:
        counts[(item.bank, item.area)] = counts.get((item.bank, item.area), 0) + 1
    assert max(counts.values()) <= 2
    assert len(response.suggestions) <= 3


@pytest.mark.asyncio
async def test_empty_statements_skip_client() -> None:
    client = FakeClient()

    response = await suggest_questions(_patient([]), client=client)

    assert len(client.calls) == 0
    assert response.routing.method == "none"
    assert response.routing.unclear
    assert response.low_confidence
    assert response.suggestions[0].question_id == "general:q_clarify_main_complaint"


class Stage1FailClient:
    def __init__(self, error: TypeSafeAPIError) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def system_one(self, state, questions, **kwargs):
        self.calls.append({"state": state, "question_keys": list(questions.keys())})
        raise self.error


@pytest.mark.asyncio
async def test_degraded_keyword_hit_uses_keyword_router() -> None:
    error = TypeSafeAPIError(429, {}, Headers({}), message="rate limited")
    client = Stage1FailClient(error)

    response = await suggest_questions(_patient("my tooth hurts"), client=client)

    assert response.degraded
    assert response.routing.method == "keyword"
    assert response.routing.primary == "oral"
    assert {item.bank for item in response.suggestions} == {"oral"}
    assert response.red_flags == []


@pytest.mark.asyncio
async def test_degraded_no_keyword_hit_uses_general_fallback() -> None:
    error = TypeSafeAPIError(429, {}, Headers({}), message="rate limited")
    client = Stage1FailClient(error)

    response = await suggest_questions(_patient("I feel strange"), client=client)

    assert response.degraded
    assert response.routing.method == "none"
    assert response.routing.unclear
    assert response.suggestions[0].question_id == "general:q_clarify_main_complaint"


class Stage2FailClient(FakeClient):
    def __init__(self, answers: dict[str, object], error: TypeSafeAPIError) -> None:
        super().__init__(answers)
        self.error = error

    async def system_one(self, state, questions, **kwargs):
        call = {
            "state": state,
            "questions": questions,
            "question_keys": list(questions.keys()),
        }
        self.calls.append(call)
        if any(key.startswith("score__") for key in call["question_keys"]):
            raise self.error
        return await super().system_one(state, questions, **kwargs)


@pytest.mark.asyncio
async def test_stage2_failure_preserves_routing_and_red_flags() -> None:
    answers = domain_answers(digestive=0.9)
    answers["red_flag__cardiovascular__rf_ongoing_chest_pain"] = noul(0.8)
    client = Stage2FailClient(answers, TypeSafeAPIError(500, {}, Headers({}), message="boom"))

    response = await suggest_questions(_patient("I've been vomiting blood since this morning"), client=client)

    assert response.degraded
    assert response.routing.primary == "digestive"
    assert any(flag.bank == "cardiovascular" for flag in response.red_flags)
    assert response.suggestions[0].bank == "digestive"


@pytest.mark.asyncio
async def test_chunking_merges_multiple_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "MAX_QUESTIONS_PER_REQUEST", 10)
    answers = domain_answers(urinary=0.9)
    answers.update(score_answers("urinary", {q.id: 2.0 for q in REGISTRY.banks["urinary"].questions}))
    client = FakeClient(answers)

    response = await suggest_questions(_patient("I'm having trouble peeing"), client=client)

    assert len(client.calls) > 2
    assert response.suggestions
