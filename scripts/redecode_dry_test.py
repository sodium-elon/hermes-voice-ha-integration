#!/usr/bin/env python3
"""Dynamic DB-driven re-decode dry-test harness.

Proves the "iterate on the initial recording" mechanism WITHOUT touching the
live voice pipeline. Given a WAV, it:

  pass 1  unguided STT              -> raw transcript (often garbled)
  parse  extract suspect artist slots (primary / featured / verbatim)
  build  hotwords DYNAMICALLY from the 2.9M-artist DB (fame-ranked), NOT the
         static curated txt — any popular artist is reachable
  pass 2  STT again on the SAME audio, hotwords + initial_prompt biased
         -> re-listened transcript

Shows both transcripts + the resolution attempt. Exit 0 when pass 2 resolves
where pass 1 failed/stalled.

This is a DRY/RESEARCH harness. It never sends a command to Alexa/Apple Music
and never mutates the live gateway. Run it directly with python3.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.voice_stack import music  # noqa: E402
from plugins.voice_stack.engines.stt import FasterWhisperEngine  # noqa: E402

MIN_CONF = float(os.getenv("DRY_REDECODE_MIN_CONF", "0.60"))
MAX_PASSES = int(os.getenv("DRY_REDECODE_MAX_PASSES", "2"))
HOTWORD_LIMIT = int(os.getenv("DRY_REDECODE_HOTWORD_LIMIT", "120"))
# Broad fame-descending fallback so a completely off-phonetics garble can still
# reach popular artists. Bounded: hotword lists beyond this add noise.
FAME_FALLBACK_LIMIT = int(os.getenv("DRY_REDECODE_FAME_FALLBACK", "200"))


def _slots_from_transcript(transcript: str) -> list[str]:
    """Pull suspect artist slots the same way the music gate does."""
    stripped = music._strip_verbs(str(transcript))
    primary = music._extract_primary_artist(stripped)
    featured, _ = music._extract_featured(stripped)
    primary_slot, featured_slot = music._split_suspect_slots(stripped)
    slots, seen = [], set()
    for s in (primary, primary_slot, featured, featured_slot):
        s = re.sub(r"^\s+|\s+$", "", s or "")
        s = re.sub(r"\s+", " ", s).strip(".,!?")
        if s and s.casefold() not in seen:
            seen.add(s.casefold())
            slots.append(s)
    return slots or [str(transcript).strip()]


def _build_dynamic_hotwords(slots: list[str]) -> list[str]:
    """Hotwords drawn from the DB fame/popularity rank, NOT the static txt.

    This is the dynamic core John demanded: the hotword set is the DB's
    popular-artist pool — `popularity=1000` marks ~8.3k known artists (Tove Lo,
    Stromae, Madonna, … all rank in it). Per-slot phonetic/Jaro hits anchor the
    decode to what was *spoken*; the popular-artist rank guarantees any famous
    name the speaker might have said is reachable even when phonetics garble it.
    """
    names, seen = [], set()

    def add(name):
        if not name:
            return
        key = str(name).casefold()
        if key not in seen:
            seen.add(key)
            names.append(str(name))

    # 1. Popular-artist rank from the DB (the dynamic source of truth).
    try:
        con = music._get_artist_duckdb_connection()
        with music._ARTIST_DUCKDB_LOCK:
            rows = con.execute(
                "select canonical_name from artist_names "
                "where popularity > 0 "
                "order by popularity desc, fame desc, canonical_name asc "
                "limit ?",
                [HOTWORD_LIMIT],
            ).fetchall()
            for row in rows:
                add(row[0])
    except Exception:
        pass
    # 2. Per-slot phonetic + Jaro-Winkler hits anchor to what was actually said.
    for slot in slots:
        for cand in music._duckdb_artist_candidates(slot, limit=HOTWORD_LIMIT):
            add(cand)
    # 3. Curated listening vocab rides along as owner prior.
    for n in music._load_artist_vocabulary():
        add(n)
    return names[:HOTWORD_LIMIT]


def _transcribe(engine, wav: str, hotwords: list[str] | None, initial_prompt: str | None = None):
    # Per-call override via the engine's established calibration hook; leaves
    # the construction-time hotwords/initial_prompt untouched for other callers.
    # faster-whisper needs hotwords as a ", "-joined string (it calls .strip()).
    engine._extra_transcribe_options = {
        "hotwords": ", ".join(hotwords) if hotwords else None,
        "initial_prompt": initial_prompt,
    }
    try:
        result = engine.transcribe_with_confidence(wav, language=None)
    finally:
        engine._extra_transcribe_options = None
    return result.get("text", "").strip(), float(result.get("confidence", 0.0))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wav", help="path to the recorded WAV")
    ap.add_argument("--model", default=os.getenv("HERMES_STT_MODEL", "large-v3"))
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    if not os.path.isfile(args.wav):
        print(f"ERROR: no such WAV: {args.wav}", file=sys.stderr)
        return 2

    engine = FasterWhisperEngine(model_size=args.model, compute_type="int8", vad_filter=True)

    # ---- pass 1: unguided ----
    t1, c1 = _transcribe(engine, args.wav, None)
    slots = _slots_from_transcript(t1) if t1 else []
    hot = _build_dynamic_hotwords(slots) if slots else []
    initial = "Music artist names include: " + ", ".join(hot[:40]) if hot else None

    # ---- pass 2: re-decode SAME audio, DB-dynamic hotwords ----
    t2, c2 = _transcribe(engine, args.wav, hot, initial) if hot else (t1, c1)

    resolved = c2 >= MIN_CONF or (c2 > c1 + 0.15)
    report = {
        "model": args.model,
        "pass1": {"text": t1, "confidence": round(c1, 3)},
        "slots": slots,
        "hotword_count": len(hot),
        "hotword_sample": hot[:25],
        "pass2": {"text": t2, "confidence": round(c2, 3)},
        "judged_better": resolved,
        "min_conf": MIN_CONF,
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if resolved else 1

    print("=== Dynamic DB-driven re-decode dry test ===")
    print(f"model        : {args.model}")
    print(f"WAV          : {args.wav}")
    print(f"--- pass 1 (unguided) ---")
    print(f'  transcript : {t1!r}')
    print(f'  confidence : {c1:.3f}')
    print(f"--- suspect slots ---")
    for s in slots:
        print(f"  - {s!r}")
    print(f"--- dynamic hotwords ({len(hot)}) from DB, fame-ranked ---")
    print(f"  sample     : {hot[:25]}")
    print(f"--- pass 2 (re-decode with DB hotwords) ---")
    print(f'  transcript : {t2!r}')
    print(f'  confidence : {c2:.3f}')
    print(f"--- verdict ---")
    print(f"  resolved   : {'YES' if resolved else 'no (fell back to ask-back)'}")
    print()
    print("This ran against the captured audio only. Nothing was dispatched.")
    return 0 if resolved else 1


if __name__ == "__main__":
    sys.exit(main())