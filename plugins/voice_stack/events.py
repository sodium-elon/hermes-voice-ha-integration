"""Structured voice-pipeline event stream.

The voice stack already logs fragments of an interaction to ``agent.log``, but
the interesting parts — which route handled the transcript and what Hermes
actually answered — never reach a log line. This module emits one JSON object
per pipeline stage to a dedicated newline-delimited file so external tools can
follow a voice interaction end to end:

    wake -> record -> heard -> route -> reply -> spoken

Emission is best-effort by design: a monitoring side channel must never be able
to break the voice pipeline, so every failure is swallowed.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_EVENT_LOG = Path.home() / ".hermes" / "logs" / "voice_events.jsonl"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024

_LOCK = threading.Lock()
_TURN = 0


def enabled() -> bool:
    """Return True unless the event stream is explicitly disabled."""
    return os.getenv("HERMES_VOICE_EVENTS", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def event_log_path() -> Path:
    """Return the configured event log path."""
    override = os.getenv("HERMES_VOICE_EVENT_LOG", "").strip()
    return Path(override).expanduser() if override else DEFAULT_EVENT_LOG


def _max_bytes() -> int:
    try:
        return max(0, int(os.getenv("HERMES_VOICE_EVENT_MAX_BYTES", str(DEFAULT_MAX_BYTES))))
    except ValueError:
        return DEFAULT_MAX_BYTES


def begin_turn() -> int:
    """Start a new interaction turn and return its id.

    A turn spans one wake-word detection through the spoken response, letting
    the monitor group stages that belong to the same interaction.
    """
    global _TURN
    with _LOCK:
        _TURN += 1
        return _TURN


def current_turn() -> int:
    with _LOCK:
        return _TURN


def _rotate_if_needed(path: Path, limit: int) -> None:
    if limit <= 0:
        return
    try:
        if path.stat().st_size < limit:
            return
    except OSError:
        return
    try:
        path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def emit(kind: str, **fields: Any) -> None:
    """Append one event to the stream. Never raises."""
    if not enabled():
        return
    try:
        now = time.time()
        record: dict[str, Any] = {
            "t": round(now, 3),
            "ts": datetime.fromtimestamp(now, timezone.utc)
            .astimezone()
            .isoformat(timespec="milliseconds"),
            "turn": current_turn(),
            "kind": kind,
        }
        for key, value in fields.items():
            if value is not None:
                record[key] = value

        path = event_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path, _max_bytes())
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with _LOCK:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:  # pragma: no cover - monitoring must never break voice
        pass


def truncate(text: Optional[str], limit: int = 600) -> str:
    """Clamp free text so a runaway response cannot bloat the stream."""
    value = (text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"
