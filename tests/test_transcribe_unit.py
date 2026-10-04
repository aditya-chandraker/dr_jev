from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, transcribe
from app.models import TranscriptResponse, TranscriptTurn
from app.transcribe import TranscriptionError, build_exchanges, parse_turns, transcribe_encounter


@pytest.fixture(autouse=True)
def _reset_model_cooldowns():
    transcribe.MODEL_COOLDOWNS.clear()
    yield
    transcribe.MODEL_COOLDOWNS.clear()


def _gemini_payload(turns: list[dict[str, str]]) -> dict[str, object]:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps({"turns": turns})}]}}]}


def test_parse_turns_keeps_simplified_text_for_patient_turns_only() -> None:
    turns = parse_turns(
        _gemini_payload(
            [
                {"speaker": "physician", "text": "Any cough?", "simplified": "ignored"},
                {"speaker": "patient", "text": "Um, yeah, for like two weeks, mostly at night.", "simplified": "I have had a cough for two weeks."},
                {"speaker": "patient", "text": "No fever though.", "simplified": "I have not had a fever."},
            ]
        )
    )

    assert [(turn.speaker, turn.simplified) for turn in turns] == [
        ("physician", ""),
        ("patient", "I have had a cough for two weeks. I have not had a fever."),
    ]


def test_parse_turns_merges_consecutive_speakers_and_drops_blanks() -> None:
    turns = parse_turns(
        _gemini_payload(
            [
                {"speaker": "physician", "text": "What brings you in?"},
                {"speaker": "patient", "text": "I can't pee."},
                {"speaker": "patient", "text": "It started yesterday."},
                {"speaker": "physician", "text": "   "},
                {"speaker": "nurse", "text": "Vitals are done."},
            ]
        )
    )

    assert [(turn.speaker, turn.text) for turn in turns] == [
        ("physician", "What brings you in?"),
        ("patient", "I can't pee. It started yesterday."),
        ("other", "Vitals are done."),
    ]


