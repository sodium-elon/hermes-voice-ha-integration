"""Voice stack plugin — local wake-word → STT → LLM → TTS → HA media_player.

P1: Real engine implementations replacing P0 stubs.

Registered tools:
- voice_status   — show engine availability and pipeline state
- voice_enable   — enable continuous voice mode with HA media_player
- voice_disable  — disable voice mode
- voice_speak    — TTS-only: speak text through the configured TTS engine
- voice_listen   — one-shot: listen for a command, transcribe, and return
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import events, music, sessions_api  # music: UNAVAILABLE contract + gate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy engine import (engines have heavy deps — only load when needed)
# ---------------------------------------------------------------------------

_pipeline: Optional[Any] = None  # VoicePipeline instance
_pipeline_lock = threading.Lock()
_voice_ready = threading.Event()

_wake_word_engine: Optional[Any] = None
_stt_engine: Optional[Any] = None
_tts_engine: Optional[Any] = None
_plugin_ctx: Optional[Any] = None


def _get_config() -> Dict[str, Any]:
    """Load voice stack config from plugin.yaml or env."""
    wake_word_model_paths = [
        path.strip()
        for path in os.getenv("HERMES_WAKE_WORD_MODEL", "").split(",")
        if path.strip()
    ]
    return {
        "wake_word": {
            "engine": os.getenv("HERMES_WAKE_WORD_ENGINE", "porcupine"),
            "keyword": os.getenv("HERMES_WAKE_WORD", "computer"),
            "model_paths": wake_word_model_paths,
            "threshold": float(os.getenv("HERMES_WAKE_WORD_THRESHOLD", "0.55")),
            # Silero VAD gate on wake predictions: 0 disables, 0.5 blocks
            # non-speech triggers (calibrated 2026-08-15).
            "vad_threshold": float(v) if (v := os.getenv("HERMES_WAKE_VAD_THRESHOLD", "0.5").strip()) else None,
            "cooldown": float(os.getenv("HERMES_WAKE_COOLDOWN", "5.0")),
            # Argv for the "command" engine, used when wake detection runs off
            # this host (e.g. a Wyoming satellite on the machine that owns the
            # mic). CommandWWEngine substitutes {timeout} at call time.
            "command": shlex.split(os.getenv("HERMES_WAKE_WORD_COMMAND", "")),
        },
        "stt": {
            "engine": os.getenv("HERMES_STT_ENGINE", "faster-whisper"),
            "model_size": os.getenv("HERMES_STT_MODEL", "tiny"),
            "language": os.getenv("HERMES_STT_LANGUAGE", "en").strip() or "en",
            "initial_prompt": os.getenv("HERMES_STT_INITIAL_PROMPT", "").strip() or None,
            "hotwords": os.getenv("HERMES_STT_HOTWORDS", "").strip() or None,
            "vad_filter": os.getenv("HERMES_STT_VAD", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            "min_confidence": float(os.getenv("HERMES_STT_MIN_CONFIDENCE", "0.15")),
            "follow_up_min_confidence": float(os.getenv(
                "HERMES_STT_FOLLOW_UP_MIN_CONFIDENCE", "0.02"
            )),
            "speech_threshold": float(os.getenv("HERMES_SPEECH_THRESHOLD", "0.005")),
            "condition_on_previous_text": os.getenv(
                "HERMES_STT_CONDITION_ON_PREVIOUS_TEXT", "false"
            ).strip().lower() in {"1", "true", "yes", "on"},
            "no_speech_threshold": float(v) if (v := os.getenv("HERMES_STT_NO_SPEECH_THRESHOLD", "").strip()) else None,
            "log_prob_threshold": float(v) if (v := os.getenv("HERMES_STT_LOG_PROB_THRESHOLD", "").strip()) else None,
        },
        "tts": {
            "engine": os.getenv("HERMES_TTS_ENGINE", "edge"),
            "voice": os.getenv("HERMES_TTS_VOICE", "en-US-AriaNeural"),
        },
        "media_player_entity": os.getenv("HERMES_MEDIA_PLAYER", ""),
        "alexa_media_player_entity": os.getenv("HERMES_ALEXA_MEDIA_PLAYER", ""),
        "max_record_duration": float(os.getenv("HERMES_RECORD_DURATION", "10")),
        "silence_timeout": float(os.getenv("HERMES_SILENCE_TIMEOUT", "2.0")),
        "record_gain": float(os.getenv("HERMES_RECORD_GAIN", "1.0")),
    }


def _init_engines() -> bool:
    """Initialise TTS + STT engines from config. Returns True if both ready."""
    global _tts_engine, _stt_engine, _wake_word_engine
    config = _get_config()

    # TTS
    from .engines.tts import create_tts_engine
    try:
        _tts_engine = create_tts_engine(
            engine_type=config["tts"]["engine"],
            voice=config["tts"]["voice"],
        )
    except Exception as exc:
        logger.warning("TTS engine init failed: %s", exc)
        _tts_engine = None

    # STT
    from .engines.stt import create_stt_engine
    try:
        _stt_engine = create_stt_engine(
            engine_type=config["stt"]["engine"],
            model_size=config["stt"].get("model_size", "tiny"),
            initial_prompt=config["stt"].get("initial_prompt"),
            hotwords=config["stt"].get("hotwords"),
            vad_filter=config["stt"].get("vad_filter", True),
            condition_on_previous_text=config["stt"].get("condition_on_previous_text", False),
            no_speech_threshold=config["stt"].get("no_speech_threshold"),
            log_prob_threshold=config["stt"].get("log_prob_threshold"),
        )
    except Exception as exc:
        logger.warning("STT engine init failed: %s", exc)
        _stt_engine = None

    # Wake Word (optional — voice mode works without it via voice_listen)
    from .engines.wake_word import create_wake_word_engine
    try:
        _wake_word_engine = create_wake_word_engine(
            engine_type=config["wake_word"]["engine"],
            keywords=[config["wake_word"]["keyword"]],
            model_paths=config["wake_word"]["model_paths"],
            threshold=config["wake_word"]["threshold"],
            vad_threshold=config["wake_word"].get("vad_threshold"),
            command=config["wake_word"].get("command") or ["false"],
        )
    except Exception as exc:
        logger.warning("Wake word engine init failed: %s", exc)
        _wake_word_engine = None

    tts_ok = _tts_engine is not None and _tts_engine.available()
    stt_ok = _stt_engine is not None and _stt_engine.available()
    logger.info("Voice engines: TTS=%s STT=%s WakeWord=%s", tts_ok, stt_ok, _wake_word_engine is not None)

    if tts_ok and stt_ok:
        _voice_ready.set()
    return tts_ok and stt_ok


def _check_voice_available() -> bool:
    """Check if voice engines are available (check_fn for tools)."""
    return _voice_ready.is_set()


def _ensure_voice_ready() -> bool:
    """Lazy-init engines on first tool call."""
    if not _voice_ready.is_set():
        return _init_engines()
    return True


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _handle_voice_status(args: dict, **kw) -> str:
    """Show Voice Stack engine states and pipeline status."""
    _ensure_voice_ready()

    def _engine_status(engine, name: str) -> Dict[str, Any]:
        if engine is None:
            return {"status": "not_configured"}
        try:
            avail = engine.available()
        except Exception:
            avail = False
        return {"status": "ready" if avail else "unavailable"}

    engines = {
        "wake_word": _engine_status(_wake_word_engine, "wake_word"),
        "stt": _engine_status(_stt_engine, "stt"),
        "tts": _engine_status(_tts_engine, "tts"),
    }

    # Get voice list for TTS
    tts_voices: list = []
    if _tts_engine and _tts_engine.available():
        try:
            tts_voices = _tts_engine.list_voices()
        except Exception:
            pass

    pipeline_state: Dict[str, Any] = {}
    if _pipeline:
        pipeline_state = _pipeline.state.to_dict()

    result: Dict[str, Any] = {
        "engines": engines,
        "tts_voices": tts_voices[:10],  # Limit to 10
        "pipeline": pipeline_state,
        "ready": _voice_ready.is_set(),
    }
    return json.dumps(result, default=str)


VOICE_AGENT_SYSTEM_PROMPT = (
    "You are Hermes, answering a local wake-word voice request. "
    "Respond in one or two concise plain sentences suitable for speech. "
    "Do not use markdown, lists, URLs, or file paths. "
    "Use web or Home Assistant tools when they are needed to answer."
)


def _run_full_agent(
    text: str,
    *,
    profile: Optional[str] = None,
    session: Optional[str] = None,
    session_title: Optional[str] = None,
) -> str:
    """Run one tool-capable Hermes turn for an unmatched voice query.

    Goes through the gateway's API-server platform rather than spawning a
    `hermes chat` subprocess: the agent is already warm, so this skips the
    per-turn CLI cold start, and the SSE stream reports tool calls live
    instead of leaving them to be scraped back out of agent.log.

    ``session`` overrides the default ``voice-stack`` session id, letting
    separate voice surfaces (e.g. the Alexa skill via HA Assist) keep their
    own conversation thread and history.
    """
    started = time.monotonic()
    try:
        response = sessions_api.complete(
            text,
            profile=profile,
            system_prompt=VOICE_AGENT_SYSTEM_PROMPT,
            session=session,
            session_title=session_title,
        )
    except Exception:
        events.emit(
            "agent",
            runner="sessions api",
            ok=False,
            seconds=round(time.monotonic() - started, 2),
        )
        raise

    events.emit(
        "agent",
        runner="sessions api",
        ok=True,
        profile=profile,
        session=sessions_api.session_id(),
        seconds=round(time.monotonic() - started, 2),
    )

    response = (response or "").strip()
    if not response:
        raise RuntimeError("Hermes voice agent returned no response")
    return response


def _complete_voice_with_hermes(ctx: Any, text: str, *, profile: Optional[str] = None) -> str:
    """Run the full tool-capable agent, degrading to raw LLM only on failure."""
    profile = profile or _voice_profile_for(text)
    try:
        if profile:
            return _run_full_agent(text, profile=profile)
        return _run_full_agent(text)
    except Exception as exc:
        if profile:
            logger.warning("Profile voice agent %s failed: %s", profile, exc)
            display_name = {"sportscoach": "Sports Coach", "music": "DJ Yakkuza"}.get(profile, profile)
            return f"{display_name} is unavailable right now."
        logger.warning("Tool-capable voice fallback failed; using raw LLM: %s", exc)

    result = ctx.llm.complete(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Hermes, responding to a local wake-word voice request. "
                    "Reply naturally and concisely for text-to-speech. "
                    "If the request needs unavailable context, ask one brief clarification."
                ),
            },
            {"role": "user", "content": text},
        ],
        max_tokens=512,
        temperature=0.2,
        purpose="voice_stack.wake_word",
    )
    return (result.text or "").strip()


def _voice_profile_for(text: str) -> Optional[str]:
    """Route personal running-data questions to the profile that owns them."""
    import re

    if re.search(
        r"\b(?:alpha\s*runner|alpha\s+run|garmin|run(?:ning)?\s+(?:stats?|data|history|record|progress)|fastest\s+run|personal\s+best|\bpb\b)\b",
        text,
        re.IGNORECASE,
    ):
        return "sportscoach"
    return None


# Provisional specialist-routing floor. Only a confident Jev Choice routes to a
# specialist; ambiguity degrades to the general agent. Not a calibrated claim.
ASSIST_ROUTE_FLOOR = 0.6


def _route_assist_profile(text: str) -> Optional[str]:
    """Choose the specialist profile for a text relay, or None for the general agent.

    A cheap regex first resolves the well-worn running-data phrasing straight to
    sportscoach (no Jev round-trip). Everything else goes to one Jev Choice over
    {sportscoach, music, neither}; only a confident specialist answer routes.
    Ambiguity, a missing key, or a Jev outage fall back to the general agent —
    never a wrong specialist. This is forwarding only: no DB, no playback.
    """
    regex_profile = _voice_profile_for(text)
    if regex_profile:
        return regex_profile
    return _jev_route_specialist(text)


def _jev_route_specialist(text: str) -> Optional[str]:
    """One Jev Choice: which specialist owns this request? None on ambiguity/outage."""
    from typesafe_sdk import Choice, TypeSafeClient

    key = music._load_api_key()
    if not key:
        return None  # no credential => general agent, never a wrong specialist

    criteria = {
        "sportscoach": (
            "Sports or fitness: running, workouts, races, Garmin/AlphaRunner "
            "stats, personal records, training data, pace, distance, heart rate."
        ),
        "music": (
            "Music: playing, discussing, recommending, or asking about music, "
            "artists, albums, songs, playlists, or genres."
        ),
        "neither": (
            "Anything else: household/device commands, weather, news, timers, "
            "general questions, or requests unrelated to sports or music."
        ),
    }
    try:
        with TypeSafeClient(api_key=key) as client:
            response = client.system_one(
                model="jev-latest",
                state={
                    "request": text,
                    "note": (
                        "A spoken voice request relayed through Home Assistant, "
                        "possibly with imperfect transcription."
                    ),
                },
                questions={
                    "route": Choice(
                        instructions=(
                            "Which specialist should own this request? Choose "
                            "sportscoach for sports/fitness, music for music, "
                            "neither if no specialist applies."
                        ),
                        criteria=criteria,
                    )
                },
            )
    except Exception:
        return None

    answer = response.answers["route"]
    choice = str(getattr(answer, "choice", "") or "neither")
    if choice not in ("sportscoach", "music"):
        return None
    try:
        confidence = float(getattr(answer, "confidence", 0))
    except (TypeError, ValueError):
        return None
    if confidence < ASSIST_ROUTE_FLOOR:
        return None

    events.emit("route", target=choice, confidence=confidence, source="assist_relay")
    return choice


def _jev_music_gate(ctx: Any, text: str):
    """One Jev judgment: is this music-related? None means service unavailable.

    The second tuple position is a legacy text-only routing compatibility slot;
    live recorded voice turns only consume the music decision.
    """
    try:
        from typesafe_sdk import Noul, TypeSafeClient

        from .music import TypeSafeUnavailable, _load_api_key
    except ImportError:
        return None

    key = _load_api_key()
    if not key:
        return None
    state = {
        "transcript": text,
        "note": "Home Assistant could not handle this request; it may be a music request spoken to a voice assistant, possibly with imperfect transcription.",
    }
    try:
        with TypeSafeClient(api_key=key) as client:
            response = client.system_one(
                model="jev-latest",
                state=state,
                questions={
                    "music": Noul(
                        instructions=(
                            "Is this a music-related request: playing, discussing, "
                            "recommending, or asking about music, artists, albums, "
                            "or songs? Consider garbled speech-transcription."
                        )
                    ),
                },
            )
        is_music = float(response.answers["music"].noul) >= 0.5
        events.emit(
            "route",
            target="jev_music_gate",
            outcome="music" if is_music else "not_music",
        )
        return (is_music, False)
    except Exception:
        return None


MUSIC_RESOLVER_SYSTEM_PROMPT = (
    "Resolve descriptive music requests into one Alexa playback command. "
    "Use your music knowledge to identify lyrics, scenes, eras, or other clues. "
    "Return JSON only: {\"command\":\"Play TITLE by ARTIST\"}. "
    "If the clue is genuinely insufficient, return JSON only: "
    "{\"clarification\":\"one short spoken question\"}. "
    "Never include commentary, markdown, URLs, or instructions other than the play command."
)


def _needs_music_resolution(text: str) -> bool:
    """Return whether a play request describes music instead of naming it."""
    return bool(
        re.search(
            r"\b(?:that|the)\s+(?:song|track|music)\s+(?:that|where|with|from)\b"
            r"|\b(?:lyrics?|words?)\b"
            r"|\b(?:goes|sounds?)\s+(?:like|something)\b",
            text,
            re.IGNORECASE,
        )
    )


def _resolve_music_request(
    ctx: Any,
    text: str,
    *,
    confidence: float = 1.0,
    evidence: list[dict] | None = None,
) -> dict:
    """Interpret every music request using the bounded read-only resolver."""
    from .music import resolve
    return resolve(ctx, text, confidence=confidence, evidence=evidence)


def _genre_year_request(text: str) -> bool:
    """True for a bounded genre + release-year playback request.

    This shape has no artist identity to repair. Keep it deliberately narrow so
    low-confidence artist/title requests still fail closed through the evidence
    planner rather than leaking into raw Alexa dispatch.
    """
    return re.fullmatch(
        r"\s*(?:play|put\s+on|put\s+some|play\s+some|play\s+me)\s+"
        r"(?:some\s+)?[a-z][a-z0-9 &'’-]{0,40}\s+music\s+"
        r"(?:from|in)\s+(?:19|20)\d{2}\s*[.!?]*\s*",
        str(text),
        re.IGNORECASE,
    ) is not None


def _evidence_agrees_verbatim(evidence: list[dict]) -> bool:
    """Require two independent STT hypotheses with identical normalized text."""
    if len(evidence) < 2:
        return False

    def normalized(value: object) -> str:
        value = re.sub(r"[.!?]+$", "", str(value or "").strip())
        return re.sub(r"\s+", " ", value).casefold()

    texts = [normalized(row.get("text")) for row in evidence]
    return bool(texts[0]) and all(value == texts[0] for value in texts[1:])


_MUSIC_REDECODE_PROMPT = (
    "This is a spoken music playback request. Transcribe the complete request "
    "faithfully. Preserve every named performer and relationship word such as "
    "and, with, featuring, or feat. Do not infer missing words."
)


def _collect_music_evidence(text: str, confidence: float, redecode=None) -> list[dict] | None:
    """Gather one bounded artist-agnostic re-listen for low-confidence music.

    Returns the immutable evidence packet, or None when a re-listen was
    required (confidence below the repair floor) but could not be obtained.
    Callers must treat None as insufficient evidence and fail closed to a
    clarification rather than plan from a single degraded hypothesis.
    """
    evidence = [
        {"text": str(text), "confidence": float(confidence), "source": "initial"}
    ]
    if confidence >= music.STT_REPAIR_CONFIDENCE_FLOOR or not callable(redecode):
        return evidence
    try:
        # Bare, artist-agnostic re-listen. NO artist-candidate lookup happens
        # before Jev selects the operation (the FLOOR-gated re-listen only
        # recovers the words; routing decides what they mean).
        second = redecode(initial_prompt=_MUSIC_REDECODE_PROMPT) or {}
        if not isinstance(second, dict):
            # Re-listening did not produce a usable hypothesis.
            return None
        second_text = str(second.get("text") or "").strip()
        if not second_text:
            return None
        second_confidence = float(second.get("confidence", 0.0) or 0.0)
        evidence.append(
            {
                "text": second_text,
                "confidence": second_confidence,
                "source": "music_domain_redecode",
            }
        )
        events.emit(
            "stt_redecode",
            text=events.truncate(second_text),
            confidence=round(second_confidence, 3),
            source="music_domain_redecode",
        )
    except Exception as exc:
        logger.warning("Music evidence re-decode failed: %s", exc)
        events.emit("error", stage="music_redecode", detail=str(exc))
        return None
    return evidence


def _is_clean_play_clause(text: str) -> bool:
    """True for a single-clause play request with no embedded secondary action."""
    if not text or not str(text).strip():
        return False
    if re.search(r"[;\r\n]|&&|\|\||[`$<>]", text):
        return False
    # A second imperative (stop, unlock, turn off, next...) after the music
    # phrase is a chained/secondary action — do not forward as a play command.
    remainder = re.sub(
        r"^\s*(?:play|put\s+on|put\s+some|play\s+some|shuffle|play\s+me)\s+",
        "", text, flags=re.IGNORECASE)
    remainder = remainder.strip()
    if not remainder:
        return False
    return not re.search(
        r"\b(?:and|then|after|plus)\s+(?:play|stop|pause|unlock|turn\s+"
        r"(?:on|off)|open|close|set)\b",
        remainder,
        re.IGNORECASE,
    )


def _normalize_provider(text: str) -> str:
    """Pin a clean music phrase to the default provider, Apple Music."""
    cleaned = re.sub(r"\s+on\s+(?:apple\s+)?music\s*$", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+on\s+\w[\w\s]*$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"[.!?]+$", "", cleaned).strip()
    if not cleaned:
        return ""
    return f"{cleaned} on Apple Music"


def _dispatch_alexa_playback(playback_command: str, alexa_target: str) -> bool:
    """Send a verified playback command to the configured Echo."""
    from . import ha_conversation

    playback = ha_conversation.play_alexa_media(playback_command, alexa_target)
    if playback.get("ok"):
        events.emit(
            "route",
            target="alexa_media",
            outcome="handled",
            targets=[alexa_target],
        )
        return True
    events.emit(
        "route",
        target="alexa_media",
        outcome="failed",
        targets=[alexa_target],
        detail=playback.get("error") or playback.get("reason"),
    )
    return False


def _alexa_playback_return(
    playback_command: str, alexa_target: str, ctx: Any, text: str
) -> str:
    """Acknowledge dispatch; reserve spoken confirmation for genuine failures.

    In dry-run mode (HERMES_VOICE_DRY_RUN=1) the command is reported back as
    speech instead of dispatched, so the operator can confirm what WOULD have
    been sent to Alexa/Apple Music before letting it actually fire.
    """
    from .pipeline import SILENT_HANDLED

    dry_run = os.getenv("HERMES_VOICE_DRY_RUN", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if dry_run:
        _write_playback_telemetry(False, playback_command, ctx, text)
        return f"Dry run: I would ask Alexa to {playback_command}"

    dispatched = _dispatch_alexa_playback(playback_command, alexa_target)
    _write_playback_telemetry(dispatched, playback_command, ctx, text)
    # Dispatch confirmed by HA success: playback itself is the acknowledgement.
    # Speaking a second Alexa response here would interrupt the requested music.
    if dispatched:
        return SILENT_HANDLED
    return "Alexa could not start that music."


def _write_playback_telemetry(dispatched: bool, command: str, ctx: Any, text: str) -> None:
    """Log dispatch (not playback confirmation) for later verification."""
    try:
        from . import music as _music

        _music._write_telemetry({
            "outcome": "dispatched" if dispatched else "dispatch_failed",
            "command": command,
            "source": text,
        })
    except Exception:
        pass


def _legacy_music_resolver_unused(ctx: Any, text: str) -> dict:
    try:
        result = ctx.llm.complete(
            messages=[
                {"role": "system", "content": MUSIC_RESOLVER_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            max_tokens=120,
            temperature=0.1,
            purpose="voice_stack.music_resolver",
        )
        raw = str(getattr(result, "text", "") or "").strip()
    except Exception as exc:
        logger.warning("Music resolver unavailable: %s", exc)
        events.emit(
            "route",
            target="music_resolver",
            outcome="unavailable",
            detail=str(exc),
        )
        return {"clarification": "Which song do you mean?"}
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        return {"clarification": "Which song do you mean?"}
    if not isinstance(result, dict):
        return {"clarification": "Which song do you mean?"}

    raw_command = str(result.get("command") or "")
    if re.search(r"[;\r\n]|&&|\|\|", raw_command):
        return {"clarification": "Which song do you mean?"}
    command = " ".join(raw_command.split())
    if 5 <= len(command) <= 200 and re.fullmatch(r"play\s+\S.+", command, re.IGNORECASE):
        return {"command": command}

    clarification = " ".join(str(result.get("clarification") or "").split())
    if clarification:
        return {"clarification": clarification[:160]}
    return {"clarification": "Which song do you mean?"}


def _route_voice_transcript(
    ctx: Any,
    text: str,
    *,
    language: str = "en",
    confidence: float = 1.0,
    redecode=None,
    audio_path: str | None = None,
    _music_decided: bool = False,
) -> str:
    """Route a recorded music turn directly to DJ; HA owns nonmusic turns."""
    from . import ha_conversation, music

    if audio_path:
        from . import music_handoff, dj_audio
        import math

        recording = os.path.realpath(audio_path)
        if not os.path.isfile(recording):
            return "I couldn't access the recording. Please try again."
        try:
            acoustic_confidence = float(confidence)
        except (ValueError, TypeError):
            acoustic_confidence = 0.0
        if not math.isfinite(acoustic_confidence) or not 0 <= acoustic_confidence <= 1:
            acoustic_confidence = 0.0
        low_confidence = acoustic_confidence < _get_config()["stt"]["min_confidence"]
        hypotheses = [{"text": text, "source": "initial_stt", "confidence": acoustic_confidence}]
        if low_confidence:
            try:
                alternate = dj_audio.transcribe_recording(recording)
                if alternate:
                    hypotheses.append({"text": alternate, "source": "same_wav_medium_stt"})
                    events.emit("stt_redecode", source="music_gate", text=events.truncate(alternate))
            except Exception as exc:
                # A failed alternate does not erase the original evidence.
                logger.warning("Music gate alternate transcription failed: %s", exc)
        try:
            decision = music_handoff.decide(hypotheses)
        except music.TypeSafeUnavailable:
            return music.UNAVAILABLE
        if decision["route"] == "music":
            events.emit("route", target="music_profile", outcome="recording_handoff",
                        profile="music", probability=decision["probability"])
            return _complete_voice_with_hermes(
                ctx, music_handoff.build_prompt(recording, decision), profile="music")
        if decision["route"] == "uncertain" or low_confidence:
            return "I couldn't hear that clearly. Could you repeat it?"
        # Explicit nonmusic decision: retain HA/general handling, but never
        # re-enter the historical regex/resolver music path after an HA miss.
        return _route_voice_transcript(
            ctx, text, language=language, confidence=confidence, _music_decided=True)

    result = ha_conversation.process_conversation(
        text,
        language=language,
        conversation_id=None,
        agent_id="conversation.home_assistant",
    )
    if result.get("error"):
        logger.warning("Home Assistant voice routing failed: %s", result["error"])
        events.emit("route", target="home_assistant", outcome="unavailable", detail=result["error"])
        return "Home Assistant is unavailable right now."

    response = result.get("response") or {}
    response_type = str(response.get("response_type") or "").lower()
    speech = (
        ((response.get("speech") or {}).get("plain") or {}).get("speech") or ""
    ).strip()

    if response_type != "error":
        events.emit(
            "route",
            target="home_assistant",
            outcome="handled",
            response_type=response_type,
            targets=_intent_targets(response),
        )
        return speech or "Done."

    data = response.get("data") or {}
    import re

    channel_target = None
    channel_command = re.fullmatch(
        r"\s*(?:(?:turn|switch)\s+on|play|run|start|activate)\s+(.+?)\s*",
        text,
        re.IGNORECASE,
    )
    if channel_command:
        channel_target = channel_command.group(1)
    elif len(re.findall(r"[A-Za-z0-9]+", text)) <= 3:
        channel_target = text.strip()

    if channel_target:
        live_channel = ha_conversation.run_live_channel(channel_target)
        if live_channel.get("ok"):
            events.emit(
                "route",
                target="home_assistant",
                outcome="handled_live_channel",
                targets=[live_channel.get("entity_id")],
                media_title=live_channel.get("media_title"),
            )
            return f"Started {live_channel['name']}."
        if live_channel.get("reason") == "verification_failed":
            name = live_channel.get("name") or channel_target
            events.emit(
                "route",
                target="home_assistant",
                outcome="channel_verification_failed",
                targets=[live_channel.get("entity_id")],
            )
            return f"I found {name}, but the channel did not switch."

    # HA couldn't handle the request. Both "no_valid_targets" and
    # "no_intent_match" mean the same thing here: no device/entity was matched.
    # Music routing (regex fast path + Jev gate) must run for EITHER — a
    # garbled "play Tove Lo" can land under either code depending on how HA's
    # NLU fails, and skipping the gate silently drops the music path.
    if data.get("code") in ("no_valid_targets", "no_intent_match"):

        # Music routing: interpret EVERY music request into a precise,
        # catalog-verified "… on Apple Music" command. Natural phrasing
        # ("play", "put on", "put some", "shuffle") is recognized; media
        # channels are handled earlier by the live-channel resolver.
        music_match = re.fullmatch(
            r"\s*(?:(?:play|put\s+on|put\s+some|play\s+some|shuffle|play\s+me)\s+(.+?)|(.+?\s+music))\s*",
            text,
            re.IGNORECASE,
        )
        alexa_target = os.getenv("HERMES_ALEXA_MEDIA_PLAYER", "").strip()
        if music_match and alexa_target and not _music_decided:
            evidence = _collect_music_evidence(text, confidence, redecode)
            if evidence is None:
                # A re-listen was required (low confidence) but could not be
                # obtained. Fail closed: never plan from a single degraded
                # hypothesis. Ask the speaker to repeat instead.
                return "I couldn't re-listen to that. Could you say it again?"

            # A genre+year request has no artist slot. If the bare pass and the
            # independent re-listen agree exactly, that consensus is stronger
            # evidence than either confidence score and can be forwarded
            # literally. Do this BEFORE artist planning; asking "which artist?"
            # for "jazz music from 2026" is categorically wrong.
            if _genre_year_request(text) and _evidence_agrees_verbatim(evidence):
                raw = _normalize_provider(text)
                if raw:
                    events.emit(
                        "music_resolver",
                        outcome="genre_year_consensus",
                        command=events.truncate(raw),
                    )
                    return _alexa_playback_return(raw, alexa_target, ctx, text)

            resolution = _resolve_music_request(
                ctx,
                text,
                confidence=confidence,
                evidence=evidence,
            )
            # The resolver may fail closed to the exact UNAVAILABLE string
            # (TypeSafe API down / no credential). Honor that string verbatim
            # and do NOT fall back to a raw command — the contract is exact.
            if isinstance(resolution, str):
                return resolution
            playback_command = str(resolution.get("command") or "").strip()
            if playback_command:
                return _alexa_playback_return(playback_command, alexa_target, ctx, text)
            # Resolver could not produce a verified command. Only fall back to
            # the exact words pinned to Apple Music when the transcript is
            # trustworthy (high confidence) and clean. Below the repair floor
            # the words are suspect — honor the clarification ("who did you
            # mean?") instead of dispatching STT garbage verbatim.
            if (
                    confidence >= music.STT_REPAIR_CONFIDENCE_FLOOR
                    and _is_clean_play_clause(text)
                    and not _needs_music_resolution(text)
                    and not music._latest_artist_name(text)
                ):
                    # Raw passthrough only for simple, unambiguous play clauses
                    # ("play jazz", "play Nelly Furtado"). A "<latest> song/album
                    # by <artist>" phrase embeds an artist that may be STT-garbled
                    # and implies an online fresh-track lookup — never dispatch it
                    # verbatim ("Play the latest song by Tuvalu …").
                    raw = _normalize_provider(text)
                    if raw:
                        return _alexa_playback_return(raw, alexa_target, ctx, text)
            return str(
                resolution.get("clarification") or "Which song do you mean?"
            )

        # Jev music gate: the regex fast path missed. One system_one call
        # decides music vs not, playback vs discussion — this catches
        # STT-garbled music requests ("stay to the loop featuring stormlight")
        # and music conversation, neither of which a regex can see. The gate
        # runs even without an Alexa target: discussion needs no player.
        if not music_match and not _music_decided:
            gate = _jev_music_gate(ctx, text)
            if gate is None:
                return music.UNAVAILABLE
            is_music, wants_playback = gate
            if is_music and wants_playback and alexa_target:
                resolution = _resolve_music_request(
                    ctx,
                    text,
                    confidence=confidence,
                    evidence=_collect_music_evidence(text, confidence, redecode),
                )
                if isinstance(resolution, str):
                    return resolution
                playback_command = str(resolution.get("command") or "").strip()
                if playback_command:
                    return _alexa_playback_return(playback_command, alexa_target, ctx, text)
                return str(
                    resolution.get("clarification") or "Which song do you mean?"
                )
            if is_music and not wants_playback:
                # Music discussion ("what has Stromae released?") → DJ Yakkuza.
                events.emit(
                    "route",
                    target="music_profile",
                    outcome="escalated",
                    profile="music",
                )
                return _complete_voice_with_hermes(ctx, text, profile="music")

        match = re.fullmatch(r"\s*(?:turn|switch)\s+on\s+(.+?)\s*", text, re.IGNORECASE)
        if match:
            script_name = match.group(1)
            retry_text = f"run {script_name}"
            retry = ha_conversation.process_conversation(
                retry_text,
                language=language,
                conversation_id=None,
                agent_id="conversation.home_assistant",
            )
            retry_response = retry.get("response") or {}
            if str(retry_response.get("response_type") or "").lower() == "error":
                number_words = {
                    "0": "Zero",
                    "1": "One",
                    "2": "Two",
                    "3": "Three",
                    "4": "Four",
                    "5": "Five",
                    "6": "Six",
                    "7": "Seven",
                    "8": "Eight",
                    "9": "Nine",
                }
                spoken_name = re.sub(
                    r"\b[0-9]\b",
                    lambda number: number_words[number.group(0)],
                    script_name,
                )
                if spoken_name != script_name:
                    retry_text = f"run {spoken_name}"
                    retry = ha_conversation.process_conversation(
                        retry_text,
                        language=language,
                        conversation_id=None,
                        agent_id="conversation.home_assistant",
                    )
                    retry_response = retry.get("response") or {}
            if str(retry_response.get("response_type") or "").lower() != "error":
                successes = ((retry_response.get("data") or {}).get("success") or [])
                script_target = next(
                    (
                        target
                        for target in successes
                        if str(target.get("id") or "").startswith("script.")
                    ),
                    None,
                )
                events.emit(
                    "route",
                    target="home_assistant",
                    outcome="handled_after_script_retry",
                    retried_as=retry_text,
                    targets=_intent_targets(retry_response),
                )
                if script_target and script_target.get("name"):
                    return f"Started {script_target['name']}."
                retry_speech = (
                    ((retry_response.get("speech") or {}).get("plain") or {}).get("speech")
                    or ""
                ).strip()
                return retry_speech or "Done."

    import re

    if re.match(
        r"^\s*(?:turn|switch|power|start|stop|open|close|lock|unlock|set|dim|brighten|play|pause|mute|unmute)\b",
        text,
        re.IGNORECASE,
    ):
        events.emit(
            "route",
            target="home_assistant",
            outcome="no_match",
            response_type=response_type,
            code=data.get("code"),
            detail="command verb held in HA; not escalated to Hermes",
        )
        return speech or "Home Assistant could not find a matching device."

    events.emit(
        "route",
        target="hermes",
        outcome="escalated",
        response_type=response_type,
        code=data.get("code"),
    )
    return _complete_voice_with_hermes(ctx, text)


def _intent_targets(response: dict) -> list:
    """Extract the entity ids a Home Assistant intent acted on, for monitoring."""
    try:
        data = response.get("data") or {}
        targets = []
        for bucket in ("success", "targets", "failed"):
            for item in data.get(bucket) or []:
                entity_id = str((item or {}).get("id") or "").strip()
                if entity_id and entity_id not in targets:
                    targets.append(entity_id)
        return targets[:12]
    except Exception:  # pragma: no cover - monitoring must never break voice
        return []


def _handle_voice_enable(args: dict, **kw) -> str:
    """Enable continuous voice mode with wake word and HA media_player."""
    global _pipeline

    _ensure_voice_ready()
    if not _voice_ready.is_set():
        return json.dumps({
            "ok": False,
            "error": "Voice engines not available. Check voice_status for details.",
        })

    with _pipeline_lock:
        if _pipeline and _pipeline.state.enabled:
            return json.dumps({"ok": True, "message": "Voice mode already enabled."})

        config = _get_config()
        media_player = args.get("media_player_entity") or config["media_player_entity"] or None
        alexa_media_player = config["alexa_media_player_entity"] or None

        from .pipeline import VoicePipeline

        # Define the callback that sends user text through Hermes's supported
        # plugin LLM facade. The pipeline runs in a background thread, so use
        # the synchronous facade rather than the async Assist receiver path.
        def _voice_callback(
            text: str,
            *,
            confidence: float = 1.0,
            audio_path: str | None = None,
            redecode=None,
        ) -> str:
            """Route STT text through native HA before Hermes conversation."""
            logger.info("Voice callback received: %s", text)
            if _plugin_ctx is None:
                return "Hermes voice processing is unavailable."
            return _route_voice_transcript(
                _plugin_ctx, text, language=config["stt"]["language"],
                confidence=confidence,
                redecode=redecode,
                audio_path=audio_path,
            )

        _pipeline = VoicePipeline(
            callback=_voice_callback,
            wake_word_engine=_wake_word_engine,
            stt_engine=_stt_engine,
            tts_engine=_tts_engine,
            media_player_entity=media_player,
            alexa_media_player_entity=alexa_media_player,
            max_record_duration=config["max_record_duration"],
            silence_timeout=config["silence_timeout"],
            speech_threshold=config["stt"]["speech_threshold"],
            record_gain=config["record_gain"],
            language=config["stt"]["language"],
            min_confidence=config["stt"]["min_confidence"],
            follow_up_min_confidence=config["stt"]["follow_up_min_confidence"],
            route_low_confidence=True,
            wake_cooldown=config["wake_word"].get("cooldown", 5.0),
        )

        if not _pipeline.available:
            _pipeline = None
            return json.dumps({"ok": False, "error": "Engines not all available."})

        started = _pipeline.start()
        if not started:
            _pipeline = None
            return json.dumps({"ok": False, "error": "Voice pipeline failed to start."})
    return json.dumps({"ok": True, "message": "Voice mode enabled. Wake word active."})



def _handle_voice_disable(args: dict, **kw) -> str:
    """Disable voice mode."""
    global _pipeline
    with _pipeline_lock:
        if _pipeline:
            _pipeline.stop()
            _pipeline = None
            return json.dumps({"ok": True, "message": "Voice mode disabled."})
    return json.dumps({"ok": True, "message": "Voice mode was not active."})


def _handle_voice_speak(args: dict, **kw) -> str:
    """Speak text through TTS engine + playback."""
    _ensure_voice_ready()
    if not _tts_engine or not _tts_engine.available():
        return json.dumps({"ok": False, "error": "TTS engine not available."})

    text = args.get("text", "")
    if not text:
        return json.dumps({"ok": False, "error": "No text provided."})

    try:
        from .pipeline import play_audio_local, play_audio_ha

        audio_path = _tts_engine.synthesize(text)
        media_player = args.get("media_player_entity") or _get_config().get("media_player_entity", "")
        if media_player:
            ok = play_audio_ha(audio_path, media_player)
        else:
            ok = play_audio_local(audio_path)

        return json.dumps({"ok": ok, "audio_path": audio_path})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def _handle_voice_listen(args: dict, **kw) -> str:
    """One-shot: record audio, transcribe, and return text.

    This is a simpler alternative to continuous voice mode.
    Useful for testing STT or for push-to-talk workflows.
    """
    _ensure_voice_ready()
    if not _stt_engine or not _stt_engine.available():
        return json.dumps({"ok": False, "error": "STT engine not available.", "error_category": "engine_unavailable"})

    config = _get_config()
    try:
        duration = float(args.get("duration", config["max_record_duration"]))
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "duration must be numeric", "error_category": "invalid_duration"})
    if duration <= 0 or duration > 60:
        return json.dumps({"ok": False, "error": "duration must be between 0 and 60 seconds", "error_category": "invalid_duration"})
    language = args.get("language", None)

    import tempfile
    from .pipeline import record_audio

    cache_dir = Path.home() / ".hermes" / "voice_cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return json.dumps({"ok": False, "error": str(exc), "error_category": "cache_unavailable"})

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=str(cache_dir)) as tmp:
        audio_path = tmp.name

    try:
        recorded = record_audio(
            audio_path,
            duration=duration,
            gain=config.get("record_gain", 1.0),
        )
        if not recorded:
            return json.dumps({"ok": False, "error": "No speech detected.", "error_category": "no_speech"})
        try:
            result = _stt_engine.transcribe_with_confidence(audio_path, language=language)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc), "error_category": "transcription_failed"})
        return json.dumps({
            "ok": True,
            "text": result.get("text", ""),
            "confidence": round(result.get("confidence", 1.0), 3),
            "language": result.get("language", "unknown"),
        })
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc), "error_category": "recording_failed"})
    finally:
        try:
            os.unlink(audio_path)
        except OSError:
            pass


def _handle_voice_prompt(args: dict, **kw) -> str:
    """Return the voice-optimised system prompt with current HA context."""
    from .pipeline import build_voice_system_prompt

    areas = None
    entities = None
    try:
        from ..home_assistant.ha_assistant import search_entities
    except ImportError:
        pass
    else:
        try:
            entities = search_entities().get("entities", [])[:30]
        except Exception:
            pass

    prompt = build_voice_system_prompt(areas=areas, entities=entities)
    return json.dumps({"ok": True, "prompt": prompt})


ALEXA_AGENT_SYSTEM_PROMPT = (
    "You are Hermes, answering through an Alexa skill via Home Assistant Assist. "
    "Respond in one or two concise plain sentences suitable for speech. "
    "Do not use markdown, lists, URLs, or file paths. "
    "Use web or Home Assistant tools when they are needed to answer."
)

ALEXA_SESSION_ID = "alexa_hermes"
ALEXA_SESSION_TITLE = "Voice (Alexa)"
# Alexa cuts the session at ~8s; Lambda read timeout is 10s. An agent turn
# that can't finish inside this budget gets an immediate spoken ack over
# notify.alexa_media, and the real answer is spoken when it's ready.
ALEXA_AGENT_BUDGET_SECONDS = float(os.getenv("HERMES_ALEXA_AGENT_BUDGET", "6.5"))
ALEXA_SLOW_ACK = os.getenv("HERMES_ALEXA_SLOW_ACK", "On it — I'll have the answer on your speaker in a moment.")
# Dedicated executor so slow Alexa runs never exhaust the loop's default
# executor (shared with other plugin work).
ALEXA_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="alexa-agent"
)


def _alexa_media_player() -> str:
    """Resolve the Echo used for slow-answer delivery (same sink as Jarvis)."""
    return os.getenv("HERMES_ALEXA_MEDIA_PLAYER", "").strip()


def _resolve_last_called_media_player(timeout: float = 3.0) -> str:
    """Ask HA which Echo Amazon last interacted with (alexa_media last_called).

    At query time this is the Echo the user just spoke to, so answers land on
    the asking device instead of a hardcoded one. Empty string when HA cannot
    be reached or no device is flagged.
    """
    import urllib.request

    hass_url = os.getenv("HASS_URL", "http://homeassistant.local:8123").rstrip("/")
    hass_token = os.getenv("HASS_TOKEN", "")
    if not hass_token:
        return ""
    template = (
        "{{ states.media_player "
        "| selectattr('attributes.last_called', 'eq', true) "
        "| map(attribute='entity_id') | join(',') }}"
    )
    request = urllib.request.Request(
        f"{hass_url}/api/template",
        data=json.dumps({"template": template}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {hass_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = response.read().decode("utf-8").strip()
        return result.split(",")[0].strip() if result else ""
    except Exception as exc:
        logger.warning("last_called lookup failed: %s", exc)
        return ""


def _deliver_slow_answer(text: str, target: Optional[str] = None) -> None:
    """Speak a finished slow answer through notify.alexa_media (best effort)."""
    from .pipeline import play_text_alexa

    sink = (target or "").strip() or _alexa_media_player()
    if not sink:
        logger.warning("Slow Alexa answer ready but no target media_player resolved")
        return
    try:
        ok = play_text_alexa(text, sink)
        events.emit("spoken", sink=sink, attempt=1, slow=True, ok=ok)
    except Exception:
        logger.exception("Slow Alexa answer delivery failed")


def _deliver_when_done(fut: "concurrent.futures.Future[str]", target: Optional[str] = None) -> None:
    """Done-callback: speak the agent's answer once a slow turn completes."""
    try:
        response = fut.result()
    except Exception:
        return
    response = (response or "").strip()
    if response:
        _deliver_slow_answer(response, target=target)


