"""Independent, read-only transcription of a live Hey Jarvis WAV for DJ Yakkuza.

The router sends the *recording*, not an asserted artist. This helper runs inside
DJ's turn while the owner callback still holds the temporary WAV open.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def transcribe_recording(path: str, *, model_factory=None) -> str:
    recording = Path(path).resolve(strict=True)
    if recording.suffix.lower() != ".wav" or recording.stat().st_size > 30_000_000:
        raise ValueError("Expected a short WAV recording")
    with recording.open("rb") as source:
        if source.read(4) != b"RIFF" or (source.read(8)[4:] != b"WAVE"):
            raise ValueError("Invalid WAV header")
    if model_factory is None:
        from faster_whisper import WhisperModel
        model_factory = WhisperModel
    model = model_factory("medium", device="cpu", compute_type="int8", local_files_only=True)
    segments, _ = model.transcribe(str(recording), language="en", vad_filter=True, beam_size=5)
    return " ".join(segment.text.strip() for segment in segments if segment.text.strip())


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("Usage: dj_audio.py /path/to/recording.wav", file=sys.stderr)
        return 2
    path = Path(argv[0]).resolve()
    voice_cache = (Path.home() / ".hermes" / "voice_cache").resolve()
    if path.parent != voice_cache or not path.name.startswith("tmp"):
        print("Recording must be a live voice-cache WAV", file=sys.stderr)
        return 2
    try:
        transcript = transcribe_recording(str(path))
    except (OSError, ValueError) as exc:
        print(f"Cannot transcribe recording: {exc}", file=sys.stderr)
        return 1
    print(transcript or "[No intelligible speech]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
