"""STT Engine — speech-to-text transcription.

Concrete engines:
- FasterWhisperEngine: CTranslate2-backed Whisper (fast, efficient)
- WhisperCPPEngine: whisper.cpp via CLI (GGUF models, runs on CPU)
- CommandSTTEngine: Generic CLI STT (for custom engines)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Cache dir for STT temp files
_STT_CACHE_DIR = Path(os.getenv("HERMES_VOICE_CACHE", str(Path.home() / ".hermes" / "voice_cache")))
_STT_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class STTEngine(ABC):
    """Abstract STT engine interface."""

    @abstractmethod
    def available(self) -> bool:
        """Return True if this engine's dependencies are installed."""
        ...

    @abstractmethod
    def transcribe(self, audio_path: str, language: Optional[str] = None) -> str:
        """Transcribe audio file to text. Returns the transcript."""
        ...

    @abstractmethod
    def transcribe_with_confidence(self, audio_path: str, language: Optional[str] = None) -> Dict[str, Any]:
        """Transcribe and return text + confidence score."""
        ...

    def list_languages(self) -> List[str]:
        """Return supported language codes, or empty list if unknown."""
        return []


# ---------------------------------------------------------------------------
# Faster-Whisper (CTranslate2 — fast, efficient, Python-native)
# ---------------------------------------------------------------------------

class FasterWhisperEngine(STTEngine):
    """Faster-Whisper STT — CTranslate2-backed, 4x faster than openai-whisper.

    Install: pip install faster-whisper

    Models: tiny, base, small, medium, large-v3
    First call downloads the model from HuggingFace (~75MB for tiny).
    """

    def __init__(
        self,
        model_size: str = "tiny",
        device: str = "auto",
        compute_type: str = "int8",
        initial_prompt: Optional[str] = None,
        hotwords: Optional[str] = None,
        vad_filter: bool = False,
        condition_on_previous_text: bool = False,
        no_speech_threshold: Optional[float] = None,
        log_prob_threshold: Optional[float] = None,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._initial_prompt = initial_prompt
        self._hotwords = hotwords
        self._vad_filter = vad_filter
        # Hallucination controls. condition_on_previous_text defaults to True
        # upstream and lets one hallucinated segment seed the next; decoding
        # each utterance independently keeps "Thanks for watching!" loops out.
        self._condition_on_previous_text = condition_on_previous_text
        self._no_speech_threshold = no_speech_threshold
        self._log_prob_threshold = log_prob_threshold
        self._model = None  # Lazy-loaded

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
            )
        return self._model

    def _transcribe_options(self, language: Optional[str], hotwords: Optional[str] = None, initial_prompt: Optional[str] = None) -> Dict[str, Any]:
        # Per-call overrides (used by the candidate-biased re-decode loop) layer
        # over the construction-time values, letting the caller re-listen to the
        # SAME audio biased toward repair candidates.
        opts_hotwords = hotwords if hotwords is not None else self._hotwords
        opts_prompt = initial_prompt if initial_prompt is not None else self._initial_prompt
        options: Dict[str, Any] = {
            "language": language,
            "beam_size": 5,
            "initial_prompt": opts_prompt,
            "hotwords": opts_hotwords,
            "vad_filter": self._vad_filter,
            "condition_on_previous_text": self._condition_on_previous_text,
        }
        if self._no_speech_threshold is not None:
            options["no_speech_threshold"] = self._no_speech_threshold
        if self._log_prob_threshold is not None:
            options["log_prob_threshold"] = self._log_prob_threshold
        # Ad-hoc overrides injected by the calibration harness (--sweep).
        extra = getattr(self, "_extra_transcribe_options", None)
        if extra:
            options.update(extra)
        return options

    def transcribe(self, audio_path: str, language: Optional[str] = None, *, hotwords: Optional[str] = None, initial_prompt: Optional[str] = None) -> str:
        model = self._get_model()
        segments, info = model.transcribe(
            audio_path, **self._transcribe_options(language, hotwords, initial_prompt)
        )
        return " ".join(seg.text.strip() for seg in segments)

    def transcribe_with_confidence(self, audio_path: str, language: Optional[str] = None, *, hotwords: Optional[str] = None, initial_prompt: Optional[str] = None) -> Dict[str, Any]:
        model = self._get_model()
        segments, info = model.transcribe(
            audio_path, **self._transcribe_options(language, hotwords, initial_prompt)
        )
        segment_list = list(segments)
        if not segment_list:
            return {"text": "", "confidence": 1.0, "language": info.language}
        text = " ".join(seg.text.strip() for seg in segment_list)
        avg_logprob = sum(seg.avg_logprob for seg in segment_list) / len(segment_list)
        confidence = min(max(avg_logprob + 1.0, 0.0), 1.0)  # Normalise from [-1,0] to [0,1]
        return {"text": text, "confidence": confidence, "language": info.language}

    def list_languages(self) -> List[str]:
        try:
            from faster_whisper.utils import available_languages
            return available_languages()
        except Exception:
            return [
                "en", "fr", "de", "es", "it", "pt", "nl", "ru", "ja", "ko", "zh",
                "ar", "hi", "vi", "tr", "pl", "sv", "da", "fi", "no", "cs", "ro",
            ]


