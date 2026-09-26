"""Read-only, structured Apple Music request resolution (never device control).

The resolver turns a spoken music request into one precise Alexa playback
command, always suffixed with ``on Apple Music``. The decision layer is a
TypeSafe System One (Jev) call: parallel Choice + Noul questions classify what
to ask for and how to formulate it. Jev returns typed answers — never a
free-form command. The final command is built locally from those typed fields
plus Apple's own catalog (public iTunes Search API) as the source of truth.

When the TypeSafe API is unavailable the resolver rejects the request with the
exact string ``TypeSafe API (Jev)  is unavailable``. It never guesses, never
falls back to a raw passthrough, and never lets the model emit a command.

Research online:
- ``latest`` album requests are resolved against the live iTunes album catalog
  (newest studio release by releaseDate), never from model memory.
- Named artists/tracks/albums are verified against the live catalog.
- Ambiguous lyric clues are identified via Jev and confirmed against the catalog.

On any ambiguity, inability to confirm, or malformed input the resolver returns
a short clarification instead of guessing.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import urllib.parse
import urllib.request
from functools import lru_cache

CLARIFY = {"clarification": "Which song do you mean?"}
UNAVAILABLE = "TypeSafe API (Jev)  is unavailable"


class TypeSafeUnavailable(RuntimeError):
    """Raised when the TypeSafe/Jev API cannot be reached or has no credential."""

# Question thresholds (0..1). Calibrate against real traffic; 0.5 is neutral.
_YES = 0.5


def _load_api_key():
    """Return the TYPESAFE_API_KEY from the environment or hermes secrets."""
    key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    candidate = os.path.join(
        os.getenv("HERMES_HOME", os.path.expanduser("~/.hermes")),
        "secrets",
        "typesafe.env",
    )
    try:
        for line in open(candidate, encoding="utf-8"):
            line = line.strip()
            if line.startswith("TYPESAFE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _build_questions():
    """Jev questions deciding what to ask for and how to formulate it."""
    from typesafe_sdk import Choice, Noul

    return {
        "kind": Choice(
            instructions="What kind of music playback is the user asking for?",
            criteria={
                "song": "A specific named song",
                "album": "A named album, or the newest/latest album",
                "artist": "A named artist (songs by that artist)",
                "genre": "A genre of music (jazz, pop, rock, ...)",
                "playlist": "A named playlist",
                "music": "Generic music, no specific target",
                "unclear": "Ambiguous, multiple, conflicting, or impossible",
                "nonmusic": "Devices, television, podcasts, audiobooks, or non-music",
            },
        ),
        "shuffle": Noul(instructions="Did the user ask to shuffle or mix the music?"),
        "research": Noul(
            instructions=(
                "Is the request a descriptive clue (lyric fragment, scene, movie, "
                "or other description) that needs identification rather than a "
                "plainly named title? A plainly named song such as 'Words by Bee Gees' "
                "is NOT a clue."
            )
        ),
        "latest": Noul(instructions="Is the user asking for the newest or latest album?"),
        "studio": Noul(
            instructions="Did the user explicitly ask for a studio album?"
        ),
        "chained": Noul(
            instructions=(
                "Does the request chain a secondary imperative after the music ask "
                "(for example 'and then turn off the lights', 'then stop', 'unlock')?"
            )
        ),
    }


def _classify(text):
    """Run TypeSafe Jev classification. Returns intent dict or None on failure.

    Returns *None* (not UNAVAILABLE) when the classifier simply fails to decide;
    ``resolve`` maps that to a clarification. A hard API outage raises a
    TypeSafeUnavailable so the caller can emit the exact UNAVAILABLE string.
    """
    key = _load_api_key()
    if not key:
        raise TypeSafeUnavailable("TYPESAFE_API_KEY is not set")
    try:
        from typesafe_sdk import TypeSafeClient  # import inside try: missing SDK = outage

        with TypeSafeClient(api_key=key) as client:
            response = client.system_one(
                model="jev-latest",
                state={"request": text},
                questions=_build_questions(),
            )
        answers = response.answers
    except Exception:
        raise TypeSafeUnavailable(UNAVAILABLE) from None

    kind_raw = str(getattr(answers["kind"], "choice", "") or "").strip()
    kind_map = {
        "song": "song",
        "album": "album",
        "artist": "artist",
        "genre": "genre",
        "playlist": "playlist",
        "music": "music",
        "unclear": "unknown",
        "nonmusic": "nonmusic",
    }
    kind = kind_map.get(kind_raw)
    if kind not in {
        "song", "album", "artist", "genre", "playlist", "music", "unknown", "nonmusic",
    }:
        return None
    if float(getattr(answers["chained"], "noul", 0.0) or 0.0) > _YES:
        return {"kind": "unknown", "reason": "chained"}
    return {
        "kind": kind,
        "title": "",
        "artist": "",
        "shuffle": float(getattr(answers["shuffle"], "noul", 0.0) or 0.0) > _YES,
        "research": float(getattr(answers["research"], "noul", 0.0) or 0.0) > _YES,
        "latest": float(getattr(answers["latest"], "noul", 0.0) or 0.0) > _YES,
        "studio": float(getattr(answers["studio"], "noul", 0.0) or 0.0) > _YES,
    }


def _safe_name(value):
    """Return a cleaned library name or None if it is unsafe or not a string."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if re.search(r"[;\r\n]|&&|\|\||[`$<>]", value):
        return None
    collapsed = " ".join(value.split())
    if not collapsed or len(collapsed) > 120:
        return None
    return collapsed


def _catalog(output="json", **params):
    """Query Apple's public iTunes Search API (read-only, used as catalog source)."""
    params.setdefault("country", "SE")
    params.setdefault("limit", 50)
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        dict(output=output, **params)
    )
    with urllib.request.urlopen(url, timeout=8) as response:
        data = json.load(response)
    return data.get("results") or []


def _write_telemetry(event):
    """Best-effort resolver telemetry; never raises."""
    try:
        from . import events

        events.emit("music_resolver", **event)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Catalog resolution. The decision layer told us WHAT; these local resolvers
# find the canonical facts (exact lookups stay in code, not judgment).
# ---------------------------------------------------------------------------

def _strip_verbs(text):
    """Return the music target text with playback verbs and provider removed.

    Only leading classifiers ("the", "album/song/playlist") are stripped so a
    phrase like "latest song featuring Stromae" keeps "song" intact mid-string.
    A trailing ", by <artist>" is preserved for downstream splitting.
    """
    cleaned = re.sub(
        r"^\s*(?:put\s+some|put\s+on|play\s+some|play\s+me|shuffle|play)\s+",
        "", text, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+on\s+(?:apple\s+)?music\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+on\s+\w[\w\s]*$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"[.!?]+$", "", cleaned).strip()
    cleaned = re.sub(r"^(?:the)\s+", "", cleaned, count=1, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^(?:album|song|playlist|\d+)\s+", "", cleaned, count=1, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+(?:playlist|album)$", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def _resolve_artist(intent, name):
    if not name:
        return None
    try:
        rows = _catalog(term=name, entity="musicArtist")
        canonical = next(
            (r["artistName"] for r in rows
             if str(r.get("artistName") or "").casefold() == name.casefold()),
            None,
        )
        if not canonical:
            return None
        canonical = _safe_name(canonical)
    except Exception:
        return None
    if not canonical:
        return None
    if intent["shuffle"]:
        return {"command": f"Shuffle music by {canonical} on Apple Music"}
    return {"command": f"Play songs by {canonical} on Apple Music"}


def _artist_db_fallback(artist, confidence):
    """Recover a garbled-yet-confident single artist from DB + listening vocab.

    Whisper can be 10/10 confident on a name that is physically wrong
    ("Bill McCartney" for "Dolly Parton"). Because confidence clears the
    repair floor, the low-confidence path never fires, and when the exact
    catalog resolve also misses, the old code punted straight to a repeat ask.
    That is the ``asked again`` loop the user hit.

    This runs on that miss and tries the other way: it hands Jev the FULL
    owner listening vocabulary (never starved by a file-order quota, so Dolly
    at line 18 is always in the pool) plus DB phonetic anchors, and only emits
    a command when Jev picks a real, confident artist that actually resolves
    in the catalog. Returns a command dict, or None to fall through to CLARIFY.
    """
    suspect = _safe_name(artist) or ""
    if not suspect:
        return None
    # Only recover when the STT side was actually confident. If the utterance
    # was clearly garbled/low-confidence, asking for a repeat stays correct.
    if confidence < STT_REPAIR_CONFIDENCE_FLOOR:
        return None
    cands, seen = [], set()

    def add(name):
        name = _safe_name(name)
        if not name:
            return
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            cands.append(name)

    # 1. Full listening vocabulary first — all entries, not a quota, so the
    #    owner's real artists (incl. Dolly at line 18) can never be starved.
    for name in _load_artist_vocabulary():
        add(name)
    # 2. DB phonetic anchors on the suspect fill in any gap and anchor Jev to
    #    the sounds actually spoken.
    try:
        indexed = _duckdb_artist_candidates(suspect, limit=12) or _postgres_artist_candidates(suspect, limit=12)
        for name in indexed:
            add(name)
    except Exception:
        pass
    cands = [c for c in cands if c.casefold() != suspect.casefold()]
    cands = cands[:24]
    if len(cands) < 2:
        return None
    state = {"transcript": suspect, "suspect_term": suspect, "candidates": cands}
    try:
        chosen, conf = _jev_select_artist(state, None, cands)
    except TypeSafeUnavailable:
        # Best-effort recovery: without Jev we cannot distinguish a garbled name
        # from a genuinely unknown one. Degrade to clarification (ask again)
        # rather than guessing — never worse than the prior behaviour.
        return None
    except Exception:
        return None
    if chosen == "none" or conf < STT_REPAIR_MIN_CHOICE_CONFIDENCE:
        return None
    resolved = _resolve_artist(
        {"shuffle": False, "research": False, "latest": False, "studio": False},
        chosen,
    )
    if resolved:
        return resolved
    # Chosen artist is not in the live catalog; do not fabricate a fact.
    return None


def _resolve_song(intent, title, artist):
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        return None
    try:
        if intent["research"] and artist:
            rows = _catalog(term=f"{title} {artist}", entity="song")
            row = next(
                (r for r in rows
                 if (s := _safe_name(r.get("artistName")))
                 and s.casefold() == artist.casefold()),
                None,
            )
            canonical_title = _safe_name((row or {}).get("trackName"))
            canonical_artist = _safe_name((row or {}).get("artistName"))
        elif intent["research"]:
            # Descriptive clue: Jev selects among the catalog candidates rather
            # than letting a phrase search pick a random lookalike. Propagates
            # TypeSafeUnavailable (the exact-message contract) untouched.
            return _select_song_candidate(intent, title)
        else:
            rows = _catalog(term=f"{title} {artist}" if artist else title, entity="song")
            title_key = _normalize_artist_key(title)
            artist_key = _normalize_artist_key(artist)
            row = next(
                (
                    candidate
                    for candidate in rows
                    if _normalize_artist_key(candidate.get("trackName")) == title_key
                    and (
                        not artist_key
                        or _normalize_artist_key(candidate.get("artistName")) == artist_key
                    )
                ),
                None,
            )
            canonical_title = _safe_name((row or {}).get("trackName"))
            canonical_artist = _safe_name((row or {}).get("artistName"))
        if not canonical_title:
            return None
        if canonical_artist:
            return {"command": f"Play the song {canonical_title} by {canonical_artist} on Apple Music"}
        return {"command": f"Play the song {canonical_title} on Apple Music"}
    except TypeSafeUnavailable:
        raise
    except Exception:
        return None


def _select_song_candidate(intent, clue):
    """Use Jev Choice to pick the intended song among live-catalog candidates.

    Returns {command: ...} or None. On TypeSafe failure propagates to the caller,
    which maps it to the exact UNAVAILABLE string.
    """
    clue = (clue or "").strip()
    if not clue:
        return None
    try:
        rows = _catalog(term=clue, entity="song", limit=8)
    except Exception:
        return None
    candidates = []
    for row in rows:
        title = _safe_name(row.get("trackName"))
        artist = _safe_name(row.get("artistName"))
        if title:
            candidates.append((title, artist))

    # Nothing to select against and no canonical name -> cannot confirm safely.
    if not candidates:
        return None

    criteria = {}
    for i, (title, artist) in enumerate(candidates):
        criteria[str(i)] = (
            f"{title} by {artist}" if artist else title
        )
    criteria["none"] = "None of the candidates matches the description"

    from typesafe_sdk import Choice, TypeSafeClient

    key = _load_api_key()
    if not key:
        raise TypeSafeUnavailable("TYPESAFE_API_KEY is not set")
    try:
        with TypeSafeClient(api_key=key) as client:
            response = client.system_one(
                model="jev-latest",
                state={
                    "clue": clue,
                    "candidates": [
                        {"trackName": t, "artistName": a} for (t, a) in candidates
                    ],
                },
                questions={
                    "which": Choice(
                        instructions={
                            "question": (
                                "Which song matches the spoken description in `clue`? "
                                "Pick the exact song the user means."
                            ),
                            "clue": clue,
                        },
                        criteria=criteria,
                    )
                },
            )
        chosen = str(getattr(response.answers["which"], "choice", "") or "none")
    except Exception:
        raise TypeSafeUnavailable(UNAVAILABLE) from None

    if chosen == "none" or not chosen.isdigit():
        return None
    idx = int(chosen)
    if idx >= len(candidates):
        return None
    title, artist = candidates[idx]
    if artist:
        return {"command": f"Play the song {title} by {artist} on Apple Music"}
    return {"command": f"Play the song {title} on Apple Music"}


def _resolve_album(intent, artist, title):
    artist = (artist or "").strip()
    title = (title or "").strip()
    if intent["latest"]:
        if not artist:
            return None
        try:
            rows = _catalog(term=artist, entity="album", attribute="artistTerm", limit=200)
            candidates = []
            for row in rows:
                if str(row.get("artistName") or "").casefold() != artist.casefold():
                    continue
                name = str(row.get("collectionName") or "")
                release = str(row.get("releaseDate") or "")[:10]
                candidates.append((release, name))
            if not candidates:
                return None
            if intent["studio"]:
                pool = [
                    c for c in candidates
                    if not re.search(r"\b(live|single|ep)\b", c[1], re.IGNORECASE)
                ]
            else:
                studio = [
                    c for c in candidates
                    if not re.search(r"\b(live|single|ep)\b", c[1], re.IGNORECASE)
                ]
                pool = studio or candidates
            if not pool:
                return None
            pool.sort(key=lambda c: (c[0] == "", c[0]), reverse=True)
            chosen = pool[0][1] if pool else None
            if not chosen:
                return None
            return {"command": f"Play the album {chosen} by {artist} on Apple Music"}
        except Exception:
            return None
    if not title:
        return None
    try:
        rows = _catalog(term=f"{title} {artist}" if artist else title, entity="album")
        row = rows[0] if rows else None
        canonical_title = _safe_name((row or {}).get("collectionName"))
        canonical_artist = _safe_name((row or {}).get("artistName"))
        if not canonical_title:
            return None
        artist_used = canonical_artist or artist or ""
        if artist_used:
            return {"command": f"Play the album {canonical_title} by {artist_used} on Apple Music"}
        return {"command": f"Play the album {canonical_title} on Apple Music"}
    except Exception:
        return None


def _extract_featured(text):
    """Return the featured artist from collab phrasing.

    Recognizes: "featuring X", "feat. X", "ft. X", "collaboration with X",
    "collab with X", "collaborating with X". Greedy capture supports multi-word
    names; call after _strip_verbs so a trailing provider is already gone.
    Returns (artist, None) or (None, None).
    """
    # Two explicit shapes so the captured artist never includes a "with".
    patterns = (
        r"\b(?:featuring|feat\.?|ft\.?)\s+"
        r"([A-Za-z0-9][A-Za-z0-9'&.\- ]{1,60})(?:[,.!]|$)",
        r"\b(?:collaboration|collab|collaborating)\s+with\s+"
        r"([A-Za-z0-9][A-Za-z0-9'&.\- ]{1,60})(?:[,.!]|$)",
    )
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        artist = _safe_name(m.group(1).strip())
        if artist:
            return artist, None
    return None, None


def _extract_primary_artist(text):
    """Return the primary artist named before a feature clause, or "".

    In "latest song by Tove Lo, featuring Stromae" the primary artist is
    Tove Lo. Capture stops at the feature marker or sentence punctuation so
    the feature clause never leaks into the artist name.
    """
    m = re.search(
        r"\bby\s+([A-Za-z0-9][A-Za-z0-9'&.\- ]*?)"
        r"(?:\s+(?:featuring|feat\.?|ft\.?|collaboration\s+with|collab\s+with|collaborating\s+with)|[,.!?]|$)",
        text, re.IGNORECASE)
    if not m:
        return ""
    artist = _safe_name(m.group(1).strip())
    return artist or ""


def _featured_artist(text):
    """Return the featured artist after a feature marker, or ''.

    Captures the leading chunk of '…featuring X' (commas and descriptor words
    end the capture) so trailing clutter — 'early in the summer', 'her latest
    song' — never leaks into the featured-artist slot.
    """
    m = re.search(
        r"\b(?:featuring|feat\.?|ft\.?|collaboration\s+with|collab\s+with|collaborating\s+with)\s+"
        r"([A-Za-z0-9][A-Za-z0-9'&.\- ]*?)\s*(?:,|\.|;|!|\?|$)",
        text, re.IGNORECASE)
    if not m:
        return ""
    raw = m.group(1).strip()
    raw = re.split(r",", raw, maxsplit=1)[0].strip()
    # Trim any trailing descriptor clause (" early in the summer", " latest
    # song") off the featured spot; a multi-word artist like "David Bowie"
    # is untouched because it contains none of these boundary words.
    raw = re.sub(
        r"\s+(?:early|late|in|on|at|for|with|the|her|his|their|they|"
        r"latest|newest|new|now|song|track|album|full|more|very)\b.*$",
        "", raw, flags=re.IGNORECASE).strip()
    return _safe_name(raw) or ""


def _split_suspect_slots(text):
    """Return ``(primary_slot, featured_slot)`` suspect artist names.

    Split 'X featuring Y' into two independent cross-reference slots so the DB
    is consulted for each separately — never for the whole, garbled sentence.
    The primary slot is stripped of a leading ``play`` prefix; the featured
    slot drops trailing descriptors down to its first clause.
    """
    stripped = re.sub(r"\s+", " ", str(text or "").strip())
    featured = _featured_artist(stripped)
    primary_pat = re.search(
        r"^\s*(?:(?:play|put\s+on|put\s+some|play\s+some|shuffle|play\s+me|and)\s+)?"
        r"([A-Za-z0-9][A-Za-z0-9'&.\- ]*?)\s*"
        r"(?:,|\.|;|!|\?|\s+(?:featuring|feat\.?|ft\.?|collaboration\s+with|collab\s+with|collaborating\s+with)|$)",
        stripped, re.IGNORECASE)
    primary = _safe_name(primary_pat.group(1).strip()) if primary_pat else ""
    return (primary or ""), (featured or "")


def _is_plausible_artist(slot):
    """True when a manual primary slot is an artist, not request qualifiers.

    'To The Loop' (a garbled artist) is offered for repair; 'latest song',
    'the album', 'some music' are pure noise before a feature marker and are
    NOT treated as a primary artist."""
    if not slot:
        return False
    noise = {
        "a", "an", "the", "some", "any", "me", "all", "my", "more", "of", "for",
        "play", "playing", "song", "songs", "album", "track", "music", "single",
        "playlist", "latest", "newest", "recent", "new", "favourite", "favorite",
        "just", "only", "with", "and", "its", "it's", "now", "very", "this",
    }
    tokens = [t.casefold() for t in slot.split()]
    return bool(tokens) and any(t not in noise for t in tokens)


def _repair_artist_slot(term):
    """Verify ONE suspect artist name against the DB via a Jev Choice.

    Per the Jev gist: if an ordinary check already decides (the verbatim word
    is itself a known artist in the vocabulary), don't spend a judgement on it —
    only run the bounded Choice for genuinely mangled names. Returns the chosen
    canonical artist name, or None when unverifiable.
    """
    term = re.sub(r"\s+", " ", (term or "").strip())
    if len(term) < 2:
        return None
    vocab_keys = [v.casefold() for v in _load_artist_vocabulary()]
    suspect_key = term.casefold()
    if suspect_key in vocab_keys:
        # Return the canonical spelling from the vocabulary, not the possibly
        # lowercased verbatim transcript ("stromae" -> "Stromae").
        canonical = next(v for v in _load_artist_vocabulary() if v.casefold() == suspect_key)
        return canonical
    cands = _repair_candidates([term])
    if len(cands) < 2:
        return None
    # Context containment per the Jev gist: a slot's Choice must see ONLY that
    # slot's suspect term — never the whole collab phrase ('X featuring Y'),
    # which already carries the other, possibly-correct name and would let Jev
    # score it over the mangled slot. `term` isolates the evidence.
    state = {"transcript": term, "suspect_term": term, "candidates": cands}
    try:
        chosen, conf = _jev_select_artist(state, None, cands)
    except TypeSafeUnavailable:
        raise
    except Exception:
        return None
    if chosen == "none" or conf < STT_REPAIR_MIN_CHOICE_CONFIDENCE:
        return None
    if chosen.isdigit():
        idx = int(chosen)
        return cands[idx] if 0 <= idx < len(cands) else None
    return chosen if chosen in cands else None


def _resolve_collab_artists(primary, featured, transcript, confidence):
    """Resolve a 'X featuring Y' request to a by-artist command.

    Both artist slots are Jev-verified against the DB cross-reference; the
    command is 'Play songs by X & Y' — never an invented track title. If a slot
    cannot be confidently verified, ask rather than guess.
    """
    p_slot = _repair_artist_slot(primary) if primary else None
    f_slot = _repair_artist_slot(featured) if featured else None

    # Never emit a duplicate pair ("Tove Lo & Tove Lo") — a verification that
    # collapses both slots to one artist resolves to that single artist.
    if p_slot and f_slot and p_slot.casefold() == f_slot.casefold():
        f_slot = None

    pair = " & ".join(n for n in (p_slot, f_slot) if n)
    if not pair:
        return None
    lead = "Shuffle" if bool(re.search(r"\bshuffle\b", transcript, re.IGNORECASE)) else "Play"
    _write_telemetry({"outcome": "collab_by_artist", "primary": p_slot, "featured": f_slot})
    return {"command": f"{lead} songs by {pair} on Apple Music"}


def _resolve_genre_playlist(intent, name):
    name = (name or "").strip()
    if not name:
        return None
    if intent["kind"] == "genre":
        lead = "Shuffle" if intent["shuffle"] else "Play"
        return {"command": f"{lead} {name} music on Apple Music"}
    return {"command": f"Play the playlist {name} on Apple Music"}


def _has_secondary_action(text: str) -> bool:
    """Refuse requests that chain a secondary imperative after the music ask."""
    remainder = re.sub(
        r"^\s*(?:play|put\s+on|put\s+some|play\s+some|shuffle|play\s+me)\s+",
        "", text, flags=re.IGNORECASE).strip()
    if not remainder:
        return False
    return bool(re.search(
        r"\b(?:and|then|after|plus)\s+(?:play|stop|pause|unlock|turn\s+"
        r"(?:on|off)|open|close|set|dim)\b",
        remainder,
        re.IGNORECASE,
    ))


def _load_artist_vocabulary():
    """Load the STT-repair artist vocabulary (Meloman's growing list)."""
    import os as _os
    path = _os.path.expanduser(
        "~/.hermes/profiles/music/skills/music-domain/references/artist-vocabulary.txt"
    )
    try:
        with open(path) as f:
            return [
                line.strip() for line in f
                if line.strip() and not line.startswith("#")
            ]
    except OSError:
        return []


def _artist_db_dsn():
    """Local Postgres artist index DSN (no secret; socket trust is local-only)."""
    return os.getenv(
        "HERMES_MUSIC_ARTIST_DB_DSN",
        "host=/home/john/projects/postgres-hosting.workspace/data/postgres-socket dbname=palladium user=john",
    )


ARTIST_DUCKDB_PATH = (
    "/home/john/projects/postgres-hosting.workspace/data/music-artist-index/"
    "duckdb/artist_index.duckdb"
)
ARTIST_DUCKDB_WORKDIR = (
    "/home/john/projects/postgres-hosting.workspace/data/music-artist-index/duckdb"
)
ARTIST_DUCKDB_THREADS = 14
ARTIST_DUCKDB_MEMORY_LIMIT = "4GB"
ARTIST_DUCKDB_JARO_FLOOR = 0.72
_ARTIST_DUCKDB_CONNECTION = None
_ARTIST_DUCKDB_CONNECTION_PATH = None
_ARTIST_DUCKDB_LOCK = threading.RLock()


def _get_artist_duckdb_connection():
    """Open/configure the native index once per gateway process.

    DuckDB Python connections are not safe for overlapping mutations/query
    setup, so all callers serialize through ``_ARTIST_DUCKDB_LOCK``. Tests may
    replace ``ARTIST_DUCKDB_PATH``; a path change closes the old connection.
    """
    global _ARTIST_DUCKDB_CONNECTION, _ARTIST_DUCKDB_CONNECTION_PATH
    import duckdb

    path = ARTIST_DUCKDB_PATH
    if _ARTIST_DUCKDB_CONNECTION is not None and _ARTIST_DUCKDB_CONNECTION_PATH == path:
        return _ARTIST_DUCKDB_CONNECTION
    if _ARTIST_DUCKDB_CONNECTION is not None:
        try:
            _ARTIST_DUCKDB_CONNECTION.close()
        except Exception:
            pass
    work = ARTIST_DUCKDB_WORKDIR
    temp_dir = os.path.join(work, "tmp")
    extension_dir = os.path.join(work, "extensions")
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(extension_dir, exist_ok=True)
    con = duckdb.connect(path, read_only=True)
    con.execute("SET temp_directory = ?", [temp_dir])
    con.execute("SET extension_directory = ?", [extension_dir])
    con.execute("SET home_directory = ?", [work])
    con.execute("SET memory_limit = ?", [ARTIST_DUCKDB_MEMORY_LIMIT])
    con.execute("SET threads = ?", [int(ARTIST_DUCKDB_THREADS)])
    con.execute("SET preserve_insertion_order = false")
    _ARTIST_DUCKDB_CONNECTION = con
    _ARTIST_DUCKDB_CONNECTION_PATH = path
    return con


# Minimum fuzzy score for an artist to be offered as a repair candidate.
# 0.6 keeps phonetic hits (0.92) and strong spelling matches (levenshtein/
# trigram >= 0.6) while dropping the 0.2-0.4 lookalike noise.
ARTIST_MATCH_SCORE_FLOOR = 0.6

# A phonetic (dmetaphone) hit is evidence, but weak on its own: "Tuvalu"
# dmetaphones to TFL, which also matches "D Flow", "Da'Ville" and "Duvall".
# Give every phonetic hit the same score so the final ordering is decided by
# fame/popularity rather than by which lookalike happens to share more letters.
ARTIST_PHONETIC_SCORE = 0.9

# How many phonetic lookalikes to admit. A common code returns many; a short
# list keeps the chooser decisive instead of drowning the right answer.
ARTIST_PHONETIC_LIMIT = 6


def _levenshtein(a: str, b: str) -> int:
    """Plain Levenshtein distance for short strings (candidate scoring only)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _normalize_artist_key(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _duckdb_artist_candidates(term, limit=12):
    """Return artist repair candidates from the native read-only DuckDB index.

    The primary list is phonetic and fame-ranked; Jaro-Winkler fills spelling
    misses such as ``radiohed``/``Radiohead`` and ``beetles``/``The Beatles``.
    DuckDB configuration is explicit per the upstream configuration contract:
    absolute working paths, bounded memory/temp, parallel scan threads, and no
    insertion-order preservation overhead.
    """
    cleaned = re.sub(r"\s+", " ", str(term or "").strip())
    if len(cleaned) < 2 or not os.path.isfile(ARTIST_DUCKDB_PATH):
        return []
    try:
        from metaphone import doublemetaphone
    except Exception:
        return []

    try:
        with _ARTIST_DUCKDB_LOCK:
            con = _get_artist_duckdb_connection()
            names, seen = [], set()

            def add(name):
                if not name:
                    return
                key = str(name).casefold()
                if key not in seen:
                    seen.add(key)
                    names.append(str(name))

            dm = (doublemetaphone(cleaned)[0] or "").strip()
            if dm:
                rows = con.execute(
                    """
                    select canonical_name
                    from artist_names
                    where name_dm = ?
                    order by fame desc, popularity desc
                    limit ?
                    """,
                    [dm, min(ARTIST_PHONETIC_LIMIT, int(limit))],
                ).fetchall()
                for row in rows:
                    add(row[0])

            # Vectorized scan of the native 2.9M-row file. On this host it is
            # ~35 ms at 14 threads; unlike Postgres, no network/socket roundtrip.
            rows = con.execute(
                """
                select canonical_name
                from artist_names
                where jaro_winkler_similarity(lower(name), ?) >= ?
                order by jaro_winkler_similarity(lower(name), ?) desc,
                         fame desc, popularity desc
                limit ?
                """,
                [cleaned.lower(), ARTIST_DUCKDB_JARO_FLOOR,
                 cleaned.lower(), int(limit)],
            ).fetchall()
            for row in rows:
                add(row[0])
            return names[:limit]
    except Exception:
        return []


def _postgres_artist_candidates(term, limit=12):
    """Return fuzzy artist candidates from the local Postgres artist index.

    Two index-assisted lookups — GiST trigram KNN plus a phonetic (dmetaphone)
    btree probe — are merged and scored in Python. Scoring deliberately stays
    out of SQL: a per-row similarity()/levenshtein() predicate over the ~2.9M
    row table is a sequential scan and blows the voice latency budget.

    Candidates below ARTIST_MATCH_SCORE_FLOOR are dropped: the trigram KNN
    happily returns 0.2–0.4 lookalikes ("Tush"/"Tusk" for "Tuvalu") that add
    nothing but noise, and a diluted list measurably lowers the chooser's
    confidence in the right answer.
    """
    cleaned = re.sub(r"\s+", " ", str(term or "").strip())
    if len(cleaned) < 2:
        return []
    try:
        import psycopg
    except Exception:
        return []

    q = cleaned.lower()
    qnorm = _normalize_artist_key(cleaned)
    pool = {}

    def spelling_score(name):
        norm = _normalize_artist_key(name)
        denom = max(len(norm), len(qnorm), 1)
        return 1.0 - (_levenshtein(norm, qnorm) / denom)

    def consider(canonical, name, popularity, fame, score):
        if not canonical or score < ARTIST_MATCH_SCORE_FLOOR:
            return
        score = min(1.0, score)
        pop = int(popularity or 0)
        fam = int(fame or 0)
        prev = pool.get(canonical)
        if prev is None or (score, pop, fam) > (prev[0], prev[1], prev[2]):
            pool[canonical] = (score, pop, fam)

    try:
        with psycopg.connect(_artist_db_dsn(), connect_timeout=1) as conn:
            with conn.cursor() as cur:
                cur.execute("set local search_path = music, iptvchannels, public")
                cur.execute("select dmetaphone(%s)", (cleaned,))
                row = cur.fetchone()
                dm = (row[0] or "") if row else ""

                # (1) Trigram KNN over the notable subset. fame > 0 matches the
                # partial GiST index and keeps this at ~20ms instead of the
                # ~560ms a full-table KNN costs. Obscure unrated artists are
                # still reachable via the phonetic probe below.
                cur.execute(
                    """
                    select canonical_name, name, popularity, fame,
                           (lower(name) <-> %s) as dist
                    from music.artist_names
                    where fame > 0
                    order by lower(name) <-> %s
                    limit 60
                    """,
                    (q, q),
                )
                for canonical, name, popularity, fame, dist in cur.fetchall():
                    trigram = max(0.0, 1.0 - float(dist or 1.0))
                    consider(
                        canonical, name, popularity, fame,
                        max(trigram, spelling_score(name)),
                    )

                # (2) Phonetic matches — btree probe on the dmetaphone key.
                # Ordered by fame so the most notable holder of the code leads,
                # and capped tight: a common code ("Tuvalu" -> TFL) matches many
                # obscure lookalikes, and a long diluted list lowers the
                # chooser's confidence in the right answer.
                if dm:
                    cur.execute(
                        """
                        select canonical_name, name, popularity, fame
                        from music.artist_names
                        where name_dm = %s
                        order by fame desc nulls last, popularity desc
                        limit %s
                        """,
                        (dm, ARTIST_PHONETIC_LIMIT),
                    )
                    for canonical, name, popularity, fame in cur.fetchall():
                        consider(canonical, name, popularity, fame, ARTIST_PHONETIC_SCORE)
    except Exception:
        return []

    ranked = sorted(pool.items(), key=lambda kv: (-kv[1][0], -kv[1][1], -kv[1][2], kv[0]))
    return [canonical for canonical, _ in ranked[:limit]]


STT_REPAIR_CONFIDENCE_FLOOR = 0.5
# Minimum chooser confidence to accept a repair. Kept conservative: the chooser
# settles near 0.45 on genuinely ambiguous phonetic garble ("Tuvalu" matches
# both "Tove Lo" and "Duvall"), and accepting a marginal pick hands the
# downstream song-research path a wrong artist, which can produce a confident
# but wrong playback command. Below this, ask the speaker to repeat instead.
STT_REPAIR_MIN_CHOICE_CONFIDENCE = 0.6


def _repair_candidates(terms, limit=12):
    """Cross-reference artist candidates for one or more suspect slots.

    ``terms`` is a single suspect string or a list (primary, featured, …). For
    each slot the famous-artist index is queried; the curated listening
    vocabulary is then ALWAYS folded in with a reserved quota so it can never
    be starved. This is the crucial bit: when Whisper mangles a name beyond
    any phonetic recovery (``Toogaloo`` -> ``Tove Lo``), the owner's own
    listening list — which contains the real artist — is still offered to the
    chooser. Quota: the index never consumes the whole budget, and a live
    catalog search on a garbled term is the noisiest so it only fills gaps.
    """
    if isinstance(terms, str):
        terms = [terms]
    slots = [re.sub(r"\s+", " ", str(t).strip()) for t in terms if t and str(t).strip()]
    names, seen = [], set()

    def add(name):
        if not name:
            return
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            names.append(name)

    # 1. Curated listening vocabulary FIRST. It is the strongest prior on
    #    "who does this owner ask for": when Whisper mangles a name (Tubaloo ->
    #    Tove Lo), the owner's own artists are the likeliest intent. But the
    #    vocab is larger than the pool, so it must not monopolize every seat —
    #    reserve room for DB phonetic hits, which anchor Jev to the actual
    #    sounds spoken (Tubaloo -> Tauboo/Table) when the listening-list artist
    #    is ambiguous.
    vocab_budget = max(0, limit - 4)
    for n in _load_artist_vocabulary():
        if len(names) >= vocab_budget:
            break
        add(n)

    # 2. Index cross-reference per suspect slot fills the remaining seats.
    #    Postgres is authoritative; DuckDB is the fast read replica fallback.
    for slot in slots:
        indexed = _duckdb_artist_candidates(slot, limit=limit)
        if not indexed:
            indexed = _postgres_artist_candidates(slot, limit=limit)
        for n in indexed:
            if len(names) >= limit:
                break
            add(n)

    # 3. Live catalog on the primary suspect (noisiest) — fill gaps only.
    if slots and len(names) < limit:
        try:
            for row in _catalog(term=slots[0], entity="song", attribute="artistTerm", limit=50):
                if len(names) >= limit:
                    break
                add(_safe_name(row.get("artistName")))
        except Exception:
            pass
    return names[:limit]


def _jev_select_artist(state, questions, candidates):
    """Ask Jev which candidate the speaker most likely named. Returns (choice, confidence)."""
    from typesafe_sdk import Choice, TypeSafeClient

    key = _load_api_key()
    if not key:
        raise TypeSafeUnavailable(UNAVAILABLE)
    criteria = {str(i): name for i, name in enumerate(candidates)}
    criteria["none"] = "None of these"
    with TypeSafeClient(api_key=key) as client:
        response = client.system_one(
            model="jev-latest",
            state=state,
            questions={
                "which": Choice(
                    instructions={
                        "question": (
                            "A speech transcript may have garbled an artist name "
                            "in `transcript`. Which candidate is the speaker most "
                            "likely to have said? Pick none if nothing matches."
                        ),
                        "transcript": state.get("transcript", ""),
                    },
                    criteria=criteria,
                )
            },
        )
    chosen = str(getattr(response.answers["which"], "choice", "") or "none")
    conf = float(getattr(response.answers["which"], "confidence", 0.0) or 0.0)
    return chosen, conf


def _best_slot_span(text, slots, repaired):
    """Return ``(start, end)`` of the suspect slot most resembling the repaired
    name, so the rewrite lands on the right artist (primary vs featured)."""
    repaired_key = _normalize_artist_key(repaired)
    best_span = None
    best_score = -1.0
    for slot in slots:
        if not slot:
            continue
        pat = re.compile(re.escape(slot), re.IGNORECASE)
        match = pat.search(text)
        if not match:
            continue
        slot_key = _normalize_artist_key(slot)
        denom = max(len(slot_key), len(repaired_key), 1)
        score = 1.0 - (_levenshtein(slot_key, repaired_key) / denom)
        if score > best_score:
            best_score = score
            best_span = (match.start(), match.end())
    return best_span


def _repair_artist(text, confidence):
    """Below the confidence floor, offer real candidates to Jev for selection.

    Returns the repaired text, or CLARIFY when no candidate wins. Select,
    never generate: candidates come only from the catalog and the listening
    vocabulary.
    """
    if confidence >= STT_REPAIR_CONFIDENCE_FLOOR:
        return text
    stripped = str(text).strip()
    # Prefer the primary artist slot ("by Tupelo featuring Stormlight") so a
    # feature clause does not leak into the repair term.
    primary = _extract_primary_artist(stripped)
    m = re.search(
        r"\b(?:by|artist)\s+([A-Za-z0-9][A-Za-z0-9'&.\- ]*?)"
        r"(?:\s+(?:featuring|feat\.?|ft\.?|collaboration\s+with|collab\s+with|collaborating\s+with)|[,.!?]|$)",
        stripped, re.IGNORECASE
    )
    play_m = None
    if not primary and not m:
        play_m = re.search(
            r"^\s*(?:play|put\s+on|put\s+some|play\s+some|shuffle|play\s+me)\s+"
            r"([A-Za-z0-9][A-Za-z0-9'&.\- ]*?)"
            r"(?:\s*,\s*(?:her|his|their|the)\b|\s+(?:her|his|their)\s+(?:latest|newest|new|song|track)\b|[,.!?]|$)",
            stripped,
            re.IGNORECASE,
        )
    # Cross-reference EVERY suspect slot independently: the primary artist,
    # the featured artist, plus verbatim captures. Whisper may mangle either
    # name, so the DB must be consulted for each — never a whole sentence.
    _from_regex = (m and m.group(1)) or (play_m and play_m.group(1)) or ""
    primary_slot, featured_slot = _split_suspect_slots(stripped)
    slots, seen_s = [], set()
    for s in (primary, _from_regex.strip(), primary_slot, featured_slot):
        s = re.sub(r"^\s+|\s+$", "", s or "")
        s = s.strip(".,!?")
        if s and s.casefold() not in seen_s:
            seen_s.add(s.casefold())
            slots.append(s)
    if not slots:
        return text
    cands = _repair_candidates(slots)
    if len(cands) < 2:
        return text
    state = {
        "transcript": stripped,
        "suspect_term": slots[0],
        "candidates": cands,
    }
    try:
        chosen, conf = _jev_select_artist(state, None, cands)
    except TypeSafeUnavailable:
        raise
    except Exception:
        return text
    if chosen == "none" or conf < STT_REPAIR_MIN_CHOICE_CONFIDENCE:
        # No confident winner: ask rather than guess.
        return None
    if chosen.isdigit():
        idx = int(chosen)
        if idx >= len(cands):
            return None
        repaired = cands[idx]
    elif chosen in cands:
        repaired = chosen
    else:
        return None
    _write_telemetry({"outcome": "stt_repaired", "from": slots[0], "to": repaired})
    # Anchor the repaired artist to the slot it replaces — the primary slot
    # if the matched name most resembles it, else the featured slot.
    span = _best_slot_span(stripped, slots, repaired)
    if span:
        return stripped[:span[0]] + repaired + stripped[span[1]:]
    return repaired


def _normalize_evidence(text, confidence, evidence=None):
    """Return immutable STT hypotheses in a small, validated wire shape.

    Confidence values that are missing, non-finite, or outside [0, 1] are
    treated as 0.0 (no confidence). This guarantees a NaN or out-of-range
    value can never slip past a ``< floor`` gate and dispatch on degraded
    evidence.
    """
    rows = evidence or [
        {"text": str(text or ""), "confidence": float(confidence), "source": "initial"}
    ]
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        hypothesis = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
        if not hypothesis:
            continue
        try:
            raw_conf = float(row.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            raw_conf = 0.0
        if not math.isfinite(raw_conf) or not (0.0 <= raw_conf <= 1.0):
            raw_conf = 0.0
        normalized.append(
            {
                "text": hypothesis,
                "confidence": raw_conf,
                "source": str(row.get("source") or "unknown"),
            }
        )
    return normalized


def _evidence_is_incomplete(evidence):
    """True when any STT pass exposes an unfinished artist relationship."""
    unfinished = re.compile(
        r"\b(?:and|with|featuring|feat\.?|ft\.?)\s*(?:\.\.\.)?\s*[.!?]*$",
        re.IGNORECASE,
    )
    return any(unfinished.search(row["text"]) for row in evidence)


# Bounded size of the DYNAMIC real-artist candidate set used to assist a
# re-listen. Deliberately SMALL: the re-decode lab proved broad lists (100+)
# dilute the bias and make Whisper worse, while a small acoustically-targeted
# set in the initial_prompt recovers the real names. Keep it tight.
MUSIC_HOTWORD_POOL_CAP = int(os.getenv("HERMES_MUSIC_HOTWORD_CAP", "16"))


def _top_artist_hotwords(limit):
    """Return the most-known real artist names from the DuckDB pool."""
    if limit <= 0 or not os.path.isfile(ARTIST_DUCKDB_PATH):
        return []
    try:
        with _ARTIST_DUCKDB_LOCK:
            con = _get_artist_duckdb_connection()
            rows = con.execute(
                "select canonical_name from artist_names "
                "order by popularity desc, fame desc limit ?",
                [limit],
            ).fetchall()
            return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def _build_music_redecode_hotwords(text, cap=MUSIC_HOTWORD_POOL_CAP):
    """Compose a SMALL, DYNAMIC real-artist re-listen prior from the live pool.

    Order is deliberate, strongest signal first:
      1. OWNER LISTENING VOCABULARY — the artists this owner actually asks for
         (Tove Lo, Stromae, ...). For real spoken requests these are the truth;
         the re-decode lab proves a small set containing the truth recovers it.
      2. PER-SLOT DB candidates — phonetic matches on the heard garble, which
         supply the near-miss shapes Whisper needs to bridge to the truth.
      3. Top-popularity fill — bounded safety net for artists not in 1 or 2.

    All deduped and bounded by ``cap``. Source stays the live pool (never a
    hardcoded artist list), matching the dynamic-pool rule.
    """
    cap = max(1, int(cap))
    text = str(text or "")
    slots = _evidence_slots(
        [{"text": text, "confidence": 1.0, "source": "initial"}]
    )
    names, seen = [], set()

    def add(name):
        name = str(name or "").strip()
        if not name or len(names) >= cap:
            return
        key = name.casefold()
        if key in seen:
            return
        seen.add(key)
        names.append(name)

    for name in _load_artist_vocabulary():
        add(name)

    for slot in slots[:6]:
        for candidate in _duckdb_artist_candidates(slot, limit=cap):
            add(candidate)

    if len(names) < cap:
        for candidate in _top_artist_hotwords(limit=cap):
            add(candidate)
            if len(names) >= cap:
                break

    return "\n".join(names)


def _evidence_slots(evidence):
    """Extract candidate-search terms from every hypothesis without rewriting it."""
    slots, seen = [], set()
    relation = re.compile(
        r"\s+(?:and|with|featuring|feat\.?|ft\.?|collaboration\s+with)\s+",
        re.IGNORECASE,
    )
    for row in evidence:
        phrase = _strip_verbs(row["text"])
        phrase = re.sub(r"^(?:that|the)\s+(?:song|track)\s+by\s+", "", phrase, flags=re.IGNORECASE)
        phrase = re.sub(r"^.*?\bby\s+", "", phrase, count=1, flags=re.IGNORECASE)
        for part in relation.split(phrase):
            part = part.strip(" .,!?")
            if not part or part.casefold() in {"that song", "the song", "song", "track"}:
                continue
            key = part.casefold()
            if key not in seen:
                seen.add(key)
                slots.append(part)
    return slots


def _candidate_records(slots, limit=24):
    """Build balanced per-slot DB choices, then fill with the owner prior.

    Each detected slot gets an independent DuckDB quota before another slot or
    the listening vocabulary can consume the bounded choice set. This prevents
    the first garble from starving a clearly heard second performer.
    """
    clean_slots = [str(slot).strip() for slot in slots if str(slot).strip()]
    names, sources, seen = [], {}, set()

    def add(name, source):
        if not name:
            return
        key = str(name).casefold()
        if key in seen or len(names) >= limit:
            return
        seen.add(key)
        names.append(str(name))
        sources[key] = source

    per_slot = max(2, limit // max(len(clean_slots), 1))
    for slot in clean_slots:
        indexed = _duckdb_artist_candidates(slot, limit=per_slot)
        if not indexed:
            indexed = _postgres_artist_candidates(slot, limit=per_slot)
        for name in indexed[:per_slot]:
            add(name, "artist_database")

    for name in _load_artist_vocabulary():
        add(name, "owner_prior")

    return [
        {"name": name, "source": sources[name.casefold()], "rank": index}
        for index, name in enumerate(names)
    ]


def _jev_plan_music(evidence, candidates):
    """Plan the whole low-confidence request once from all immutable evidence."""
    from typesafe_sdk import Choice, Noul, TypeSafeClient

    key = _load_api_key()
    if not key:
        raise TypeSafeUnavailable(UNAVAILABLE)
    criteria = {str(i): row["name"] for i, row in enumerate(candidates)}
    criteria["none"] = "No candidate is supported by the evidence"
    with TypeSafeClient(api_key=key) as client:
        response = client.system_one(
            model="jev-latest",
            state={"evidence": evidence, "candidates": candidates},
            questions={
                "kind": Choice(
                    instructions=(
                        "Plan the complete music request using every STT hypothesis. "
                        "Do not treat any single hypothesis as authoritative."
                    ),
                    criteria={
                        "artist": "Play music by one artist",
                        "artist_pair": "Play music involving two named artists",
                        "song": "Play a specifically named song",
                        "unclear": "Evidence is incomplete, conflicting, or unsupported",
                    },
                ),
                "primary": Choice(
                    instructions=(
                        "Select the primary performer supported by the complete evidence. "
                        "Candidate order and source are priors, not proof. Choose none rather than guess."
                    ),
                    criteria=criteria,
                ),
                "secondary": Choice(
                    instructions=(
                        "Select a distinct second performer only when the evidence supports one. "
                        "Choose none when no complete second name is audible."
                    ),
                    criteria=criteria,
                ),
                "complete": Noul(
                    instructions=(
                        "Is the whole requested music target complete enough to execute, "
                        "with every required performer present and no truncated relationship?"
                    )
                ),
            },
        )

    answers = response.answers

    def choice(name):
        answer = answers[name]
        selected = str(getattr(answer, "choice", "") or "none")
        conf = float(getattr(answer, "confidence", 0.0) or 0.0)
        if selected.isdigit() and int(selected) < len(candidates):
            return candidates[int(selected)]["name"], conf
        return "", conf

    primary, primary_conf = choice("primary")
    secondary, secondary_conf = choice("secondary")
    kind_answer = answers["kind"]
    return {
        "kind": str(getattr(kind_answer, "choice", "") or "unclear"),
        "kind_confidence": float(getattr(kind_answer, "confidence", 0.0) or 0.0),
        "primary": primary,
        "primary_confidence": primary_conf,
        "secondary": secondary,
        "secondary_confidence": secondary_conf,
        "complete": float(getattr(answers["complete"], "noul", 0.0) or 0.0) > _YES,
    }


def _is_anaphoric_song_request(evidence):
    """True only when 'that/the song' stands in for a missing title.

    Named titles ("the song Words") and descriptive clues ("that song from
    Titanic", "that song where they say …") carry real lookup evidence.
    """
    return any(
        re.search(
            r"\b(?:that|the)\s+(?:song|track)(?:\s+by\b[^.!?]*)?\s*[.!?]*$",
            row["text"],
            re.IGNORECASE,
        )
        is not None
        for row in evidence
    )


def _authoritative_recovered_artist(rows):
    """A single high-confidence hypothesis that names a DB-CONFIRMED artist is
    authority over lower-confidence conflicting garble (e.g. pass-1 mangled
    "To Be Loved" @0.40 vs assisted re-decode "Tove Lo" @0.82). Return the
    canonical artist, or '' when nothing clears the bar."""
    best = max(rows, key=lambda r: r.get("confidence", 0.0), default=None)
    if not best or float(best["confidence"]) < STT_REPAIR_MIN_CHOICE_CONFIDENCE:
        return ""
    slot = _latest_artist_name(best["text"])
    if not slot:
        slots = _evidence_slots([best])
        slot = slots[0] if slots else ""
    if not slot:
        return ""
    cands = _duckdb_artist_candidates(slot, limit=1)
    if cands and cands[0].casefold() == str(slot).casefold():
        return cands[0]
    return ""


def _resolve_low_confidence_evidence(text, confidence, evidence):
    """Validate one full music plan; never mutate and re-classify pass one."""
    rows = _normalize_evidence(text, confidence, evidence)
    if not rows or _evidence_is_incomplete(rows):
        _write_telemetry({"outcome": "evidence_incomplete"})
        return {"clarification": "I didn't catch the complete artist request — please say it again."}

    # A clean high-confidence DB-backed recovery beats conflicting low-confidence
    # garble refactored: the assisted re-listen is itself the recovery step, so
    # when it lands a DB-confirmed artist above the plan floor, use it. This is
    # the whole point of the DB lookup — do NOT let pass-1's noise deadlock Jev.
    authoritative = _authoritative_recovered_artist(rows)
    if authoritative:
        best_row = max(rows, key=lambda r: r.get("confidence", 0.0))
        lkind, _ = _latest_request(best_row["text"])
        intent = {"shuffle": False}
        if lkind == "album":
            result = _resolve_latest_album(authoritative) or _resolve_artist(intent, authoritative)
        elif lkind == "song":
            result = _resolve_latest_track(authoritative) or _resolve_artist(intent, authoritative)
        else:
            result = _resolve_artist(intent, authoritative)
        if result:
            _write_telemetry(
                {"outcome": "evidence_authoritative_recovered", "artist": authoritative}
            )
            return result

    slots = _evidence_slots(rows)
    candidates = _candidate_records(slots)
    if len(candidates) < 2:
        _write_telemetry({"outcome": "evidence_no_candidates"})
        return {"clarification": "I didn't catch the artist — who did you mean?"}
    plan = _jev_plan_music(rows, candidates)
    if (
        not plan.get("complete")
        or plan.get("kind") in {"", "unclear"}
        or float(plan.get("kind_confidence", 0.0)) < STT_REPAIR_MIN_CHOICE_CONFIDENCE
    ):
        _write_telemetry({"outcome": "evidence_plan_unresolved"})
        return {"clarification": "I didn't catch the complete artist request — please say it again."}
    primary = str(plan.get("primary") or "")
    secondary = str(plan.get("secondary") or "")
    if not primary or float(plan.get("primary_confidence", 0.0)) < STT_REPAIR_MIN_CHOICE_CONFIDENCE:
        return {"clarification": "I didn't catch the artist — who did you mean?"}
    if plan.get("kind") == "song" and _is_anaphoric_song_request(rows):
        return dict(CLARIFY)
    if plan.get("kind") == "artist_pair":
        if (
            not secondary
            or secondary.casefold() == primary.casefold()
            or float(plan.get("secondary_confidence", 0.0)) < STT_REPAIR_MIN_CHOICE_CONFIDENCE
        ):
            return {"clarification": "I didn't catch both artists — please say them again."}
        _write_telemetry(
            {"outcome": "evidence_plan", "kind": "artist_pair", "primary": primary, "secondary": secondary}
        )
        return {"command": f"Play songs by {primary} & {secondary} on Apple Music"}
    if plan.get("kind") == "artist":
        intent = {"shuffle": False}
        return _resolve_artist(intent, primary) or {"clarification": "Which artist do you mean?"}
    return dict(CLARIFY)


_LATEST_TRACK_RE = re.compile(
    r"^(?:the\s+)?(?:latest|newest|recent|brand\s*new)\s+"
    r"(?:single|song|track|release)"
    r"\s*(?:from|by|feat\.?|featuring|ft\.?)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_LATEST_ALBUM_RE = re.compile(
    r"^(?:the\s+)?(?:latest|newest|recent|brand\s*new)\s+"
    r"(?:album|record|ep|lp)"
    r"\s*(?:from|by|feat\.?|featuring|ft\.?)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_LATEST_POSSESSIVE_TRACK_RE = re.compile(
    r"^(.+?)'s\s+(?:latest|newest|recent)\s+(?:single|song|track|release)\s*$",
    re.IGNORECASE,
)
_LATEST_POSSESSIVE_ALBUM_RE = re.compile(
    r"^(.+?)'s\s+(?:latest|newest|recent)\s+album\s*$",
    re.IGNORECASE,
)
_TOP_ALBUM_RE = re.compile(
    r"^(?:a\s+|the\s+)?(?:top|best|most\s+popular)\s+album\s+(?:by|from)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_TOP_POSSESSIVE_ALBUM_RE = re.compile(
    r"^(.+?)'s\s+(?:top|best|most\s+popular)\s+album\s*$",
    re.IGNORECASE,
)
_TOP_TRACK_RE = re.compile(
    r"^(?:a\s+|the\s+|one\s+of\s+the\s+)?"
    r"(?:top|best|most\s+popular)\s+(?:song|track)s?\s+(?:by|from)\s+(.+?)\s*$",
    re.IGNORECASE,
)
# Whisper observed "Play a top song by Dolly Parton" as "Say something by
# Dolly Parton". This is accepted only inside the already-confirmed MUSIC+
# PLAYBACK route and only after the extracted artist is catalog-confirmed.
_VAGUE_TRACK_BY_RE = re.compile(
    r"^(?:say|play|put\s+on)\s+(?:a\s+)?something\s+(?:by|from)\s+(.+?)\s*$",
    re.IGNORECASE,
)


def _latest_request(target):
    """Return ``(subkind, artist)`` for a \"<artist>'s latest song\" / \"the
    newest album from <artist>\" phrasing with no concrete title, or
    ``(\"\", \"\")`` when target names a specific track (never shadowed)."""
    cleaned = _strip_verbs(str(target or ""))
    for pat, subkind in (
        (_LATEST_TRACK_RE, "song"),
        (_LATEST_ALBUM_RE, "album"),
        (_LATEST_POSSESSIVE_TRACK_RE, "song"),
        (_LATEST_POSSESSIVE_ALBUM_RE, "album"),
    ):
        m = pat.match(cleaned)
        if m:
            return subkind, m.group(1).strip()
    return "", ""


def _latest_artist_name(target):
    """Artist from a titless "latest/newest X from/by <artist>" phrasing."""
    _subkind, artist = _latest_request(target)
    return artist


def _top_album_artist(target):
    """Artist from a titleless "top/best album by <artist>" request."""
    cleaned = _strip_verbs(str(target or ""))
    for pat in (_TOP_ALBUM_RE, _TOP_POSSESSIVE_ALBUM_RE):
        match = pat.match(cleaned)
        if match:
            return match.group(1).strip()
    return ""


def _top_track_artist(target):
    """Artist from a titleless top-track request or its observed STT garble."""
    raw = re.sub(r"[.!?]+$", "", str(target or "").strip())
    cleaned = _strip_verbs(raw)
    match = _TOP_TRACK_RE.match(cleaned) or _VAGUE_TRACK_BY_RE.match(raw)
    return match.group(1).strip() if match else ""


def _alexa_spoken_name(value):
    """Turn catalog typography into words Alexa's conversational NLU expects."""
    spoken = re.sub(r"\s*&\s*", " and ", str(value or ""))
    spoken = re.sub(r"[:;]", " ", spoken)
    return re.sub(r"\s+", " ", spoken).strip()


def _album_command(album, artist):
    album = _alexa_spoken_name(album)
    artist = _alexa_spoken_name(artist)
    if artist:
        return {"command": f"Play the album {album} by {artist} on Apple Music"}
    return {"command": f"Play the album {album} on Apple Music"}


def _resolve_top_track(artist):
    """Use Apple's ranked song results for a titleless "top song" request."""
    if not artist:
        return None
    try:
        rows = _catalog(term=artist, entity="song", attribute="artistTerm", limit=50)
    except Exception:
        return None
    artist_key = _normalize_artist_key(artist)
    row = next(
        (
            candidate for candidate in rows
            if _normalize_artist_key(candidate.get("artistName")) == artist_key
            and _safe_name(candidate.get("trackName"))
        ),
        None,
    )
    if not row:
        return None
    title = _alexa_spoken_name(_safe_name(row.get("trackName")))
    canonical_artist = _alexa_spoken_name(_safe_name(row.get("artistName")))
    if not title or not canonical_artist:
        return None
    return {"command": f"Play the song {title} by {canonical_artist} on Apple Music"}


def _resolve_top_album(artist):
    """Use Apple's ranked catalog order for a titleless "top album" request."""
    if not artist:
        return None
    try:
        rows = _catalog(
            term=artist, entity="album", attribute="artistTerm", limit=50
        )
    except Exception:
        return None
    for row in rows:
        canonical = _safe_name(row.get("artistName"))
        album = _safe_name(row.get("collectionName"))
        if not canonical or canonical.casefold() != artist.casefold() or not album:
            continue
        if re.search(r"\s[-–]\s(?:single|ep)$", album, re.IGNORECASE):
            continue
        return _album_command(album, canonical)
    return None


def _resolve_latest_track(artist):
    """\"latest song by <artist>\" -> look up the artist's NEWEST real track in
    the live online catalog (by releaseDate) and emit a specific song command.

    Tracks are fresh and never in the artist DB, so the catalog is the source.
    Deterministic: newest releaseDate among catalog rows that match the artist.
    Returns None when no confirmed latest track exists (no fabrication).
    """
    if not artist:
        return None
    try:
        rows = _catalog(term=artist, entity="song", limit=50)
    except Exception:
        return None
    matches = []
    for r in rows:
        a = _safe_name(r.get("artistName"))
        if a and a.casefold() == artist.casefold():
            t = _safe_name(r.get("trackName"))
            if t:
                matches.append((str(r.get("releaseDate") or ""), t, a))
    if not matches:
        return None
    matches.sort(key=lambda m: m[0], reverse=True)
    _release, track, canonical = matches[0]
    return {"command": f"Play {track} by {canonical} on Apple Music"}


def _resolve_latest_album(artist):
    """\"latest album by <artist>\" -> catalog's newest album by releaseDate."""
    if not artist:
        return None
    try:
        rows = _catalog(term=artist, entity="album", limit=20)
    except Exception:
        return None
    matches = []
    for r in rows:
        a = _safe_name(r.get("artistName"))
        if a and a.casefold() == artist.casefold():
            al = _safe_name(r.get("collectionName"))
            if al:
                matches.append((str(r.get("releaseDate") or ""), al, a))
    if not matches:
        return None
    matches.sort(key=lambda m: m[0], reverse=True)
    _release, album, canonical = matches[0]
    return {"command": f"Play the album {album} by {canonical} on Apple Music"}


from .music_routing import select_route


def resolve(ctx, text, *, confidence=1.0, evidence=None, route=None,
            followup=None, wake_id=None):
    """Select a bounded Jev operation before doing any lookup; never raw dispatch."""
    from .music_routing import execute, probability
    confidence = probability(confidence)
    if not text or _has_secondary_action(str(text)):
        return dict(CLARIFY)
    try:
        pending = followup.read(wake_id) if followup is not None else ''
        route = route or select_route(str(text), confidence=confidence, pending_artist=pending)
        # Evidence is correlated. Only a genuinely above-floor hypothesis can
        # be considered, and it must get its own fresh route decision.
        if evidence and confidence < .6:
            best = max(_normalize_evidence(text, confidence, evidence), key=lambda row: row['confidence'])
            if best['confidence'] >= .6:
                text, confidence = best['text'], best['confidence']
                route = select_route(text, confidence=confidence, pending_artist=pending)
        return execute(route, str(text), confidence, followup=followup, wake_id=wake_id)
    except TypeSafeUnavailable:
        return UNAVAILABLE
    except Exception:
        return dict(CLARIFY)