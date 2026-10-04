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

from app.models import Exchange, TranscriptResponse, TranscriptTurn


load_dotenv()

GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-flash-latest").split(",") if m.strip()]
ATTEMPTS_PER_MODEL = 2
RETRYABLE_STATUSES = {429, 500, 503, 504}
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
- If there is no intelligible speech, return an empty list of turns."""

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
                },
                "required": ["speaker", "text"],
            },
        }
    },
    "required": ["turns"],
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
        if turns and turns[-1].speaker == speaker:
            turns[-1] = TranscriptTurn(speaker=speaker, text=f"{turns[-1].text} {spoken}")
        else:
            turns.append(TranscriptTurn(speaker=speaker, text=spoken))
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
    model: str = GEMINI_MODEL,
    fallback_models: list[str] | None = None,
    retry_delay_s: float = 1.0,
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
    start = time.perf_counter()
    models = list(dict.fromkeys([model, *(fallback_models if fallback_models is not None else GEMINI_FALLBACK_MODELS)]))
    client = http or httpx.AsyncClient(timeout=120.0)
    try:
        payload, used_model = await _generate_with_fallback(client, key, body, models, retry_delay_s)
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


async def _generate_with_fallback(
    client: httpx.AsyncClient,
    key: str,
    body: dict[str, Any],
    models: list[str],
    retry_delay_s: float,
) -> tuple[dict[str, Any], str]:
    last_error: TranscriptionError | None = None
    for model in models:
        for attempt in range(ATTEMPTS_PER_MODEL):
            try:
                response = await client.post(
                    f"{GEMINI_BASE_URL}/models/{model}:generateContent",
                    headers={"x-goog-api-key": key},
                    json=body,
                )
            except httpx.HTTPError as exc:
                last_error = TranscriptionError(502, f"Could not reach Gemini: {exc.__class__.__name__}")
            else:
                if response.is_success:
                    return response.json(), model
                try:
                    message = response.json().get("error", {}).get("message", response.text)
                except ValueError:
                    message = response.text
                last_error = TranscriptionError(
                    429 if response.status_code == 429 else 502,
                    f"Gemini error {response.status_code} ({model}): {message}",
                )
                if response.status_code == 404:
                    LOGGER.info("Gemini model %s is unavailable; trying the next model", model)
                    break
                if response.status_code not in RETRYABLE_STATUSES:
                    raise last_error
            LOGGER.info("Gemini %s attempt %d failed: %s", model, attempt + 1, last_error.message)
            if attempt + 1 < ATTEMPTS_PER_MODEL:
                await asyncio.sleep(retry_delay_s * (attempt + 1))
    assert last_error is not None
    raise last_error
