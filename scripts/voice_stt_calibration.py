#!/usr/bin/env python3
"""voice_stt_calibration — prove the STT stage hears what it should.

Synthesises a fixed battery of test words/phrases with the *same* TTS voice
the pipeline speaks with (edge-tts), converts to the exact audio format the
recorder produces (16 kHz mono PCM16 WAV), then transcribes each clip with
the plugin's FasterWhisperEngine using the live gateway configuration.

It answers three questions:

  1. Positive phrases — are commands transcribed exactly, and with what
     confidence? (run at several gain levels to simulate mic distance)
  2. Negative clips — do silence / noise stay silent, or hallucinate
     ("Thanks for watching!", "I'm Chris.", ...)?
  3. Option sweeps (--sweep) — which transcribe options fix the failures?

Usage:
    python3 scripts/voice_stt_calibration.py                # baseline, live config
    python3 scripts/voice_stt_calibration.py --sweep        # compare candidate options
    python3 scripts/voice_stt_calibration.py --gains 1.0    # single gain level
    python3 scripts/voice_stt_calibration.py --keep-audio   # keep wavs for listening

Stdlib + the gateway venv (faster-whisper, edge-tts, numpy) + ffmpeg.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugins"))

import numpy as np  # noqa: E402

# ---------------------------------------------------------------------------
# Live gateway configuration (source of truth: `cat /proc/<gateway>/environ`)
# ---------------------------------------------------------------------------

LIVE_CONFIG = {
    "model_size": "small.en",
    "language": "en",
    "initial_prompt": (
        "Home Assistant voice commands include: turn on CNN; run CNN; "
        "turn on LCI; turn on TF1; turn on France 2; turn on SVT1; "
        "turn on SVT2; start Netflix; start YouTube."
    ),
    "hotwords": (
        "CNN, LCI, TF1, France 2, SVT1, SVT2, ABC News, CBS News, "
        "Firedroid, Fire TV, Shield, Netflix, YouTube"
    ),
    "vad_filter": True,
    "min_confidence": 0.15,
}

TTS_VOICE = os.getenv("HERMES_TTS_VOICE", "en-US-AriaNeural")
SAMPLE_RATE = 16000

# Positive battery: every phrase the pipeline claims to handle, plus generic
# assistant turns seen in voice_events.jsonl.
TEST_PHRASES = [
    # Hotword / channel commands (from initial_prompt + hotwords)
    "Turn on CNN",
    "Turn on LCI",
    "Turn on TF1",
    "Turn on France 2",
    "Turn on SVT1",
    "Turn on SVT2",
    "Turn on ABC News",
    "Turn on CBS News",
    "Start Netflix",
    "Start YouTube",
    "Open Firedroid",
    "Switch to Fire TV",
    "Turn on the Shield",
    # Generic home-assistant commands
    "Turn on the kitchen light",
    "Turn off the living room lamp",
    "Set the thermostat to twenty two degrees",
    "Is the front door locked",
    "What's the temperature in the bedroom",
    # Conversational
    "Hey Jarvis",
    "What time is it",
    "Thank you",
]

# Negative battery: must transcribe to "" (or be discarded below the floor).
NEGATIVE_CLIPS = ["silence", "white_noise", "pink_noise"]


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def synthesise(phrase: str, dest: Path) -> None:
    """TTS a phrase with edge-tts and normalise to 16 kHz mono PCM16 WAV."""
    mp3 = dest.with_suffix(".mp3")
    asyncio.run(_edge_tts(phrase, mp3))
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "quiet", "-i", str(mp3),
         "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "wav", str(dest)],
        check=True,
    )
    mp3.unlink()


async def _edge_tts(text: str, dest: Path) -> None:
    import edge_tts
    comm = edge_tts.Communicate(text, TTS_VOICE)
    await comm.save(str(dest))


def make_noise_clip(kind: str, dest: Path, seconds: float = 4.0) -> None:
    n = int(seconds * SAMPLE_RATE)
    rng = np.random.default_rng(42)
    if kind == "silence":
        samples = np.zeros(n, dtype=np.float32)
    elif kind == "white_noise":
        samples = (rng.standard_normal(n) * 0.02).astype(np.float32)
    elif kind == "pink_noise":
        white = rng.standard_normal(n)
        # Cheap pink-ish: cumulative low-pass then normalise to ~ speech RMS.
        pink = np.convolve(white, np.ones(16) / 16, mode="same")
        pink /= np.max(np.abs(pink)) + 1e-9
        samples = (pink * 0.05).astype(np.float32)
    else:
        raise ValueError(kind)
    write_wav(dest, samples)


def write_wav(dest: Path, samples: np.ndarray) -> None:
    pcm = np.rint(np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(dest), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


def apply_gain(src: Path, gain: float) -> Path:
    """Scale amplitude to simulate mic distance. Returns a new wav path."""
    with wave.open(str(src), "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    samples = (pcm.astype(np.float32) / 32767.0) * gain
    dest = src.with_name(f"{src.stem}__gain{gain:.2f}.wav")
    write_wav(dest, samples)
    return dest


def read_wav(src: Path) -> np.ndarray:
    with wave.open(str(src), "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32767.0


def mix_with_noise(src: Path, snr_db: float) -> Path:
    """Mix speech with a pink-noise room bed (TV/fan-ish) at the given SNR."""
    speech = read_wav(src)
    rng = np.random.default_rng(7)
    white = rng.standard_normal(len(speech))
    noise = np.convolve(white, np.ones(16) / 16, mode="same")
    noise /= np.sqrt(np.mean(noise ** 2)) + 1e-9  # unit RMS
    speech_rms = np.sqrt(np.mean(speech ** 2)) + 1e-9
    noise_rms_target = speech_rms / (10 ** (snr_db / 20))
    mixed = speech + noise * noise_rms_target
    peak = np.max(np.abs(mixed))
    if peak > 0.98:
        mixed *= 0.98 / peak
    dest = src.with_name(f"{src.stem}__snr{snr_db:g}.wav")
    write_wav(dest, mixed.astype(np.float32))
    return dest


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_NUM_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
}


def normalise(text: str) -> str:
    """Case/punct-insensitive, and 'twenty two' == '22'."""
    tokens = re.sub(r"[^a-z0-9 ]", "", text.lower()).split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("twenty", "thirty") and i + 1 < len(tokens) and tokens[i + 1] in _NUM_WORDS:
            out.append(str(int(_NUM_WORDS[tok]) + int(_NUM_WORDS[tokens[i + 1]])))
            i += 2
            continue
        out.append(_NUM_WORDS.get(tok, tok))
        i += 1
    return " ".join(out)


def word_error_rate(ref: str, hyp: str) -> float:
    r, h = normalise(ref).split(), normalise(hyp).split()
    if not r:
        return 0.0 if not h else 1.0
    # Levenshtein on words
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(h)] / len(r)


# ---------------------------------------------------------------------------
# Transcription under test
# ---------------------------------------------------------------------------

def transcribe(engine, audio_path: Path) -> dict:
    result = engine.transcribe_with_confidence(str(audio_path), language=LIVE_CONFIG["language"])
    return {
        "text": result.get("text", "").strip(),
        "confidence": round(float(result.get("confidence", 1.0)), 3),
    }


def make_engine(**overrides):
    from voice_stack.engines.stt import FasterWhisperEngine
    opts = dict(LIVE_CONFIG)
    opts.update(overrides)
    opts.pop("language", None)
    opts.pop("min_confidence", None)
    return FasterWhisperEngine(**opts)


# ---------------------------------------------------------------------------
# Main battery
# ---------------------------------------------------------------------------

def run_battery(engine, gains, workdir: Path, label: str = "", snrs=()) -> dict:
    min_conf = LIVE_CONFIG["min_confidence"]
    rows = []
    for phrase in TEST_PHRASES:
        clean = workdir / f"phrase_{abs(hash(phrase)) & 0xFFFFFF}.wav"
        if not clean.exists():
            synthesise(phrase, clean)
        variants = [(g, None) for g in gains] + [(1.0, s) for s in snrs]
        for gain, snr in variants:
            clip = apply_gain(clean, gain) if gain != 1.0 else clean
            if snr is not None:
                clip = mix_with_noise(clip, snr)
            started = time.monotonic()
            r = transcribe(engine, clip)
            elapsed = time.monotonic() - started
            wer = word_error_rate(phrase, r["text"])
            accepted = r["confidence"] >= min_conf
            ok = wer == 0.0 and accepted
            rows.append({
                "type": "phrase", "phrase": phrase, "gain": gain, "snr": snr,
                "text": r["text"], "confidence": r["confidence"],
                "wer": round(wer, 3), "accepted": accepted, "ok": ok,
                "seconds": round(elapsed, 2),
            })
            if clip != clean:
                clip.unlink(missing_ok=True)

    negatives = []
    for kind in NEGATIVE_CLIPS:
        clip = workdir / f"neg_{kind}.wav"
        if not clip.exists():
            make_noise_clip(kind, clip)
        r = transcribe(engine, clip)
        clean_hallucination = r["text"] == "" or r["confidence"] < min_conf
        negatives.append({
            "type": "negative", "clip": kind, "text": r["text"],
            "confidence": r["confidence"], "ok": clean_hallucination,
        })

    return {"label": label, "rows": rows, "negatives": negatives}


def print_report(result: dict) -> None:
    label = result["label"]
    if label:
        print(f"\n=== {label} ===")
    print(f"{'phrase':<42}{'gain':>5} {'snr':>5} {'conf':>6} {'wer':>5}  {'verdict':<10} transcript")
    print("-" * 115)
    for row in result["rows"]:
        if "file" in row:  # real-file mode
            print(f"{row['file']:<42}{'':>5} {'':>5} {row['confidence']:>6.3f} "
                  f"{'':>5}  {'':<10} {row['text']}")
            continue
        verdict = "OK" if row["ok"] else ("REJECTED" if not row["accepted"] else "MISHEARD")
        text = row["text"] if row["text"] != row["phrase"] else ""
        snr = f"{row['snr']:g}" if row.get("snr") is not None else ""
        gain = f"{row['gain']:>5.2f}" if "gain" in row else f"{row.get('rms', 0):>5.2f}"
        print(f"{row['phrase']:<42}{gain} {snr:>5} {row['confidence']:>6.3f} "
              f"{row['wer']:>5.2f}  {verdict:<10} {text}")
    for neg in result["negatives"]:
        verdict = "OK (silent)" if neg["ok"] else "HALLUCINATED"
        print(f"{'[' + neg['clip'] + ']':<42}{'':>5} {neg['confidence']:>6.3f} "
              f"{'':>5}  {verdict:<10} {neg['text']}")

    rows = result["rows"]
    scored = [r for r in rows if "ok" in r]
    negs_ok = sum(1 for n in result["negatives"] if n["ok"])
    print("-" * 110)
    if scored:
        passed = sum(1 for r in scored if r["ok"])
        print(f"phrases: {passed}/{len(scored)} exact + accepted   "
              f"negatives clean: {negs_ok}/{len(result['negatives'])}   "
              f"mean conf: {sum(r['confidence'] for r in scored) / len(scored):.3f}")
    elif rows:
        print(f"files transcribed: {len(rows)}   "
              f"mean conf: {sum(r['confidence'] for r in rows) / len(rows):.3f}")


# Candidate option sets for --sweep. Each is layered on LIVE_CONFIG.
SWEEPS = {
    "baseline (live config)": {},
    "no prev-text carry-over": {"_extra": {"condition_on_previous_text": False}},
    "strict no-speech gate": {"_extra": {"no_speech_threshold": 0.6, "log_prob_threshold": -1.0}},
    "both": {"_extra": {
        "condition_on_previous_text": False,
        "no_speech_threshold": 0.6,
        "log_prob_threshold": -1.0,
    }},
}


# ---------------------------------------------------------------------------
# Real-audio modes
# ---------------------------------------------------------------------------

def run_real_files(engine, directory: Path) -> dict:
    """Transcribe every wav in a directory; ground truth inferred from filename."""
    rows = []
    for wav in sorted(directory.glob("*.wav")):
        r = transcribe(engine, wav)
        rows.append({"file": wav.name, "text": r["text"], "confidence": r["confidence"]})
    return {"label": f"real files: {directory}", "rows": rows, "negatives": []}


def run_acoustic(engine, phrases, workdir: Path, play_volume: int = 100) -> dict:
    """Full air-gap loop: speakers -> room -> mic -> recorder -> STT.

    Plays each synthesised phrase through the default output while recording
    on the default input, exactly like pipeline.record_audio does. This is
    the only mode that exercises the real mic gain, room echo and AGC.
    Wake-word phrases are skipped so we don't trip the live gateway.
    """
    import sounddevice as sd

    min_conf = LIVE_CONFIG["min_confidence"]
    rows = []
    for phrase in phrases:
        if "jarvis" in phrase.lower():
            print(f"  [skip acoustic: {phrase!r} would trip the live wake word]")
            continue
        clean = workdir / f"phrase_{abs(hash(phrase)) & 0xFFFFFF}.wav"
        if not clean.exists():
            synthesise(phrase, clean)

        duration = 5.0
        rec = sd.rec(int(duration * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                     channels=1, dtype="float32")
        time.sleep(0.4)  # let the recorder settle before playback
        subprocess.run(["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet",
                        "-volume", str(play_volume), str(clean)],
                       capture_output=True, timeout=15)
        sd.wait()

        rms = float(np.sqrt(np.mean(rec ** 2)))
        peak = float(np.max(np.abs(rec)))
        clip = workdir / "acoustic_last.wav"
        write_wav(clip, rec[:, 0])
        r = transcribe(engine, clip)
        wer = word_error_rate(phrase, r["text"])
        accepted = r["confidence"] >= min_conf
        rows.append({
            "type": "acoustic", "phrase": phrase, "text": r["text"],
            "confidence": r["confidence"], "wer": round(wer, 3),
            "accepted": accepted, "ok": wer == 0.0 and accepted,
            "rms": round(rms, 4), "peak": round(peak, 3),
        })
        print(f"  mic rms={rms:.4f} peak={peak:.2f} conf={r['confidence']:.3f} "
              f"wer={wer:.2f}  {r['text']!r}")
    return {"label": "acoustic loopback", "rows": rows, "negatives": []}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gains", type=float, nargs="+", default=[1.0, 0.3, 0.1],
                    help="amplitude scales simulating mic distance")
    ap.add_argument("--snr", type=float, nargs="+", default=[],
                    help="SNR levels (dB) for noise-mixed variants, simulating room noise")
    ap.add_argument("--sweep", action="store_true", help="compare candidate transcribe options")
    ap.add_argument("--keep-audio", action="store_true", help="keep synthesised wavs")
    ap.add_argument("--json", dest="json_out", help="write full results to a JSON file")
    ap.add_argument("--audio-dir", help="reuse a directory of synthesised wavs")
    ap.add_argument("--real", metavar="DIR", help="transcribe real recordings from DIR")
    ap.add_argument("--acoustic", action="store_true",
                    help="air-gap test: play test words on speakers, record via mic, transcribe")
    ap.add_argument("--play-volume", type=int, default=100,
                    help="ffplay playback volume 0-100 (100 = TTS loudness, ~25 = person across the room)")
    ap.add_argument("--phrases", nargs="+", help="override the test phrase list")
    args = ap.parse_args()

    if args.phrases:
        TEST_PHRASES[:] = args.phrases

    workdir = Path(args.audio_dir) if args.audio_dir else Path(tempfile.mkdtemp(prefix="voice_cal_"))
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"audio workdir: {workdir}")

    results = []
    if args.real:
        engine = make_engine()
        results.append(run_real_files(engine, Path(args.real)))
    elif args.acoustic:
        engine = make_engine()
        print("ACOUSTIC MODE: test words will play on the speakers and be recorded by the mic.")
        results.append(run_acoustic(engine, TEST_PHRASES, workdir, args.play_volume))
    elif args.sweep:
        for label, overrides in SWEEPS.items():
            extra = overrides.pop("_extra", {})
            engine = make_engine(**overrides)
            engine._extra_transcribe_options = extra  # consumed by patched stt.py
            results.append(run_battery(engine, args.gains, workdir, label, args.snr))
    else:
        engine = make_engine()
        results.append(run_battery(engine, args.gains, workdir, snrs=args.snr))

    for result in results:
        print_report(result)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json_out}")

    if not args.keep_audio and not args.audio_dir:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