def test_parse_turns_rejects_non_json_and_missing_candidates() -> None:
    with pytest.raises(TranscriptionError, match="not valid JSON"):
        parse_turns({"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})
    with pytest.raises(TranscriptionError, match="SAFETY"):
        parse_turns({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})


def test_build_exchanges_pairs_questions_with_the_following_patient_answer() -> None:
    turns = [
        TranscriptTurn(speaker="physician", text="Any fever?"),
        TranscriptTurn(speaker="patient", text="No."),
        TranscriptTurn(speaker="physician", text="Any burning?"),
        TranscriptTurn(speaker="other", text="Sorry to interrupt."),
        TranscriptTurn(speaker="physician", text="Last question, any blood?"),
    ]

    assert [(item.question, item.answer) for item in build_exchanges(turns)] == [
        ("Any fever?", "No."),
        ("Any burning?", ""),
        ("Last question, any blood?", ""),
    ]


@pytest.mark.asyncio
async def test_transcribe_encounter_sends_audio_and_schema_to_gemini() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-goog-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_gemini_payload(
                [{"speaker": "physician", "text": "Any cough?"}, {"speaker": "patient", "text": "Yes, for weeks."}]
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transcript = await transcribe_encounter(b"RIFFfake", "audio/x-wav", http=http, api_key="test-key", model="gemini-test")

    body = seen["body"]
    inline = body["contents"][0]["parts"][0]["inline_data"]
    assert seen["url"].endswith("/models/gemini-test:generateContent")
    assert seen["key"] == "test-key"
    assert inline == {"mime_type": "audio/wav", "data": base64.b64encode(b"RIFFfake").decode()}
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert "simplified" in body["generationConfig"]["responseSchema"]["properties"]["turns"]["items"]["properties"]
    assert transcript.patient_statements == ["Yes, for weeks."]
    assert [(item.question, item.answer) for item in transcript.exchanges] == [("Any cough?", "Yes, for weeks.")]


@pytest.mark.asyncio
async def test_transcribe_encounter_retries_then_falls_back_on_overload() -> None:
    models_called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        models_called.append(model)
        if model == "primary":
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        if model == "retired":
            return httpx.Response(404, json={"error": {"message": "no longer available"}})
        return httpx.Response(200, json=_gemini_payload([{"speaker": "patient", "text": "It hurts."}]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transcript = await transcribe_encounter(
            b"RIFF", "audio/wav", http=http, api_key="k", model="primary", fallback_models=["retired", "backup"], retry_delay_s=0
        )

    assert models_called == ["primary", "primary", "retired", "backup"]
    assert transcript.model == "backup"


@pytest.mark.asyncio
async def test_live_mode_tries_each_model_once_and_skips_overloaded_model_next_time() -> None:
    models_called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        models_called.append(model)
        if model == "primary":
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        return httpx.Response(200, json=_gemini_payload([{"speaker": "patient", "text": "It hurts."}]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        for _ in range(2):
            await transcribe_encounter(
                b"RIFF", "audio/wav", http=http, api_key="k", model="primary", fallback_models=["backup"], live=True
            )

    assert models_called == ["primary", "backup", "backup"]


@pytest.mark.asyncio
async def test_live_passes_default_to_the_faster_live_model() -> None:
    models_called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        models_called.append(request.url.path.rsplit("/", 1)[-1].split(":")[0])
        return httpx.Response(200, json=_gemini_payload([]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await transcribe_encounter(b"RIFF", "audio/wav", http=http, api_key="k", live=True)
        await transcribe_encounter(b"RIFF", "audio/wav", http=http, api_key="k")

    assert models_called == [transcribe.GEMINI_LIVE_MODEL, transcribe.GEMINI_MODEL]


@pytest.mark.asyncio
async def test_retries_without_thinking_config_when_model_rejects_it() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "thinkingConfig" in body["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "Thinking level LOW is not supported for this model."}})
        return httpx.Response(200, json=_gemini_payload([{"speaker": "patient", "text": "Fine."}]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transcript = await transcribe_encounter(b"RIFF", "audio/wav", http=http, api_key="k", fallback_models=[], live=True)

    assert len(bodies) == 2
    assert transcript.patient_statements == ["Fine."]


@pytest.mark.asyncio
async def test_transcribe_encounter_does_not_retry_client_errors() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(400, json={"error": {"message": "bad audio"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TranscriptionError, match="bad audio"):
            await transcribe_encounter(b"RIFF", "audio/wav", http=http, api_key="k", fallback_models=["backup"], retry_delay_s=0)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_transcribe_encounter_validates_input() -> None:
    with pytest.raises(TranscriptionError) as excinfo:
        await transcribe_encounter(b"data", "video/mp4", api_key="k")
    assert excinfo.value.status == 415
    with pytest.raises(TranscriptionError) as excinfo:
        await transcribe_encounter(b"", "audio/wav", api_key="k")
    assert excinfo.value.status == 400


def test_transcribe_endpoint_returns_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_transcribe(data: bytes, content_type: str | None, *, live: bool) -> TranscriptResponse:
        assert data == b"RIFFfake" and content_type == "audio/wav" and live is True
        return TranscriptResponse(
            turns=[TranscriptTurn(speaker="patient", text="My knee hurts.")],
            patient_statements=["My knee hurts."],
            model="gemini-test",
        )

    monkeypatch.setattr(main, "transcribe_encounter", fake_transcribe)
    with TestClient(main.app) as http:
        response = http.post(
            "/api/transcribe", files={"audio": ("encounter.wav", b"RIFFfake", "audio/wav")}, data={"live": "true"}
        )

    assert response.status_code == 200
    assert response.json()["patient_statements"] == ["My knee hurts."]


def test_transcribe_endpoint_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(data: bytes, content_type: str | None, *, live: bool) -> TranscriptResponse:
        raise TranscriptionError(415, "Unsupported audio type: video/mp4")

    monkeypatch.setattr(main, "transcribe_encounter", failing)
    with TestClient(main.app) as http:
        response = http.post("/api/transcribe", files={"audio": ("clip.mp4", b"x", "video/mp4")})

    assert response.status_code == 415
    assert "Unsupported" in response.json()["detail"]
