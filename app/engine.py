from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score, TypeSafeAPIError

from app.bank import Bank, BankQuestion, RedFlag, get_question_map, load_bank
from app.database import (
    get_asked_question_ids,
    get_question_by_id,
    get_questions_by_level,
    get_session_history,
    log_patient_session,
)
from app.models import (
    PatientInput,
    QuestionBankItem,
    RoutingRequest,
    RoutingResponse,
    SuggestResponse,
    Suggestion,
)

load_dotenv()

LOGGER = logging.getLogger(__name__)
MODEL_NAME = os.getenv("TYPESAFE_MODEL", "jev-latest")
RED_FLAG_THRESHOLD = float(os.getenv("RED_FLAG_THRESHOLD", "0.5"))
LOW_CONF_THRESHOLD = float(os.getenv("LOW_CONF_THRESHOLD", "0.35"))
MAX_QUESTIONS_PER_REQUEST = int(os.getenv("MAX_QUESTIONS_PER_REQUEST", "8"))

CLINICAL_LEVELS = [
    "1_Chief_Complaint",
    "2_Characterization",
    "3_Red_Flag",
    "4_ROS",
]


# ============================================================================
# HIERARCHICAL GATED CLINICAL INTAKE ENGINE (SQLite + TypeSafe Jev)
# ============================================================================

def _has_keyword(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


def _heuristic_match_score(text: str, question: dict[str, Any]) -> float:
    """Fallback / heuristic relevance scoring when Jev API is unavailable or offline."""
    qid = question["id"]
    lowered = text.lower()
    score = 0.5

    if qid == "cc_urinary_triage":
        if _has_keyword(lowered, ["pee", "urine", "urinate", "stream", "burn", "frequency", "leak"]):
            score = 0.85
    elif qid == "char_dysuria_burning":
        if _has_keyword(lowered, ["burn", "pain", "hurt", "sting", "dysuria"]):
            score = 0.95
    elif qid == "char_stream_hesitancy":
        if _has_keyword(lowered, ["weak", "stream", "slow", "strain", "hesitancy", "hard to start"]):
            score = 0.95
    elif qid == "char_frequency_urgency":
        if _has_keyword(lowered, ["often", "frequency", "night", "urgent", "urge", "nocturia"]):
            score = 0.95
    elif qid == "char_incontinence_leakage":
        if _has_keyword(lowered, ["leak", "incontinence", "accident", "cough"]):
            score = 0.95
    elif qid == "rf_urinary_retention":
        if _has_keyword(lowered, ["can't pee", "cannot pee", "unable to pee", "full bladder", "retention"]):
            score = 0.98
    elif qid == "rf_visible_hematuria":
        if _has_keyword(lowered, ["pink", "red", "blood", "brown", "dark urine"]):
            score = 0.98
    elif qid == "rf_flank_kidney_pain":
        if _has_keyword(lowered, ["flank", "back pain", "fever", "chills", "kidney"]):
            score = 0.90
    elif qid == "rf_neurologic_saddle":
        if _has_keyword(lowered, ["numb", "groin", "saddle", "weakness", "legs"]):
            score = 0.95

    return score


async def _evaluate_with_jev(
    client: AsyncTypeSafeClient | None,
    chat_history: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, float], float]:
    """
    Evaluates candidate questions against patient chat history using TypeSafe Jev primitives.
    Falls back to heuristic scoring if API key is absent or on network errors.
    """
    api_key = os.getenv("TYPESAFE_API_KEY")
    if not api_key or client is None and not api_key.strip():
        # Simulated Jev routing output
        scores = {q["id"]: _heuristic_match_score(chat_history, q) for q in candidates}
        avg_conf = sum(scores.values()) / max(len(scores), 1)
        return scores, round(avg_conf, 2)

    questions_payload: dict[str, Any] = {}
    for q in candidates:
        primitive = q.get("jev_primitive", "Choice")
        if primitive == "Noul":
            questions_payload[q["id"]] = Noul(
                instructions=f"Based on patient statement '{chat_history}', does the patient show evidence of: {q['rationale']}?"
            )
        elif primitive == "Score":
            questions_payload[q["id"]] = Score(
                instructions=f"Given '{chat_history}', how critical is asking: '{q['text']}'?",
                criteria=["Low priority", "Moderate priority", "High clinical priority"],
            )
        else:
            questions_payload[q["id"]] = Choice(
                instructions=f"Given '{chat_history}', evaluate relevance of: '{q['text']}'",
                criteria={"relevant": "Directly addresses symptoms", "not_relevant": "Not immediately indicated"},
            )

    try:
        if client is None:
            async with AsyncTypeSafeClient(model=MODEL_NAME) as active_client:
                response = await active_client.system_one(
                    state={"patient_chat": chat_history},
                    questions=questions_payload,
                )
        else:
            response = await client.system_one(
                state={"patient_chat": chat_history},
                questions=questions_payload,
            )

        scores: dict[str, float] = {}
        for q in candidates:
            qid = q["id"]
            answer = getattr(response, "answers", {}).get(qid)
            if hasattr(answer, "noul"):
                scores[qid] = float(answer.noul)
            elif hasattr(answer, "score"):
                scores[qid] = float(answer.score)
            elif hasattr(answer, "confidence"):
                scores[qid] = float(answer.confidence)
            else:
                scores[qid] = 0.5
        avg_conf = sum(scores.values()) / max(len(scores), 1)
        return scores, round(avg_conf, 2)

    except Exception as exc:
        LOGGER.warning("TypeSafe Jev API call failed, using simulated routing: %s", exc)
        scores = {q["id"]: _heuristic_match_score(chat_history, q) for q in candidates}
        avg_conf = sum(scores.values()) / max(len(scores), 1)
        return scores, round(avg_conf, 2)