# ---------------------------------------------------------------------------
# Whisper.cpp (CLI-based, GGUF models)
# ---------------------------------------------------------------------------

class WhisperCPPEngine(STTEngine):
    """Whisper.cpp STT — runs GGUF models on CPU via CLI.

    Install: https://github.com/ggerganov/whisper.cpp
    Models: ggml-tiny.bin through ggml-large-v3.bin
    """

    def __init__(self, model_path: Optional[str] = None) -> None:
        self._model_path = model_path or os.getenv(
            "WHISPER_CPP_MODEL",
            str(Path.home() / ".hermes" / "models" / "ggml-tiny.en.bin"),
        )

    def available(self) -> bool:
        return shutil.which("whisper-cpp") is not None or shutil.which("main") is not None

    def _find_executable(self) -> str:
        for name in ("whisper-cpp", "main"):
            p = shutil.which(name)
            if p:
                return p
        raise RuntimeError("whisper.cpp executable not found (whisper-cpp or main)")

    def transcribe(self, audio_path: str, language: Optional[str] = None) -> str:
        exe = self._find_executable()
        cmd = [exe, "-m", self._model_path, "-f", audio_path, "--no-timestamps"]
        if language:
            cmd += ["-l", language]
        result = subprocess.run(cmd, capture_output=True, timeout=60, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"whisper.cpp failed: {result.stderr.strip()}")
        # Output format: "[00:00:00.000 --> 00:00:03.120]  Transcribed text here"
        lines = [line.split("]  ", 1)[-1].strip() for line in result.stdout.strip().split("\n") if "]  " in line]
        return " ".join(lines)

    def transcribe_with_confidence(self, audio_path: str, language: Optional[str] = None) -> Dict[str, Any]:
        text = self.transcribe(audio_path, language)
        # whisper.cpp CLI doesn't output confidence per-segment by default
        return {"text": text, "confidence": 1.0}

    def list_languages(self) -> List[str]:
        return [
            "en", "fr", "de", "es", "it", "pt", "nl", "ru", "ja", "zh",
        ]


# ---------------------------------------------------------------------------
# Command STT (generic CLI wrapper)
# ---------------------------------------------------------------------------

class CommandSTTEngine(STTEngine):
    """Generic CLI-based STT engine.

    Config: stt.command = ["vosk-transcriber", "-i", "{audio}", "-o", "{output}.txt"]
    """

    def __init__(self, command: list[str]) -> None:
        self._command = command

    def available(self) -> bool:
        return shutil.which(self._command[0]) is not None if self._command else False

    def transcribe(self, audio_path: str, language: Optional[str] = None) -> str:
        output_txt = str(_STT_CACHE_DIR / f"stt_cmd_{hash(audio_path) & 0x7FFFFFFF}.txt")
        cmd = [
            part.replace("{audio}", audio_path).replace("{output}", output_txt).replace("{language}", language or "en")
            for part in self._command
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"STT command failed: {result.stderr.strip()}")
        if os.path.exists(output_txt):
            return Path(output_txt).read_text().strip()
        return result.stdout.strip()

    def transcribe_with_confidence(self, audio_path: str, language: Optional[str] = None) -> Dict[str, Any]:
        text = self.transcribe(audio_path, language)
        return {"text": text, "confidence": 1.0}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_stt_engine(engine_type: str = "faster-whisper", **kwargs: Any) -> STTEngine:
    """Create an STT engine instance by name.

    Args:
        engine_type: "faster-whisper", "whisper-cpp", or "command"
        **kwargs: Passed to engine constructor
    """
    if engine_type == "faster-whisper":
        return FasterWhisperEngine(
            model_size=kwargs.get("model_size", "tiny"),
            device=kwargs.get("device", "auto"),
            compute_type=kwargs.get("compute_type", "int8"),
            initial_prompt=kwargs.get("initial_prompt"),
            hotwords=kwargs.get("hotwords"),
            vad_filter=bool(kwargs.get("vad_filter", False)),
            condition_on_previous_text=bool(kwargs.get("condition_on_previous_text", False)),
            no_speech_threshold=kwargs.get("no_speech_threshold"),
            log_prob_threshold=kwargs.get("log_prob_threshold"),
        )
    elif engine_type == "whisper-cpp":
        return WhisperCPPEngine(model_path=kwargs.get("model_path"))
    elif engine_type == "command":
        return CommandSTTEngine(command=kwargs.get("command", ["echo", "{audio}"]))
    else:
        raise ValueError(f"Unknown STT engine '{engine_type}'. Valid: faster-whisper, whisper-cpp, command")