def _deliver_when_done_with_target(
    answer_future: "concurrent.futures.Future[str]",
    target_future: "concurrent.futures.Future[str]",
) -> None:
    """Resolve the asking Echo and deliver without blocking the event loop."""
    try:
        target = target_future.result(timeout=3.5)
    except Exception:
        target = ""
    _deliver_when_done(answer_future, target=target)


def _schedule_slow_delivery(
    answer_future: "concurrent.futures.Future[str]",
    target_future: "concurrent.futures.Future[str]",
) -> None:
    """Move blocking target resolution and REST delivery off callback threads."""
    threading.Thread(
        target=_deliver_when_done_with_target,
        args=(answer_future, target_future),
        name="alexa-slow-delivery",
        daemon=True,
    ).start()


async def _handle_assist_query_with_llm(ctx: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Turn an HA Assist query into a tool-capable Hermes agent response.

    This handles the HA-side ``assist_query`` message introduced by the
    conversation platform (the Alexa skill path). It runs the same
    tool-capable Sessions API agent as the wake-word path, but on its own
    ``alexa_hermes`` session so the two pipelines keep separate histories.

    The turn is raced against ``ALEXA_AGENT_BUDGET_SECONDS``: fast answers
    return over the WebSocket inside Alexa's ~8s window; slow turns get an
    immediate spoken ack through notify.alexa_media, and the finished answer
    is spoken on the same Echo when the agent completes. The raw
    ``concurrent.futures`` future is kept because ``asyncio.wait_for``
    cancels its wrapper on timeout — the underlying thread keeps running
    and delivers via ``add_done_callback``.
    """
    text = str(payload.get("text") or "").strip()
    language = str(payload.get("language") or "en")
    conversation_id = payload.get("conversation_id")

    profile = _route_assist_profile(text)

    target_cf = ALEXA_EXECUTOR.submit(_resolve_last_called_media_player)
    cf = ALEXA_EXECUTOR.submit(
        _run_full_agent,
        text,
        profile=profile,
        session=ALEXA_SESSION_ID,
        session_title=ALEXA_SESSION_TITLE,
    )
    try:
        response_text = await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(cf)),
            timeout=ALEXA_AGENT_BUDGET_SECONDS,
        )
        # Fast path: answer returns over the WebSocket (Alexa speaks it).
        # No done-callback is attached, so there is no duplicate spoken
        # delivery through notify.alexa_media.
        return {
            "ok": True,
            "text": response_text,
            "conversation_id": conversation_id,
            "runner": "sessions api",
            "profile": profile,
        }
    except asyncio.TimeoutError:
        # Slow path: attach delivery to the still-running raw future — the
        # asyncio wrapper was cancelled by wait_for, but the worker thread
        # keeps going and the done-callback speaks the answer on the Echo.
        # The ack itself is NOT re-spoken via notify: Alexa already speaks
        # the WS ack response natively on the asking device.
        cf.add_done_callback(lambda fut: _schedule_slow_delivery(fut, target_cf))
        return {
            "ok": True,
            "text": ALEXA_SLOW_ACK,
            "conversation_id": conversation_id,
            "runner": "sessions api+slow_delivery",
            "profile": profile,
            "slow": True,
        }
    except Exception as exc:
        logger.warning("Alexa tool-capable agent failed; falling back to raw LLM: %s", exc)

    result = await ctx.llm.acomplete(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Hermes, responding through Home Assistant Assist. "
                    "Reply naturally and concisely for text-to-speech. "
                    "If the request needs unavailable context, ask one brief clarification."
                ),
            },
            {
                "role": "user",
                "content": f"Language: {language}\nUser request: {text}",
            },
        ],
        max_tokens=512,
        temperature=0.2,
        purpose="voice_stack.assist_query",
    )
    return {
        "ok": True,
        "text": (result.text or "").strip(),
        "conversation_id": conversation_id,
        "provider": getattr(result, "provider", None),
        "model": getattr(result, "model", None),
    }


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

VOICE_STATUS_SCHEMA = {
    "name": "voice_status",
    "description": (
        "Report the current state of the Voice Stack engines "
        "(wake word, STT, TTS, media_player) and pipeline."
    ),
    "parameters": {"type": "object", "properties": {}},
}

VOICE_ENABLE_SCHEMA = {
    "name": "voice_enable",
    "description": (
        "Enable continuous voice mode — wake word detection, STT, "
        "Hermes LLM round-trip, and TTS playback through HA media_player. "
        "The pipeline runs in the background until disabled."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "media_player_entity": {
                "type": "string",
                "description": "HA media_player entity for TTS output (e.g. media_player.kitchen_speaker). "
                               "If omitted, uses HERMES_MEDIA_PLAYER env var or local speakers.",
            },
        },
    },
}

VOICE_DISABLE_SCHEMA = {
    "name": "voice_disable",
    "description": "Disable continuous voice mode.",
    "parameters": {"type": "object", "properties": {}},
}

VOICE_SPEAK_SCHEMA = {
    "name": "voice_speak",
    "description": (
        "Speak text through the configured TTS engine and output to "
        "the configured media player or local speakers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to speak.",
            },
            "media_player_entity": {
                "type": "string",
                "description": "Optional HA media_player to output to.",
            },
        },
        "required": ["text"],
    },
}

VOICE_LISTEN_SCHEMA = {
    "name": "voice_listen",
    "description": (
        "One-shot listen: record audio from the microphone, transcribe it, "
        "and return the text. Useful for testing or push-to-talk workflows."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "duration": {
                "type": "number",
                "description": "Maximum recording duration in seconds (default: 10).",
            },
            "language": {
                "type": "string",
                "description": "Language code (e.g. 'en', 'fr'). Pass to STT engine.",
            },
        },
    },
}

VOICE_PROMPT_SCHEMA = {
    "name": "voice_prompt",
    "description": (
        "Return the voice-optimised system prompt with current Home Assistant "
        "context injected. Use this when Hermes is about to enter a voice "
        "interaction to ensure concise, natural spoken responses."
    ),
    "parameters": {"type": "object", "properties": {}},
}


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

_TOOLS = (
    ("voice_status",  VOICE_STATUS_SCHEMA,  _handle_voice_status,  "🎙️"),
    ("voice_enable",  VOICE_ENABLE_SCHEMA,  _handle_voice_enable,  "🔊"),
    ("voice_disable", VOICE_DISABLE_SCHEMA, _handle_voice_disable, "🔇"),
    ("voice_speak",   VOICE_SPEAK_SCHEMA,   _handle_voice_speak,   "🗣️"),
    ("voice_listen",  VOICE_LISTEN_SCHEMA,  _handle_voice_listen,  "👂"),
    ("voice_prompt",  VOICE_PROMPT_SCHEMA,  _handle_voice_prompt,  "📋"),
)


def register(ctx) -> None:
    """Register Voice Stack tools with Hermes.

    Registration is unconditional — tools that require unavailable engines
    return descriptive errors rather than being hidden, so users can see
    what's missing via voice_status.
    """
    global _plugin_ctx
    _plugin_ctx = ctx

    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="voice_stack",
            schema=schema,
            handler=handler,
            emoji=emoji,
        )
    # Start the HA-facing WebSocket receiver used by the Home Assistant
    # custom integration at /api/hermes/ws. It is fire-and-forget: when the
    # port is already occupied or aiohttp is unavailable, the warning is logged
    # and normal tool registration still succeeds.
    try:
        from .ws_receiver import set_assist_query_handler, start_ws_receiver
        set_assist_query_handler(lambda payload: _handle_assist_query_with_llm(ctx, payload))
        start_ws_receiver()
    except Exception as exc:
        logger.warning("Hermes HA WebSocket receiver did not start: %s", exc)

    # Initialise engine state so voice_status is accurate. Long-running hosts
    # such as the Hermes gateway may opt into immediate background listening;
    # interactive CLI sessions leave this unset to avoid microphone contention.
    engines_ready = _init_engines()
    if (
        engines_ready
        and os.getenv("HERMES_VOICE_AUTO_ENABLE", "").strip().lower()
        in {"1", "true", "yes", "on"}
    ):
        result = json.loads(_handle_voice_enable({}))
        if not result.get("ok"):
            logger.warning("Voice pipeline auto-enable failed: %s", result.get("error", result))
