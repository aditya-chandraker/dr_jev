from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter
from typing import Any, Iterable

from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score, TypeSafeAPIError

from app.bank import Bank, BankQuestion, BankRegistry, DomainInfo, RedFlag, get_qualified_question_map, load_registry
from app.models import PatientInput, RoutedDomain, RoutingInfo, SuggestResponse, Suggestion


load_dotenv()

LOGGER = logging.getLogger(__name__)
ROUTER_MIN_PRIMARY = float(os.getenv("ROUTER_MIN_PRIMARY", "0.30"))
ROUTER_THRESHOLD = float(os.getenv("ROUTER_THRESHOLD", "0.50"))
ROUTER_MAX_BANKS = int(os.getenv("ROUTER_MAX_BANKS", "2"))
GLOBAL_RED_FLAG_SWEEP = os.getenv("GLOBAL_RED_FLAG_SWEEP", "true").strip().lower() not in {"0", "false", "no", "off"}
RED_FLAG_THRESHOLD = float(os.getenv("RED_FLAG_THRESHOLD", "0.30"))
RED_FLAG_MAX = int(os.getenv("RED_FLAG_MAX", "3"))
LOW_CONF_THRESHOLD = float(os.getenv("LOW_CONF_THRESHOLD", "0.35"))
MAX_QUESTIONS_PER_REQUEST = int(os.getenv("MAX_QUESTIONS_PER_REQUEST", "400"))


def _asked_qids(asked: list[dict[str, Any]], registry: BankRegistry) -> set[str]:
    qmap = get_qualified_question_map(registry)
    urinary = registry.banks.get("urinary")
    urinary_qids = {question.id: question.qid for question in urinary.questions} if urinary is not None else {}

    asked_qids: set[str] = set()
    for item in asked:
        if not isinstance(item, dict):
            continue
        raw = item.get("id") or item.get("question_id")
        if raw is None:
            continue
        qid = str(raw)
        if ":" in qid:
            if qid in qmap:
                asked_qids.add(qid)
            continue
        if qid in urinary_qids:
            asked_qids.add(urinary_qids[qid])
    return asked_qids


def _build_state(payload: PatientInput) -> dict[str, Any]:
    state: dict[str, Any] = {
        "patient_statements": payload.patient_statements,
        "demographics": payload.demographics,
        "already_asked": payload.asked,
    }
    if payload.health_record:
        state["health_record"] = payload.health_record
    return state


def _chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _dump_answer(answer: Any) -> Any:
    if hasattr(answer, "model_dump"):
        return answer.model_dump()
    return answer


def _score_question(question: BankQuestion) -> Score:
    instructions = (
        f"Given what the patient has said so far in `patient_statements`, their chart in `health_record` if present, "
        f"and what is already in `already_asked`, "
        f"how useful would it be for the clinician to ask next: '{question.text}' (purpose: {question.rationale})?"
    )
    return Score(
        instructions=instructions,
        criteria=[
            "Not useful now: already answered or documented in the chart, or unrelated to what the patient described.",
            "Somewhat useful: could add context but is not a priority.",
            "Very useful now: directly narrows the likely causes or screens for something important given what was said.",
        ],
    )


def _domain_signal_question(info: DomainInfo) -> Noul:
    return Noul(
        instructions=(
            f"Is the patient describing a symptom or problem involving {info.router_description}? "
            "Answer yes if follow-up questions from this area would be appropriate, even if other areas are also involved."
        )
    )


def _red_flag_question(red_flag: RedFlag) -> Noul:
    return Noul(instructions=red_flag.detection_instructions)


def _merge_response_fields(response: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    merged.update(getattr(response, "nouls", {}) or {})
    merged.update(getattr(response, "choices", {}) or {})
    merged.update(getattr(response, "scores", {}) or {})
    return merged


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
        question_id=question.qid,
        bank=question.bank_id,
        text=question.text,
        area=question.area,
        rationale=question.rationale,
        score=score,
        confidence=confidence,
        is_red_flag=is_red_flag,
        label=label,
    )


