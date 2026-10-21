"""Resolve voice exclusions before starting the DJ; never select or play music."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from . import events, music

NEGATION = re.compile(
    r"\b(?:not|never|except|excluding|without|avoid|don't|do not)\b", re.I
)
CLARIFY = "Which artist or song should I avoid? Please say the name clearly."


def evidence():
    """Bounded recognition vocabulary and recent attempted selections, not taste."""
    profile = Path.home() / ".hermes/profiles/music"
    try:
        artists = (
            (profile / "skills/music-domain/references/artist-vocabulary.txt")
            .read_text()
            .splitlines()
        )
    except OSError:
        artists = []
    recent = []
    log = Path.home() / ".hermes/logs/voice_events.jsonl"
    try:
        with log.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 262144))
            lines = stream.read().decode(errors="replace").splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (
                row.get("tool") == "music_playback_dispatch"
                and row.get("phase") == "started"
            ):
                command = row.get("preview", "")
                match = re.search(r" by (.+?) on Apple Music$", command)
                if match and match[1] not in recent:
                    recent.append(match[1])
                if len(recent) >= 5:
                    break
    except OSError:
        pass
    return list(dict.fromkeys(recent + artists))[:100], recent


def classify(text, artists, recent):
    from typesafe_sdk import Choice, Noul, TypeSafeClient

    key = music._load_api_key()
    if not key:
        raise ValueError("recognition unavailable")
    choices = {f"a{i}": name for i, name in enumerate(artists)}
    with TypeSafeClient(api_key=key) as client:
        response = client.system_one(
            model="jev-latest",
            state={
                "original_forwarded_request": text,
                "recent_attempted_artists": recent,
            },
            questions={
                "excluded_artist": Choice(
                    instructions=(
                        "Interpret ONLY the negative limitation in this voice transcription. "
                        "Select the artist the speaker says NOT to play, repairing a phonetic "
                        "transcription only when supported by a candidate and recent context. "
                        "A recent artist is context, not proof. Never select a song to play. "
                        "Do not turn an unrecognized negative phrase into an invented song title. "
                        "Select unclear for ambiguity or multiple excluded artists. "
                        "Use literal only for an already unambiguous non-artist limitation; "
                        "none only if there is no exclusion (e.g. a word inside a requested title)."
                    ),
                    criteria={
                        **choices,
                        "literal": "Clear literal limitation; preserve verbatim",
                        "none": "No actual exclusion",
                        "unclear": "Ambiguous exclusion; ask before DJ selection",
                    },
                ),
                "complete": Noul(
                    instructions="Can all negative limitations be preserved by this one excluded artist or the unchanged literal request, with no unresolved identity or scope?"
                ),
            },
        )
    answer = response.answers["excluded_artist"]
    confidence = float(answer.confidence)
    complete = float(response.answers["complete"].noul)
    if not all(math.isfinite(v) and 0.85 <= v <= 1 for v in (confidence, complete)):
        return None
    choice = str(answer.choice)
    if choice in ("literal", "none"):
        return ""
    return choices.get(choice)


def confirmed_alias(text, artists):
    # User-confirmed transcription corrections are recognition data, not taste.
    if len(list(NEGATION.finditer(text))) != 1:
        return None
    match = re.search(
        r"\b(?:not|never|don't|do not|avoid|except|excluding|without)\s+(?:to\s+)?(?:play\s+)?(.+?)\s*[.!?]*$",
        text,
        re.I,
    )
    if not match:
        return None
    exact = next(
        (name for name in artists if name.casefold() == match[1].strip().casefold()),
        None,
    )
    if exact:
        return exact
    path = (
        Path.home()
        / ".hermes/profiles/music/skills/music-domain/references/artist-transcription-aliases.json"
    )
    try:
        aliases = json.loads(path.read_text())
        name = aliases.get(match[1].strip().casefold())
        return name if isinstance(name, str) and name in artists else None
    except (OSError, ValueError, AttributeError):
        return None


def prepare(text):
    if not NEGATION.search(text):
        return {"text": text}
    try:
        artists, recent = evidence()
        excluded = confirmed_alias(text, artists) or classify(text, artists, recent)
    except Exception:
        excluded = None
    if excluded is None:
        events.emit("music_request_limit", outcome="clarify_before_dj")
        return {"clarification": CLARIFY}
    if not excluded:
        return {"text": text}
    return forward_limit(text, excluded)


def forward_limit(text, excluded):
    # The original is preserved in the handoff; the resolved constraint is explicit
    # and separate from the DJ's freedom to research and select music.
    forwarded = (
        text
        + "\n\n[Resolved voice limitation]\n"
        + "Excluded artist for this request: "
        + excluded
        + ".\n"
        + "The negative phrase refers to this ARTIST, not a song with the garbled name. "
        + "Do not select music by this artist. This is a request-scoped limitation, "
        + "not a permanent dislike. All other music selection remains yours."
    )
    events.emit(
        "music_request_limit", outcome="resolved_before_dj", excluded_artist=excluded
    )
    return {"text": forwarded, "excluded_artist": excluded}


class Clarification(str):
    """A typed upstream question, not a completed DJ answer."""


class PendingLimits:
    """Short-lived clarification for the existing authenticated voice conversation."""

    def __init__(self, clock=None):
        import time
        from threading import RLock

        self.clock = clock or time.monotonic
        self.items = {}
        self.lock = RLock()

    def remember(self, conversation, text):
        with self.lock:
            self.items[conversation] = (self.clock() + 180, text)
            while len(self.items) > 16:
                self.items.pop(next(iter(self.items)))

    def answer(self, conversation, text):
        with self.lock:
            item = self.items.get(conversation)
            if not item:
                return None
            if self.clock() >= item[0]:
                self.items.pop(conversation, None)
                return None
            answer = re.sub(
                r"^\s*here is for dj yakkuza:\s*", "", text, flags=re.I
            ).strip(" .!?")
            if answer.casefold() in ("cancel", "stop", "never mind", "nevermind"):
                self.items.pop(conversation, None)
                return {"cancelled": True}
            # A fresh full request replaces the pending clarification.
            if re.search(r"\b(?:play|find|recommend)\b", answer, re.I):
                self.items.pop(conversation, None)
                return None
            artists, _ = evidence()
            name = confirmed_alias("not " + answer, artists)
            if not name:
                return {"clarification": CLARIFY}
            self.items.pop(conversation, None)
            return {"text": item[1], "excluded_artist": name}


PENDING = PendingLimits()
