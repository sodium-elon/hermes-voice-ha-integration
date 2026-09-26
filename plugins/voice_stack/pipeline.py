"""Voice Pipeline Orchestrator.

Orchestrates the end-to-end voice interaction loop:

    Wake Word → STT → [Hermes Agent loop] → TTS → HA media_player

The pipeline runs in a background thread. When the wake word fires, it:
1. Records audio until silence / max duration
2. Transcribes via STT
3. Passes text to the Hermes agent via callback
4. Synthesises the agent's response via TTS
5. Plays the response through HA media_player or local audio
"""

from __future__ import annotations

import json
import inspect
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import events

logger = logging.getLogger(__name__)


class _SilentHandledResponse(str):
    """Explicit successful side effect that must not be spoken."""


SILENT_HANDLED = _SilentHandledResponse("")


class VoiceRecordingError(RuntimeError):
    """Raised when microphone capture fails before speech classification."""


def _play_wake_beep() -> None:
    """Dispatch the configured wake cue before command capture.

    Alexa dispatch is synchronous but bounded so recording begins only after HA
    accepts or rejects the cue. Local playback remains fire-and-forget because a
    sleeping HDMI sink can block ffplay.
    """
    output = os.getenv("HERMES_WAKE_CUE_OUTPUT", "local").strip().lower()
    if output in {"none", "off", "disabled"}:
        return
    if output == "alexa":
        _play_wake_beep_blocking()
        # HA accepting notify.alexa_media does not mean the Echo has played it.
        # Measured live: the user naturally begins speaking ~3.5s after wake,
        # while the API returns in ~23ms. Do not burn that remote-device latency
        # inside the fixed command capture window.
        try:
            settle = float(os.getenv("HERMES_WAKE_CUE_SETTLE_SECONDS", "2.5"))
        except ValueError:
            settle = 2.5
        settle = min(max(settle, 0.0), 5.0)
        if settle:
            time.sleep(settle)
        return
    threading.Thread(target=_play_wake_beep_blocking, daemon=True).start()


def _play_wake_beep_blocking() -> None:
    """Actually play the cue. Runs off the pipeline thread; never raises."""
    try:
        if os.getenv("HERMES_WAKE_CUE_OUTPUT", "local").strip().lower() == "alexa":
            target = os.getenv("HERMES_ALEXA_MEDIA_PLAYER", "").strip()
            if not target:
                logger.warning("Alexa wake cue requested without HERMES_ALEXA_MEDIA_PLAYER")
                return
            cue_text = os.getenv("HERMES_WAKE_CUE_TEXT", "").strip()
            accepted = play_text_alexa(
                cue_text or ".",
                target,
                notification_type="announce",
                timeout=2,
            )
            events.emit("wake_cue", target=target, accepted=accepted)
            return

        try:
            duration = float(os.getenv("HERMES_WAKE_BEEP_DURATION", "0.60"))
        except ValueError:
            duration = 0.60
        duration = min(max(duration, 0.10), 1.50)
        subprocess.run(
            [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=880:duration={duration:.2f}",
            ],
            capture_output=True,
            timeout=2,
            check=False,
            env={**os.environ, "SDL_AUDIODRIVER": "pulseaudio"},
        )
    except Exception as exc:
        logger.debug("Wake beep unavailable: %s", exc)

# ---------------------------------------------------------------------------
# Audio recording helper
# ---------------------------------------------------------------------------


def _float_audio_to_pcm16(audio: Any) -> Any:
    """Convert float audio to PCM16 without over-range integer wraparound."""
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 0.98:
        samples = samples * (0.98 / peak)
    samples = np.clip(samples, -1.0, 1.0)
    return np.rint(samples * 32767.0).astype(np.int16)


