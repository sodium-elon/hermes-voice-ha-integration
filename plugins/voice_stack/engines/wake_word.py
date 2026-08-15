"""Wake Word Engine — keyword spotting.

Concrete engines:
- PorcupineEngine: Picovoice Porcupine (commercial, most accurate)
- OpenWakeWordEngine: OpenWakeWord (open source, good accuracy)
- CommandWWEngine: Generic CLI wake word (for custom engines)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import events

logger = logging.getLogger(__name__)


class WakeWordEngine(ABC):
    """Abstract wake word engine interface."""

    @abstractmethod
    def available(self) -> bool:
        """Return True if this engine's dependencies are installed."""
        ...

    @abstractmethod
    def listen(self, timeout_seconds: float = 60.0) -> bool:
        """Block until the wake word is detected or timeout.
        Returns True if wake word was detected.
        """
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop listening (for graceful shutdown)."""
        ...

    def list_wake_words(self) -> List[str]:
        """Return available wake word models."""
        return []


# ---------------------------------------------------------------------------
# Porcupine (Picovoice — most accurate, requires API key)
# ---------------------------------------------------------------------------

class PorcupineEngine(WakeWordEngine):
    """Porcupine wake word engine via pvporcupine.

    Install: pip install pvporcupine
    Free API key from: https://console.picovoice.ai/

    Built-in wake words: "computer", "jarvis", "alexa", "hey google", "hey siri", "ok google", "porcupine", "terminator"
    Custom wake words: Train at https://console.picovoice.ai/
    """

    def __init__(
        self,
        access_key: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        sensitivities: Optional[List[float]] = None,
    ) -> None:
        self._access_key = access_key or os.getenv("PORCUPINE_ACCESS_KEY", "")
        self._keywords = keywords or ["computer"]
        self._sensitivities = sensitivities or [0.5] * len(self._keywords)
        self._porcupine = None
        self._audio_stream = None
        self._stop = False

    def available(self) -> bool:
        try:
            import pvporcupine  # noqa: F401
            import pyaudio  # noqa: F401
            return True
        except ImportError:
            return False

    def listen(self, timeout_seconds: float = 60.0) -> bool:
        import pvporcupine
        import pyaudio
        import struct
        import time

        if not self._access_key:
            raise RuntimeError("PORCUPINE_ACCESS_KEY not set")

        self._porcupine = pvporcupine.create(
            access_key=self._access_key,
            keywords=self._keywords,
            sensitivities=self._sensitivities,
        )
        self._audio = pyaudio.PyAudio()
        self._stop = False

        try:
            self._audio_stream = self._audio.open(
                rate=self._porcupine.sample_rate,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=self._porcupine.frame_length,
            )

            start = time.monotonic()
            while not self._stop:
                if time.monotonic() - start > timeout_seconds:
                    return False

                pcm = self._audio_stream.read(self._porcupine.frame_length, exception_on_overflow=False)
                pcm = struct.unpack_from("h" * self._porcupine.frame_length, pcm)
                keyword_index = self._porcupine.process(pcm)
                if keyword_index >= 0:
                    logger.info("Wake word detected: %s", self._keywords[keyword_index])
                    events.begin_turn()
                    events.emit(
                        "wake",
                        engine="porcupine",
                        word=self._keywords[keyword_index],
                    )
                    return True

            return False
        finally:
            self._cleanup()

    def stop(self) -> None:
        self._stop = True

    def _cleanup(self) -> None:
        if self._audio_stream:
            try:
                self._audio_stream.stop_stream()
                self._audio_stream.close()
            except Exception:
                pass
            self._audio_stream = None
        if self._audio:
            try:
                self._audio.terminate()
            except Exception:
                pass
        if self._porcupine:
            try:
                self._porcupine.delete()
            except Exception:
                pass
            self._porcupine = None

    def list_wake_words(self) -> List[str]:
        return [
            "computer", "jarvis", "alexa", "hey google", "hey siri",
            "ok google", "porcupine", "terminator", "blueberry", "bumblebee",
            "grapefruit", "grasshopper", "hey barista", "hey edison",
            "picovoice", "pico clock",
        ]


# ---------------------------------------------------------------------------
# OpenWakeWord (open-source, onnxruntime-based)
# ---------------------------------------------------------------------------

class OpenWakeWordEngine(WakeWordEngine):
    """OpenWakeWord engine.

    Install: pip install openwakeword
    Pre-trained models: https://github.com/fwartner/openwakeword-models

    This engine uses a simpler polling-based approach that checks audio
    chunks for wake word activation.
    """

    def __init__(
        self,
        model_paths: Optional[List[str]] = None,
        threshold: float = 0.5,
        vad_threshold: Optional[float] = None,
    ) -> None:
        self._model_paths = model_paths or []
        self._threshold = threshold
        # Silero VAD gate (bundled with openwakeword): predictions only count
        # while speech is present. Calibrated 2026-08-15: real wake clips keep
        # max score 0.99 with vad_threshold=0.5 while ambient TV drops to 0.0.
        self._vad_threshold = vad_threshold
        self._models: List[Any] = []
        self._audio_stream = None
        self._stop = False

    def available(self) -> bool:
        try:
            import openwakeword  # noqa: F401
            import numpy  # noqa: F401
            import pyaudio  # noqa: F401
            return True
        except ImportError:
            return False

    def listen(self, timeout_seconds: float = 60.0) -> bool:
        import pyaudio
        import numpy as np
        import time
        from openwakeword.model import Model

        if not self._models:
            if not self._model_paths:
                # Use built-in pre-trained models that ship with openwakeword
                self._models = [Model(wakeword_models=["alexa"], vad_threshold=self._vad_threshold)]
            else:
                self._models = [
                    Model(
                        wakeword_models=[path],
                        inference_framework="onnx" if Path(path).suffix.lower() == ".onnx" else "tflite",
                        vad_threshold=self._vad_threshold,
                    )
                    for path in self._model_paths
                ]

        # A fresh listen session starts a fresh audio stream; drop any
        # melspectrogram features left over from the previous turn so the
        # TTS reply we just played cannot bleed into these predictions.
        for model in self._models:
            reset = getattr(model, "reset", None)
            if callable(reset):
                reset()

        if self._audio_stream is None:
            self._audio = pyaudio.PyAudio()
            self._audio_stream = self._audio.open(
                rate=16000,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=1280,
            )
        self._stop = False
        chunk_rate = 16000

        try:
            start = time.monotonic()
            while not self._stop:
                if time.monotonic() - start > timeout_seconds:
                    return False

                pcm = np.frombuffer(self._audio_stream.read(1280, exception_on_overflow=False), dtype=np.int16)

                for model in self._models:
                    predictions = model.predict(pcm)
                    for wake_word, score in predictions.items():
                        if score > self._threshold:
                            logger.info("Wake word '%s' detected (score: %.2f)", wake_word, score)
                            events.begin_turn()
                            events.emit(
                                "wake",
                                engine="openwakeword",
                                word=wake_word,
                                score=round(float(score), 3),
                                threshold=self._threshold,
                            )
                            self._cleanup()
                            return True

            return False
        except Exception:
            self._cleanup()
            raise
        finally:
            if self._stop:
                self._cleanup()

    def stop(self) -> None:
        self._stop = True

    def _cleanup(self) -> None:
        if self._audio_stream:
            try:
                self._audio_stream.stop_stream()
                self._audio_stream.close()
            except Exception:
                pass
            self._audio_stream = None
        if hasattr(self, "_audio") and self._audio:
            try:
                self._audio.terminate()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Command Wake Word (generic CLI wrapper)
# ---------------------------------------------------------------------------

class CommandWWEngine(WakeWordEngine):
    """Generic CLI-based wake word engine.

    Config: wake_word.command = ["my-wake-detector", "--timeout", "{timeout}"]
    The {timeout} placeholder is substituted at runtime. The command should
    exit 0 when the wake word is detected, non-zero on timeout or error.
    """

    def __init__(self, command: list[str]) -> None:
        self._command = command
        self._proc = None

    def available(self) -> bool:
        return shutil.which(self._command[0]) is not None if self._command else False

    def listen(self, timeout_seconds: float = 60.0) -> bool:
        cmd = [part.replace("{timeout}", str(int(timeout_seconds))) for part in self._command]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        try:
            stdout, _ = self._proc.communicate(timeout=timeout_seconds + 5)
            detected = self._proc.returncode == 0
        except subprocess.TimeoutExpired:
            self.stop()
            return False
        finally:
            self._proc = None

        if detected:
            # Detection happened wherever the command runs, so there is no score
            # or threshold to report -- only the name it printed, if any. Emit
            # regardless: the other engines open the turn here, and without it
            # the monitor shows no wake at all and every later stage of the
            # interaction lands in turn 0.
            events.begin_turn()
            events.emit(
                "wake",
                engine="command",
                word=(stdout or "").strip() or "unknown",
            )
        return detected

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_wake_word_engine(engine_type: str = "porcupine", **kwargs: Any) -> WakeWordEngine:
    """Create a wake word engine instance by name.

    Args:
        engine_type: "porcupine", "openwakeword", or "command"
        **kwargs: Passed to engine constructor
    """
    if engine_type == "porcupine":
        return PorcupineEngine(
            access_key=kwargs.get("access_key"),
            keywords=kwargs.get("keywords", ["computer"]),
            sensitivities=kwargs.get("sensitivities"),
        )
    elif engine_type == "openwakeword":
        return OpenWakeWordEngine(
            model_paths=kwargs.get("model_paths"),
            threshold=float(kwargs.get("threshold", 0.5)),
            vad_threshold=(
                float(kwargs["vad_threshold"])
                if kwargs.get("vad_threshold") not in (None, 0, "")
                else None
            ),
        )
    elif engine_type == "command":
        return CommandWWEngine(command=kwargs.get("command", ["false"]))
    else:
        raise ValueError(f"Unknown wake word engine '{engine_type}'. Valid: porcupine, openwakeword, command")
