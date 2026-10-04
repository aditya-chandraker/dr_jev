from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import SimplifyResponse, TranscriptTurn
from app.speaker import classify_speaker, heuristic_speaker
from app.transcribe import TranscriptionError, simplify_answer
from tests.fakes import FakeClient


class FailingClient:
    async def system_one(self, **kwargs):
        raise RuntimeError("TypeSafe is down")


@pytest.mark.asyncio
async def test_classify_speaker_asks_jev_with_labeled_history() -> None:
    client = FakeClient({"speaker": "patient"})
    history = [TranscriptTurn(speaker="physician", text=f"Question {index}?") for index in range(8)]

    result = await classify_speaker("It started last week.", history, client)

    assert (result.speaker, result.confidence, result.method) == ("patient", 1.0, "jev")
    state = client.calls[0]["state"]
    assert state["latest_utterance"] == "It started last week."
    assert state["previous_utterances"] == [f"physician: Question {index}?" for index in range(2, 8)]
    assert set(client.calls[0]["questions"]["speaker"].criteria) == {"physician", "patient", "other"}


@pytest.mark.asyncio
async def test_classify_speaker_falls_back_to_heuristic() -> None:
    for client in (None, FailingClient()):
        result = await classify_speaker("Any fever?", [], client)
        assert (result.speaker, result.method) == ("physician", "heuristic")
    assert heuristic_speaker("No, not really.") == "patient"


def test_speaker_endpoint_uses_app_client() -> None:
    with TestClient(main.app) as http:
        main.app.state.typesafe_client = FakeClient({"speaker": "physician"})
        response = http.post(
            "/api/speaker",
            json={"text": "Does it hurt when you breathe in?", "history": [{"speaker": "patient", "text": "My chest hurts."}]},
        )
        empty = http.post("/api/speaker", json={"text": ""})

    assert response.status_code == 200
    assert response.json()["speaker"] == "physician"
    assert empty.status_code == 422


@pytest.mark.asyncio
async def test_simplify_answer_sends_question_and_answer_as_text() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        text = json.dumps({"simplified": "I have had a cough for two weeks."})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await simplify_answer("Um, like two weeks, this cough", "How long have you had the cough?", http=http, api_key="k")

    prompt = seen["body"]["contents"][0]["parts"][0]["text"]
    assert "How long have you had the cough?" in prompt and "Um, like two weeks, this cough" in prompt
    assert "inline_data" not in json.dumps(seen["body"])
    assert result.simplified == "I have had a cough for two weeks."


def test_simplify_endpoint_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(text: str, question: str) -> SimplifyResponse:
        return SimplifyResponse(simplified=f"{text}!", model="gemini-test", latency_ms=5)

    async def failing(text: str, question: str) -> SimplifyResponse:
        raise TranscriptionError(503, "GEMINI_API_KEY is not configured")

    with TestClient(main.app) as http:
        monkeypatch.setattr(main, "simplify_answer", ok)
        good = http.post("/api/simplify", json={"text": "No fever", "question": "Any fever?"})
        monkeypatch.setattr(main, "simplify_answer", failing)
        bad = http.post("/api/simplify", json={"text": "No fever"})

    assert good.json()["simplified"] == "No fever!"
    assert bad.status_code == 503
