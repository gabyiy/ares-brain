from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import select
import shutil
import subprocess
import tempfile
import time
import wave
from typing import Any, Dict, List, Optional, Sequence

from core.Contracts import VoiceActivityCaptureRequestV1, VoiceActivityCaptureResultV1
from core.Microphone import AudioChunk, CancelCheck, MicrophoneAdapter, MicrophoneResult
from core.VoiceActivityDetection import (
    RmsVoiceActivityCapture,
    VAD_STATUS_DEVICE_ERROR,
    validate_voice_activity_request,
)


DEFAULT_ALSA_SAMPLE_RATE_HZ = 16000
DEFAULT_ALSA_CHANNELS = 1
DEFAULT_ALSA_SAMPLE_FORMAT = "S16_LE"
DEFAULT_ALSA_RECORD_SECONDS = 3
DEFAULT_ALSA_TIMEOUT_PADDING_SECONDS = 5
MAX_ALSA_RECORD_SECONDS = 60
MAX_ALSA_TIMEOUT_SECONDS = 120

ALSA_STATUS_ARECORD_MISSING = "arecord_missing"
ALSA_STATUS_NO_CAPTURE_DEVICE = "no_capture_device"
ALSA_STATUS_INVALID_DEVICE = "invalid_device"
ALSA_STATUS_RECORDING_TIMEOUT = "recording_timeout"
ALSA_STATUS_RECORDING_FAILED = "recording_failed"
ALSA_STATUS_OUTPUT_MISSING = "wav_output_missing"
ALSA_STATUS_OUTPUT_EMPTY = "wav_output_empty"
ALSA_STATUS_INVALID_WAV = "invalid_wav_output"


@dataclass(frozen=True)
class SafeProcessResult:
    args: List[str]
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


class SafeSubprocessRunner:
    """Narrow subprocess boundary for arecord. Shell execution is never used."""

    def which(self, executable: str) -> Optional[str]:
        return shutil.which(executable)

    def run(self, args: Sequence[str], timeout_seconds: float) -> SafeProcessResult:
        safe_args = [str(arg) for arg in args]
        try:
            completed = subprocess.run(
                safe_args,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            return SafeProcessResult(
                args=safe_args,
                returncode=-1,
                stdout=str(error.stdout or ""),
                stderr=str(error.stderr or ""),
                timed_out=True,
                error_message="process_timeout",
            )
        except FileNotFoundError:
            return SafeProcessResult(
                args=safe_args,
                returncode=-1,
                error_message="process_not_found",
            )
        except OSError as error:
            return SafeProcessResult(
                args=safe_args,
                returncode=-1,
                error_message=f"process_os_error:{error.__class__.__name__}",
                metadata={"errno": getattr(error, "errno", None)},
            )

        return SafeProcessResult(
            args=safe_args,
            returncode=int(completed.returncode),
            stdout=str(completed.stdout or ""),
            stderr=str(completed.stderr or ""),
        )


class SubprocessPcmFrameSource:
    """Bounded raw-PCM reader for one foreground arecord process."""

    def __init__(self, args: Sequence[str]):
        self.args = [str(arg) for arg in args]
        self.process = subprocess.Popen(
            self.args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
        )
        self.closed = False
        self.stderr = ""

    def read_frame(self, frame_bytes: int, timeout_seconds: float) -> bytes:
        if self.closed or self.process.stdout is None:
            raise RuntimeError("pcm_stream_closed")
        deadline = time.monotonic() + max(0.01, float(timeout_seconds))
        frame = bytearray()
        while len(frame) < frame_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("pcm_frame_read_timeout")
            readable, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not readable:
                raise TimeoutError("pcm_frame_read_timeout")
            chunk = self.process.stdout.read(frame_bytes - len(frame))
            if not chunk:
                raise EOFError("arecord_pcm_stream_ended")
            frame.extend(chunk)
        return bytes(frame)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2.0)
        if self.process.stderr is not None:
            try:
                self.stderr = self.process.stderr.read(1000).decode("utf-8", errors="replace")
            except (AttributeError, OSError):
                self.stderr = ""
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class SafePcmStreamRunner:
    """Starts allowlisted arecord argument lists; never invokes a shell."""

    def start(self, args: Sequence[str]) -> SubprocessPcmFrameSource:
        return SubprocessPcmFrameSource(args)


