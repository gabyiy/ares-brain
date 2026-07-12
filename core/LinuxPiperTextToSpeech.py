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
from core.VoiceProfiles import (
    DEFAULT_VOICE_PROFILE_CONFIG_PATH,
    VoiceProfile,
    VoiceProfileError,
    VoiceProfileRegistry,
    load_voice_profile_registry,
)


DEFAULT_PIPER_COMMAND = "piper"
DEFAULT_PIPER_OUTPUT_DIR = "data/manual_tts_samples"
DEFAULT_PIPER_TIMEOUT_SECONDS = 120.0
MAX_TTS_TEXT_CHARS = 500

PIPER_STATUS_EXECUTABLE_MISSING = "piper_executable_missing"
PIPER_STATUS_MODEL_MISSING = "piper_model_missing"
PIPER_STATUS_CONFIG_MISSING = "piper_model_config_missing"
PIPER_STATUS_OUTPUT_UNWRITABLE = "tts_output_unwritable"
PIPER_STATUS_INVALID_VOICE = "invalid_voice"
PIPER_STATUS_PROFILE_CONFIG_INVALID = "voice_profile_config_invalid"
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
        voice_registry: Optional[VoiceProfileRegistry] = None,
        voice_profiles_config_path: str | Path = DEFAULT_VOICE_PROFILE_CONFIG_PATH,
        project_root: str | Path | None = None,
        allow_external_voice_paths: bool = False,
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
        self.project_root = Path(
            project_root or Path(__file__).resolve().parent.parent
        ).expanduser().resolve()
        self.voice_profiles_config_path = Path(voice_profiles_config_path).expanduser()
        self.voice_registry = voice_registry
        self.voice_registry_error: Optional[VoiceProfileError] = None
        if self.voice_registry is None:
            try:
                self.voice_registry = load_voice_profile_registry(
                    self.voice_profiles_config_path,
                    project_root=self.project_root,
                    allow_external_paths=allow_external_voice_paths,
                )
            except VoiceProfileError as error:
                self.voice_registry_error = error
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

    def health_check(self, voice_profile_id: str = "") -> TextToSpeechResultV1:
        try:
            profile = self._resolve_profile(voice_profile_id)
        except VoiceProfileError as error:
            return self._failure(
                PIPER_STATUS_PROFILE_CONFIG_INVALID
                if error.code not in {"unknown_profile", "profile_disabled"}
                else PIPER_STATUS_INVALID_VOICE,
                error.code,
                requested_voice_profile=str(voice_profile_id or ""),
                data={"voice_profile_error": error.message},
            )
        return self._health_check_profile(profile, str(voice_profile_id or ""))

    def _health_check_profile(
        self,
        profile: VoiceProfile,
        requested_voice_profile: str,
    ) -> TextToSpeechResultV1:
        binary = self._find_piper_binary()
        if not binary:
            return self._failure(
                PIPER_STATUS_EXECUTABLE_MISSING,
                "piper_executable_missing",
                profile=profile,
                requested_voice_profile=requested_voice_profile,
            )
        assert self.voice_registry is not None
        profile_health = self.voice_registry.validate_installed(profile)
        if not profile_health.success:
            status = (
                PIPER_STATUS_MODEL_MISSING
                if profile_health.status.startswith("model_")
                else PIPER_STATUS_CONFIG_MISSING
            )
            return self._failure(
                status,
                profile_health.status,
                profile=profile,
                requested_voice_profile=requested_voice_profile,
                data={"voice_profile_health": profile_health.to_dict()},
            )
        writable = _ensure_writable_dir(self.output_dir)
        if not writable["success"]:
            return self._failure(
                PIPER_STATUS_OUTPUT_UNWRITABLE,
                str(writable["error_message"]),
                profile=profile,
                requested_voice_profile=requested_voice_profile,
                data={"output_dir": str(self.output_dir)},
            )
        speaker = self.speaker_adapter.health_check()
        if not speaker.success:
            return self._failure(
                "speaker_unavailable",
                speaker.error_message or speaker.status,
                profile=profile,
                requested_voice_profile=requested_voice_profile,
                data={"speaker": speaker.to_dict()},
            )
        model_path = self.voice_registry.model_path(profile)
        config_path = self.voice_registry.config_path(profile)
        return self._result(
            True,
            "healthy",
            "Linux Piper TTS health check passed.",
            profile=profile,
            requested_voice_profile=requested_voice_profile,
            data={
                "piper_binary_path": binary,
                "model_path": str(model_path),
                "model_config_path": str(config_path),
                "output_dir": str(self.output_dir),
                "voice_profile_health": profile_health.to_dict(),
                "speaker": speaker.to_dict(),
            },
        )

    def get_status(self) -> TextToSpeechResultV1:
        profiles = self._profile_listing()
        return self._result(
            True,
            "started" if self.started else "stopped",
            "Linux Piper TTS status discovered.",
            data={
                "started": self.started,
                "piper_available": bool(self._find_piper_binary()),
                "voice_profiles": profiles,
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
                "voice_profiles": self._profile_listing(),
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
        requested_voice = str(request.voice_profile_id or request.voice_id or "").strip()
        try:
            profile = self._resolve_profile(requested_voice)
        except VoiceProfileError as error:
            return self._failure(
                PIPER_STATUS_INVALID_VOICE,
                error.code,
                normalized_text=normalized,
                request=request,
                requested_voice_profile=requested_voice,
                data={"voice_profile_error": error.message},
            )
        health = self._health_check_profile(profile, requested_voice)
        if not health.success:
            return self._failure(
                health.status,
                health.error_message or health.status,
                normalized_text=normalized,
                request=request,
                profile=profile,
                requested_voice_profile=requested_voice,
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
                profile=profile,
                requested_voice_profile=requested_voice,
                generated_audio_path=str(output_path),
                data={"output_path": str(output_path)},
            )
        command = self._piper_command(profile, output_path, request.speaking_rate)
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
                profile=profile,
                requested_voice_profile=requested_voice,
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
                profile=profile,
                requested_voice_profile=requested_voice,
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
                profile=profile,
                requested_voice_profile=requested_voice,
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

        profile_fields = self._profile_contract_fields(profile, requested_voice)
        return TextToSpeechResultV1(
            success=success,
            status=status,
            normalized_text=normalized,
            engine="piper",
            voice_id=profile.profile_id,
            **profile_fields,
            generated_audio_path=str(output_path),
            duration_seconds=float(wav.get("duration_seconds", 0.0)),
            processing_time_seconds=processing_time,
            playback_status=playback_status,
            error_message=error_message,
            data={
                **self._base_data(profile),
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

    def _piper_command(
        self,
        profile: VoiceProfile,
        output_path: Path,
        speaking_rate: float,
    ) -> list[str]:
        assert self.voice_registry is not None
        command = [
            self._find_piper_binary(),
            "--model",
            str(self.voice_registry.model_path(profile)),
            "--config",
            str(self.voice_registry.config_path(profile)),
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
        profile: Optional[VoiceProfile] = None,
        requested_voice_profile: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> TextToSpeechResultV1:
        profile_fields = self._profile_contract_fields(profile, requested_voice_profile)
        return TextToSpeechResultV1(
            success=success,
            status=status,
            engine="piper",
            voice_id=profile.profile_id if profile else "",
            **profile_fields,
            playback_status="disabled",
            error_message="" if success else status,
            data={**self._base_data(profile), "message": message, **dict(data or {})},
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
        profile: Optional[VoiceProfile] = None,
        requested_voice_profile: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> TextToSpeechResultV1:
        profile_fields = self._profile_contract_fields(profile, requested_voice_profile)
        return TextToSpeechResultV1(
            success=False,
            status=status,
            normalized_text=normalized_text,
            engine="piper",
            voice_id=profile.profile_id if profile else "",
            **profile_fields,
            generated_audio_path=generated_audio_path,
            processing_time_seconds=processing_time_seconds,
            playback_status="not_attempted",
            error_message=error_message,
            data={
                **self._base_data(profile),
                "request": request.to_dict() if request else None,
                "text_output_fallback": normalized_text,
                **dict(data or {}),
            },
            metadata=self._metadata(),
        )

    def _base_data(self, profile: Optional[VoiceProfile] = None) -> Dict[str, Any]:
        payload = {
            "source": self.source,
            "tts": "offline_piper",
            "speech_engine": "piper",
            "speech_engine_access": "offline_local_process",
            "voice_profiles_config": str(self.voice_profiles_config_path),
            "internet": "disabled",
            "wake_word": "disabled",
            "background_listening": "disabled",
        }
        if profile and self.voice_registry:
            payload["voice_profile"] = self.voice_registry.profile_metadata(profile)
        return payload

    def _resolve_profile(self, requested_voice_profile: str = "") -> VoiceProfile:
        if self.voice_registry_error is not None:
            raise self.voice_registry_error
        if self.voice_registry is None:
            raise VoiceProfileError("registry_unavailable", "Voice profile registry is unavailable")
        return self.voice_registry.resolve(requested_voice_profile)

    def _profile_listing(self) -> list[Dict[str, Any]]:
        if self.voice_registry_error is not None:
            return [{"status": self.voice_registry_error.code, "error": self.voice_registry_error.message}]
        if self.voice_registry is None:
            return []
        return [
            self.voice_registry.profile_metadata(profile)
            for profile in self.voice_registry.list_profiles()
        ]

    def _profile_contract_fields(
        self,
        profile: Optional[VoiceProfile],
        requested_voice_profile: str,
    ) -> Dict[str, Any]:
        if profile is None or self.voice_registry is None:
            return {"requested_voice_profile": str(requested_voice_profile or "")}
        return {
            "requested_voice_profile": str(requested_voice_profile or ""),
            "resolved_voice_profile": profile.profile_id,
            "voice_display_name": profile.display_name,
            "language": profile.language,
            "locale": profile.locale,
            "gender": profile.gender,
            "quality": profile.quality,
            "model_path": str(self.voice_registry.model_path(profile)),
            "config_path": str(self.voice_registry.config_path(profile)),
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