def _condition_capture(audio_data, sample_rate: int, gain: float = 1.0):
    """Condition a raw mic capture to maximize STT signal.

    Transport (TCP PulseAudio, echo, distance) delivers speech buried under
    low-frequency hum and a broadband noise floor. Three cheap stages:

    1. High-pass (4th-order Butterworth @ 150 Hz) — strips sub-150 Hz hum and
       room tone (measured: ~38% of capture energy in the retained WAV).
    2. Adaptive noise gate — estimates the quiet-frame noise floor and gates
       frames below a safety margin to true silence, restoring word boundaries
       so VAD reads real gaps instead of an amplified noise floor.
    3. Modest peak restore — pulls a weak capture up to a healthy peak (0.5),
       but never amplifies into clipping and caps the multiplier so it cannot
       turn the noise floor back into the dominant signal.

    Returns the conditioned float32 array (same length/shape as input).
    """
    import numpy as np
    if audio_data.ndim > 1:
        audio_data = audio_data.reshape(-1)
    if audio_data.size == 0:
        return audio_data
    arr = np.asarray(audio_data, dtype=np.float32)

    # 1. High-pass: strip mains/room hum.
    try:
        from scipy import signal as _sig
        nyq = float(sample_rate) / 2.0
        b, a = _sig.butter(4, 150.0 / nyq, btype="high")
        hi = _sig.filtfilt(b, a, arr)
    except Exception:
        hi = arr  # scipy unavailable: proceed ungated on raw

    # 2. Adaptive noise gate. Noise floor estimate = low percentile of 30 ms
    #    frame RMS. Gate threshold sits a safe margin above it (speech is
    #    >2-3x the floor; margin catches the noise without eating soft onsets).
    frame = max(int(0.030 * sample_rate), 16)
    n_frames = max(len(hi) // frame, 1)
    frms = np.sqrt(
        (hi[: n_frames * frame].reshape(n_frames, frame) ** 2).mean(axis=1)
    )
    floor = float(np.percentile(frms, 5))
    thr = max(floor * 1.6, 1e-4)
    # Soft gate: keep above-threshold frames as-is, ramp down the noise floor.
    gated = hi.copy()
    below = frms <= thr
    for idx in np.where(below)[0]:
        s = idx * frame
        e = s + frame
        pe = float(np.max(np.abs(gated[s:e])))
        gated[s:e] = np.where(
            np.abs(gated[s:e]) > thr,
            gated[s:e],
            gated[s:e] * (thr / pe if pe > thr else 0.0),
        )

    # 3. Modest peak restore toward a healthy speech level.
    out = gated
    if gain > 1.0:
        peak = float(np.max(np.abs(gated))) if gated.size else 0.0
        if 0.0 < peak < 0.9:
            out = gated * min(gain, 0.5 / peak)
        elif peak >= 0.9:
            out = gated  # already hot; do not clip
        else:
            out = gated
    return np.asarray(out, dtype=np.float32)


def record_audio(
    output_path: str,
    duration: float = 10.0,
    sample_rate: int = 16000,
    silence_timeout: float = 2.0,
    silence_threshold: float = 0.02,
    gain: float = 1.0,
) -> bool:
    """Record audio from the default microphone.

    Records until silence is detected for `silence_timeout` seconds
    or `duration` is reached. Captured samples are conditioned to fix
    transport noise (low-frequency hum delivered over TCP PulseAudio) and a
    weak source level: a high-pass strips sub-150 Hz hum/room tone, an adaptive
    noise gate restores real silence between words (so VAD sees true gaps, not
    an amplified noise floor), and `gain` (>1.0) modestly restores peak level
    without blindly amplifying the noise floor. Returns True if audio was
    recorded, False on hardware error.
    """
    import numpy as np
    try:
        import sounddevice as sd
    except ImportError as exc:
        logger.error("sounddevice not installed — cannot record audio")
        raise VoiceRecordingError("sounddevice not installed — cannot record audio") from exc

    try:
        # Record raw audio
        audio_data = sd.rec(
            int(duration * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
        )
        sd.wait()

        audio_data = _condition_capture(audio_data, sample_rate, gain)

        # Detect speech onset (simple energy threshold)
        rms = np.sqrt(np.mean(audio_data ** 2))
        if rms < silence_threshold:
            logger.debug("Audio too quiet (RMS=%.4f), discarding", rms)
            events.emit(
                "no_speech",
                reason="below_rms_floor",
                rms=round(float(rms), 4),
                threshold=silence_threshold,
            )
            return False

        # Trim trailing silence
        frame_size = int(0.1 * sample_rate)  # 100ms frames
        energy = np.array([
            np.sqrt(np.mean(audio_data[i:i + frame_size] ** 2))
            for i in range(0, len(audio_data) - frame_size, frame_size)
        ])
        speech_frames = energy > silence_threshold
        if not speech_frames.any():
            events.emit(
                "no_speech",
                reason="no_speech_frames",
                rms=round(float(rms), 4),
                threshold=silence_threshold,
            )
            return False

        # Find last speech frame
        last_speech = np.where(speech_frames)[0][-1]
        trim_end = min((last_speech + int(silence_timeout / 0.1)) * frame_size, len(audio_data))
        trimmed = audio_data[:trim_end]

        # Save as WAV. PulseAudio can deliver float samples above 1.0 when the
        # source gain exceeds unity; normalize before PCM conversion so signed
        # int16 values do not wrap and destroy the waveform.
        import wave
        trimmed_int16 = _float_audio_to_pcm16(trimmed)
        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(trimmed_int16.tobytes())

        duration_actual = len(trimmed) / sample_rate
        logger.info("Recorded %.1fs of audio to %s", duration_actual, output_path)
        events.emit("record", seconds=round(duration_actual, 2), rms=round(float(rms), 4))
        return True

    except Exception as exc:
        logger.error("Audio recording failed: %s", exc)
        events.emit("error", stage="record", detail=str(exc))
        raise VoiceRecordingError(str(exc)) from exc


def _finalize_recording(audio_path: str) -> None:
    """Retain a debug recording or remove it after all consumers are finished."""
    try:
        if os.getenv("HERMES_VOICE_RETAIN_WAV", "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            retained = audio_path + ".retained.wav"
            os.replace(audio_path, retained)
            logger.info("Retained WAV for re-decode lab: %s", retained)
        else:
            os.unlink(audio_path)
    except OSError as exc:
        logger.warning("Could not finalize recording %s: %s", audio_path, exc)


def _invoke_callback(callback, text: str, **available_kwargs):
    """Call once, passing only keyword arguments supported by the callback."""
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(text, **available_kwargs)
    parameters = signature.parameters
    accepts_all = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs = available_kwargs if accepts_all else {
        name: value for name, value in available_kwargs.items() if name in parameters
    }
    return callback(text, **kwargs)


# ---------------------------------------------------------------------------
# Audio playback
# ---------------------------------------------------------------------------

def _probe_audio_duration(audio_path: str) -> Optional[float]:
    """Return media duration in seconds, or None when ffprobe is unavailable."""
    if not shutil.which("ffprobe"):
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except Exception as exc:
        logger.debug("Could not probe audio duration: %s", exc)
        return None


def _audio_playback_timeout(audio_path: str) -> float:
    """Bound playback above clip duration while still killing stuck players."""
    duration = _probe_audio_duration(audio_path)
    if duration is None:
        return 60.0
    return min(120.0, max(30.0, duration + 10.0))


def play_audio_local(audio_path: str, device_id: Optional[int] = None) -> bool:
    """Play an audio file through local speakers via ffplay."""
    if not os.path.exists(audio_path):
        logger.error("Audio file not found: %s", audio_path)
        return False

    # Prefer ffplay (part of ffmpeg)
    for player in ("ffplay", "afplay", "aplay", "paplay"):
        if shutil.which(player):
            cmd = [player]
            if player == "ffplay":
                cmd += ["-autoexit", "-nodisp", "-loglevel", "quiet"]
                if device_id is not None:
                    cmd += ["-audio_device", str(device_id)]
            cmd.append(audio_path)
            try:
                subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=_audio_playback_timeout(audio_path),
                    check=True,
                )
                return True
            except Exception as exc:
                logger.error("%s playback failed: %s", player, exc)
                return False

    logger.error("No audio player found (install ffmpeg)")
    return False


def play_audio_ha(audio_path: str, media_player_entity: str) -> bool:
    """Play an audio file through a Home Assistant media_player entity.

    Uses the Hermes HTTP server to serve the audio file, then calls
    media_player.play_media on the HA entity to stream it.
    """
    # Import the HA bridge to call services
    try:
        from ..home_assistant.ha_assistant import call_service
    except ImportError:
        logger.error("HA bridge not available — cannot use media_player playback")
        return False

    # Determine the audio URL (serve from Hermes HTTP server)
    audio_url = f"file://{audio_path}"

    result = call_service(
        "media_player",
        "play_media",
        entity_id=media_player_entity,
        data={
            "media_content_id": audio_url,
            "media_content_type": "music",
        },
    )
    if "error" in result:
        logger.error("HA media_player.play_media failed: %s", result["error"])
        return False
    return True


def play_text_alexa(
    text: str,
    media_player_entity: str,
    notification_type: str = "tts",
    timeout: float = 12,
) -> bool:
    """Speak text through Alexa Media Player's notify TTS REST service."""
    import urllib.request

    hass_url = os.getenv("HASS_URL", "").rstrip("/")
    hass_token = os.getenv("HASS_TOKEN", "")
    if not hass_token:
        # Profile gateways do not inherit the default profile's .env. Reuse the
        # machine-local HA credentials without copying secrets between profiles
        # (same fallback as ha_conversation._get_config).
        try:
            from dotenv import dotenv_values

            shared = dotenv_values(Path.home() / ".hermes" / ".env")
            hass_url = hass_url or str(shared.get("HASS_URL") or "")
            hass_token = str(shared.get("HASS_TOKEN") or "")
        except Exception:
            pass
    if not hass_url:
        hass_url = "http://homeassistant.local:8123"
    if not hass_token:
        logger.error("HASS_TOKEN is not configured — cannot use Alexa TTS")
        return False
    hass_url = hass_url.rstrip("/")

    payload = json.dumps({
        "message": text,
        "target": [media_player_entity],
        "data": {"type": notification_type},
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{hass_url}/api/services/notify/alexa_media",
        data=payload,
        headers={
            "Authorization": f"Bearer {hass_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            if not 200 <= response.status < 300:
                logger.error("notify.alexa_media returned HTTP %s", response.status)
                return False
    except Exception as exc:
        logger.error("notify.alexa_media failed: %s", exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Pipeline state
# ---------------------------------------------------------------------------

class VoicePipelineState:
    """Encapsulates the mutable state of the voice pipeline."""

    def __init__(self) -> None:
        self.enabled: bool = False
        self.listening: bool = False
        self.wake_word_detected: bool = False
        self.last_transcript: str = ""
        self.last_confidence: float = 1.0
        self.total_interactions: int = 0
        self.total_errors: int = 0
        self.start_time: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        uptime = time.monotonic() - self.start_time if self.start_time else 0
        return {
            "enabled": self.enabled,
            "listening": self.listening,
            "wake_word_detected": self.wake_word_detected,
            "last_transcript": self.last_transcript[:100],
            "last_confidence": round(self.last_confidence, 3),
            "total_interactions": self.total_interactions,
            "total_errors": self.total_errors,
            "uptime_seconds": round(uptime, 1),
        }


# ---------------------------------------------------------------------------
# Voice System Prompt
# ---------------------------------------------------------------------------

VOICE_SYSTEM_PROMPT = """You are Hermes, a voice-controlled home assistant running entirely on-device.
No cloud. No latency. No subscription.

Current time: {current_time}
Home areas: {areas}
Key entities: {entities}

RULES:
1. Respond CONCISELY — this is voice. One sentence when possible, at most two.
2. Use ha_call_service to control devices.
3. Use ha_search_entities to find entities by name, domain, or area.
4. If ambiguous, ask a brief clarifying question (≤7 words).
5. Never return internal state JSON — summarise in plain English.
6. After calling a service, confirm success in ≤5 words (e.g. "Done. Light is off.")
7. If a service fails, tell the user what went wrong in one sentence.
8. Use the user's name if known, but don't overdo it.

VOICE-ONLY CONSTRAINTS:
- No markdown, code blocks, or bullet points
- No URLs or file paths
- Spell out numbers naturally (e.g. "twenty-two degrees" not "22°C")
- Prefer "living room" over "living_room"
- Never say "entity_id" or "service" — speak like a person"""


def build_voice_system_prompt(
    current_time: Optional[str] = None,
    areas: Optional[Dict[str, Any]] = None,
    entities: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build a voice-optimised system prompt with current context."""
    from datetime import datetime

    if current_time is None:
        current_time = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")

    areas_str = json.dumps(areas, indent=2) if areas else "Unknown"

    if entities is None:
        entities_str = "No entity data loaded"
    elif len(entities) > 20:
        # Summarise for the prompt
        by_domain: Dict[str, int] = {}
        for e in entities:
            domain = e.get("entity_id", "").split(".")[0]
            by_domain[domain] = by_domain.get(domain, 0) + 1
        entities_str = f"{len(entities)} entities across {len(by_domain)} domains: {json.dumps(by_domain)}"
    else:
        entities_str = json.dumps(entities, indent=2)

    return VOICE_SYSTEM_PROMPT.format(
        current_time=current_time,
        areas=areas_str,
        entities=entities_str,
    )


# ---------------------------------------------------------------------------
# Pipeline Orchestrator
# ---------------------------------------------------------------------------


def _estimate_alexa_speech_seconds(text: str) -> float:
    """Estimate bounded Alexa speech time when no completion event exists."""
    words = len(text.split())
    return min(12.0, max(1.5, 0.8 + words / 2.5))


def _response_requests_follow_up(text: str) -> bool:
    """Recognize explicit questions and short clarification directives."""
    stripped = text.rstrip().rstrip("\"'")
    if stripped.endswith("?"):
        return True
    clarification_directive = re.search(
        r"(?:^|[;,:]\s+|\bso\s+)(?:please\s+)?"
        r"(?:name|choose|pick|specify|clarify|confirm|tell me|let me know)\b"
        r"[^.!?]*[.!]?$",
        stripped[-200:],
        flags=re.IGNORECASE,
    )
    missing_detail = re.search(
        r"\b(?:i|we)\s+(?:still\s+)?need\s+(?:a|an|the|your)?\s*"
        r"(?:city|country|location|place|name|choice|answer|confirmation|details?)\b"
        r"[^.!?]*[.!]?$",
        stripped[-200:],
        flags=re.IGNORECASE,
    )
    return bool(clarification_directive or missing_detail)


class VoicePipeline:
    """Orchestrates the voice pipeline: WakeWord → STT → LLM → TTS → Playback.

    Usage:
        pipeline = VoicePipeline(callback=handle_user_text)
        pipeline.start()
        # ... wake word triggers recording → STT → callback → TTS ...
        pipeline.stop()
    """

    def __init__(
        self,
        callback: Callable[..., str],
        *,
        wake_word_engine: Any = None,
        stt_engine: Any = None,
        tts_engine: Any = None,
        media_player_entity: Optional[str] = None,
        alexa_media_player_entity: Optional[str] = None,
        max_record_duration: float = 10.0,
        silence_timeout: float = 2.0,
        speech_threshold: float = 0.005,
        record_gain: float = 1.0,
        language: str = "en",
        min_confidence: float = 0.15,
        follow_up_min_confidence: float = 0.02,
        route_low_confidence: bool = False,
        confidence_threshold: float = 0.70,
        wake_cooldown: float = 5.0,
        follow_up_delay: float = 0.35,
        max_follow_up_turns: int = 2,
    ) -> None:
        self._callback = callback  # (text: str) -> response: str
        self._wake_word = wake_word_engine
        self._stt = stt_engine
        self._tts = tts_engine
        self._media_player_entity = media_player_entity
        self._alexa_media_player_entity = alexa_media_player_entity
        self._max_record_duration = max_record_duration
        self._silence_timeout = silence_timeout
        self._speech_threshold = speech_threshold
        self._record_gain = record_gain
        self._language = language
        self._min_confidence = min_confidence
        self._follow_up_min_confidence = follow_up_min_confidence
        self._route_low_confidence = route_low_confidence
        # Retained as an ignored constructor argument for compatibility with
        # older callers. The low floor above silently rejects hallucinations;
        # it never triggers the former fake confirmation branch.
        # Refractory period (cf. wyoming-satellite --wake-refractory-seconds):
        # after a turn's TTS finishes, do not run wake-word detection until
        # this many seconds have passed. The mic hears our own reply through
        # the speakers and its reverb tail otherwise re-triggers the wake word
        # within a second (observed scores 0.75–0.98 right after "spoken").
        self._wake_cooldown = max(0.0, float(wake_cooldown))
        self._last_turn_end = 0.0
        self._follow_up_delay = max(0.0, float(follow_up_delay))
        self._max_follow_up_turns = max(0, int(max_follow_up_turns))
        self._follow_up_pending = False
        self._follow_up_turns = 0
        self._alexa_reply_ready_at = 0.0

        self._thread: Optional[threading.Thread] = None
        self._state = VoicePipelineState()
        self._lock = threading.Lock()
        # Follow-up timeout: when the assistant asks a short follow-up question,
        # stay armed for a quick answer only this long, then give up and return
        # to wake-word listening. The wake-word listener itself stays ON always
        # (HERMES_FOLLOW_UP_TIMEOUT); it must never be disarmed by inactivity.
        self._follow_up_timeout = max(0.0, float(os.getenv("HERMES_FOLLOW_UP_TIMEOUT", "6")))

    @property
    def state(self) -> VoicePipelineState:
        return self._state

    @property
    def available(self) -> bool:
        """Return True if at minimum TTS and STT engines are available."""
        tts_ok = self._tts is not None and self._tts.available()
        stt_ok = self._stt is not None and self._stt.available()
        return tts_ok and stt_ok

    def start(self) -> bool:
        """Start the voice pipeline in a background thread."""
        if not self.available:
            logger.error("Cannot start voice pipeline: engines not available")
            return False

        if self._thread and self._thread.is_alive():
            logger.warning("Voice pipeline already running")
            return False

        self._state.enabled = True
        self._state.start_time = time.monotonic()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="voice-pipeline")
        self._thread.start()
        logger.info("Voice pipeline started")
        return True

    def stop(self) -> None:
        """Stop the voice pipeline."""
        self._state.enabled = False
        if self._wake_word:
            self._wake_word.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        logger.info("Voice pipeline stopped")

    def _wait_out_cooldown(self) -> None:
        """Sleep (in stop-responsive slices) until the wake cooldown expires."""
        while self._state.enabled:
            remaining = self._wake_cooldown - (time.monotonic() - self._last_turn_end)
            if remaining <= 0 or self._wake_cooldown <= 0:
                return
            logger.debug("Wake cooldown: %.1fs remaining", remaining)
            time.sleep(min(0.25, remaining))

    def _wait_before_follow_up(self) -> None:
        """Avoid cueing over an Alexa question whose playback is still likely active."""
        delay = self._follow_up_delay
        if self._alexa_media_player_entity:
            delay = max(delay, self._alexa_reply_ready_at - time.monotonic())
        if delay > 0:
            time.sleep(delay)

    def _confidence_floor(self, *, follow_up: bool) -> float:
        """Use a lower floor for short answers inside an armed conversation."""
        return self._follow_up_min_confidence if follow_up else self._min_confidence

    def _run_loop(self) -> None:
        """Main voice pipeline loop (runs in background thread)."""
        while self._state.enabled:
            try:
                self._state.listening = True

                follow_up = self._follow_up_pending
                self._follow_up_pending = False
                if follow_up:
                    self._wait_before_follow_up()
                    events.emit("follow_up", phase="listening", turn=self._follow_up_turns)
                    _play_wake_beep()
                else:
                    self._wait_out_cooldown()

                    # 1. Wait for wake word
                    if self._wake_word:
                        detected = self._wake_word.listen(timeout_seconds=5.0)
                        if not detected:
                            continue
                        self._state.wake_word_detected = True
                        _play_wake_beep()

                # 2. Record audio
                cache_dir = Path.home() / ".hermes" / "voice_cache"
                cache_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=str(cache_dir)) as tmp:
                    audio_path = tmp.name

                try:
                    # Follow-ups wait only a bounded window for the quick answer;
                    # an unanswered follow-up times out and returns to wake listening.
                    record_duration = (
                        self._follow_up_timeout if follow_up and self._follow_up_timeout
                        else self._max_record_duration
                    )
                    recorded = record_audio(
                        audio_path,
                        duration=record_duration,
                        silence_timeout=self._silence_timeout,
                        silence_threshold=self._speech_threshold,
                        gain=self._record_gain,
                    )
                except Exception:
                    try:
                        os.unlink(audio_path)
                    except OSError:
                        pass
                    raise
                if not recorded:
                    os.unlink(audio_path)
                    self._state.wake_word_detected = False
                    self._follow_up_turns = 0
                    continue

                # 3. STT
                stt_started = time.monotonic()
                try:
                    stt_result = self._stt.transcribe_with_confidence(
                        audio_path, language=self._language
                    )
                except Exception:
                    # The WAV is owned by this turn; never leak it on an STT
                    # failure. Finalize (delete or retain) then propagate.
                    _finalize_recording(audio_path)
                    raise
                stt_elapsed = time.monotonic() - stt_started
                transcript = stt_result.get("text", "").strip()
                confidence = stt_result.get("confidence", 1.0)

                self._state.last_transcript = transcript
                self._state.last_confidence = confidence

                if not transcript:
                    _finalize_recording(audio_path)
                    events.emit(
                        "no_speech",
                        reason="empty_transcript",
                        stt_seconds=round(stt_elapsed, 2),
                    )
                    continue

                confidence_floor = self._confidence_floor(follow_up=follow_up)
                if confidence < confidence_floor and not self._route_low_confidence:
                    _finalize_recording(audio_path)
                    logger.debug(
                        "Discarding STT hallucination below confidence floor: %.3f %r",
                        confidence,
                        transcript,
                    )
                    events.emit(
                        "discarded",
                        text=events.truncate(transcript),
                        confidence=round(float(confidence), 3),
                        min_confidence=confidence_floor,
                        stt_seconds=round(stt_elapsed, 2),
                    )
                    self._state.wake_word_detected = False
                    continue

                logger.info("Voice input: \"%s\" (confidence=%.2f)", transcript, confidence)
                events.emit(
                    "heard",
                    text=events.truncate(transcript),
                    confidence=round(float(confidence), 3),
                    stt_seconds=round(stt_elapsed, 2),
                    language=self._language,
                )

                # 4. LLM callback
                think_started = time.monotonic()

                def redecode(*, hotwords=None, initial_prompt=None):
                    """Bounded same-recording STT pass for evidence gathering."""
                    return self._stt.transcribe_with_confidence(
                        audio_path,
                        language=self._language,
                        hotwords=hotwords,
                        initial_prompt=initial_prompt,
                    )

                try:
                    response = _invoke_callback(
                        self._callback,
                        transcript,
                        confidence=confidence,
                        audio_path=audio_path,
                        redecode=redecode,
                    )
                finally:
                    _finalize_recording(audio_path)
                think_elapsed = time.monotonic() - think_started

                if response is SILENT_HANDLED:
                    events.emit(
                        "reply",
                        text="",
                        empty=True,
                        handled=True,
                        think_seconds=round(think_elapsed, 2),
                    )
                    self._last_turn_end = time.monotonic()
                    self._state.total_interactions += 1
                    self._state.wake_word_detected = False
                    self._follow_up_turns = 0
                    continue

                if not response:
                    events.emit(
                        "reply",
                        text="",
                        empty=True,
                        think_seconds=round(think_elapsed, 2),
                    )
                    continue

                events.emit(
                    "reply",
                    text=events.truncate(response),
                    think_seconds=round(think_elapsed, 2),
                )

                # 5. TTS synthesis + playback
                spoke = self._speak(response)
                # Start the refractory clock when playback ends (success or
                # not): the speaker tail is what re-triggers the wake word.
                self._last_turn_end = time.monotonic()
                if not spoke:
                    continue

                self._state.total_interactions += 1
                self._state.wake_word_detected = False
                requests_follow_up = _response_requests_follow_up(response)
                if requests_follow_up and self._follow_up_turns < self._max_follow_up_turns:
                    self._follow_up_turns += 1
                    self._follow_up_pending = True
                    events.emit("follow_up", phase="armed", turn=self._follow_up_turns)
                else:
                    self._follow_up_turns = 0

            except Exception as exc:
                logger.error("Voice pipeline error: %s", exc, exc_info=True)
                events.emit("error", stage="pipeline", detail=str(exc))
                self._state.total_errors += 1
                time.sleep(1.0)  # Back off on error
            finally:
                self._state.listening = False

    def _speak(self, text: str) -> bool:
        """Speak text through the configured Alexa, HA, or local output."""
        sink = self._alexa_media_player_entity or self._media_player_entity or "local speakers"
        for attempt in (1, 2):
            started = time.monotonic()
            try:
                if self._alexa_media_player_entity:
                    played = play_text_alexa(text, self._alexa_media_player_entity)
                    if not played:
                        raise RuntimeError("Alexa TTS failed")
                    self._alexa_reply_ready_at = (
                        time.monotonic() + _estimate_alexa_speech_seconds(text)
                    )
                    events.emit(
                        "spoken",
                        sink=sink,
                        attempt=attempt,
                        tts_seconds=round(time.monotonic() - started, 2),
                    )
                    return True

                audio_path = self._tts.synthesize(text)
                if not audio_path:
                    raise RuntimeError("TTS engine returned no audio path")
                if self._media_player_entity:
                    played = play_audio_ha(audio_path, self._media_player_entity)
                else:
                    played = play_audio_local(audio_path)
                if not played:
                    raise RuntimeError("Audio playback failed")
                events.emit(
                    "spoken",
                    sink=sink,
                    attempt=attempt,
                    tts_seconds=round(time.monotonic() - started, 2),
                )
                return True
            except Exception as exc:
                logger.warning("TTS speak failed (attempt %d/2): %s", attempt, exc)
                events.emit("speak_failed", sink=sink, attempt=attempt, detail=str(exc))
                if attempt == 1:
                    time.sleep(0.5)  # brief backoff before retry
                else:
                    logger.error("TTS speak failed after 2 attempts: %s", exc)
        return False
