#!/usr/bin/env python3
"""Dry-test harness for the candidate-biased STT re-decode loop.

Captures a real mic phrase, then runs the SAME audio through two passes:
  pass 1: plain faster-whisper (this is what the live resolver does today)
  pass 2: re-decode biased with the music resolver's candidate hotwords
          (the proposed loop — re-listen to the audio with the listening
          vocabulary as lexical priors)

Only the raw .wav recording is written. Nothing is dispatched anywhere; this
is purely a demonstration for John to eyeball before touching the gateway.

Usage:
  python3 scripts/dry_redecode.py [--seconds 6] [--out PATH]
Then, to replay an existing recording through the passes instead of recording:
  python3 scripts/dry_redecode.py --replay /path/to/file.wav
"""
import argparse
import json
import os
import sys
import tempfile
import wave

# Make the plugin package importable from the repo root (run from repo root).
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import numpy as np  # noqa: E402


def record(duration, out_path):
    """Record microphone audio to a 16 kHz mono wav (mirrors pipeline.record_audio)."""
    import sounddevice as sd

    sr = 16000
    audio = sd.rec(int(duration * sr), samplerate=sr, channels=1, dtype="float32")
    sd.wait()
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    # Apply the same 8x software gain the gateway now uses, so the demo matches prod.
    if peak > 0.0 and peak < 0.9:
        audio = audio * min(8.0, 0.9 / peak)
    samples = np.rint(np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    return rms


def load_candidate_hotword_candidates():
    """The vocab-first repair pool the live resolver would offer — reused verbatim."""
    from plugins.voice_stack import music

    return music._repair_candidates(["Tove Lo featuring Stromae"], limit=12)


def run_pass(stt, path, hotwords=None, prompt=None):
    res = stt.transcribe_with_confidence(
        path, language="en", hotwords=hotwords, initial_prompt=prompt
    )
    return res.get("text", "").strip(), float(res.get("confidence", 0.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--out", default=None, help="where to save the recording")
    ap.add_argument("--replay", default=None, help="run passes on an existing wav instead of recording")
    ap.add_argument("--model", default="large-v3", help="faster-whisper model size")
    args = ap.parse_args()

    from plugins.voice_stack.engines.stt import FasterWhisperEngine

    stt = FasterWhisperEngine(model_size=args.model, device="auto", compute_type="int8")

    recording = args.replay
    rms = None
    if not recording:
        tmpdir = tempfile.mkdtemp(prefix="dry_redcode_")
        recording = args.out or os.path.join(tmpdir, "capture.wav")
        print(f"[recording {args.seconds}s] ... speak NOW")
        rms = record(args.seconds, recording)
        print(f"[saved {recording} rms={rms:.4f}]")
    else:
        print(f"[replay {recording}]")

    print("\n--- pass 1: plain decoding (today's behavior) ---")
    p1_text, p1_conf = run_pass(stt, recording)
    print(f"  transcript : {p1_text!r}")
    print(f"  confidence : {p1_conf:.3f}")

    candidates = load_candidate_hotword_candidates()
    hotwords = ", ".join(candidates[:8])
    print(f"\n--- candidate hotwords (vocab-first pool) ---")
    print(f"  {hotwords}")

    print("\n--- pass 2: re-decode with candidate hotwords ---")
    p2_text, p2_conf = run_pass(stt, recording, hotwords=hotwords, prompt=hotwords)
    print(f"  transcript : {p2_text!r}")
    print(f"  confidence : {p2_conf:.3f}")

    print("\n=== verdict ===")
    print(f"  pass1: {p1_text!r}  conf {p1_conf:.3f}")
    print(f"  pass2: {p2_text!r}  conf {p2_conf:.3f}")
    if rms is not None:
        print(f"  audio rms: {rms:.4f}")


if __name__ == "__main__":
    main()