# Complete History body systems in standardized clinical order
COMPLETE_HISTORY_SYSTEMS = [
    "general",
    "urinary",
    "respiratory",
    "cardiovascular",
    "gastrointestinal",
    "neurologic",
    "musculoskeletal",
    "ophthalmic",
    "oral_dental",
    "dermatology",
    "endocrinology",
    "hematology",
    "infectious_disease",
    "psychiatry",
    "lifestyle_diet",
    "social_history",
    "family_history",
]

SYSTEM_KEYWORDS: dict[str, list[str]] = {
    "urinary": ["pee", "urine", "urinate", "bladder", "burn", "burning", "dysuria", "stream", "flow", "leak", "incontinence", "frequency", "urgent", "urge", "nocturia", "pink", "blood", "straining", "hesitancy"],
    "respiratory": ["cough", "phlegm", "sputum", "shortness of breath", "short of breath", "breathless", "wheeze", "wheezing", "congestion"],
    "cardiovascular": ["chest pain", "tightness", "pressure", "palpitation", "fluttering", "heart racing", "swollen ankles", "leg swelling", "angina"],
    "gastrointestinal": ["stomach pain", "belly", "nausea", "vomit", "vomiting", "heartburn", "diarrhea", "constipation", "bowel"],
    "neurologic": ["headache", "dizzy", "dizziness", "numb", "numbness", "weakness", "balance", "trouble walking", "tingling", "stroke"],
    "musculoskeletal": ["joint", "knee", "back pain", "hip", "shoulder", "stiffness", "arthritis", "neck pain", "muscle", "sciatica"],
    "ophthalmic": ["eye", "vision", "blur", "blurry", "double vision", "flashes", "floaters", "eye pain", "red eye"],
    "oral_dental": ["tooth", "teeth", "dental", "gums", "bleeding gums", "mouth sore", "chewing", "swallowing"],
    "dermatology": ["rash", "mole", "itching", "hives", "blister", "skin lesion", "eczema", "psoriasis"],
    "endocrinology": ["thirsty", "excessive thirst", "thyroid", "blood sugar", "cold intolerance", "heat intolerance", "hormone"],
    "hematology": ["bruising", "easy bruising", "bleeding gums", "nosebleed", "anemia", "pale skin", "swollen lymph"],
    "infectious_disease": ["tick bite", "travel", "chills", "persistent fever", "night sweat", "infection", "animal bite"],
    "psychiatry": ["depressed", "depression", "anxiety", "anxious", "panic", "hopeless", "ptsd", "suicidal"],
    "general": ["fever", "chills", "sweats", "exhausted", "weight loss", "fatigue"],
    "lifestyle_diet": ["loss of appetite", "not eating", "poor diet", "fluid restriction"],
    "social_history": ["smoke", "smoking", "cigarettes", "vape", "tobacco", "alcohol", "drinking", "drugs"],
    "family_history": ["father had", "mother had", "family history", "runs in my family", "brother had", "sister had"],
}

NORMAL_NEGATIVE_INDICATORS = [
    "no", "nope", "not really", "none", "nothing", "fine", "normal",
    "healthy", "all good", "no problems", "no issue", "negative", "never",
    "no fever", "no blood", "no cough", "no pain"
]


