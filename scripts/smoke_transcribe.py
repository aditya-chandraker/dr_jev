"""Live check of the Gemini transcription path.

Usage:
    python scripts/smoke_transcribe.py [path/to/audio.wav]
    python scripts/smoke_transcribe.py --save dialogue.mp3

Without a file, a short synthetic doctor/patient exchange is generated with
ElevenLabs text-to-speech (two different voices) and transcribed. --save also
writes that synthesized audio to disk.
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.transcribe import transcribe_encounter  # noqa: E402

ELEVENLABS_URL = "https://api.elevenlabs.io/v1"
DIALOGUE = [
    ("physician", "Hi, what brings you in today?"),
    ("patient", "I've had a cough for about three weeks, and I get winded climbing the stairs."),
    ("physician", "Are you bringing anything up when you cough, like phlegm or blood?"),
    ("patient", "Some yellow phlegm in the mornings. No blood that I've seen."),
]


VOICE_FOR = {
    "physician": os.getenv("SMOKE_PHYSICIAN_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb"),
    "patient": os.getenv("SMOKE_PATIENT_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
}


async def synthesize_dialogue(http: httpx.AsyncClient, api_key: str) -> bytes:
    audio = b""
    for speaker, text in DIALOGUE:
        response = await http.post(
            f"{ELEVENLABS_URL}/text-to-speech/{VOICE_FOR[speaker]}",
            headers={"xi-api-key": api_key},
            params={"output_format": "mp3_44100_128"},
            json={"text": text, "model_id": "eleven_multilingual_v2"},
        )
        if not response.is_success:
            raise SystemExit(f"ElevenLabs text-to-speech failed with {response.status_code}: {response.text[:300]}")
        audio += response.content
    return audio


async def main() -> None:
    args = sys.argv[1:]
    save_path = Path(args.pop(args.index("--save") + 1)) if "--save" in args else None
    if save_path is not None:
        args.remove("--save")
    async with httpx.AsyncClient(timeout=120.0) as http:
        if args:
            path = Path(args[0])
            audio, mime = path.read_bytes(), mimetypes.guess_type(path.name)[0] or "audio/wav"
            print("file", path, len(audio), "bytes", mime)
        else:
            api_key = os.getenv("ELEVENLABS_API_KEY", "").strip().strip('"')
            if not api_key:
                print("Pass an audio file, or set ELEVENLABS_API_KEY to synthesize one.")
                return
            audio, mime = await synthesize_dialogue(http, api_key), "audio/mp3"
            print("synthesized", len(audio), "bytes of dialogue")
            if save_path is not None:
                save_path.write_bytes(audio)
                print("saved", save_path)
        transcript = await transcribe_encounter(audio, mime, http=http)

    print("model", transcript.model, "latency_ms", transcript.latency_ms)
    for turn in transcript.turns:
        print(f"  {turn.speaker:>9}: {turn.text}")
    print("patient_statements", transcript.patient_statements)
    print("exchanges", [exchange.model_dump() for exchange in transcript.exchanges])


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    asyncio.run(main())
