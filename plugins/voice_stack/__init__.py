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

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import events, sessions_api

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
        "max_record_duration": float(os.getenv("HERMES_RECORD_DURATION", "10")),
        "silence_timeout": float(os.getenv("HERMES_SILENCE_TIMEOUT", "2.0")),
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


def _run_full_agent(text: str, *, profile: Optional[str] = None) -> str:
    """Run one tool-capable Hermes turn for an unmatched voice query.

    Goes through the gateway's API-server platform rather than spawning a
    `hermes chat` subprocess: the agent is already warm, so this skips the
    per-turn CLI cold start, and the SSE stream reports tool calls live
    instead of leaving them to be scraped back out of agent.log.
    """
    started = time.monotonic()
    try:
        response = sessions_api.complete(
            text,
            profile=profile,
            system_prompt=VOICE_AGENT_SYSTEM_PROMPT,
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


def _complete_voice_with_hermes(ctx: Any, text: str) -> str:
    """Run the full tool-capable agent, degrading to raw LLM only on failure."""
    profile = _voice_profile_for(text)
    try:
        if profile:
            return _run_full_agent(text, profile=profile)
        return _run_full_agent(text)
    except Exception as exc:
        if profile:
            logger.warning("Profile voice agent %s failed: %s", profile, exc)
            display_name = {"sportscoach": "Sports Coach"}.get(profile, profile)
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


def _route_voice_transcript(ctx: Any, text: str, *, language: str = "en") -> str:
    """Let native HA execute commands; use Hermes only for unmatched conversation."""
    from . import ha_conversation

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

    if data.get("code") == "no_valid_targets":

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

        from .pipeline import VoicePipeline

        # Define the callback that sends user text through Hermes's supported
        # plugin LLM facade. The pipeline runs in a background thread, so use
        # the synchronous facade rather than the async Assist receiver path.
        def _voice_callback(text: str) -> str:
            """Route STT text through native HA before Hermes conversation."""
            logger.info("Voice callback received: %s", text)
            if _plugin_ctx is None:
                return "Hermes voice processing is unavailable."
            return _route_voice_transcript(
                _plugin_ctx, text, language=config["stt"]["language"]
            )

        _pipeline = VoicePipeline(
            callback=_voice_callback,
            wake_word_engine=_wake_word_engine,
            stt_engine=_stt_engine,
            tts_engine=_tts_engine,
            media_player_entity=media_player,
            max_record_duration=config["max_record_duration"],
            silence_timeout=config["silence_timeout"],
            speech_threshold=config["stt"]["speech_threshold"],
            language=config["stt"]["language"],
            min_confidence=config["stt"]["min_confidence"],
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
        recorded = record_audio(audio_path, duration=duration)
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


async def _handle_assist_query_with_llm(ctx: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Turn an HA Assist query into a Hermes LLM response.

    This handles the HA-side ``assist_query`` message introduced by the
    conversation platform. It intentionally uses ``ctx.llm`` rather than a
    placeholder acknowledgement so Home Assistant receives a real spoken reply.
    """
    text = str(payload.get("text") or "").strip()
    language = str(payload.get("language") or "en")
    conversation_id = payload.get("conversation_id")

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
