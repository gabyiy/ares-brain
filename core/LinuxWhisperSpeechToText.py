from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence
import wave

from core.LinuxAlsaMicrophone import SafeProcessResult, SafeSubprocessRunner
from core.Microphone import AudioChunk
from core.SpeechToText import SpeechToTextAdapter, TranscriptionResult


DEFAULT_WHISPER_COMMAND = "whisper-cli"
DEFAULT_WHISPER_MODEL_PATH = "models/whisper/ggml-tiny.en.bin"
DEFAULT_WHISPER_LANGUAGE = "auto"
DEFAULT_WHISPER_TIMEOUT_SECONDS = 120.0
MAX_WHISPER_TIMEOUT_SECONDS = 900.0

WHISPER_STATUS_BINARY_MISSING = "whisper_binary_missing"
WHISPER_STATUS_MODEL_MISSING = "whisper_model_missing"
WHISPER_STATUS_INVALID_AUDIO = "invalid_audio"
WHISPER_STATUS_TRANSCRIPTION_TIMEOUT = "transcription_timeout"
WHISPER_STATUS_TRANSCRIPTION_FAILED = "transcription_failed"
WHISPER_STATUS_NO_TRANSCRIPTION = "no_transcription"
WHISPER_STATUS_TRANSCRIBED = "transcribed"
WHISPER_STATUS_AUDIO_SILENT = "audio_silent"
WHISPER_STATUS_AUDIO_BELOW_THRESHOLD = "audio_below_threshold"
WHISPER_STATUS_NO_USABLE_SPEECH = "no_usable_speech"
NO_SPEECH_MARKERS = frozenset(
    {
        "blankaudio",
        "nospeech",
        "silence",
    }
)

Clock = Callable[[], float]


@dataclass(frozen=True)
class WhisperTranscriptionMetadata:
    processing_time_seconds: float
    language: str = ""
    model_path: str = ""
    whisper_command: str = ""
    audio_path: str = ""
    engine: str = "whisper.cpp"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "processing_time_seconds": round(max(0.0, self.processing_time_seconds), 6),
            "language": self.language,
            "model_path": self.model_path,
            "whisper_command": self.whisper_command,
            "audio_path": self.audio_path,
            "engine": self.engine,
            "metadata": dict(self.metadata),
        }


