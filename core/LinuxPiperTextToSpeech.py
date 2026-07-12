from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Dict, Optional, Sequence

from core.Contracts import TextToSpeechRequestV1, TextToSpeechResultV1
from core.LinuxAlsaSpeaker import (
    ALSA_SPEAKER_STATUS_DISABLED,
    LinuxAlsaSpeakerAdapter,
    SpeakerOutputAdapter,
)
from core.LinuxWhisperSpeechToText import analyze_wav_audio
from core.TextToSpeech import TextToSpeechAdapter


DEFAULT_PIPER_COMMAND = "piper"
DEFAULT_PIPER_VOICE_ID = "en_US-amy-low"
DEFAULT_PIPER_LANGUAGE = "en_US"
DEFAULT_PIPER_MODEL_PATH = "models/piper/en_US-amy-low.onnx"
DEFAULT_PIPER_MODEL_CONFIG_PATH = "models/piper/en_US-amy-low.onnx.json"
DEFAULT_PIPER_OUTPUT_DIR = "data/manual_tts_samples"
DEFAULT_PIPER_TIMEOUT_SECONDS = 120.0
MAX_TTS_TEXT_CHARS = 500

PIPER_STATUS_EXECUTABLE_MISSING = "piper_executable_missing"
PIPER_STATUS_MODEL_MISSING = "piper_model_missing"
PIPER_STATUS_CONFIG_MISSING = "piper_model_config_missing"
PIPER_STATUS_OUTPUT_UNWRITABLE = "tts_output_unwritable"
PIPER_STATUS_INVALID_VOICE = "invalid_voice"
PIPER_STATUS_EMPTY_TEXT = "empty_text"
PIPER_STATUS_TEXT_TOO_LONG = "text_too_long"
PIPER_STATUS_TIMEOUT = "tts_timeout"
PIPER_STATUS_FAILED = "tts_failed"
PIPER_STATUS_OUTPUT_MISSING = "tts_output_missing"
PIPER_STATUS_OUTPUT_EMPTY = "tts_output_empty"
PIPER_STATUS_INVALID_WAV = "tts_invalid_wav"
PIPER_STATUS_SYNTHESIZED = "synthesized"
PIPER_STATUS_PLAYBACK_FAILED = "playback_failed"


@dataclass(frozen=True)
class SafeTextProcessResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "args": list(self.args),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "error_message": self.error_message,
            "metadata": dict(self.metadata),
        }