def _detect_abnormality(text: str, default_system: str | None = None) -> tuple[bool, str | None]:
    """Evaluates whether a patient response indicates an abnormality or normal baseline."""
    cleaned = text.strip().lower()
    if not cleaned:
        return False, default_system

    # Explicit normal or negative responses without an affirmative 'but'
    explicit_normal = any(neg in cleaned for neg in [
        "no fever", "no chills", "no cough", "no pain", "no blood", "no issues",
        "no problems", "all good", "feeling fine", "feels fine", "everything is normal",
        "everything feels normal", "normal", "healthy", "none of that", "not really"
    ])
    has_adversative = any(but in cleaned for but in ["but ", "however ", "actually ", "except "])

    if explicit_normal and not has_adversative:
        return False, default_system

    # Check for specific system symptom mentions
    matched_system = None
    for sys, kws in SYSTEM_KEYWORDS.items():
        if any(kw in cleaned for kw in kws):
            matched_system = sys
            break

    # If general negative indicator and no specific symptom
    is_negation = any(cleaned == neg or cleaned.startswith(f"{neg} ") or cleaned.endswith(f" {neg}") for neg in ["no", "nope", "none", "nothing", "negative", "never"])
    if is_negation and not matched_system:
        return False, default_system

    if matched_system:
        return True, matched_system

    if any(pos in cleaned for pos in ["yes", "yeah", "it does", "i do", "problem", "hurts", "pain", "bad"]):
        return True, default_system

    return False, default_system


async def route_patient_hierarchical(
    request: RoutingRequest,
    client: AsyncTypeSafeClient | None = None,
) -> RoutingResponse:
    """
    Adaptive Complete History Engine:
    Continuously conducts broad Level 1 screening across body systems (general, urinary,
    respiratory, cardio, neuro, eye, dental, diet, social, family).
    When an abnormality is indicated, immediately branches into a Level 2 Deep Characterization
    and Level 3 Red-Flag screen for that specific system before resuming general screening.
    """
    start_time = time.perf_counter()
    session_id = request.session_id
    chat_history = request.chat_history.strip()
    latest_input = (request.patient_response or chat_history).strip()

    asked_ids = get_asked_question_ids(session_id)
    session_history = get_session_history(session_id)

    screened_systems: dict[str, str] = {s: "pending" for s in COMPLETE_HISTORY_SYSTEMS}
    abnormal_systems: list[str] = []
    drilldown_counts: Counter[str] = Counter()

    # Reconstruct history of systems screened and abnormalities found
    for row in session_history:
        qid = row.get("question_asked_id")
        resp = row.get("patient_response") or ""
        if not qid:
            continue
        q_record = get_question_by_id(qid)
        if not q_record:
            continue
        q_sys = q_record.get("system", "")
        q_lvl = q_record.get("level", "")

        if q_lvl == "1_Chief_Complaint":
            is_abn, detected_sys = _detect_abnormality(resp, q_sys)
            target_sys = detected_sys or q_sys
            if is_abn:
                screened_systems[target_sys] = "abnormal"
                if target_sys not in abnormal_systems:
                    abnormal_systems.append(target_sys)
            else:
                if screened_systems.get(target_sys) != "abnormal":
                    screened_systems[target_sys] = "normal"
        else:
            drilldown_counts[q_sys] += 1

    # Evaluate current input for new abnormality
    is_abn_current, detected_sys_current = _detect_abnormality(latest_input, request.current_system)
    if is_abn_current and detected_sys_current:
        if detected_sys_current not in abnormal_systems:
            abnormal_systems.append(detected_sys_current)
        screened_systems[detected_sys_current] = "abnormal"

    # Handle explicit level override (for scenario / legacy tests)
    explicit_level = request.current_level
    active_system = request.current_system or "urinary"
    active_level = "1_Chief_Complaint"
    drilldown_active = False
    is_abnormal = False
    candidates: list[dict[str, Any]] = []

    if explicit_level:
        active_level = explicit_level
        candidates = get_questions_by_level(active_system, active_level, exclude_ids=asked_ids)

    # If no explicit level override, determine whether to drill down or screen next system
    if not candidates:
        # Check if any abnormal system requires Level 2 characterization or Level 3 red flag
        active_drilldown_sys = None
        for sys in abnormal_systems:
            if drilldown_counts[sys] >= 2:
                continue
            lvl2_available = get_questions_by_level(sys, "2_Characterization", exclude_ids=asked_ids)
            lvl3_available = get_questions_by_level(sys, "3_Red_Flag", exclude_ids=asked_ids)
            if lvl2_available or lvl3_available:
                active_drilldown_sys = sys
                break

        if active_drilldown_sys:
            active_system = active_drilldown_sys
            drilldown_active = True
            is_abnormal = True

            # Check if high risk indicators trigger Red Flag (Level 3)
            high_risk = _has_keyword(latest_input, ["can't pee", "cannot pee", "burst", "blood", "crushing", "drooping", "stroke", "severe"])
            lvl3_candidates = get_questions_by_level(active_system, "3_Red_Flag", exclude_ids=asked_ids)
            lvl2_candidates = get_questions_by_level(active_system, "2_Characterization", exclude_ids=asked_ids)

            if lvl3_candidates and (high_risk or drilldown_counts[active_system] >= 1):
                active_level = "3_Red_Flag"
                candidates = lvl3_candidates
            elif lvl2_candidates:
                active_level = "2_Characterization"
                candidates = lvl2_candidates
            elif lvl3_candidates:
                active_level = "3_Red_Flag"
                candidates = lvl3_candidates
        else:
            # Complete History Mode: Find next unscreened Level 1 system
            next_screen_sys = None
            for sys in COMPLETE_HISTORY_SYSTEMS:
                if screened_systems.get(sys) == "pending":
                    lvl1_avail = get_questions_by_level(sys, "1_Chief_Complaint", exclude_ids=asked_ids)
                    if lvl1_avail:
                        next_screen_sys = sys
                        break

            if next_screen_sys:
                active_system = next_screen_sys
                active_level = "1_Chief_Complaint"
                candidates = get_questions_by_level(next_screen_sys, "1_Chief_Complaint", exclude_ids=asked_ids)
            else:
                # All systems screened and all abnormalities investigated
                latency = int((time.perf_counter() - start_time) * 1000)
                return RoutingResponse(
                    session_id=session_id,
                    current_system=active_system,
                    current_level="Completed",
                    is_abnormal=len(abnormal_systems) > 0,
                    drilldown_active=False,
                    abnormal_systems=abnormal_systems,
                    screened_systems=screened_systems,
                    gate_satisfied=True,
                    next_question=None,
                    confidence=1.0,
                    rationale="Complete medical history and all indicated clinical drill-downs have been successfully conducted.",
                    is_red_flag=False,
                    completed=True,
                    latency_ms=latency,
                )

    # Evaluate candidate questions with Jev
    scores, confidence = await _evaluate_with_jev(client, chat_history, candidates)
    candidates.sort(key=lambda q: scores.get(q["id"], 0.0), reverse=True)
    best_candidate = candidates[0] if candidates else None

    # Populate top 3 questions
    top_questions = [QuestionBankItem(**q) for q in candidates[:3]]

    # Detect red flag alerts
    rf_items: list[QuestionBankItem] = []
    is_red_flag = False
    for q in candidates:
        if q.get("level") == "3_Red_Flag":
            score = scores.get(q["id"], 0.0)
            if score >= RED_FLAG_THRESHOLD or _has_keyword(latest_input, ["burst", "can't pee", "blood", "crushing", "drooping"]):
                is_red_flag = True
                rf_items.append(QuestionBankItem(**q))

    if best_candidate and best_candidate.get("level") == "3_Red_Flag":
        is_red_flag = True

    # Audit log the decision in patient_sessions
    if best_candidate:
        log_patient_session(
            session_id=session_id,
            question_asked_id=best_candidate["id"],
            patient_response=latest_input,
            jev_confidence_score=confidence,
        )

    latency = int((time.perf_counter() - start_time) * 1000)
    return RoutingResponse(
        session_id=session_id,
        current_system=active_system,
        current_level=active_level,
        is_abnormal=is_abnormal or (active_system in abnormal_systems),
        drilldown_active=drilldown_active,
        abnormal_systems=abnormal_systems,
        screened_systems=screened_systems,
        gate_satisfied=(active_level != "1_Chief_Complaint"),
        next_question=QuestionBankItem(**best_candidate) if best_candidate else None,
        questions=top_questions,
        red_flags=rf_items,
        confidence=confidence,
        rationale=best_candidate.get("rationale", "") if best_candidate else "No candidates remaining",
        is_red_flag=is_red_flag,
        completed=False,
        latency_ms=latency,
    )