def _keyword_route(statements: list[str], registry: BankRegistry) -> tuple[str | None, float]:
    haystack = " ".join(statements).lower()
    best_id: str | None = None
    best_hits = 0
    for domain_id, info in registry.domains.items():
        hits = sum(1 for keyword in info.keywords if keyword and keyword in haystack)
        if hits > best_hits:
            best_hits = hits
            best_id = domain_id
    if best_hits == 0:
        return None, 0.0
    return best_id, float(best_hits)


def _route(domain_probs: dict[str, float], registry: BankRegistry) -> RoutingInfo:
    ordered = list(registry.domains.values())
    scored = sorted(
        ordered,
        key=lambda info: (-domain_probs.get(info.id, 0.0), ordered.index(info)),
    )
    domains = [RoutedDomain(id=info.id, label=info.label, score=domain_probs.get(info.id, 0.0)) for info in scored]
    primary = scored[0].id if scored and domain_probs.get(scored[0].id, 0.0) >= ROUTER_MIN_PRIMARY else None
    return RoutingInfo(primary=primary, domains=domains, unclear=primary is None, method="llm")


def _selected_bank_ids(domain_probs: dict[str, float], registry: BankRegistry, routing: RoutingInfo) -> list[str]:
    if routing.primary is None:
        return []
    ordered = list(registry.domains.values())
    scored = sorted(
        ordered,
        key=lambda info: (-domain_probs.get(info.id, 0.0), ordered.index(info)),
    )
    active: list[str] = [routing.primary]
    for info in scored:
        if info.id == routing.primary:
            continue
        if domain_probs.get(info.id, 0.0) < ROUTER_THRESHOLD:
            continue
        active.append(info.id)
        if len(active) >= ROUTER_MAX_BANKS:
            break
    return active


async def _run_system_one_batches(client: Any, state: dict[str, Any], questions: dict[str, Any]) -> list[Any]:
    batches = list(_chunked(list(questions.items()), MAX_QUESTIONS_PER_REQUEST))
    if not batches:
        return []

    async def run_batch(batch: list[tuple[str, Any]]) -> Any:
        return await client.system_one(state=state, questions=dict(batch))

    return list(await asyncio.gather(*(run_batch(batch) for batch in batches)))


def _collect_red_flags(
    registry: BankRegistry,
    answers: dict[str, Any],
    asked_qids: set[str],
    bank_ids: list[str] | None,
) -> list[Suggestion]:
    qmap = get_qualified_question_map(registry)
    red_flags: list[tuple[float, int, int, Suggestion]] = []
    banks = [registry.banks[bank_id] for bank_id in bank_ids] if bank_ids is not None else list(registry.banks.values())

    for bank_index, bank in enumerate(banks):
        for rf_index, red_flag in enumerate(bank.red_flags):
            key = f"red_flag__{bank.bank_id}__{red_flag.id}"
            answer = answers.get(key)
            probability = _noul_probability(answer)
            confidence = _confidence_from_answer(answer)
            if probability > 0.1:
                LOGGER.info("Red flag %s probability=%.3f confidence=%.3f", key, probability, confidence)
            if probability < RED_FLAG_THRESHOLD:
                continue
            prompt_qid = red_flag.prompt_qid
            if prompt_qid in asked_qids:
                continue
            prompt_question = qmap.get(prompt_qid)
            if prompt_question is None:
                raise RuntimeError(f"Missing prompt question for red flag {red_flag.qid}")
            red_flags.append(
                (
                    probability,
                    bank_index,
                    rf_index,
                    _make_suggestion(
                        prompt_question,
                        score=probability,
                        confidence=confidence,
                        is_red_flag=True,
                        label="Red flag",
                    ),
                )
            )

    red_flags.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in red_flags[:RED_FLAG_MAX]]