class SafeTextSubprocessRunner:
    """Narrow text-input subprocess boundary. Shell execution is never used."""

    def which(self, executable: str) -> Optional[str]:
        return shutil.which(executable)

    def run(
        self,
        args: Sequence[str],
        timeout_seconds: float,
        input_text: str = "",
    ) -> SafeTextProcessResult:
        safe_args = [str(arg) for arg in args]
        try:
            completed = subprocess.run(
                safe_args,
                input=str(input_text or ""),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            return SafeTextProcessResult(
                args=safe_args,
                returncode=-1,
                stdout=str(error.stdout or ""),
                stderr=str(error.stderr or ""),
                timed_out=True,
                error_message="process_timeout",
            )
        except FileNotFoundError:
            return SafeTextProcessResult(
                args=safe_args,
                returncode=-1,
                error_message="process_not_found",
            )
        except OSError as error:
            return SafeTextProcessResult(
                args=safe_args,
                returncode=-1,
                error_message=f"process_os_error:{error.__class__.__name__}",
                metadata={"errno": getattr(error, "errno", None)},
            )
        return SafeTextProcessResult(
            args=safe_args,
            returncode=int(completed.returncode),
            stdout=str(completed.stdout or ""),
            stderr=str(completed.stderr or ""),
        )


class LinuxPiperTextToSpeechAdapter(TextToSpeechAdapter):
    """Offline Piper TTS adapter for Raspberry Pi/Linux.

    Piper generates a WAV file from explicit text. Optional playback is delegated
    to a speaker adapter; the Brain and CoreService never call Piper or ALSA.
    """

    def __init__(
        self,
        piper_command: str = DEFAULT_PIPER_COMMAND,
        model_path: str | Path = DEFAULT_PIPER_MODEL_PATH,
        model_config_path: str | Path = DEFAULT_PIPER_MODEL_CONFIG_PATH,
        voice_id: str = DEFAULT_PIPER_VOICE_ID,
        language: str = DEFAULT_PIPER_LANGUAGE,
        output_dir: str | Path = DEFAULT_PIPER_OUTPUT_DIR,
        timeout_seconds: float = DEFAULT_PIPER_TIMEOUT_SECONDS,
        max_text_chars: int = MAX_TTS_TEXT_CHARS,
        speaker_adapter: Optional[SpeakerOutputAdapter] = None,
        runner: Optional[SafeTextSubprocessRunner] = None,
        clock=time.perf_counter,
        source: str = "linux_piper_text_to_speech_adapter",
    ):
        self.piper_command = str(
            piper_command
            or os.environ.get("ARES_PIPER_COMMAND")
            or DEFAULT_PIPER_COMMAND
        ).strip()
        self.model_path = Path(
            model_path
            or os.environ.get("ARES_PIPER_MODEL_PATH")
            or DEFAULT_PIPER_MODEL_PATH
        ).expanduser()
        self.model_config_path = Path(
            model_config_path
            or os.environ.get("ARES_PIPER_MODEL_CONFIG_PATH")
            or DEFAULT_PIPER_MODEL_CONFIG_PATH
        ).expanduser()
        self.voice_id = str(voice_id or DEFAULT_PIPER_VOICE_ID).strip()
        self.language = str(language or DEFAULT_PIPER_LANGUAGE).strip()
        self.output_dir = Path(output_dir or DEFAULT_PIPER_OUTPUT_DIR).expanduser()
        self.timeout_seconds = _positive_timeout(timeout_seconds)
        self.max_text_chars = _bounded_text_limit(max_text_chars)
        self.speaker_adapter = speaker_adapter or LinuxAlsaSpeakerAdapter()
        self.runner = runner or SafeTextSubprocessRunner()
        self.clock = clock
        self.source = source
        self.started = False
        self.synthesis_count = 0
        self.speech_engine_accessed = False
        self.audio_hardware_accessed = False

    def start(self) -> TextToSpeechResultV1:
        health = self.health_check()
        if not health.success:
            return health
        self.started = True
        return self._result(
            True,
            "started",
            "Linux Piper TTS adapter is ready.",
            data={"health": health.to_dict()},
        )

    def stop(self) -> TextToSpeechResultV1:
        self.started = False
        return self._result(True, "stopped", "Linux Piper TTS adapter stopped.")

    def health_check(self) -> TextToSpeechResultV1:
        binary = self._find_piper_binary()
        if not binary:
            return self._failure(PIPER_STATUS_EXECUTABLE_MISSING, "piper_executable_missing")
        model = _validate_required_file(self.model_path, "piper_model")
        if not model["success"]:
            return self._failure(
                PIPER_STATUS_MODEL_MISSING,
                str(model["error_message"]),
                data={"model_path": str(self.model_path)},
            )
        config = _validate_required_file(self.model_config_path, "piper_model_config")
        if not config["success"]:
            return self._failure(
                PIPER_STATUS_CONFIG_MISSING,
                str(config["error_message"]),
                data={"model_config_path": str(self.model_config_path)},
            )
        writable = _ensure_writable_dir(self.output_dir)
        if not writable["success"]:
            return self._failure(
                PIPER_STATUS_OUTPUT_UNWRITABLE,
                str(writable["error_message"]),
                data={"output_dir": str(self.output_dir)},
            )
        speaker = self.speaker_adapter.health_check()
        if not speaker.success:
            return self._failure(
                "speaker_unavailable",
                speaker.error_message or speaker.status,
                data={"speaker": speaker.to_dict()},
            )
        return self._result(
            True,
            "healthy",
            "Linux Piper TTS health check passed.",
            data={
                "piper_binary_path": binary,
                "model_path": str(self.model_path),
                "model_config_path": str(self.model_config_path),
                "output_dir": str(self.output_dir),
                "speaker": speaker.to_dict(),
            },
        )

    def get_status(self) -> TextToSpeechResultV1:
        return self._result(
            True,
            "started" if self.started else "stopped",
            "Linux Piper TTS status discovered.",
            data={
                "started": self.started,
                "piper_available": bool(self._find_piper_binary()),
                "model_available": self.model_path.exists(),
                "model_config_available": self.model_config_path.exists(),
                "voice_id": self.voice_id,
                "language": self.language,
                "output_dir": str(self.output_dir),
                "synthesis_count": self.synthesis_count,
                "speaker": self.speaker_adapter.get_status().to_dict(),
            },
        )

    def get_capabilities(self) -> TextToSpeechResultV1:
        return self._result(
            True,
            "capabilities",
            "Linux Piper TTS capabilities discovered.",
            data={
                "supported_modes": ["offline_piper_wav_generation"],
                "engine": "piper",
                "voice_id": self.voice_id,
                "language": self.language,
                "max_text_chars": self.max_text_chars,
                "playback_default": "disabled",
                "speaker_adapter": type(self.speaker_adapter).__name__,
                "internet": "disabled",
                "wake_word": "disabled",
                "background_listening": "disabled",
            },
        )

    def execute(self, request: Any) -> TextToSpeechResultV1:
        payload = getattr(request, "payload", request)
        if isinstance(payload, TextToSpeechRequestV1):
            return self.synthesize(payload)
        if isinstance(payload, dict):
            allowed = set(TextToSpeechRequestV1().to_dict())
            clean_payload = {key: value for key, value in payload.items() if key in allowed}
            return self.synthesize(TextToSpeechRequestV1(**clean_payload))
        return self._failure("invalid_tts_request", "invalid_tts_request")

    def synthesize(self, request: TextToSpeechRequestV1) -> TextToSpeechResultV1:
        self.synthesis_count += 1
        start_time = self.clock()
        normalized = _normalize_text(request.text)
        if not normalized:
            return self._failure(PIPER_STATUS_EMPTY_TEXT, "empty_text", request=request)
        if len(normalized) > self.max_text_chars:
            return self._failure(
                PIPER_STATUS_TEXT_TOO_LONG,
                "text_too_long",
                normalized_text=normalized[:120],
                request=request,
                data={"max_text_chars": self.max_text_chars, "actual_chars": len(normalized)},
            )
        requested_voice = str(request.voice_id or self.voice_id).strip()
        if requested_voice != self.voice_id:
            return self._failure(
                PIPER_STATUS_INVALID_VOICE,
                "invalid_voice",
                normalized_text=normalized,
                request=request,
                data={"requested_voice": requested_voice, "available_voice": self.voice_id},
            )
        health = self.health_check()
        if not health.success:
            return self._failure(
                health.status,
                health.error_message or health.status,
                normalized_text=normalized,
                request=request,
                data={"health": health.to_dict()},
            )

        output_path = self._output_path(request.output_wav_path)
        output = _prepare_output_path(output_path)
        if not output["success"]:
            return self._failure(
                PIPER_STATUS_OUTPUT_UNWRITABLE,
                str(output["error_message"]),
                normalized_text=normalized,
                request=request,
                generated_audio_path=str(output_path),
                data={"output_path": str(output_path)},
            )
        command = self._piper_command(output_path, request.speaking_rate)
        result = self.runner.run(
            command,
            timeout_seconds=_positive_timeout(request.timeout_seconds or self.timeout_seconds),
            input_text=normalized,
        )
        self.speech_engine_accessed = True
        processing_time = _elapsed(self.clock, start_time)
        if result.timed_out:
            return self._failure(
                PIPER_STATUS_TIMEOUT,
                "piper_timeout",
                normalized_text=normalized,
                request=request,
                processing_time_seconds=processing_time,
                generated_audio_path=str(output_path),
                data={"process": _safe_process_data(result)},
            )
        if result.returncode != 0:
            return self._failure(
                PIPER_STATUS_FAILED,
                f"piper_exit_{result.returncode}",
                normalized_text=normalized,
                request=request,
                processing_time_seconds=processing_time,
                generated_audio_path=str(output_path),
                data={"process": _safe_process_data(result)},
            )

        wav = _validate_generated_wav(output_path)
        if not wav["success"]:
            return self._failure(
                str(wav["status"]),
                str(wav["error_message"]),
                normalized_text=normalized,
                request=request,
                processing_time_seconds=processing_time,
                generated_audio_path=str(output_path),
                data={"process": _safe_process_data(result), "wav": wav},
            )

        playback_status = ALSA_SPEAKER_STATUS_DISABLED
        playback_data: Dict[str, Any] = {"enabled": False}
        success = True
        status = PIPER_STATUS_SYNTHESIZED
        error_message = ""
        if request.playback_enabled:
            playback = self.speaker_adapter.play_wav(output_path)
            self.audio_hardware_accessed = bool(
                self.audio_hardware_accessed
                or getattr(self.speaker_adapter, "audio_hardware_accessed", False)
            )
            playback_status = playback.status
            playback_data = playback.to_dict()
            if not playback.success:
                success = False
                status = PIPER_STATUS_PLAYBACK_FAILED
                error_message = playback.error_message or playback.status

        return TextToSpeechResultV1(
            success=success,
            status=status,
            normalized_text=normalized,
            engine="piper",
            voice_id=self.voice_id,
            generated_audio_path=str(output_path),
            duration_seconds=float(wav.get("duration_seconds", 0.0)),
            processing_time_seconds=processing_time,
            playback_status=playback_status,
            error_message=error_message,
            data={
                **self._base_data(),
                "request": request.to_dict(),
                "process": _safe_process_data(result),
                "wav": wav,
                "playback": playback_data,
                "text_output_fallback": "" if success else normalized,
            },
            metadata=self._metadata(),
        )

    def _find_piper_binary(self) -> str:
        found = self.runner.which(self.piper_command)
        if found:
            return str(found)
        path = Path(self.piper_command).expanduser()
        try:
            executable = os.access(path, os.R_OK)
            if os.name == "posix":
                executable = executable and os.access(path, os.X_OK)
            if (
                path.exists()
                and path.is_file()
                and path.stat().st_size > 0
                and executable
            ):
                return str(path)
        except OSError:
            return ""
        return ""

    def _piper_command(self, output_path: Path, speaking_rate: float) -> list[str]:
        command = [
            self._find_piper_binary(),
            "--model",
            str(self.model_path),
            "--config",
            str(self.model_config_path),
            "--output_file",
            str(output_path),
        ]
        rate = _bounded_rate(speaking_rate)
        if rate != 1.0:
            command.extend(["--length_scale", str(round(1.0 / rate, 4))])
        return command

    def _output_path(self, requested: str) -> Path:
        if str(requested or "").strip():
            return Path(str(requested)).expanduser()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return self.output_dir / f"ares_tts_{timestamp}.wav"

    def _result(
        self,
        success: bool,
        status: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> TextToSpeechResultV1:
        return TextToSpeechResultV1(
            success=success,
            status=status,
            engine="piper",
            voice_id=self.voice_id,
            playback_status="disabled",
            error_message="" if success else status,
            data={**self._base_data(), "message": message, **dict(data or {})},
            metadata=self._metadata(),
        )

    def _failure(
        self,
        status: str,
        error_message: str,
        normalized_text: str = "",
        request: Optional[TextToSpeechRequestV1] = None,
        processing_time_seconds: float = 0.0,
        generated_audio_path: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> TextToSpeechResultV1:
        return TextToSpeechResultV1(
            success=False,
            status=status,
            normalized_text=normalized_text,
            engine="piper",
            voice_id=self.voice_id,
            generated_audio_path=generated_audio_path,
            processing_time_seconds=processing_time_seconds,
            playback_status="not_attempted",
            error_message=error_message,
            data={
                **self._base_data(),
                "request": request.to_dict() if request else None,
                "text_output_fallback": normalized_text,
                **dict(data or {}),
            },
            metadata=self._metadata(),
        )

    def _base_data(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "tts": "offline_piper",
            "speech_engine": "piper",
            "speech_engine_access": "offline_local_process",
            "model_path": str(self.model_path),
            "model_config_path": str(self.model_config_path),
            "language": self.language,
            "internet": "disabled",
            "wake_word": "disabled",
            "background_listening": "disabled",
        }

    def _metadata(self) -> Dict[str, Any]:
        return {
            "safe": True,
            "source": self.source,
            "offline": True,
            "speech_engine_accessed": self.speech_engine_accessed,
            "audio_hardware_accessed": self.audio_hardware_accessed,
            "subprocess_shell": False,
        }


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _bounded_text_limit(value: int) -> int:
    parsed = int(value)
    if parsed <= 0 or parsed > 10_000:
        raise ValueError("max_text_chars must be > 0 and <= 10000")
    return parsed


def _bounded_rate(value: float) -> float:
    parsed = float(value or 1.0)
    if parsed <= 0.25 or parsed > 4.0:
        raise ValueError("speaking_rate must be > 0.25 and <= 4.0")
    return parsed


def _positive_timeout(value: float) -> float:
    parsed = float(value)
    if parsed <= 0 or parsed > 900:
        raise ValueError("timeout_seconds must be > 0 and <= 900")
    return parsed


def _elapsed(clock, start_time: float) -> float:
    return round(max(0.0, float(clock()) - float(start_time)), 6)


def _ensure_writable_dir(path: Path) -> Dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        marker = path / ".ares_tts_write_test"
        marker.write_text("ok", encoding="utf-8")
        marker.unlink()
    except OSError as error:
        return {
            "success": False,
            "error_message": f"output_unwritable:{error.__class__.__name__}",
        }
    return {"success": True}


def _validate_required_file(path: Path, label: str) -> Dict[str, Any]:
    try:
        if not path.exists() or not path.is_file():
            return {"success": False, "error_message": f"{label}_missing"}
        if path.stat().st_size <= 0:
            return {"success": False, "error_message": f"{label}_empty"}
        if not os.access(path, os.R_OK):
            return {"success": False, "error_message": f"{label}_unreadable"}
    except OSError as error:
        return {
            "success": False,
            "error_message": f"{label}_unreadable:{error.__class__.__name__}",
        }
    return {"success": True}


def _prepare_output_path(path: Path) -> Dict[str, Any]:
    try:
        if path.exists() and path.is_dir():
            return {"success": False, "error_message": "output_path_is_directory"}
    except OSError as error:
        return {
            "success": False,
            "error_message": f"output_path_unreadable:{error.__class__.__name__}",
        }
    writable = _ensure_writable_dir(path.parent)
    if not writable["success"]:
        return writable
    return {"success": True}


def _validate_generated_wav(path: Path) -> Dict[str, Any]:
    try:
        if not path.exists() or not path.is_file():
            return {
                "success": False,
                "status": PIPER_STATUS_OUTPUT_MISSING,
                "error_message": "tts_output_missing",
                "path": str(path),
            }
        if path.stat().st_size <= 0:
            return {
                "success": False,
                "status": PIPER_STATUS_OUTPUT_EMPTY,
                "error_message": "tts_output_empty",
                "path": str(path),
            }
    except OSError as error:
        return {
            "success": False,
            "status": PIPER_STATUS_INVALID_WAV,
            "error_message": f"tts_output_unreadable:{error.__class__.__name__}",
            "path": str(path),
        }
    wav = analyze_wav_audio(path)
    if not wav.get("success"):
        return {
            **wav,
            "status": PIPER_STATUS_INVALID_WAV,
            "error_message": wav.get("error_message", "invalid_wav"),
        }
    if int(wav.get("byte_count", 0)) <= 0:
        return {
            **wav,
            "success": False,
            "status": PIPER_STATUS_OUTPUT_EMPTY,
            "error_message": "tts_output_empty",
        }
    return {**wav, "status": PIPER_STATUS_SYNTHESIZED}


def _safe_process_data(result: SafeTextProcessResult) -> Dict[str, Any]:
    return {
        "args": list(result.args),
        "command": " ".join(str(arg) for arg in result.args),
        "returncode": result.returncode,
        "stdout": str(result.stdout or ""),
        "stderr": str(result.stderr or ""),
        "stdout_preview": str(result.stdout or "")[:4000],
        "stderr_preview": str(result.stderr or "")[:4000],
        "timed_out": result.timed_out,
        "error_message": result.error_message,
    }
