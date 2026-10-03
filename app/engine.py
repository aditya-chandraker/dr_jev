from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score, TypeSafeAPIError

from app.bank import Bank, BankQuestion, RedFlag, get_question_map, load_bank
from app.models import PatientInput, SuggestResponse, Suggestion


load_dotenv()

LOGGER = logging.getLogger(__name__)
MODEL_NAME = os.getenv("TYPESAFE_MODEL", "jev-latest")
RED_FLAG_THRESHOLD = float(os.getenv("RED_FLAG_THRESHOLD", "0.5"))
LOW_CONF_THRESHOLD = float(os.getenv("LOW_CONF_THRESHOLD", "0.35"))
MAX_QUESTIONS_PER_REQUEST = int(os.getenv("MAX_QUESTIONS_PER_REQUEST", "8"))


@dataclass(frozen=True)
class EngineResult:
    suggestions: list[Suggestion]
    red_flags: list[Suggestion]
    low_confidence: bool
    degraded: bool
    latency_ms: int
    error_category: str | None = None


def _asked_ids(asked: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for item in asked:
        if isinstance(item, dict):
            question_id = item.get("id") or item.get("question_id")
            if question_id:
                ids.add(str(question_id))
    return ids


def _build_state(payload: PatientInput) -> dict[str, Any]:
    return {
        "patient_statements": payload.patient_statements,
        "demographics": payload.demographics,
        "already_asked": payload.asked,
    }


def _has_urinary_signal(statements: list[str]) -> bool:
    haystack = " ".join(statements).lower()
    keywords = [
        "pee",
        "urine",
        "urinary",
        "bladder",
        "void",
        "stream",
        "frequency",
        "urgency",
        "nocturia",
        "incontinence",
        "dysuria",
        "burn",
        "pink",
        "red",
        "brown",
        "flank",
        "catheter",
    ]
    return any(keyword in haystack for keyword in keywords)


def _clarification_question() -> Choice:
    return Choice(
        instructions=(
            "Given the patient's urinary complaint, which category best matches the main symptom? "
            "Choose the closest match and use the no-match option when the complaint is still unclear."
        ),
        criteria={
            "hesitancy_weak_stream": "Trouble starting, slow stream, weak stream, or straining.",
            "dysuria_burning": "Burning or pain with urination.",
            "frequency_urgency": "Going often or having strong urgency.",
            "incontinence": "Leakage or accidents.",
            "unable_to_pass_urine": "Unable to pass urine or only tiny amounts.",
            "blood_in_urine": "Pink, red, or brown urine.",
            "unclear_or_other": "The complaint is unclear or does not fit the above.",
        },
    )


def _red_flag_question(red_flag: RedFlag) -> Noul:
    return Noul(instructions=red_flag.detection_instructions)


def _score_question(question: BankQuestion, state: dict[str, Any]) -> Score:
    instructions = (
        f"Given what the patient has said so far in `patient_statements`, how useful would it be for the clinician to ask next: "
        f"'{question.text}' (purpose: {question.rationale})?"
    )
    return Score(
        instructions=instructions,
        criteria=[
            "Not useful now: already answered, or unrelated to what the patient described.",
            "Somewhat useful: could add context but is not a priority.",
            "Very useful now: directly narrows the likely causes or screens for something important given what was said.",
        ],
    )


def _chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _score_from_answer(answer: Any) -> float:
    if answer is None:
        return 0.0
    if hasattr(answer, "score"):
        return float(answer.score)
    return 0.0


def _confidence_from_answer(answer: Any) -> float:
    if answer is None:
        return 0.0
    if hasattr(answer, "confidence"):
        return float(answer.confidence)
    if hasattr(answer, "noul"):
        return abs(2 * float(answer.noul) - 1)
    return 0.0


def _noul_probability(answer: Any) -> float:
    if answer is None:
        return 0.0
    return float(getattr(answer, "noul", 0.0))


def _make_suggestion(question: BankQuestion, *, score: float, confidence: float, is_red_flag: bool = False, label: str = "") -> Suggestion:
    return Suggestion(
        question_id=question.id,
        text=question.text,
        area=question.area,
        rationale=question.rationale,
        score=score,
        confidence=confidence,
        is_red_flag=is_red_flag,
        label=label,
    )


async def _call_system_one(client: AsyncTypeSafeClient, state: dict[str, Any], questions: dict[str, Any]):
    return await client.system_one(state=state, questions=questions)


async def suggest_questions(payload: PatientInput, client: AsyncTypeSafeClient | None = None) -> SuggestResponse:
    start = time.perf_counter()
    bank = load_bank()
    question_map = get_question_map(bank)
    asked_ids = _asked_ids(payload.asked)
    state = _build_state(payload)

    active_questions: dict[str, Any] = {}
    clarification_key = "clarification"
    if len(payload.patient_statements) <= 2:
        active_questions[clarification_key] = _clarification_question()

    for red_flag in bank.red_flags:
        active_questions[f"red_flag__{red_flag.id}"] = _red_flag_question(red_flag)

    for question in bank.questions:
        if question.id in asked_ids:
            continue
        active_questions[f"score__{question.id}"] = _score_question(question, state)

    batches = list(_chunked(list(active_questions.items()), MAX_QUESTIONS_PER_REQUEST))

    async def run_batches(active_client: AsyncTypeSafeClient) -> list[Any]:
        results: list[Any] = []
        for batch in batches:
            batch_questions = dict(batch)
            results.append(await _call_system_one(active_client, state, batch_questions))
        return results

    degraded = False
    error_category: str | None = None
    response_batches: list[Any]
    owns_client = client is None
    try:
        if client is None:
            async with AsyncTypeSafeClient(model=MODEL_NAME) as active_client:
                response_batches = await run_batches(active_client)
        else:
            response_batches = await run_batches(client)
    except TypeSafeAPIError as exc:
        degraded = True
        error_category = _categorize_error(exc.status)
        return _fallback_response(bank, start, degraded=True, low_confidence=False, error_category=error_category)
    except asyncio.TimeoutError:
        degraded = True
        error_category = "timeout"
        return _fallback_response(bank, start, degraded=True, low_confidence=False, error_category=error_category)
    except Exception as exc:  # pragma: no cover - defensive fallback for demo reliability
        LOGGER.exception("Unexpected error from TypeSafe")
        degraded = True
        error_category = "unexpected_error"
        return _fallback_response(bank, start, degraded=True, low_confidence=False, error_category=error_category)

    raw_answers: dict[str, Any] = {}
    for response in response_batches:
        raw_answers.update(getattr(response, "answers", {}))

    LOGGER.info("Raw TypeSafe answers: %s", {key: getattr(value, "model_dump", lambda: value)() if hasattr(value, "model_dump") else value for key, value in raw_answers.items()})

    red_flags: list[Suggestion] = []
    clarification_choice = raw_answers.get(clarification_key)
    clarification_winner = getattr(clarification_choice, "choice", "") if clarification_choice else ""
    clarification_confidence = _confidence_from_answer(clarification_choice)

    for red_flag in bank.red_flags:
        answer = raw_answers.get(f"red_flag__{red_flag.id}")
        probability = _noul_probability(answer)
        confidence = _confidence_from_answer(answer)
        LOGGER.info("Red flag %s probability=%s confidence=%s", red_flag.id, probability, confidence)
        if probability >= RED_FLAG_THRESHOLD:
            question = question_map[red_flag.prompt_question_id]
            red_flags.append(
                _make_suggestion(question, score=probability, confidence=confidence, is_red_flag=True, label="Red flag")
            )

    red_flags.sort(key=lambda item: item.score, reverse=True)

    scored_candidates: list[Suggestion] = []
    for question in bank.questions:
        if question.id in asked_ids:
            continue
        answer = raw_answers.get(f"score__{question.id}")
        if answer is None:
            continue
        scored_candidates.append(
            _make_suggestion(
                question,
                score=_score_from_answer(answer),
                confidence=_confidence_from_answer(answer),
            )
        )

    scored_candidates.sort(key=lambda item: item.score, reverse=True)

    if clarification_winner in {"unclear_or_other", "unable_to_pass_urine"} or clarification_confidence < LOW_CONF_THRESHOLD:
        clarifying_question = question_map["q_clarify_trouble_peeing"]
        if clarifying_question.id not in asked_ids:
            scored_candidates = [
                _make_suggestion(
                    clarifying_question,
                    score=clarification_confidence,
                    confidence=clarification_confidence,
                )
            ] + [candidate for candidate in scored_candidates if candidate.question_id != clarifying_question.id]

    selected = _apply_diversity(scored_candidates, limit=3)
    top_confidence = max([item.confidence for item in red_flags + selected] or [0.0])
    low_confidence = (top_confidence < LOW_CONF_THRESHOLD and not red_flags) or (not red_flags and not _has_urinary_signal(payload.patient_statements))

    if low_confidence:
        return _fallback_response(bank, start, degraded=False, low_confidence=True, error_category=None)

    latency_ms = int((time.perf_counter() - start) * 1000)
    return SuggestResponse(
        suggestions=selected,
        red_flags=red_flags,
        low_confidence=low_confidence,
        degraded=degraded,
        latency_ms=latency_ms,
    )


def _apply_diversity(items: list[Suggestion], limit: int) -> list[Suggestion]:
    selected: list[Suggestion] = []
    counts: Counter[str] = Counter()
    for item in items:
        if counts[item.area] >= 2:
            continue
        selected.append(item)
        counts[item.area] += 1
        if len(selected) >= limit:
            break
    return selected


def _fallback_response(bank: Bank, start: float, *, degraded: bool, low_confidence: bool, error_category: str | None) -> SuggestResponse:
    question_map = get_question_map(bank)
    suggestions = [
        _make_suggestion(question_map[qid], score=0.0, confidence=0.0)
        for qid in bank.fallback_checklist[:3]
    ]
    red_flags: list[Suggestion] = []
    latency_ms = int((time.perf_counter() - start) * 1000)
    return SuggestResponse(
        suggestions=suggestions,
        red_flags=red_flags,
        low_confidence=low_confidence,
        degraded=degraded,
        latency_ms=latency_ms,
    )


def _categorize_error(status: int | None) -> str:
    if status == 401 or status == 403:
        return "auth"
    if status == 429:
        return "rate_limit"
    if status and 500 <= status < 600:
        return "server_error"
    return "api_error"
