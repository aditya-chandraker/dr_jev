from __future__ import annotations

import logging
import time

from typesafe_sdk import AsyncTypeSafeClient, Choice

from app.models import SpeakerResponse, TranscriptTurn

LOGGER = logging.getLogger(__name__)
HISTORY_TURNS = 6
SPEAKER_QUESTION = Choice(
    instructions=(
        "Who most likely said the latest utterance in this clinical interview between a physician and a patient, "
        "given the labeled utterances before it?"
    ),
    criteria={
        "physician": "The clinician: asks about symptoms or history, explains, examines, or gives instructions.",
        "patient": "The patient or a companion speaking for them: describes their own symptoms or history, or answers the physician.",
        "other": "Anyone else, or speech that cannot be attributed.",
    },
)


def heuristic_speaker(text: str) -> str:
    return "physician" if text.rstrip().endswith("?") else "patient"


async def classify_speaker(
    text: str, history: list[TranscriptTurn], client: AsyncTypeSafeClient | None
) -> SpeakerResponse:
    start = time.perf_counter()
    if client is not None:
        state = {
            "previous_utterances": [f"{turn.speaker}: {turn.text}" for turn in history[-HISTORY_TURNS:]],
            "latest_utterance": text,
        }
        try:
            response = await client.system_one(state=state, questions={"speaker": SPEAKER_QUESTION})
            answer = response.answers["speaker"]
            return SpeakerResponse(
                speaker=answer.choice,
                confidence=answer.confidence,
                method="jev",
                latency_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as exc:
            LOGGER.warning("Jev speaker classification failed, using heuristic: %s", exc.__class__.__name__)
    return SpeakerResponse(
        speaker=heuristic_speaker(text),
        confidence=0.5,
        method="heuristic",
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
