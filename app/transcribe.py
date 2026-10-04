from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv

from app.models import Exchange, SimplifyResponse, TranscriptResponse, TranscriptTurn


load_dotenv()

GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.5-flash")
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-flash-latest").split(",") if m.strip()]
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "low").strip()
ATTEMPTS_PER_MODEL = 2
RETRYABLE_STATUSES = {429, 500, 503, 504}
MODEL_COOLDOWN_S = float(os.getenv("GEMINI_MODEL_COOLDOWN_S", "60"))
MODEL_COOLDOWNS: dict[str, float] = {}
LOGGER = logging.getLogger(__name__)
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(15 * 1024 * 1024)))
SUPPORTED_AUDIO_TYPES = {
    "audio/wav", "audio/x-wav", "audio/wave", "audio/mp3", "audio/mpeg", "audio/aiff",
    "audio/aac", "audio/ogg", "audio/flac", "audio/webm",
}
SPEAKERS = ("physician", "patient", "other")

PROMPT = """This audio is a recording from a clinical interview between a physician and a patient.
Transcribe it into speaker turns, in the order they were spoken.

Label each turn by conversational role, not by voice alone:
- "physician": the clinician asking questions, explaining, or examining.
- "patient": the person describing their own symptoms, history, or answering the physician's questions. A companion speaking for the patient also counts as "patient".
- "other": anyone else, or speech you cannot attribute.

Rules:
- Transcribe what was said faithfully. Do not summarize, translate, or add medical interpretation.
- Drop pure filler ("um", "uh") but keep the meaning and the patient's own words.
- Start a new turn whenever the speaker changes.
- If there is no intelligible speech, return an empty list of turns.

For every "patient" turn, also fill "simplified": the patient's answer restated as one or two short, plain sentences in the patient's first person. Keep every clinical detail they gave (symptoms, timing, duration, severity, triggers, and anything they deny), drop rambling and repetition, and add nothing they did not say. Leave "simplified" empty for other turns."""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "turns": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "speaker": {"type": "STRING", "enum": list(SPEAKERS)},
                    "text": {"type": "STRING"},
                    "simplified": {"type": "STRING"},
                },
                "required": ["speaker", "text"],
            },
        }
    },
    "required": ["turns"],
}


SIMPLIFY_PROMPT = """A patient in a clinical interview gave the answer below. Restate it as one or two short, plain sentences in the patient's first person. Keep every clinical detail they gave (symptoms, timing, duration, severity, triggers, and anything they deny), drop rambling and repetition, and add nothing they did not say.

Physician's question (may be empty): {question}
Patient's answer: {answer}"""

SIMPLIFY_SCHEMA = {
    "type": "OBJECT",
    "properties": {"simplified": {"type": "STRING"}},
    "required": ["simplified"],
}


class TranscriptionError(Exception):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def normalize_mime_type(content_type: str | None) -> str:
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime not in SUPPORTED_AUDIO_TYPES:
        raise TranscriptionError(415, f"Unsupported audio type: {content_type or 'missing'}")
    return {"audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/mpeg": "audio/mp3"}.get(mime, mime)


def parse_turns(payload: dict[str, Any]) -> list[TranscriptTurn]:
    candidates = payload.get("candidates") or []
    if not candidates:
        reason = (payload.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
        raise TranscriptionError(502, f"Gemini returned no transcript ({reason})")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts)
    try:
        body = json.loads(text) if text.strip() else {"turns": []}
    except json.JSONDecodeError as exc:
        raise TranscriptionError(502, "Gemini returned a transcript that is not valid JSON") from exc
    turns: list[TranscriptTurn] = []
    for item in body.get("turns") or []:
        spoken = str(item.get("text") or "").strip()
        if not spoken:
            continue
        speaker = item.get("speaker") if item.get("speaker") in SPEAKERS else "other"
        simplified = str(item.get("simplified") or "").strip() if speaker == "patient" else ""
        if turns and turns[-1].speaker == speaker:
            previous = turns[-1]
            turns[-1] = TranscriptTurn(
                speaker=speaker,
                text=f"{previous.text} {spoken}",
                simplified=" ".join(part for part in (previous.simplified, simplified) if part),
            )
        else:
            turns.append(TranscriptTurn(speaker=speaker, text=spoken, simplified=simplified))
    return turns


def build_exchanges(turns: list[TranscriptTurn]) -> list[Exchange]:
    """Pair each physician turn with the patient turn that answers it."""
    exchanges: list[Exchange] = []
    for index, turn in enumerate(turns):
        if turn.speaker != "physician":
            continue
        following = turns[index + 1] if index + 1 < len(turns) else None
        answer = following.text if following is not None and following.speaker == "patient" else ""
        exchanges.append(Exchange(question=turn.text, answer=answer))
    return exchanges