def _bank_fallback(
    registry: BankRegistry,
    bank: Bank,
    asked_qids: set[str],
    routing: RoutingInfo,
    *,
    degraded: bool,
    low_confidence: bool,
    red_flags: list[Suggestion] | None = None,
) -> SuggestResponse:
    qmap = get_qualified_question_map(registry)
    suggestions: list[Suggestion] = []
    for raw_id in bank.fallback_checklist:
        qid = f"{bank.bank_id}:{raw_id}" if bank.bank_id else raw_id
        if qid in asked_qids:
            continue
        question = qmap.get(qid)
        if question is None:
            continue
        suggestions.append(_make_suggestion(question, score=0.0, confidence=0.0))
        if len(suggestions) >= 3:
            break
    return SuggestResponse(
        suggestions=suggestions,
        red_flags=red_flags or [],
        low_confidence=low_confidence,
        degraded=degraded,
        latency_ms=0,
        routing=routing,
    )


def _general_fallback(
    registry: BankRegistry,
    asked_qids: set[str],
    routing: RoutingInfo,
    *,
    degraded: bool,
    low_confidence: bool,
    red_flags: list[Suggestion] | None = None,
) -> SuggestResponse:
    return _bank_fallback(
        registry,
        registry.general,
        asked_qids,
        routing,
        degraded=degraded,
        low_confidence=low_confidence,
        red_flags=red_flags,
    )


def _selected_bank_fallback(
    registry: BankRegistry,
    selected_bank_ids: list[str],
    asked_qids: set[str],
    routing: RoutingInfo,
    *,
    degraded: bool,
    low_confidence: bool,
    red_flags: list[Suggestion] | None = None,
) -> SuggestResponse:
    if routing.primary and routing.primary in registry.banks:
        bank = registry.banks[routing.primary]
    elif selected_bank_ids:
        bank = registry.banks[selected_bank_ids[0]]
    else:
        bank = registry.general
    return _bank_fallback(
        registry,
        bank,
        asked_qids,
        routing,
        degraded=degraded,
        low_confidence=low_confidence,
        red_flags=red_flags,
    )


