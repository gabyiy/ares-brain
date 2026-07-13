from __future__ import annotations

from collections import deque
import math
from pathlib import Path
import time
from typing import Any, Callable, Deque, Dict, Optional, Protocol, Tuple
import wave

from core.Contracts import (
    CONTRACT_VOICE_ACTIVITY_CAPTURE_REQUEST,
    VoiceActivityCaptureRequestV1,
    VoiceActivityCaptureResultV1,
    validate_contract,
)
from core.WavAudio import analyze_pcm_audio, analyze_wav_audio


CAPTURE_MODE_FIXED_DURATION = "fixed_duration"
CAPTURE_MODE_AUTO_STOP = "auto_stop"
CAPTURE_MODES = (CAPTURE_MODE_FIXED_DURATION, CAPTURE_MODE_AUTO_STOP)

VAD_STATUS_SPEECH_DETECTED = "speech_detected"
VAD_STATUS_NO_SPEECH_TIMEOUT = "no_speech_timeout"
VAD_STATUS_COMPLETED_AFTER_SILENCE = "completed_after_silence"
VAD_STATUS_MAXIMUM_DURATION = "maximum_duration_reached"
VAD_STATUS_CANCELLED = "cancelled"
VAD_STATUS_DEVICE_ERROR = "device_error"
VAD_STATUS_INVALID_AUDIO = "invalid_audio"
VAD_STATUS_TIMEOUT = "timeout"

VAD_STATE_STOPPED = "stopped"
VAD_STATE_READY = "ready"
VAD_STATE_BUSY = "busy"


CancelCheck = Callable[[], bool]