async def transcribe_encounter(
    audio: bytes,
    content_type: str | None,
    *,
    http: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    model: str | None = None,
    fallback_models: list[str] | None = None,
    retry_delay_s: float = 1.0,
    live: bool = False,
    thinking_level: str = GEMINI_THINKING_LEVEL,
) -> TranscriptResponse:
    key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise TranscriptionError(503, "GEMINI_API_KEY is not configured")
    if not audio:
        raise TranscriptionError(400, "The audio upload is empty")
    if len(audio) > MAX_AUDIO_BYTES:
        raise TranscriptionError(413, f"Audio is larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
    mime_type = normalize_mime_type(content_type)

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(audio).decode("ascii")}},
                    {"text": PROMPT},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }
    if thinking_level:
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": thinking_level}
    start = time.perf_counter()
    primary = model or (GEMINI_LIVE_MODEL if live else GEMINI_MODEL)
    fallbacks = fallback_models if fallback_models is not None else [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]
    models = list(dict.fromkeys([primary, *fallbacks]))
    client = http or httpx.AsyncClient(timeout=120.0)
    try:
        payload, used_model = await _generate_with_fallback(
            client, key, body, models, retry_delay_s, attempts=1 if live else ATTEMPTS_PER_MODEL
        )
    finally:
        if http is None:
            await client.aclose()

    turns = parse_turns(payload)
    return TranscriptResponse(
        turns=turns,
        patient_statements=[turn.text for turn in turns if turn.speaker == "patient"],
        exchanges=build_exchanges(turns),
        model=used_model,
        latency_ms=int((time.perf_counter() - start) * 1000),
    )


async def simplify_answer(
    answer: str,
    question: str = "",
    *,
    http: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    thinking_level: str = GEMINI_THINKING_LEVEL,
) -> SimplifyResponse:
    key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise TranscriptionError(503, "GEMINI_API_KEY is not configured")
    body: dict[str, Any] = {
        "contents": [
            {"role": "user", "parts": [{"text": SIMPLIFY_PROMPT.format(question=question.strip(), answer=answer.strip())}]}
        ],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseSchema": SIMPLIFY_SCHEMA},
    }
    if thinking_level:
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": thinking_level}
    start = time.perf_counter()
    models = list(dict.fromkeys([GEMINI_LIVE_MODEL, GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]))
    client = http or httpx.AsyncClient(timeout=30.0)
    try:
        payload, used_model = await _generate_with_fallback(client, key, body, models, 0.0, attempts=1)
    finally:
        if http is None:
            await client.aclose()
    parts = ((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    try:
        simplified = str(json.loads("".join(part.get("text", "") for part in parts)).get("simplified") or "").strip()
    except (json.JSONDecodeError, AttributeError) as exc:
        raise TranscriptionError(502, "Gemini returned a simplification that is not valid JSON") from exc
    return SimplifyResponse(
        simplified=simplified or answer.strip(),
        model=used_model,
        latency_ms=int((time.perf_counter() - start) * 1000),
    )


async def _generate_with_fallback(
    client: httpx.AsyncClient,
    key: str,
    body: dict[str, Any],
    models: list[str],
    retry_delay_s: float,
    attempts: int = ATTEMPTS_PER_MODEL,
) -> tuple[dict[str, Any], str]:
    now = time.monotonic()
    ordered = sorted(models, key=lambda name: MODEL_COOLDOWNS.get(name, 0.0) > now)
    last_error: TranscriptionError | None = None
    for model in ordered:
        model_body = body
        attempt = 0
        while attempt < attempts:
            try:
                response = await client.post(
                    f"{GEMINI_BASE_URL}/models/{model}:generateContent",
                    headers={"x-goog-api-key": key},
                    json=model_body,
                )
            except httpx.HTTPError as exc:
                last_error = TranscriptionError(502, f"Could not reach Gemini: {exc.__class__.__name__}")
            else:
                if response.is_success:
                    MODEL_COOLDOWNS.pop(model, None)
                    return response.json(), model
                try:
                    message = response.json().get("error", {}).get("message", response.text)
                except ValueError:
                    message = response.text
                last_error = TranscriptionError(
                    429 if response.status_code == 429 else 502,
                    f"Gemini error {response.status_code} ({model}): {message}",
                )
                if response.status_code == 400 and "thinking" in message.lower() and "thinkingConfig" in model_body["generationConfig"]:
                    LOGGER.info("Gemini model %s rejected the thinking setting; retrying without it", model)
                    model_body = {**model_body, "generationConfig": {k: v for k, v in model_body["generationConfig"].items() if k != "thinkingConfig"}}
                    continue
                if response.status_code == 404:
                    LOGGER.info("Gemini model %s is unavailable; trying the next model", model)
                    break
                if response.status_code not in RETRYABLE_STATUSES:
                    raise last_error
                MODEL_COOLDOWNS[model] = time.monotonic() + MODEL_COOLDOWN_S
            attempt += 1
            LOGGER.info("Gemini %s attempt %d failed: %s", model, attempt, last_error.message)
            if attempt < attempts:
                await asyncio.sleep(retry_delay_s * attempt)
    assert last_error is not None
    raise last_error