async def _suggest_questions_with_client(payload: PatientInput, client: Any) -> SuggestResponse:
    start = time.perf_counter()
    registry = load_registry()
    qmap = get_qualified_question_map(registry)
    asked_qids = _asked_qids(payload.asked, registry)

    if not any(statement.strip() for statement in payload.patient_statements):
        routing = RoutingInfo(primary=None, domains=[], unclear=True, method="none")
        return _general_fallback(registry, asked_qids, routing, degraded=False, low_confidence=True)

    state = _build_state(payload)
    stage1_questions: dict[str, Any] = {
        f"domain_signal__{info.id}": _domain_signal_question(info) for info in registry.domains.values()
    }
    if GLOBAL_RED_FLAG_SWEEP:
        for bank in [*registry.banks.values(), registry.general]:
            for red_flag in bank.red_flags:
                stage1_questions[f"red_flag__{bank.bank_id}__{red_flag.id}"] = _red_flag_question(red_flag)

    stage1_answers: dict[str, Any] = {}
    try:
        stage1_responses = await _run_system_one_batches(client, state, stage1_questions)
        for response in stage1_responses:
            stage1_answers.update(_merge_response_fields(response))
    except TypeSafeAPIError as exc:
        LOGGER.info("Stage 1 failed: %s", _categorize_error(exc.status))
        keyword_primary, _ = _keyword_route(payload.patient_statements, registry)
        if keyword_primary and keyword_primary in registry.banks:
            routing = RoutingInfo(primary=keyword_primary, domains=[RoutedDomain(id=keyword_primary, label=registry.domains[keyword_primary].label, score=1.0)], unclear=False, method="keyword")
            return _bank_fallback(registry, registry.banks[keyword_primary], asked_qids, routing, degraded=True, low_confidence=False)
        routing = RoutingInfo(primary=None, domains=[], unclear=True, method="none")
        return _general_fallback(registry, asked_qids, routing, degraded=True, low_confidence=False)
    except asyncio.TimeoutError:
        LOGGER.info("Stage 1 failed: timeout")
        keyword_primary, _ = _keyword_route(payload.patient_statements, registry)
        if keyword_primary and keyword_primary in registry.banks:
            routing = RoutingInfo(primary=keyword_primary, domains=[RoutedDomain(id=keyword_primary, label=registry.domains[keyword_primary].label, score=1.0)], unclear=False, method="keyword")
            return _bank_fallback(registry, registry.banks[keyword_primary], asked_qids, routing, degraded=True, low_confidence=False)
        routing = RoutingInfo(primary=None, domains=[], unclear=True, method="none")
        return _general_fallback(registry, asked_qids, routing, degraded=True, low_confidence=False)
    except Exception:
        LOGGER.exception("Stage 1 failed with unexpected error")
        keyword_primary, _ = _keyword_route(payload.patient_statements, registry)
        if keyword_primary and keyword_primary in registry.banks:
            routing = RoutingInfo(primary=keyword_primary, domains=[RoutedDomain(id=keyword_primary, label=registry.domains[keyword_primary].label, score=1.0)], unclear=False, method="keyword")
            return _bank_fallback(registry, registry.banks[keyword_primary], asked_qids, routing, degraded=True, low_confidence=False)
        routing = RoutingInfo(primary=None, domains=[], unclear=True, method="none")
        return _general_fallback(registry, asked_qids, routing, degraded=True, low_confidence=False)

    domain_probs = {domain_id: _noul_probability(stage1_answers.get(f"domain_signal__{domain_id}")) for domain_id in registry.domains}
    LOGGER.info("Domain probabilities: %s", domain_probs)
    routing = _route(domain_probs, registry)
    selected_bank_ids = _selected_bank_ids(domain_probs, registry, routing)
    LOGGER.info("Selected banks: %s", selected_bank_ids)

    if GLOBAL_RED_FLAG_SWEEP:
        red_flags = _collect_red_flags(registry, stage1_answers, asked_qids, None)
    else:
        red_flags = []

    if routing.unclear:
        latency_ms = int((time.perf_counter() - start) * 1000)
        response = _general_fallback(
            registry,
            asked_qids,
            routing,
            degraded=False,
            low_confidence=True,
            red_flags=red_flags,
        )
        response.latency_ms = latency_ms
        return response

    selected_banks = [registry.banks[bank_id] for bank_id in selected_bank_ids if bank_id in registry.banks]
    selected_red_flag_prompt_qids = {red_flag.prompt_qid for bank in selected_banks for red_flag in bank.red_flags}

    stage2_questions: dict[str, Any] = {}
    for bank in selected_banks:
        for question in bank.questions:
            if question.qid in asked_qids or question.qid in selected_red_flag_prompt_qids:
                continue
            stage2_questions[f"score__{bank.bank_id}__{question.id}"] = _score_question(question)
        if not GLOBAL_RED_FLAG_SWEEP:
            for red_flag in bank.red_flags:
                stage2_questions[f"red_flag__{bank.bank_id}__{red_flag.id}"] = _red_flag_question(red_flag)

    try:
        stage2_answers: dict[str, Any] = {}
        stage2_responses = await _run_system_one_batches(client, state, stage2_questions)
        for response in stage2_responses:
            stage2_answers.update(_merge_response_fields(response))
        if not GLOBAL_RED_FLAG_SWEEP:
            red_flags = _collect_red_flags(registry, stage2_answers, asked_qids, selected_bank_ids)
    except TypeSafeAPIError as exc:
        LOGGER.info("Stage 2 failed: %s", _categorize_error(exc.status))
        latency_ms = int((time.perf_counter() - start) * 1000)
        response = _selected_bank_fallback(
            registry,
            selected_bank_ids,
            asked_qids,
            routing,
            degraded=True,
            low_confidence=False,
            red_flags=red_flags,
        )
        response.latency_ms = latency_ms
        return response
    except asyncio.TimeoutError:
        LOGGER.info("Stage 2 failed: timeout")
        latency_ms = int((time.perf_counter() - start) * 1000)
        response = _selected_bank_fallback(
            registry,
            selected_bank_ids,
            asked_qids,
            routing,
            degraded=True,
            low_confidence=False,
            red_flags=red_flags,
        )
        response.latency_ms = latency_ms
        return response
    except Exception:
        LOGGER.exception("Stage 2 failed with unexpected error")
        latency_ms = int((time.perf_counter() - start) * 1000)
        response = _selected_bank_fallback(
            registry,
            selected_bank_ids,
            asked_qids,
            routing,
            degraded=True,
            low_confidence=False,
            red_flags=red_flags,
        )
        response.latency_ms = latency_ms
        return response

    raw_answers = {**stage1_answers, **stage2_answers}
    LOGGER.info("Raw TypeSafe answers: %s", {key: _dump_answer(value) for key, value in raw_answers.items()})

    candidates_by_raw_id: dict[str, tuple[float, float, Suggestion]] = {}
    for bank in selected_banks:
        domain_prob = domain_probs.get(bank.bank_id, 0.0)
        prompt_qids = {red_flag.prompt_qid for red_flag in bank.red_flags}
        for question in bank.questions:
            if question.qid in asked_qids or question.qid in prompt_qids:
                continue
            answer = stage2_answers.get(f"score__{bank.bank_id}__{question.id}")
            if answer is None:
                continue
            confidence = _confidence_from_answer(answer)
            score = _score_from_answer(answer) * (0.5 + 0.5 * confidence) * (0.6 + 0.4 * domain_prob)
            suggestion = _make_suggestion(question, score=score, confidence=confidence)
            existing = candidates_by_raw_id.get(question.id)
            if existing is None or (score, confidence) > (existing[0], existing[1]):
                candidates_by_raw_id[question.id] = (score, confidence, suggestion)

    candidates = sorted((item[2] for item in candidates_by_raw_id.values()), key=lambda item: (item.score, item.confidence), reverse=True)
    selected = _apply_diversity(candidates, limit=3)

    primary_prob = domain_probs.get(routing.primary or "", 0.0)
    top_confidence = max([item.confidence for item in red_flags + selected] or [0.0])
    low_confidence = (top_confidence < LOW_CONF_THRESHOLD and not red_flags) or (not red_flags and primary_prob < LOW_CONF_THRESHOLD)
    if low_confidence:
        latency_ms = int((time.perf_counter() - start) * 1000)
        response = _selected_bank_fallback(
            registry,
            selected_bank_ids,
            asked_qids,
            routing,
            degraded=False,
            low_confidence=True,
            red_flags=red_flags,
        )
        response.latency_ms = latency_ms
        return response

    latency_ms = int((time.perf_counter() - start) * 1000)
    return SuggestResponse(
        suggestions=selected,
        red_flags=red_flags,
        low_confidence=low_confidence,
        degraded=False,
        latency_ms=latency_ms,
        routing=routing,
    )


def _apply_diversity(items: list[Suggestion], limit: int) -> list[Suggestion]:
    selected: list[Suggestion] = []
    counts: Counter[tuple[str, str]] = Counter()
    for item in items:
        key = (item.bank, item.area)
        if counts[key] >= 2:
            continue
        selected.append(item)
        counts[key] += 1
        if len(selected) >= limit:
            break
    return selected


def _categorize_error(status: int | None) -> str:
    if status == 401 or status == 403:
        return "auth"
    if status == 429:
        return "rate_limit"
    if status and 500 <= status < 600:
        return "server_error"
    return "api_error"


async def suggest_questions(payload: PatientInput, client: Any | None = None) -> SuggestResponse:
    if client is None:
        async with AsyncTypeSafeClient() as active_client:
            return await _suggest_questions_with_client(payload, active_client)
    return await _suggest_questions_with_client(payload, client)