class PcmFrameSource(Protocol):
    """Bounded PCM source owned by a concrete microphone adapter."""

    def read_frame(self, frame_bytes: int, timeout_seconds: float) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class RmsVoiceActivityCapture:
    """Hardware-neutral energy VAD over mono signed 16-bit PCM frames."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.state = VAD_STATE_STOPPED
        self.execution_count = 0

    def start(self) -> VoiceActivityCaptureResultV1:
        self.state = VAD_STATE_READY
        return self._lifecycle_result(True, "started")

    def health_check(self) -> VoiceActivityCaptureResultV1:
        return self._lifecycle_result(
            True,
            "healthy",
            data={
                "algorithm": "pcm_frame_rms_hysteresis",
                "supported_sample_width_bytes": [2],
                "supported_channels": [1],
                "background_listening": False,
            },
        )

    def stop(self) -> VoiceActivityCaptureResultV1:
        self.state = VAD_STATE_STOPPED
        return self._lifecycle_result(True, "stopped")

    def execute(
        self,
        request: VoiceActivityCaptureRequestV1,
        frame_source: PcmFrameSource,
        cancel_requested: Optional[CancelCheck | Any] = None,
    ) -> VoiceActivityCaptureResultV1:
        started_at = self.clock()
        try:
            request = validate_voice_activity_request(request)
        except (TypeError, ValueError) as error:
            return self._failure(
                request if isinstance(request, VoiceActivityCaptureRequestV1) else None,
                VAD_STATUS_INVALID_AUDIO,
                str(error),
                started_at,
            )
        if self.state != VAD_STATE_READY:
            return self._failure(
                request,
                VAD_STATUS_DEVICE_ERROR,
                "voice_activity_capture_not_started",
                started_at,
            )

        self.state = VAD_STATE_BUSY
        self.execution_count += 1
        try:
            return self._capture(request, frame_source, cancel_requested, started_at)
        finally:
            self.state = VAD_STATE_READY

    def _capture(
        self,
        request: VoiceActivityCaptureRequestV1,
        frame_source: PcmFrameSource,
        cancel_requested: Optional[CancelCheck | Any],
        started_at: float,
    ) -> VoiceActivityCaptureResultV1:
        frame_seconds = request.frame_duration_ms / 1000.0
        samples_per_frame = request.sample_rate_hz * request.frame_duration_ms // 1000
        frame_bytes = samples_per_frame * request.channels * request.sample_width_bytes
        wait_frames = max(1, math.ceil(request.speech_wait_timeout_seconds / frame_seconds))
        maximum_frames = max(1, math.ceil(request.maximum_utterance_seconds / frame_seconds))
        silence_frames = max(1, math.ceil(request.silence_duration_seconds / frame_seconds))
        pre_roll_frames = max(0, math.ceil(request.pre_roll_seconds / frame_seconds))
        pre_roll: Deque[Tuple[bytes, float]] = deque(
            maxlen=max(1, pre_roll_frames + request.required_speech_frames)
        )
        trailing_silence: list[Tuple[bytes, float]] = []
        captured: list[bytes] = []
        ambient_levels: list[float] = []
        speech_levels: list[float] = []
        all_levels: list[float] = []
        speech_detected = False
        consecutive_speech = 0
        active_frames = 0
        total_frames = 0
        stop_reason = ""
        silence_at_stop = 0.0

        while True:
            if _is_cancelled(cancel_requested):
                return self._failure(
                    request,
                    VAD_STATUS_CANCELLED,
                    "voice_activity_capture_cancelled",
                    started_at,
                    speech_detected=speech_detected,
                    frame_count=total_frames,
                    levels=all_levels,
                )
            try:
                frame = frame_source.read_frame(
                    frame_bytes,
                    request.frame_read_timeout_seconds,
                )
            except TimeoutError:
                return self._failure(
                    request,
                    VAD_STATUS_TIMEOUT,
                    "pcm_frame_read_timeout",
                    started_at,
                    speech_detected=speech_detected,
                    frame_count=total_frames,
                    levels=all_levels,
                )
            except (EOFError, OSError, RuntimeError) as error:
                return self._failure(
                    request,
                    VAD_STATUS_DEVICE_ERROR,
                    f"pcm_stream_error:{error.__class__.__name__}",
                    started_at,
                    speech_detected=speech_detected,
                    frame_count=total_frames,
                    levels=all_levels,
                )

            if not isinstance(frame, (bytes, bytearray)) or len(frame) != frame_bytes:
                return self._failure(
                    request,
                    VAD_STATUS_INVALID_AUDIO,
                    "invalid_pcm_frame_size",
                    started_at,
                    speech_detected=speech_detected,
                    frame_count=total_frames,
                    levels=all_levels,
                    data={"expected_frame_bytes": frame_bytes, "received_frame_bytes": len(frame or b"")},
                )

            frame = bytes(frame)
            total_frames += 1
            try:
                signal = analyze_pcm_audio(frame, request.sample_width_bytes)
            except ValueError as error:
                return self._failure(
                    request,
                    VAD_STATUS_INVALID_AUDIO,
                    str(error),
                    started_at,
                    speech_detected=speech_detected,
                    frame_count=total_frames,
                    levels=all_levels,
                )
            rms = float(signal["rms_amplitude"])
            all_levels.append(rms)

            if not speech_detected:
                pre_roll.append((frame, rms))
                if rms >= request.speech_start_rms:
                    consecutive_speech += 1
                else:
                    consecutive_speech = 0
                    ambient_levels.append(rms)
                if consecutive_speech >= request.required_speech_frames:
                    speech_detected = True
                    captured.extend(item[0] for item in pre_roll)
                    speech_levels.extend(
                        level for _, level in pre_roll if level > request.silence_rms
                    )
                    active_frames = request.required_speech_frames
                    stop_reason = VAD_STATUS_SPEECH_DETECTED
                    continue
                if total_frames >= wait_frames:
                    return self._failure(
                        request,
                        VAD_STATUS_NO_SPEECH_TIMEOUT,
                        "speech_not_detected_before_timeout",
                        started_at,
                        speech_detected=False,
                        frame_count=total_frames,
                        levels=all_levels,
                        ambient_levels=ambient_levels,
                    )
                continue

            active_frames += 1
            if rms <= request.silence_rms:
                trailing_silence.append((frame, rms))
                if len(trailing_silence) >= silence_frames:
                    stop_reason = VAD_STATUS_COMPLETED_AFTER_SILENCE
                    silence_at_stop = len(trailing_silence) * frame_seconds
                    break
            else:
                if trailing_silence:
                    captured.extend(item[0] for item in trailing_silence)
                    trailing_silence.clear()
                captured.append(frame)
                speech_levels.append(rms)

            if active_frames >= maximum_frames:
                stop_reason = VAD_STATUS_MAXIMUM_DURATION
                silence_at_stop = len(trailing_silence) * frame_seconds
                break

        pcm = b"".join(captured)
        if not pcm:
            return self._failure(
                request,
                VAD_STATUS_INVALID_AUDIO,
                "captured_pcm_is_empty",
                started_at,
                speech_detected=True,
                frame_count=total_frames,
                levels=all_levels,
            )

        try:
            wav_path = _write_pcm_wav_atomic(request, pcm)
            wav = analyze_wav_audio(wav_path)
        except (OSError, ValueError, wave.Error) as error:
            return self._failure(
                request,
                VAD_STATUS_INVALID_AUDIO,
                f"wav_write_failed:{error.__class__.__name__}",
                started_at,
                speech_detected=True,
                frame_count=total_frames,
                levels=all_levels,
            )
        if not wav.get("success"):
            return self._failure(
                request,
                VAD_STATUS_INVALID_AUDIO,
                str(wav.get("error_message") or "invalid_wav_output"),
                started_at,
                speech_detected=True,
                frame_count=total_frames,
                levels=all_levels,
            )

        return VoiceActivityCaptureResultV1(
            success=True,
            status=stop_reason,
            wav_path=str(wav_path),
            speech_detected=True,
            duration_seconds=float(wav.get("duration_seconds", 0.0)),
            speech_duration_seconds=round(len(speech_levels) * frame_seconds, 6),
            silence_duration_at_stop_seconds=round(silence_at_stop, 6),
            peak_amplitude=int(wav.get("peak_amplitude", 0)),
            rms_amplitude=float(wav.get("rms_amplitude", 0.0)),
            ambient_rms=_combined_rms(ambient_levels),
            speech_rms=_combined_rms(speech_levels),
            maximum_frame_rms=max(all_levels, default=0.0),
            sample_rate_hz=request.sample_rate_hz,
            channels=request.channels,
            sample_width_bytes=request.sample_width_bytes,
            frame_count=total_frames,
            selected_device=request.microphone_device,
            stop_reason=stop_reason,
            processing_time_seconds=round(max(0.0, self.clock() - started_at), 6),
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            data={
                "frame_duration_ms": request.frame_duration_ms,
                "speech_start_rms": request.speech_start_rms,
                "silence_rms": request.silence_rms,
                "required_speech_frames": request.required_speech_frames,
                "speech_start_status": VAD_STATUS_SPEECH_DETECTED,
                "silence_frames_required": silence_frames,
                "pre_roll_frames": pre_roll_frames,
                "terminal_silence_trimmed": True,
                "wav": wav,
            },
            metadata={
                **dict(request.metadata or {}),
                "safe": True,
                "source": "rms_voice_activity_capture",
                "algorithm": "pcm_frame_rms_hysteresis",
                "raw_audio_persisted_in_metadata": False,
            },
        )

    def _failure(
        self,
        request: Optional[VoiceActivityCaptureRequestV1],
        status: str,
        error_message: str,
        started_at: float,
        speech_detected: bool = False,
        frame_count: int = 0,
        levels: Optional[list[float]] = None,
        ambient_levels: Optional[list[float]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> VoiceActivityCaptureResultV1:
        return VoiceActivityCaptureResultV1(
            success=False,
            status=status,
            speech_detected=speech_detected,
            frame_count=frame_count,
            selected_device=request.microphone_device if request else "",
            stop_reason=status,
            processing_time_seconds=round(max(0.0, self.clock() - started_at), 6),
            ambient_rms=_combined_rms(ambient_levels or []),
            maximum_frame_rms=max(levels or [0.0]),
            error_message=str(error_message or status),
            correlation_id=request.correlation_id if request else "",
            session_id=request.session_id if request else "",
            data=dict(data or {}),
            metadata={
                **(dict(request.metadata or {}) if request else {}),
                "safe": True,
                "source": "rms_voice_activity_capture",
                "raw_audio_persisted_in_metadata": False,
            },
        )

    def _lifecycle_result(
        self,
        success: bool,
        status: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> VoiceActivityCaptureResultV1:
        return VoiceActivityCaptureResultV1(
            success=success,
            status=status,
            stop_reason=status,
            data={"state": self.state, **dict(data or {})},
            metadata={
                "safe": True,
                "source": "rms_voice_activity_capture",
                "background_listening": False,
            },
        )


def validate_voice_activity_request(
    request: VoiceActivityCaptureRequestV1,
) -> VoiceActivityCaptureRequestV1:
    if not isinstance(request, VoiceActivityCaptureRequestV1):
        raise TypeError("voice_activity_capture_request_required")
    compatibility = validate_contract(
        request,
        expected_contract_name=CONTRACT_VOICE_ACTIVITY_CAPTURE_REQUEST,
    )
    if not compatibility.success:
        raise ValueError(compatibility.error_message or compatibility.status)
    if request.sample_rate_hz != 16000:
        raise ValueError("voice_activity_sample_rate_must_be_16000")
    if request.channels != 1:
        raise ValueError("voice_activity_channels_must_be_mono")
    if request.sample_width_bytes != 2:
        raise ValueError("voice_activity_sample_width_must_be_16_bit")
    if not 10 <= request.frame_duration_ms <= 100:
        raise ValueError("frame_duration_ms_out_of_range")
    if request.sample_rate_hz * request.frame_duration_ms % 1000:
        raise ValueError("frame_duration_does_not_align_with_sample_rate")
    if request.speech_start_rms <= request.silence_rms:
        raise ValueError("speech_start_rms_must_exceed_silence_rms")
    if request.silence_rms < 0:
        raise ValueError("silence_rms_must_be_non_negative")
    if not 1 <= request.required_speech_frames <= 50:
        raise ValueError("required_speech_frames_out_of_range")
    if not 0.1 <= request.silence_duration_seconds <= 10.0:
        raise ValueError("silence_duration_seconds_out_of_range")
    if not 0.1 <= request.speech_wait_timeout_seconds <= 120.0:
        raise ValueError("speech_wait_timeout_seconds_out_of_range")
    if not 0.2 <= request.maximum_utterance_seconds <= 120.0:
        raise ValueError("maximum_utterance_seconds_out_of_range")
    if not 0.0 <= request.pre_roll_seconds <= 2.0:
        raise ValueError("pre_roll_seconds_out_of_range")
    if not 0.01 <= request.frame_read_timeout_seconds <= 30.0:
        raise ValueError("frame_read_timeout_seconds_out_of_range")
    if not str(request.output_wav_path or "").lower().endswith(".wav"):
        raise ValueError("voice_activity_output_path_must_be_wav")
    return request


def _write_pcm_wav_atomic(request: VoiceActivityCaptureRequestV1, pcm: bytes) -> Path:
    output = Path(request.output_wav_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with wave.open(str(temporary), "wb") as wav_file:
            wav_file.setnchannels(request.channels)
            wav_file.setsampwidth(request.sample_width_bytes)
            wav_file.setframerate(request.sample_rate_hz)
            wav_file.writeframes(pcm)
        validation = analyze_wav_audio(temporary)
        if not validation.get("success"):
            raise ValueError(str(validation.get("error_message") or "invalid_temporary_wav"))
        temporary.replace(output)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return output


def _combined_rms(levels: list[float]) -> float:
    if not levels:
        return 0.0
    return round(math.sqrt(sum(level * level for level in levels) / len(levels)), 6)


def _is_cancelled(cancel_requested: Optional[CancelCheck | Any]) -> bool:
    if cancel_requested is None:
        return False
    is_set = getattr(cancel_requested, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if hasattr(cancel_requested, "requested"):
        return bool(getattr(cancel_requested, "requested"))
    if callable(cancel_requested):
        return bool(cancel_requested())
    return bool(cancel_requested)