class LinuxAlsaMicrophoneAdapter(MicrophoneAdapter):
    """Linux ALSA microphone adapter backed by arecord.

    This adapter is hardware-specific and remains outside the Brain. It performs
    one-shot capture only; it does not start STT, wake-word detection, background
    listeners, internet access, or speaker output.
    """

    def __init__(
        self,
        device: Optional[str] = None,
        record_seconds: int = DEFAULT_ALSA_RECORD_SECONDS,
        sample_rate_hz: int = DEFAULT_ALSA_SAMPLE_RATE_HZ,
        channels: int = DEFAULT_ALSA_CHANNELS,
        sample_format: str = DEFAULT_ALSA_SAMPLE_FORMAT,
        timeout_seconds: Optional[float] = None,
        arecord_command: str = "arecord",
        runner: Optional[SafeSubprocessRunner] = None,
        stream_runner: Optional[SafePcmStreamRunner] = None,
        voice_activity_capture: Optional[RmsVoiceActivityCapture] = None,
        source: str = "linux_alsa_microphone_adapter",
    ):
        self.device = _normalize_optional_device(device)
        self.record_seconds = _bounded_record_seconds(record_seconds)
        self.sample_rate_hz = _positive_int(sample_rate_hz, "sample_rate_hz")
        self.channels = _positive_int(channels, "channels")
        self.sample_format = str(sample_format or DEFAULT_ALSA_SAMPLE_FORMAT).strip()
        self.timeout_seconds = _bounded_timeout(
            timeout_seconds
            if timeout_seconds is not None
            else self.record_seconds + DEFAULT_ALSA_TIMEOUT_PADDING_SECONDS
        )
        self.arecord_command = str(arecord_command or "arecord").strip()
        self.runner = runner or SafeSubprocessRunner()
        self.stream_runner = stream_runner or SafePcmStreamRunner()
        self.voice_activity_capture = voice_activity_capture or RmsVoiceActivityCapture()
        self.source = source
        self.started = False
        self.start_count = 0
        self.stop_count = 0
        self.read_count = 0
        self.record_count = 0
        self.audio_hardware_accessed = False
        self._active_stream: Optional[Any] = None

    def start(self) -> MicrophoneResult:
        self.start_count += 1
        health = self.health_check()
        if not health.success:
            return health
        vad_start = self.voice_activity_capture.start()
        if not vad_start.success:
            return self._failure(
                status=VAD_STATUS_DEVICE_ERROR,
                text="Linux ALSA microphone VAD component failed to start.",
                error_message=vad_start.error_message or vad_start.status,
                data={"voice_activity_capture": vad_start.to_dict()},
            )
        self.started = True
        return self._success(
            status="started",
            text="Linux ALSA microphone adapter is ready for one-shot recording.",
            data={"health": health.to_dict()},
        )

    def stop(self) -> MicrophoneResult:
        self.stop_count += 1
        self.cancel_current()
        vad_stop = self.voice_activity_capture.stop()
        self.started = False
        return self._success(
            status="stopped",
            text="Linux ALSA microphone adapter stopped. No background capture is running.",
            data={"voice_activity_capture": vad_stop.to_dict()},
        )

    def cancel_current(self) -> None:
        stream = self._active_stream
        self._active_stream = None
        if stream is not None:
            try:
                stream.close()
            except (OSError, RuntimeError):
                pass

    def read_chunk(
        self,
        timeout_seconds: Optional[float] = None,
        cancel_requested: Optional[CancelCheck | Any] = None,
    ) -> MicrophoneResult:
        self.read_count += 1
        if _is_cancelled(cancel_requested):
            return self._failure(
                status="cancelled",
                text="Linux ALSA microphone read was cancelled before recording.",
                error_message="microphone_read_cancelled",
                data={"timeout_seconds": timeout_seconds},
            )
        if not self.started:
            return self._failure(
                status="not_started",
                text="Linux ALSA microphone must be started before reading audio.",
                error_message="microphone_not_started",
                data={"timeout_seconds": timeout_seconds},
            )

        with tempfile.TemporaryDirectory(prefix="ares_alsa_capture_") as temp_dir:
            wav_path = Path(temp_dir) / "capture.wav"
            return self.record_wav(
                wav_path,
                seconds=self.record_seconds,
                timeout_seconds=timeout_seconds,
                overwrite=True,
                temporary_output=True,
            )

    def get_status(self) -> MicrophoneResult:
        arecord_path = self._find_arecord()
        return self._success(
            status="started" if self.started else "stopped",
            text="Linux ALSA microphone adapter status discovered.",
            data={
                "source": self.source,
                "started": self.started,
                "arecord_available": bool(arecord_path),
                "arecord_path": arecord_path or "",
                "selected_device": self.device or "",
                "record_seconds": self.record_seconds,
                "voice_activity_capture_state": self.voice_activity_capture.state,
                "sample_rate_hz": self.sample_rate_hz,
                "channels": self.channels,
                "sample_format": self.sample_format,
                "timeout_seconds": self.timeout_seconds,
                "background_listening": "disabled",
                "stt": "not_configured",
            },
        )

    def get_capabilities(self) -> MicrophoneResult:
        return self._success(
            status="capabilities",
            text="Linux ALSA microphone adapter capabilities discovered.",
            data={
                "source": self.source,
                "supported_modes": ["arecord_wav_capture", "arecord_pcm_rms_auto_stop"],
                "supports_device_selection": True,
                "supports_capture_device_listing": True,
                "writes_wav_file": True,
                "sample_rate_hz": self.sample_rate_hz,
                "channels": self.channels,
                "sample_format": self.sample_format,
                "timeout_handling": "safe_timeout_result",
                "voice_activity_detection": "pcm_frame_rms_hysteresis",
                "automatic_end_of_speech": True,
                "background_listening": "disabled",
                "stt": "not_configured",
                "wake_word": "disabled",
                "internet": "disabled",
            },
        )

    def health_check(self) -> MicrophoneResult:
        devices = self.list_capture_devices()
        if not devices.success:
            return devices
        if self.device and _device_looks_like_hw(self.device):
            available = {device["alsa_device"] for device in devices.data.get("devices", [])}
            if self.device not in available:
                return self._failure(
                    status=ALSA_STATUS_INVALID_DEVICE,
                    text=f"Selected ALSA capture device is not listed: {self.device}",
                    error_message="alsa_device_not_found",
                    data={"selected_device": self.device, "available_devices": sorted(available)},
                )
        vad_health = self.voice_activity_capture.health_check()
        if not vad_health.success:
            return self._failure(
                status=VAD_STATUS_DEVICE_ERROR,
                text="Linux ALSA microphone VAD health check failed.",
                error_message=vad_health.error_message or vad_health.status,
                data={"voice_activity_capture": vad_health.to_dict()},
            )
        return self._success(
            status="healthy",
            text="Linux ALSA microphone health check passed.",
            data={
                "arecord_available": True,
                "device_count": len(devices.data.get("devices", [])),
                "selected_device": self.device or "",
                "devices": devices.data.get("devices", []),
                "voice_activity_capture": vad_health.to_dict(),
            },
        )

    def list_capture_devices(self) -> MicrophoneResult:
        arecord_path = self._find_arecord()
        if not arecord_path:
            return self._failure(
                status=ALSA_STATUS_ARECORD_MISSING,
                text="Linux ALSA microphone adapter could not find arecord.",
                error_message="arecord_missing",
            )

        result = self.runner.run([arecord_path, "-l"], timeout_seconds=min(self.timeout_seconds, 10.0))
        if result.timed_out:
            return self._failure(
                status="device_list_timeout",
                text="Timed out while listing ALSA capture devices.",
                error_message="arecord_device_list_timeout",
                data={"process": _safe_process_data(result)},
            )
        if result.returncode != 0:
            return self._failure(
                status="device_list_failed",
                text="arecord failed while listing capture devices.",
                error_message=f"arecord_exit_{result.returncode}",
                data={"process": _safe_process_data(result)},
            )

        devices = parse_arecord_capture_devices(result.stdout)
        if not devices:
            return self._failure(
                status=ALSA_STATUS_NO_CAPTURE_DEVICE,
                text="arecord is available, but no ALSA capture devices were found.",
                error_message="no_capture_device",
                data={"process": _safe_process_data(result), "devices": []},
            )

        return self._success(
            status="devices",
            text=f"Detected {len(devices)} ALSA capture device(s).",
            data={"devices": devices, "process": _safe_process_data(result)},
        )

    def record_wav(
        self,
        output_path: str | Path,
        seconds: Optional[int] = None,
        device: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        overwrite: bool = False,
        temporary_output: bool = False,
    ) -> MicrophoneResult:
        self.record_count += 1
        arecord_path = self._find_arecord()
        if not arecord_path:
            return self._failure(
                status=ALSA_STATUS_ARECORD_MISSING,
                text="Linux ALSA microphone adapter could not find arecord.",
                error_message="arecord_missing",
            )

        try:
            selected_device = _normalize_optional_device(device) if device is not None else self.device
            duration = _bounded_record_seconds(seconds or self.record_seconds)
            timeout = _bounded_timeout(
                timeout_seconds
                if timeout_seconds is not None
                else duration + DEFAULT_ALSA_TIMEOUT_PADDING_SECONDS
            )
            wav_path = Path(output_path).expanduser()
            _validate_output_path(wav_path, overwrite=overwrite)
            wav_path.parent.mkdir(parents=True, exist_ok=True)
        except ValueError as error:
            return self._failure(
                status=ALSA_STATUS_INVALID_DEVICE if "device" in str(error) else "invalid_request",
                text="Linux ALSA microphone request was rejected before recording.",
                error_message=str(error),
            )
        except OSError as error:
            return self._failure(
                status="output_path_error",
                text="Linux ALSA microphone could not prepare the WAV output path.",
                error_message=f"output_path_error:{error.__class__.__name__}",
            )

        command = self._record_command(
            arecord_path=arecord_path,
            wav_path=wav_path,
            seconds=duration,
            device=selected_device,
        )
        result = self.runner.run(command, timeout_seconds=timeout)
        self.audio_hardware_accessed = True

        if result.timed_out:
            return self._failure(
                status=ALSA_STATUS_RECORDING_TIMEOUT,
                text=f"arecord timed out after {timeout} seconds.",
                error_message="arecord_recording_timeout",
                data={"process": _safe_process_data(result), "wav_path": str(wav_path)},
            )
        if result.returncode != 0:
            status = (
                ALSA_STATUS_INVALID_DEVICE
                if _stderr_indicates_invalid_device(result.stderr)
                else ALSA_STATUS_RECORDING_FAILED
            )
            return self._failure(
                status=status,
                text="arecord failed while recording microphone audio.",
                error_message=f"arecord_exit_{result.returncode}",
                data={"process": _safe_process_data(result), "wav_path": str(wav_path)},
            )

        validation = _validate_wav_file(wav_path)
        if not validation["success"]:
            return self._failure(
                status=str(validation["status"]),
                text=str(validation["text"]),
                error_message=str(validation["error_message"]),
                data={
                    "process": _safe_process_data(result),
                    "wav_path": str(wav_path),
                    "validation": validation,
                },
            )

        chunk = _read_wav_audio_chunk(
            wav_path,
            source=self.source,
            metadata={
                "wav_path": str(wav_path),
                "temporary_output": temporary_output,
                "selected_device": selected_device or "",
                "arecord_command": " ".join(command[:1]),
            },
        )
        return self._success(
            status="recorded",
            text=f"Recorded {duration} second(s) of Linux ALSA microphone audio.",
            chunk=chunk,
            data={
                "wav_path": str(wav_path),
                "temporary_output": temporary_output,
                "duration_seconds": duration,
                "selected_device": selected_device or "",
                "process": _safe_process_data(result),
                "wav": validation,
                "chunk": chunk.to_dict(),
            },
        )

    def record_until_silence(
        self,
        output_path: str | Path,
        device: Optional[str] = None,
        calibration_enabled: bool = True,
        calibration_duration_seconds: float = 0.75,
        speech_start_rms: float = 200.0,
        speech_continue_rms: float = 160.0,
        silence_rms: float = 120.0,
        required_speech_frames: int = 3,
        required_continue_frames: int = 3,
        required_silence_frames: int = 5,
        silence_seconds: float = 0.9,
        speech_wait_timeout_seconds: float = 10.0,
        maximum_utterance_seconds: float = 15.0,
        pre_roll_seconds: float = 0.25,
        frame_duration_ms: int = 20,
        frame_read_timeout_seconds: float = 1.0,
        minimum_speech_start_rms: float = 200.0,
        maximum_speech_start_rms: float = 1200.0,
        minimum_speech_continue_rms: float = 140.0,
        maximum_speech_continue_rms: float = 900.0,
        minimum_silence_rms: float = 80.0,
        maximum_silence_rms: float = 600.0,
        frame_debug_enabled: bool = False,
        cancel_requested: Optional[CancelCheck | Any] = None,
        correlation_id: str = "",
        session_id: str = "",
    ) -> VoiceActivityCaptureResultV1:
        """Capture one foreground utterance and trim terminal silence."""

        self.record_count += 1
        started_at = time.monotonic()
        selected_device = self.device
        try:
            selected_device = (
                _normalize_optional_device(device) if device is not None else self.device
            )
            if not self.started:
                raise RuntimeError("microphone_not_started")
            if self.sample_format.upper() != DEFAULT_ALSA_SAMPLE_FORMAT:
                raise ValueError("voice_activity_capture_requires_s16_le")
            arecord_path = self._find_arecord()
            if not arecord_path:
                raise FileNotFoundError("arecord_missing")
            request = VoiceActivityCaptureRequestV1(
                output_wav_path=str(Path(output_path).expanduser()),
                microphone_device=selected_device or "",
                sample_rate_hz=self.sample_rate_hz,
                channels=self.channels,
                sample_width_bytes=2,
                frame_duration_ms=frame_duration_ms,
                calibration_enabled=calibration_enabled,
                calibration_duration_seconds=calibration_duration_seconds,
                speech_start_rms=speech_start_rms,
                speech_continue_rms=speech_continue_rms,
                silence_rms=silence_rms,
                required_speech_frames=required_speech_frames,
                required_continue_frames=required_continue_frames,
                required_silence_frames=required_silence_frames,
                silence_duration_seconds=silence_seconds,
                speech_wait_timeout_seconds=speech_wait_timeout_seconds,
                maximum_utterance_seconds=maximum_utterance_seconds,
                pre_roll_seconds=pre_roll_seconds,
                frame_read_timeout_seconds=frame_read_timeout_seconds,
                minimum_speech_start_rms=minimum_speech_start_rms,
                maximum_speech_start_rms=maximum_speech_start_rms,
                minimum_speech_continue_rms=minimum_speech_continue_rms,
                maximum_speech_continue_rms=maximum_speech_continue_rms,
                minimum_silence_rms=minimum_silence_rms,
                maximum_silence_rms=maximum_silence_rms,
                frame_debug_enabled=frame_debug_enabled,
                correlation_id=correlation_id,
                session_id=session_id,
                metadata={
                    "safe": True,
                    "source": self.source,
                    "hardware_specific": "linux_alsa",
                    "background_listening": False,
                },
            )
            validate_voice_activity_request(request)
            command = self._stream_command(
                arecord_path=arecord_path,
                device=selected_device,
            )
            stream = self.stream_runner.start(command)
            self._active_stream = stream
            self.audio_hardware_accessed = True
            result = self.voice_activity_capture.execute(
                request,
                stream,
                cancel_requested=cancel_requested,
            )
            stream.close()
            self._active_stream = None
            return replace(
                result,
                data={
                    **dict(result.data),
                    "process": {
                        "args": command,
                        "shell": False,
                        "stderr": _bounded_text(getattr(stream, "stderr", "")),
                        "returncode": getattr(getattr(stream, "process", None), "returncode", None),
                    },
                },
                metadata={
                    **dict(result.metadata),
                    "subprocess_shell": False,
                    "speech_engine_accessed": False,
                },
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
            return VoiceActivityCaptureResultV1(
                success=False,
                status=VAD_STATUS_DEVICE_ERROR,
                selected_device=selected_device or "",
                stop_reason=VAD_STATUS_DEVICE_ERROR,
                processing_time_seconds=round(time.monotonic() - started_at, 6),
                error_message=str(error)[:200],
                correlation_id=correlation_id,
                session_id=session_id,
                metadata={
                    "safe": True,
                    "source": self.source,
                    "subprocess_shell": False,
                    "raw_audio_persisted_in_metadata": False,
                },
            )
        finally:
            stream = self._active_stream
            self._active_stream = None
            if stream is not None:
                try:
                    stream.close()
                except (OSError, RuntimeError):
                    pass

    def _find_arecord(self) -> str:
        found = self.runner.which(self.arecord_command)
        return str(found or "")

    def _record_command(
        self,
        arecord_path: str,
        wav_path: Path,
        seconds: int,
        device: Optional[str],
    ) -> List[str]:
        command = [
            arecord_path,
            "-q",
            "-f",
            self.sample_format,
            "-c",
            str(self.channels),
            "-r",
            str(self.sample_rate_hz),
            "-d",
            str(seconds),
            "-t",
            "wav",
        ]
        if device:
            command.extend(["-D", device])
        command.append(str(wav_path))
        return command

    def _stream_command(
        self,
        arecord_path: str,
        device: Optional[str],
    ) -> List[str]:
        command = [
            arecord_path,
            "-q",
            "-f",
            self.sample_format,
            "-c",
            str(self.channels),
            "-r",
            str(self.sample_rate_hz),
            "-t",
            "raw",
        ]
        if device:
            command.extend(["-D", device])
        command.append("-")
        return command

    def _success(
        self,
        status: str,
        text: str,
        chunk: Optional[AudioChunk] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> MicrophoneResult:
        return MicrophoneResult(
            success=True,
            status=status,
            text=text,
            chunk=chunk,
            data={**self._base_data(), **dict(data or {})},
            metadata=self._metadata(),
        )

    def _failure(
        self,
        status: str,
        text: str,
        error_message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> MicrophoneResult:
        return MicrophoneResult(
            success=False,
            status=status,
            text=text,
            error_message=error_message,
            data={**self._base_data(), **dict(data or {})},
            metadata=self._metadata(),
        )

    def _base_data(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "started": self.started,
            "selected_device": self.device or "",
            "audio_hardware_access": "linux_alsa_arecord",
            "background_listening": "disabled",
            "stt": "not_configured",
        }

    def _metadata(self) -> Dict[str, Any]:
        return {
            "safe": True,
            "source": self.source,
            "mock": False,
            "hardware_specific": "linux_alsa",
            "subprocess_shell": False,
            "audio_hardware_accessed": self.audio_hardware_accessed,
            "speech_engine_accessed": False,
        }


def parse_arecord_capture_devices(output: str) -> List[Dict[str, Any]]:
    devices: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"^card\s+(?P<card_index>\d+):\s*"
        r"(?P<card_id>[^\[]+)\[(?P<card_name>[^\]]+)\],\s*"
        r"device\s+(?P<device_index>\d+):\s*"
        r"(?P<device_id>[^\[]+)\[(?P<device_name>[^\]]+)\]"
    )
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        match = pattern.match(line)
        if not match:
            continue
        card_index = int(match.group("card_index"))
        device_index = int(match.group("device_index"))
        devices.append(
            {
                "card_index": card_index,
                "card_id": match.group("card_id").strip(),
                "card_name": match.group("card_name").strip(),
                "device_index": device_index,
                "device_id": match.group("device_id").strip(),
                "device_name": match.group("device_name").strip(),
                "alsa_device": f"hw:{card_index},{device_index}",
                "raw_line": line,
            }
        )
    return devices


def _safe_process_data(result: SafeProcessResult) -> Dict[str, Any]:
    return {
        "args": list(result.args),
        "returncode": result.returncode,
        "stdout_preview": _bounded_text(result.stdout),
        "stderr_preview": _bounded_text(result.stderr),
        "timed_out": result.timed_out,
        "error_message": result.error_message,
    }


def _bounded_text(text: str, limit: int = 500) -> str:
    clean = str(text or "")
    return clean[:limit]


def _positive_int(value: Any, name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _bounded_record_seconds(value: Any) -> int:
    seconds = int(value)
    if seconds <= 0:
        raise ValueError("record_seconds must be positive")
    if seconds > MAX_ALSA_RECORD_SECONDS:
        raise ValueError(f"record_seconds must be <= {MAX_ALSA_RECORD_SECONDS}")
    return seconds


def _bounded_timeout(value: Any) -> float:
    timeout = float(value)
    if timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    if timeout > MAX_ALSA_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be <= {MAX_ALSA_TIMEOUT_SECONDS}")
    return timeout


def _normalize_optional_device(device: Optional[str]) -> Optional[str]:
    if device is None:
        return None
    clean = str(device).strip()
    if not clean:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.,:+\-=]+", clean):
        raise ValueError("invalid ALSA device identifier")
    return clean


def _device_looks_like_hw(device: str) -> bool:
    return bool(re.fullmatch(r"(?:plug)?hw:\d+,\d+", str(device or "")))


def _validate_output_path(path: Path, overwrite: bool) -> None:
    if not path.name:
        raise ValueError("output_path must include a WAV filename")
    if path.exists() and not overwrite:
        raise ValueError("output_path already exists")


def _validate_wav_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "success": False,
            "status": ALSA_STATUS_OUTPUT_MISSING,
            "text": "arecord completed but did not create a WAV output file.",
            "error_message": "wav_output_missing",
        }
    size = path.stat().st_size
    if size <= 44:
        return {
            "success": False,
            "status": ALSA_STATUS_OUTPUT_EMPTY,
            "text": "arecord completed but WAV output was empty.",
            "error_message": "wav_output_empty",
            "byte_count": size,
        }
    try:
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            frame_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            duration = frames / frame_rate if frame_rate else 0.0
    except (wave.Error, EOFError, OSError) as error:
        return {
            "success": False,
            "status": ALSA_STATUS_INVALID_WAV,
            "text": "arecord output is not a valid WAV file.",
            "error_message": f"invalid_wav:{error.__class__.__name__}",
            "byte_count": size,
        }
    return {
        "success": True,
        "status": "valid_wav",
        "byte_count": size,
        "frames": frames,
        "sample_rate_hz": frame_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "duration_seconds": duration,
    }


def _read_wav_audio_chunk(path: Path, source: str, metadata: Dict[str, Any]) -> AudioChunk:
    with wave.open(str(path), "rb") as wav_file:
        frames = wav_file.readframes(wav_file.getnframes())
        return AudioChunk(
            data=frames,
            sample_rate_hz=wav_file.getframerate(),
            channels=wav_file.getnchannels(),
            sample_width_bytes=wav_file.getsampwidth(),
            source=source,
            metadata={**dict(metadata), "encoding": "pcm_from_wav"},
        )


def _stderr_indicates_invalid_device(stderr: str) -> bool:
    lower = str(stderr or "").lower()
    return any(
        marker in lower
        for marker in (
            "unknown pcm",
            "no such file or directory",
            "audio open error",
            "cannot open audio device",
            "device or resource busy",
        )
    )


def _is_cancelled(cancel_requested: Optional[CancelCheck | Any]) -> bool:
    if cancel_requested is None:
        return False
    is_set = getattr(cancel_requested, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(cancel_requested):
        return bool(cancel_requested())
    return bool(cancel_requested)
