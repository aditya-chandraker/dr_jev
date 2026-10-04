"""Live check of the Gemini transcription path.

Usage:
    python scripts/smoke_transcribe.py path/to/audio.wav

Record a short doctor/patient exchange (wav, mp3, ogg, flac, aac, or webm) and
pass its path.
"""
from __future__ import annotations

import asyncio
import mimetypes
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.transcribe import transcribe_encounter  # noqa: E402


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/smoke_transcribe.py path/to/audio.wav")
        return
    path = Path(sys.argv[1])
    audio, mime = path.read_bytes(), mimetypes.guess_type(path.name)[0] or "audio/wav"
    print("file", path, len(audio), "bytes", mime)
    async with httpx.AsyncClient(timeout=120.0) as http:
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