class LinuxWhisperSpeechToTextAdapter(SpeechToTextAdapter):
    """Offline Whisper STT adapter for Linux/Raspberry Pi.

    The adapter runs a local Whisper executable against local WAV files. It does
    not call internet services, start wake-word detection, run TTS, or create
    conversation loops.
    """

    def __init__(
        self,
        model_path: Optional[str | Path] = None,
        whisper_command: str = DEFAULT_WHISPER_COMMAND,
        language: str = DEFAULT_WHISPER_LANGUAGE,
        timeout_seconds: float = DEFAULT_WHISPER_TIMEOUT_SECONDS,
        minimum_rms: float = 0.0,
        runner: Optional[SafeSubprocessRunner] = None,
        clock: Clock = time.perf_counter,
        source: str = "linux_whisper_speech_to_text_adapter",
    ):
        self.model_path = Path(
            model_path
            or os.environ.get("ARES_WHISPER_MODEL_PATH")
            or DEFAULT_WHISPER_MODEL_PATH
        ).expanduser()
        self.whisper_command = str(
            whisper_command
            or os.environ.get("ARES_WHISPER_COMMAND")
            or DEFAULT_WHISPER_COMMAND
        ).strip()
        self.language = str(language or DEFAULT_WHISPER_LANGUAGE).strip()
        self.timeout_seconds = _bounded_timeout(timeout_seconds)
        self.minimum_rms = _non_negative_float(minimum_rms, "minimum_rms")
        self.runner = runner or SafeSubprocessRunner()
        self.clock = clock
        self.source = source
        self.transcription_count = 0
        self.speech_engine_accessed = False
        self.audio_hardware_accessed = False

    def transcribe(self, audio_chunk: AudioChunk) -> TranscriptionResult:
        if audio_chunk.byte_count == 0:
            self.transcription_count += 1
            return self._success(
                status="empty_audio",
                text="",
                confidence=0.0,
                audio_path="",
                processing_time_seconds=0.0,
                extra_data={
                    "audio_chunk": audio_chunk.to_dict(),
                    "message": "Offline Whisper received empty audio.",
                },
            )

        wav_path = _audio_chunk_wav_path(audio_chunk)
        if wav_path:
            return self.transcribe_wav(wav_path, audio_chunk=audio_chunk)

        with tempfile.TemporaryDirectory(prefix="ares_whisper_audio_") as temp_dir:
            temp_wav = Path(temp_dir) / "audio_chunk.wav"
            try:
                _write_audio_chunk_wav(audio_chunk, temp_wav)
            except (OSError, wave.Error, ValueError) as error:
                return self._failure(
                    status=WHISPER_STATUS_INVALID_AUDIO,
                    error_message=f"invalid_audio:{error.__class__.__name__}",
                    audio_path=str(temp_wav),
                    processing_time_seconds=0.0,
                    extra_data={"audio_chunk": audio_chunk.to_dict()},
                )
            return self.transcribe_wav(temp_wav, audio_chunk=audio_chunk)

    def transcribe_wav(
        self,
        wav_path: str | Path,
        audio_chunk: Optional[AudioChunk] = None,
        language: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> TranscriptionResult:
        self.transcription_count += 1
        start_time = self.clock()
        audio_path = Path(wav_path).expanduser()
        binary_path = self._find_whisper_binary()
        if not binary_path:
            return self._failure(
                status=WHISPER_STATUS_BINARY_MISSING,
                error_message="whisper_binary_missing",
                audio_path=str(audio_path),
                processing_time_seconds=_elapsed(self.clock, start_time),
            )
        if not self.model_path.exists():
            return self._failure(
                status=WHISPER_STATUS_MODEL_MISSING,
                error_message="whisper_model_missing",
                audio_path=str(audio_path),
                processing_time_seconds=_elapsed(self.clock, start_time),
            )

        audio_validation = _validate_wav_audio(audio_path)
        if not audio_validation["success"]:
            return self._failure(
                status=WHISPER_STATUS_INVALID_AUDIO,
                error_message=str(audio_validation["error_message"]),
                audio_path=str(audio_path),
                processing_time_seconds=_elapsed(self.clock, start_time),
                extra_data={"audio_validation": audio_validation},
            )
        if int(audio_validation.get("peak_amplitude", 0)) <= 0:
            return self._failure(
                status=WHISPER_STATUS_AUDIO_SILENT,
                error_message="audio_silent",
                audio_path=str(audio_path),
                processing_time_seconds=_elapsed(self.clock, start_time),
                extra_data={"audio_validation": audio_validation},
            )
        if self.minimum_rms > 0 and float(audio_validation.get("rms_amplitude", 0.0)) < self.minimum_rms:
            return self._failure(
                status=WHISPER_STATUS_AUDIO_BELOW_THRESHOLD,
                error_message="audio_below_threshold",
                audio_path=str(audio_path),
                processing_time_seconds=_elapsed(self.clock, start_time),
                extra_data={
                    "audio_validation": audio_validation,
                    "minimum_rms": self.minimum_rms,
                },
            )

        timeout = _bounded_timeout(
            timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        )
        requested_language = str(language or self.language or DEFAULT_WHISPER_LANGUAGE).strip()
        effective_language = _resolve_whisper_language(requested_language, self.model_path)
        model_english_only = _is_english_only_whisper_model(self.model_path)
        with tempfile.TemporaryDirectory(prefix="ares_whisper_output_") as temp_dir:
            output_base = Path(temp_dir) / "transcript"
            command = self._transcribe_command(
                binary_path=binary_path,
                audio_path=audio_path,
                output_base=output_base,
                language=effective_language,
            )
            result = self.runner.run(command, timeout_seconds=timeout)
            self.speech_engine_accessed = True
            elapsed = _elapsed(self.clock, start_time)

            if result.timed_out:
                return self._failure(
                    status=WHISPER_STATUS_TRANSCRIPTION_TIMEOUT,
                    error_message="whisper_transcription_timeout",
                    audio_path=str(audio_path),
                    processing_time_seconds=elapsed,
                    extra_data={"process": _safe_process_data(result)},
                )
            if result.returncode != 0:
                return self._failure(
                    status=WHISPER_STATUS_TRANSCRIPTION_FAILED,
                    error_message=f"whisper_exit_{result.returncode}",
                    audio_path=str(audio_path),
                    processing_time_seconds=elapsed,
                    extra_data={"process": _safe_process_data(result)},
                )

            transcript = _normalize_transcript_text(_read_transcript_text(output_base, result))
            detected_language = (
                _detect_language(result.stdout, result.stderr)
                if effective_language == "auto"
                else effective_language
            )
            if not transcript:
                return self._failure(
                    status=WHISPER_STATUS_NO_USABLE_SPEECH,
                    error_message="no_usable_speech",
                    audio_path=str(audio_path),
                    processing_time_seconds=elapsed,
                    extra_data={
                        "audio_chunk": audio_chunk.to_dict() if audio_chunk else None,
                        "audio_validation": audio_validation,
                        "process": _safe_process_data(result),
                        "language_requested": requested_language,
                        "language_effective": effective_language,
                        "language": detected_language,
                        "model_english_only": model_english_only,
                    },
                )

            return self._success(
                status=WHISPER_STATUS_TRANSCRIBED,
                text=transcript,
                confidence=1.0,
                audio_path=str(audio_path),
                processing_time_seconds=elapsed,
                extra_data={
                    "audio_chunk": audio_chunk.to_dict() if audio_chunk else None,
                    "audio_validation": audio_validation,
                    "process": _safe_process_data(result),
                    "language_requested": requested_language,
                    "language_effective": effective_language,
                    "language": detected_language,
                    "model_english_only": model_english_only,
                },
            )

    def get_status(self) -> TranscriptionResult:
        binary_path = self._find_whisper_binary()
        model_exists = self.model_path.exists()
        status = "ready" if binary_path and model_exists else "unavailable"
        return TranscriptionResult(
            success=True,
            status=status,
            text="",
            confidence=1.0 if status == "ready" else 0.0,
            data={
                **self._base_data(),
                "whisper_binary_available": bool(binary_path),
                "whisper_binary_path": binary_path or "",
                "model_available": model_exists,
                "model_path": str(self.model_path),
                "language": self.language,
                "language_effective": _resolve_whisper_language(self.language, self.model_path),
                "timeout_seconds": self.timeout_seconds,
                "minimum_rms": self.minimum_rms,
            },
            metadata=self._metadata(),
        )

    def get_capabilities(self) -> TranscriptionResult:
        return TranscriptionResult(
            success=True,
            status="capabilities",
            text="",
            confidence=1.0,
            data={
                **self._base_data(),
                "supported_input": "WAV file or AudioChunk",
                "supported_modes": ["offline_whisper_wav_transcription"],
                "recommended_model": "ggml-tiny.en.bin",
                "confidence": "not_reported_by_whisper_cli",
                "language": "auto_or_configured",
                "language_resolution": "English-only GGML models resolve auto to en.",
                "minimum_rms": self.minimum_rms,
                "internet": "disabled",
                "wake_word": "disabled",
                "background_listening": "disabled",
                "tts": "disabled",
            },
            metadata=self._metadata(),
        )

    def health_check(self) -> TranscriptionResult:
        binary_path = self._find_whisper_binary()
        if not binary_path:
            return self._failure(
                status=WHISPER_STATUS_BINARY_MISSING,
                error_message="whisper_binary_missing",
                audio_path="",
                processing_time_seconds=0.0,
            )
        if not self.model_path.exists():
            return self._failure(
                status=WHISPER_STATUS_MODEL_MISSING,
                error_message="whisper_model_missing",
                audio_path="",
                processing_time_seconds=0.0,
                extra_data={"whisper_binary_path": binary_path},
            )
        return TranscriptionResult(
            success=True,
            status="healthy",
            text="",
            confidence=1.0,
            data={
                **self._base_data(),
                "whisper_binary_available": True,
                "whisper_binary_path": binary_path,
                "model_available": True,
                "model_path": str(self.model_path),
                "language": self.language,
                "language_effective": _resolve_whisper_language(self.language, self.model_path),
                "minimum_rms": self.minimum_rms,
            },
            metadata=self._metadata(),
        )

    def _find_whisper_binary(self) -> str:
        found = self.runner.which(self.whisper_command)
        return str(found or "")

    def _transcribe_command(
        self,
        binary_path: str,
        audio_path: Path,
        output_base: Path,
        language: str,
    ) -> List[str]:
        command = [
            binary_path,
            "-m",
            str(self.model_path),
            "-f",
            str(audio_path),
            "-otxt",
            "-of",
            str(output_base),
        ]
        if language:
            command.extend(["-l", language])
        return command

    def _success(
        self,
        status: str,
        text: str,
        confidence: float,
        audio_path: str,
        processing_time_seconds: float,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> TranscriptionResult:
        language = str(dict(extra_data or {}).get("language") or "")
        timing = WhisperTranscriptionMetadata(
            processing_time_seconds=processing_time_seconds,
            language=language,
            model_path=str(self.model_path),
            whisper_command=self.whisper_command,
            audio_path=audio_path,
        )
        return TranscriptionResult(
            success=True,
            status=status,
            text=text,
            confidence=confidence,
            data={
                **self._base_data(),
                **timing.to_dict(),
                **dict(extra_data or {}),
            },
            metadata=self._metadata(),
        )

    def _failure(
        self,
        status: str,
        error_message: str,
        audio_path: str,
        processing_time_seconds: float,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> TranscriptionResult:
        timing = WhisperTranscriptionMetadata(
            processing_time_seconds=processing_time_seconds,
            model_path=str(self.model_path),
            whisper_command=self.whisper_command,
            audio_path=audio_path,
        )
        return TranscriptionResult(
            success=False,
            status=status,
            text="",
            confidence=0.0,
            error_message=error_message,
            data={
                **self._base_data(),
                **timing.to_dict(),
                **dict(extra_data or {}),
            },
            metadata=self._metadata(),
        )

    def _base_data(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "stt": "offline_whisper",
            "speech_engine": "whisper.cpp",
            "speech_engine_access": "offline_local_process",
            "internet": "disabled",
            "wake_word": "disabled",
            "background_listening": "disabled",
            "tts": "disabled",
        }

    def _metadata(self) -> Dict[str, Any]:
        return {
            "safe": True,
            "source": self.source,
            "mock": False,
            "offline": True,
            "speech_engine_accessed": self.speech_engine_accessed,
            "audio_hardware_accessed": self.audio_hardware_accessed,
            "subprocess_shell": False,
        }


def _audio_chunk_wav_path(audio_chunk: AudioChunk) -> Optional[Path]:
    wav_path = dict(audio_chunk.metadata or {}).get("wav_path")
    if not wav_path:
        return None
    return Path(str(wav_path)).expanduser()


def _write_audio_chunk_wav(audio_chunk: AudioChunk, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(audio_chunk.channels)
        wav_file.setsampwidth(audio_chunk.sample_width_bytes)
        wav_file.setframerate(audio_chunk.sample_rate_hz)
        wav_file.writeframes(audio_chunk.data)


def _validate_wav_audio(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "success": False,
            "error_message": "audio_file_missing",
            "path": str(path),
        }
    size = path.stat().st_size
    if size == 0:
        return {
            "success": False,
            "error_message": "audio_file_empty",
            "path": str(path),
            "byte_count": size,
        }
    try:
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            frame_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_data = wav_file.readframes(frames)
    except (wave.Error, EOFError, OSError) as error:
        return {
            "success": False,
            "error_message": f"invalid_wav:{error.__class__.__name__}",
            "path": str(path),
        }
    if frames <= 0:
        return {
            "success": False,
            "error_message": "audio_has_no_frames",
            "path": str(path),
        }
    try:
        signal = _pcm_signal_stats(frame_data, sample_width)
    except ValueError as error:
        return {
            "success": False,
            "error_message": str(error),
            "path": str(path),
            "byte_count": size,
        }
    return {
        "success": True,
        "path": str(path),
        "byte_count": size,
        "frames": frames,
        "sample_rate_hz": frame_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "duration_seconds": frames / frame_rate if frame_rate else 0.0,
        **signal,
    }


def analyze_wav_audio(path: str | Path) -> Dict[str, Any]:
    return _validate_wav_audio(Path(path).expanduser())


def _read_transcript_text(output_base: Path, process_result: SafeProcessResult) -> str:
    text_path = output_base.with_suffix(".txt")
    if text_path.exists():
        try:
            return text_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""
    return _extract_transcript_from_stdout(process_result.stdout)


def _extract_transcript_from_stdout(stdout: str) -> str:
    lines: List[str] = []
    for raw_line in str(stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith(
            (
                "whisper_",
                "system_info",
                "main:",
                "ggml_",
                "whisper_print",
                "detected language",
            )
        ):
            continue
        line = re.sub(r"^\[[^\]]+\]\s*", "", line).strip()
        if line:
            lines.append(line)
    return " ".join(lines).strip()


def _normalize_transcript_text(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    clean = re.sub(
        r"<\|\s*(?:blank[_\s-]*audio|no[_\s-]*speech|nospeech|silence)\s*\|>",
        " ",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"\[\s*(?:blank[_\s-]*audio|no[_\s-]*speech|nospeech|silence)\s*\]",
        " ",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"\(\s*(?:blank[_\s-]*audio|no[_\s-]*speech|nospeech|silence)\s*\)",
        " ",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"\s+", " ", clean).strip()
    if _is_no_speech_marker(clean):
        return ""
    return clean


def _detect_language(stdout: str, stderr: str) -> str:
    combined = f"{stdout}\n{stderr}"
    match = re.search(r"detected language:\s*([A-Za-z_-]+)", combined, flags=re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _resolve_whisper_language(language: str, model_path: Path) -> str:
    requested = str(language or DEFAULT_WHISPER_LANGUAGE).strip() or DEFAULT_WHISPER_LANGUAGE
    if requested.lower() == "auto" and _is_english_only_whisper_model(model_path):
        return "en"
    return requested


def _is_english_only_whisper_model(model_path: Path) -> bool:
    name = Path(model_path).name.lower()
    stem = name[:-4] if name.endswith(".bin") else name
    return bool(re.search(r"\.en(?:[._-]|$)", stem))


def _is_no_speech_marker(text: str) -> bool:
    marker = re.sub(r"[\s_\-]+", "", str(text or "").strip().lower())
    marker = marker.strip("[]()<>|")
    return marker in NO_SPEECH_MARKERS


def _safe_process_data(result: SafeProcessResult) -> Dict[str, Any]:
    return {
        "args": list(result.args),
        "command": " ".join(str(arg) for arg in result.args),
        "returncode": result.returncode,
        "stdout_preview": _bounded_text(result.stdout, limit=4000),
        "stderr_preview": _bounded_text(result.stderr, limit=4000),
        "timed_out": result.timed_out,
        "error_message": result.error_message,
    }


def _bounded_text(text: str, limit: int = 500) -> str:
    return str(text or "")[:limit]


def _bounded_timeout(value: Any) -> float:
    timeout = float(value)
    if timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    if timeout > MAX_WHISPER_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be <= {MAX_WHISPER_TIMEOUT_SECONDS}")
    return timeout


def _non_negative_float(value: Any, name: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _pcm_signal_stats(frame_data: bytes, sample_width: int) -> Dict[str, Any]:
    samples = list(_iter_pcm_samples(frame_data, sample_width))
    if not samples:
        return {
            "sample_count": 0,
            "peak_amplitude": 0,
            "rms_amplitude": 0.0,
        }
    peak = max(abs(sample) for sample in samples)
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return {
        "sample_count": len(samples),
        "peak_amplitude": int(peak),
        "rms_amplitude": round(math.sqrt(mean_square), 6),
    }


def _iter_pcm_samples(frame_data: bytes, sample_width: int):
    if sample_width not in (1, 2, 3, 4):
        raise ValueError(f"unsupported_sample_width:{sample_width}")
    for offset in range(0, len(frame_data) - sample_width + 1, sample_width):
        raw = frame_data[offset : offset + sample_width]
        if sample_width == 1:
            yield int(raw[0]) - 128
            continue
        yield int.from_bytes(raw, byteorder="little", signed=True)


def _elapsed(clock: Clock, start_time: float) -> float:
    return max(0.0, float(clock()) - float(start_time))