# ============================================================================
# LEGACY BANK SUGGESTION ENGINE (Maintained for Backward Compatibility)
# ============================================================================

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
        "pee", "urine", "urinary", "bladder", "void", "stream",
        "frequency", "urgency", "nocturia", "incontinence", "dysuria",
        "burn", "pink", "red", "brown", "flank", "catheter",
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


def _make_suggestion(
    question: BankQuestion,
    *,
    score: float,
    confidence: float,
    is_red_flag: bool = False,
    label: str = "",
) -> Suggestion:
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
    except Exception:
        LOGGER.exception("Unexpected error from TypeSafe")
        degraded = True
        error_category = "unexpected_error"
        return _fallback_response(bank, start, degraded=True, low_confidence=False, error_category=error_category)

    raw_answers: dict[str, Any] = {}
    for response in response_batches:
        raw_answers.update(getattr(response, "answers", {}))

    red_flags: list[Suggestion] = []
    clarification_choice = raw_answers.get(clarification_key)
    clarification_winner = getattr(clarification_choice, "choice", "") if clarification_choice else ""
    clarification_confidence = _confidence_from_answer(clarification_choice)

    for red_flag in bank.red_flags:
        answer = raw_answers.get(f"red_flag__{red_flag.id}")
        probability = _noul_probability(answer)
        confidence = _confidence_from_answer(answer)
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
