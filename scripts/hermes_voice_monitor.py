#!/usr/bin/env python3
"""hermes-voice-monitor — chat-style live view of the Hermes voice pipeline.

A temporary debugging window onto what the wake word heard, which route
handled it, which tools/services fired, and what was spoken back. Attach it
when something is behaving oddly, read the conversation, then Ctrl-C.

It merges two sources that already exist on disk:

  ~/.hermes/logs/voice_events.jsonl   pipeline stages (voice_stack/events.py),
                                      including tool calls streamed live off
                                      the Hermes Sessions API
  ~/.hermes/ha_audit.log              Home Assistant service calls Hermes made

Nothing here writes state, holds a port, or needs the gateway restarted; it is
purely a reader. Stdlib only — no install step, no virtualenv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

HOME = Path.home()
DEFAULT_EVENTS = HOME / ".hermes" / "logs" / "voice_events.jsonl"
DEFAULT_AUDIT = HOME / ".hermes" / "ha_audit.log"
POLL_SECONDS = 0.25

ANSI_RE = re.compile(r"\033\[[0-9;]*m")


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

class Style:
    """ANSI styling that degrades to plain text when not on a TTY."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return f"\033[{';'.join(codes)}m{text}\033[0m"


DIM, BOLD = "2", "1"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GREY = "31", "32", "33", "34", "35", "36", "90"


def _hhmmss(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def _term_width(default: int = 100) -> int:
    try:
        return max(60, min(shutil.get_terminal_size().columns, 140))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Source readers — each yields (epoch, record) tuples
# ---------------------------------------------------------------------------

def _tail(path: Path, from_start: bool, follow: bool) -> Iterator[str]:
    """Yield lines from a file, tolerating rotation and late creation."""
    handle = None
    inode = None
    position = 0

    while True:
        if handle is None:
            try:
                handle = path.open("r", encoding="utf-8", errors="replace")
                inode = os.fstat(handle.fileno()).st_ino
                if not from_start and follow:
                    handle.seek(0, os.SEEK_END)
                else:
                    handle.seek(position)
            except FileNotFoundError:
                if not follow:
                    return
                yield ""  # let the caller keep its cadence
                time.sleep(POLL_SECONDS)
                continue

        line = handle.readline()
        if line:
            position = handle.tell()
            yield line
            continue

        if not follow:
            return

        # No data: check whether the file was rotated out from under us.
        try:
            if path.stat().st_ino != inode:
                handle.close()
                handle, position, from_start = None, 0, True
                continue
        except FileNotFoundError:
            handle.close()
            handle, position, from_start = None, 0, True
            continue

        yield ""
        time.sleep(POLL_SECONDS)


def _read_events(path: Path, from_start: bool, follow: bool) -> Iterator[tuple[float, dict]]:
    for line in _tail(path, from_start, follow):
        if not line.strip():
            yield (0.0, {})
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        record["_source"] = "voice"
        yield (float(record.get("t") or 0.0), record)


def _read_audit(path: Path, from_start: bool, follow: bool) -> Iterator[tuple[float, dict]]:
    for line in _tail(path, from_start, follow):
        if not line.strip():
            yield (0.0, {})
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            stamp = datetime.strptime(record["ts"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except (KeyError, ValueError):
            stamp = time.time()
        record["_source"] = "audit"
        yield (stamp, record)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class Renderer:
    def __init__(self, style: Style, show_noise: bool, width: int) -> None:
        self.s = style
        self.show_noise = show_noise
        self.width = width
        self.turn: Optional[int] = None
        self.wake_at: Optional[float] = None

    def _rule(self, label: str) -> None:
        bar = "─" * max(4, self.width - len(label) - 3)
        print(self.s(f"\n── {label} {bar}", GREY))

    def _line(self, when: float, glyph: str, who: str, body: str, *codes: str) -> None:
        stamp = self.s(_hhmmss(when), GREY)
        who_col = self.s(f"{who:>7}", *codes) if who else " " * 7
        print(f"{stamp} {glyph} {who_col}  {body}")

    def _note(self, when: float, body: str) -> None:
        print(f"{self.s(_hhmmss(when), GREY)} {self.s('·', GREY)} {' ' * 7}  {self.s(body, GREY)}")

    def render(self, when: float, record: dict) -> None:
        source = record.get("_source")
        if source == "voice":
            self._render_voice(when, record)
        elif source == "audit":
            self._render_audit(when, record)

    # -- voice pipeline ----------------------------------------------------

    def _render_voice(self, when: float, r: dict) -> None:
        kind = r.get("kind")
        turn = r.get("turn")

        if turn is not None and turn != self.turn:
            self.turn = turn
            self.wake_at = when
            self._rule(f"turn {turn}")

        elapsed = f" +{when - self.wake_at:.1f}s" if self.wake_at else ""

        if kind == "wake":
            word = r.get("word", "?")
            score = r.get("score")
            detail = f"{word}" + (f"  score {score:.2f}" if isinstance(score, float) else "")
            self._line(when, "◉", "wake", self.s(detail, BOLD, MAGENTA), MAGENTA)

        elif kind == "record":
            self._note(when, f"recorded {r.get('seconds')}s  (rms {r.get('rms')}){elapsed}")

        elif kind == "heard":
            text = r.get("text") or ""
            meta = []
            if r.get("confidence") is not None:
                meta.append(f"conf {r['confidence']:.2f}")
            if r.get("stt_seconds") is not None:
                meta.append(f"stt {r['stt_seconds']}s")
            if r.get("source") == "ha_assist":
                meta.append("via HA Assist")
            suffix = self.s("  (" + ", ".join(meta) + ")", GREY) if meta else ""
            self._line(when, "🎤", "heard", self.s(f'"{text}"', BOLD, CYAN) + suffix, CYAN)

        elif kind == "discarded":
            self._line(
                when, "✂", "dropped",
                self.s(f'"{r.get("text")}"', YELLOW)
                + self.s(f"  below confidence floor ({r.get('confidence')} < {r.get('min_confidence')})", GREY),
                YELLOW,
            )

        elif kind == "no_speech":
            # Kept visible by default: a wake with no transcript behind it is
            # the signature of a false trigger, which is the usual reason to
            # attach this monitor in the first place.
            self._note(when, f"no speech captured — {r.get('reason')}{elapsed}")

        elif kind == "route":
            target = r.get("target")
            outcome = r.get("outcome")
            if target == "home_assistant" and outcome in {"handled", "handled_after_script_retry"}:
                targets = ", ".join(r.get("targets") or []) or "no entity reported"
                body = self.s("Home Assistant intent", GREEN) + self.s(f" → {targets}", GREY)
                if outcome.endswith("retry"):
                    body += self.s(f"  (retried as '{r.get('retried_as')}')", GREY)
            elif target == "hermes":
                body = self.s("Hermes agent", BLUE) + self.s("  (HA found no intent match)", GREY)
            elif outcome == "no_match":
                body = self.s("no match", YELLOW) + self.s(
                    f"  HA could not resolve a target ({r.get('code')}); held at HA", GREY)
            else:
                body = self.s(f"{target} — {outcome}", RED) + self.s(f"  {r.get('detail', '')}", GREY)
            self._line(when, "⚡", "route", body, YELLOW)

        elif kind == "agent_run":
            self._note(when, f"agent run started via {r.get('runner')}  ({r.get('run_id')})")

        elif kind == "tool":
            phase = r.get("phase")
            name = str(r.get("tool"))
            if phase == "started":
                preview = r.get("preview")
                body = self.s(name, BOLD) + (self.s(f"  {preview}", GREY) if preview else "")
                self._line(when, "🔩", "tool", body, YELLOW)
            elif phase == "failed":
                self._line(when, "🔩", "tool", self.s(f"{name} failed", RED), RED)
            elif self.show_noise:
                self._note(when, f"{name} completed")

        elif kind == "usage":
            if self.show_noise:
                self._note(when, f"tokens in={r.get('input_tokens')} out={r.get('output_tokens')}")

        elif kind == "agent":
            if r.get("ok") is False:
                self._line(when, "✖", "agent", self.s(f"{r.get('runner')} failed after {r.get('seconds')}s", RED), RED)
            else:
                self._note(when, f"{r.get('runner')} answered in {r.get('seconds')}s")

        elif kind == "reply":
            if r.get("empty"):
                self._line(when, "💬", "hermes", self.s("(empty response — nothing spoken)", YELLOW), YELLOW)
            else:
                think = r.get("think_seconds")
                suffix = self.s(f"  (thought {think}s)", GREY) if think is not None else ""
                self._line(when, "💬", "hermes", self.s(f'"{r.get("text")}"', BOLD, GREEN) + suffix, GREEN)

        elif kind == "spoken":
            self._note(when, f"spoken via TTS → {r.get('sink')}  ({r.get('tts_seconds')}s){elapsed}")

        elif kind == "speak_failed":
            self._line(when, "✖", "tts", self.s(f"attempt {r.get('attempt')} failed: {r.get('detail')}", RED), RED)

        elif kind == "voice_action":
            self._note(when, f"voice_action {r.get('action')} → ok={r.get('ok')}")

        elif kind == "error":
            self._line(when, "✖", "error", self.s(f"[{r.get('stage')}] {r.get('detail')}", RED), RED)

        elif self.show_noise:
            self._note(when, json.dumps({k: v for k, v in r.items() if not k.startswith("_")}))

    # -- side effects ------------------------------------------------------

    def _render_audit(self, when: float, r: dict) -> None:
        allowed = r.get("allowed")
        call = f"{r.get('domain')}.{r.get('service')}"
        entity = r.get("entity_id") or "—"
        if allowed:
            body = self.s(f"{call}", BOLD) + self.s(f"  {entity}", GREY) + self.s("  ✓", GREEN)
        else:
            body = self.s(f"{call}  {entity}", YELLOW) + self.s(f"  blocked: {r.get('reason')}", RED)
        self._line(when, "🔧", "did", body, YELLOW)



# ---------------------------------------------------------------------------
# Status header
# ---------------------------------------------------------------------------

def _print_header(style: Style, events_path: Path, follow: bool) -> None:
    def env(name: str, default: str = "—") -> str:
        return os.getenv(name) or default

    width = _term_width()
    print(style("┌" + "─" * (width - 2) + "┐", GREY))

    def row(label: str, value: str) -> None:
        visible = len(f"│ {label:<11} ") + len(ANSI_RE.sub("", value))
        pad = max(0, width - visible - 1)
        print(style(f"│ {label:<11} ", GREY) + value + style(" " * pad + "│", GREY))

    row("monitor", style("hermes voice — live pipeline view", BOLD))
    row("events", str(events_path) + ("" if events_path.exists() else style("  (not created yet)", YELLOW)))
    row("mode", "following new events" if follow else "replaying history")
    print(style("└" + "─" * (width - 2) + "┘", GREY))
    print(style("  wake ◉   heard 🎤   route ⚡   service 🔧   tool 🔩   answer 💬     Ctrl-C to detach", GREY))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_since(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = re.fullmatch(r"(\d+)\s*([smhd])", value.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError("--since takes forms like 30s, 15m, 2h, 1d")
    amount, unit = int(match.group(1)), match.group(2)
    return time.time() - amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-voice-monitor",
        description="Chat-style live view of what the Hermes voice pipeline heard, triggered and answered.",
    )
    parser.add_argument("-s", "--since", help="replay history first: 30s, 15m, 2h, 1d")
    parser.add_argument("-n", "--no-follow", action="store_true", help="print history and exit")
    parser.add_argument("-a", "--all", action="store_true",
                        help="include noise: tool completions, token usage, unknown events")
    parser.add_argument("--json", action="store_true", help="emit raw merged JSON instead of the chat view")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("--events", type=Path, default=Path(os.getenv("HERMES_VOICE_EVENT_LOG") or DEFAULT_EVENTS))
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    args = parser.parse_args(argv)

    since = _parse_since(args.since)
    follow = not args.no_follow
    replay = since is not None or args.no_follow
    style = Style(enabled=not args.no_color and sys.stdout.isatty())

    if not args.json:
        _print_header(style, args.events, follow)

    streams = [
        _read_events(args.events, replay, follow),
        _read_audit(args.audit, replay, follow),
    ]
    renderer = Renderer(style, show_noise=args.all, width=_term_width())

    # Round-robin the readers and order by timestamp within a small window, so
    # a service call logged by one file lands next to the transcript in another.
    pending: list[tuple[float, dict]] = []
    heads: list[Optional[tuple[float, dict]]] = [None] * len(streams)
    exhausted = [False] * len(streams)

    try:
        while True:
            progressed = False
            for index, stream in enumerate(streams):
                if exhausted[index]:
                    continue
                try:
                    stamp, record = next(stream)
                except StopIteration:
                    exhausted[index] = True
                    continue
                if not record:
                    continue
                if since is not None and stamp < since:
                    continue
                pending.append((stamp, record))
                progressed = True

            if pending and (not progressed or len(pending) > 64):
                pending.sort(key=lambda item: item[0])
                for stamp, record in pending:
                    if args.json:
                        print(json.dumps({"t": stamp, **record}, ensure_ascii=False))
                    else:
                        renderer.render(stamp, record)
                sys.stdout.flush()
                pending.clear()

            if all(exhausted):
                break
            if not progressed:
                time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print(style("\n— detached —", GREY))
        return 0

    for stamp, record in sorted(pending, key=lambda item: item[0]):
        if args.json:
            print(json.dumps({"t": stamp, **record}, ensure_ascii=False))
        else:
            renderer.render(stamp, record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
