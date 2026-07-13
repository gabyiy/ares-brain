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
from core.VoiceActivityCalibration import (
    AmbientStatistics,
    VoiceActivityThresholds,
    adapt_post_speech_thresholds,
    calculate_ambient_statistics,
    derive_thresholds,
    manual_thresholds,
    update_noise_floor,
)


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

DETECTION_STATE_CALIBRATING = "CALIBRATING"
DETECTION_STATE_WAITING = "WAITING"
DETECTION_STATE_SPEECH = "SPEECH"
DETECTION_STATE_POSSIBLE_SILENCE = "POSSIBLE_SILENCE"
DETECTION_STATE_COMPLETE = "COMPLETE"


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
        calibration_frames = (
            max(1, math.ceil(request.calibration_duration_seconds / frame_seconds))
            if request.calibration_enabled
            else 0
        )
        pre_roll_frames = max(0, math.ceil(request.pre_roll_seconds / frame_seconds))
        pre_roll: Deque[Tuple[bytes, float, int]] = deque(
            maxlen=max(1, pre_roll_frames + request.required_speech_frames)
        )
        pending_silence: list[Tuple[bytes, float, int]] = []
        captured: list[bytes] = []
        calibration_levels: list[float] = []
        ambient_levels: list[float] = []
        speech_levels: list[float] = []
        all_levels: list[float] = []
        transitions: list[Dict[str, Any]] = []
        thresholds = manual_thresholds(request)
        ambient_statistics: Optional[AmbientStatistics] = None
        noise_floor = 0.0
        speech_detected = False
        consecutive_speech = 0
        consecutive_resume = 0
        consecutive_below_silence = 0
        active_frames = 0
        listening_frames = 0
        total_frames = 0
        speech_frame_count = 0
        last_confirmed_speech_frame = 0
        speech_start_frame = 0
        stop_reason = ""
        silence_at_stop = 0.0
        detection_state = (
            DETECTION_STATE_CALIBRATING
            if request.calibration_enabled
            else DETECTION_STATE_WAITING
        )

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
                    ambient_levels=ambient_levels or calibration_levels,
                    ambient_statistics=ambient_statistics,
                    thresholds=thresholds,
                    data={"transitions": transitions},
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
                    ambient_levels=ambient_levels or calibration_levels,
                    ambient_statistics=ambient_statistics,
                    thresholds=thresholds,
                    data={"transitions": transitions},
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
                    ambient_levels=ambient_levels or calibration_levels,
                    ambient_statistics=ambient_statistics,
                    thresholds=thresholds,
                    data={"transitions": transitions},
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
                    ambient_levels=ambient_levels or calibration_levels,
                    ambient_statistics=ambient_statistics,
                    thresholds=thresholds,
                    data={
                        "expected_frame_bytes": frame_bytes,
                        "received_frame_bytes": len(frame or b""),
                        "transitions": transitions,
                    },
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
                    ambient_levels=ambient_levels or calibration_levels,
                    ambient_statistics=ambient_statistics,
                    thresholds=thresholds,
                    data={"transitions": transitions},
                )
            rms = float(signal["rms_amplitude"])
            all_levels.append(rms)

            if detection_state == DETECTION_STATE_CALIBRATING:
                calibration_levels.append(rms)
                if len(calibration_levels) >= calibration_frames:
                    try:
                        ambient_statistics = calculate_ambient_statistics(calibration_levels)
                        thresholds = derive_thresholds(ambient_statistics, request)
                    except ValueError as error:
                        return self._failure(
                            request,
                            VAD_STATUS_INVALID_AUDIO,
                            str(error),
                            started_at,
                            frame_count=total_frames,
                            levels=all_levels,
                            ambient_levels=calibration_levels,
                        )
                    noise_floor = ambient_statistics.noise_floor_rms
                    _record_transition(
                        transitions,
                        DETECTION_STATE_CALIBRATING,
                        DETECTION_STATE_WAITING,
                        total_frames,
                        rms,
                    )
                    detection_state = DETECTION_STATE_WAITING
                continue

            listening_frames += 1
            if detection_state == DETECTION_STATE_WAITING:
                pre_roll.append((frame, rms, total_frames))
                if rms >= thresholds.speech_start_rms:
                    consecutive_speech += 1
                else:
                    consecutive_speech = 0
                    ambient_levels.append(rms)
                if consecutive_speech >= request.required_speech_frames:
                    speech_detected = True
                    captured.extend(item[0] for item in pre_roll)
                    qualifying = [
                        level
                        for _, level, _ in pre_roll
                        if level >= thresholds.speech_continue_rms
                    ]
                    speech_levels.extend(qualifying)
                    speech_frame_count += len(qualifying)
                    active_frames = request.required_speech_frames
                    speech_start_frame = total_frames - request.required_speech_frames + 1
                    last_confirmed_speech_frame = total_frames
                    _record_transition(
                        transitions,
                        DETECTION_STATE_WAITING,
                        DETECTION_STATE_SPEECH,
                        total_frames,
                        rms,
                    )
                    detection_state = DETECTION_STATE_SPEECH
                    continue
                if listening_frames >= wait_frames:
                    return self._failure(
                        request,
                        VAD_STATUS_NO_SPEECH_TIMEOUT,
                        "speech_not_detected_before_timeout",
                        started_at,
                        speech_detected=False,
                        frame_count=total_frames,
                        levels=all_levels,
                        ambient_levels=ambient_levels or calibration_levels,
                        ambient_statistics=ambient_statistics,
                        thresholds=thresholds,
                        data={"transitions": transitions},
                    )
                continue

            active_frames += 1
            if detection_state == DETECTION_STATE_SPEECH:
                if rms >= thresholds.speech_continue_rms:
                    captured.append(frame)
                    speech_levels.append(rms)
                    speech_frame_count += 1
                    last_confirmed_speech_frame = total_frames
                    if speech_frame_count % 5 == 0:
                        thresholds = adapt_post_speech_thresholds(
                            thresholds,
                            noise_floor or _combined_rms(calibration_levels),
                            _median_or_zero(speech_levels),
                            request,
                        )
                else:
                    pending_silence = [(frame, rms, total_frames)]
                    consecutive_resume = 0
                    consecutive_below_silence = 1 if rms <= thresholds.silence_rms else 0
                    _record_transition(
                        transitions,
                        DETECTION_STATE_SPEECH,
                        DETECTION_STATE_POSSIBLE_SILENCE,
                        total_frames,
                        rms,
                    )
                    detection_state = DETECTION_STATE_POSSIBLE_SILENCE
            elif detection_state == DETECTION_STATE_POSSIBLE_SILENCE:
                pending_silence.append((frame, rms, total_frames))
                if rms >= thresholds.speech_continue_rms:
                    consecutive_resume += 1
                    consecutive_below_silence = 0
                else:
                    consecutive_resume = 0
                    bounded_noise_sample = min(
                        rms,
                        max(
                            (noise_floor or thresholds.silence_rms) * 3.0,
                            thresholds.silence_rms * 1.5,
                        ),
                    )
                    noise_floor = update_noise_floor(
                        noise_floor or bounded_noise_sample,
                        bounded_noise_sample,
                    )
                    thresholds = adapt_post_speech_thresholds(
                        thresholds,
                        noise_floor,
                        _median_or_zero(speech_levels),
                        request,
                    )
                    if rms <= thresholds.silence_rms:
                        consecutive_below_silence += 1
                    else:
                        consecutive_below_silence = 0

                if consecutive_resume >= request.required_continue_frames:
                    captured.extend(item[0] for item in pending_silence)
                    resumed_levels = [
                        level
                        for _, level, _ in pending_silence
                        if level >= thresholds.speech_continue_rms
                    ]
                    speech_levels.extend(resumed_levels)
                    speech_frame_count += len(resumed_levels)
                    last_confirmed_speech_frame = total_frames
                    pending_silence.clear()
                    _record_transition(
                        transitions,
                        DETECTION_STATE_POSSIBLE_SILENCE,
                        DETECTION_STATE_SPEECH,
                        total_frames,
                        rms,
                    )
                    detection_state = DETECTION_STATE_SPEECH
                    consecutive_resume = 0
                    consecutive_below_silence = 0
                elif (
                    len(pending_silence) >= silence_frames
                    and consecutive_below_silence >= request.required_silence_frames
                ):
                    stop_reason = VAD_STATUS_COMPLETED_AFTER_SILENCE
                    silence_at_stop = len(pending_silence) * frame_seconds
                    _record_transition(
                        transitions,
                        DETECTION_STATE_POSSIBLE_SILENCE,
                        DETECTION_STATE_COMPLETE,
                        total_frames,
                        rms,
                    )
                    detection_state = DETECTION_STATE_COMPLETE
                    break

            if active_frames >= maximum_frames:
                stop_reason = VAD_STATUS_MAXIMUM_DURATION
                silence_at_stop = len(pending_silence) * frame_seconds
                captured.extend(item[0] for item in pending_silence)
                pending_silence.clear()
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

        if ambient_statistics is None:
            manual_ambient = ambient_levels or calibration_levels
            if manual_ambient:
                ambient_statistics = calculate_ambient_statistics(manual_ambient)
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
            ambient_rms=_combined_rms(ambient_levels or calibration_levels),
            ambient_rms_mean=ambient_statistics.mean_rms if ambient_statistics else 0.0,
            ambient_rms_median=ambient_statistics.median_rms if ambient_statistics else 0.0,
            ambient_rms_percentile=(
                ambient_statistics.percentile_rms if ambient_statistics else 0.0
            ),
            ambient_rms_peak=ambient_statistics.peak_rms if ambient_statistics else 0.0,
            ambient_noise_floor=(
                ambient_statistics.noise_floor_rms if ambient_statistics else 0.0
            ),
            speech_rms=_combined_rms(speech_levels),
            maximum_frame_rms=max(all_levels, default=0.0),
            sample_rate_hz=request.sample_rate_hz,
            channels=request.channels,
            sample_width_bytes=request.sample_width_bytes,
            frame_count=total_frames,
            speech_frame_count=speech_frame_count,
            trailing_silence_frame_count=len(pending_silence),
            selected_device=request.microphone_device,
            stop_reason=stop_reason,
            calibration_enabled=request.calibration_enabled,
            calibration_duration_seconds=(calibration_frames * frame_seconds),
            derived_speech_start_rms=thresholds.speech_start_rms,
            derived_speech_continue_rms=thresholds.speech_continue_rms,
            derived_silence_rms=thresholds.silence_rms,
            speech_start_offset_seconds=round(
                max(0, speech_start_frame - calibration_frames - 1) * frame_seconds,
                6,
            ),
            speech_end_offset_seconds=round(
                max(0, last_confirmed_speech_frame - calibration_frames) * frame_seconds,
                6,
            ),
            maximum_duration_reached=stop_reason == VAD_STATUS_MAXIMUM_DURATION,
            processing_time_seconds=round(max(0.0, self.clock() - started_at), 6),
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            data={
                "frame_duration_ms": request.frame_duration_ms,
                "calibration_enabled": request.calibration_enabled,
                "calibration_frames": calibration_frames,
                "ambient_statistics": (
                    ambient_statistics.to_dict() if ambient_statistics else {}
                ),
                "speech_start_rms": thresholds.speech_start_rms,
                "speech_continue_rms": thresholds.speech_continue_rms,
                "silence_rms": thresholds.silence_rms,
                "required_speech_frames": request.required_speech_frames,
                "required_continue_frames": request.required_continue_frames,
                "required_silence_frames": request.required_silence_frames,
                "speech_start_status": VAD_STATUS_SPEECH_DETECTED,
                "silence_frames_required": silence_frames,
                "pre_roll_frames": pre_roll_frames,
                "terminal_silence_trimmed": True,
                "transitions": transitions,
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
        ambient_statistics: Optional[AmbientStatistics] = None,
        thresholds: Optional[VoiceActivityThresholds] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> VoiceActivityCaptureResultV1:
        if ambient_statistics is None and ambient_levels:
            try:
                ambient_statistics = calculate_ambient_statistics(ambient_levels)
            except ValueError:
                ambient_statistics = None
        if thresholds is None and request is not None:
            thresholds = manual_thresholds(request)
        return VoiceActivityCaptureResultV1(
            success=False,
            status=status,
            speech_detected=speech_detected,
            frame_count=frame_count,
            selected_device=request.microphone_device if request else "",
            stop_reason=status,
            processing_time_seconds=round(max(0.0, self.clock() - started_at), 6),
            ambient_rms=_combined_rms(ambient_levels or []),
            ambient_rms_mean=ambient_statistics.mean_rms if ambient_statistics else 0.0,
            ambient_rms_median=ambient_statistics.median_rms if ambient_statistics else 0.0,
            ambient_rms_percentile=(
                ambient_statistics.percentile_rms if ambient_statistics else 0.0
            ),
            ambient_rms_peak=ambient_statistics.peak_rms if ambient_statistics else 0.0,
            ambient_noise_floor=(
                ambient_statistics.noise_floor_rms if ambient_statistics else 0.0
            ),
            maximum_frame_rms=max(levels or [0.0]),
            calibration_enabled=bool(request and request.calibration_enabled),
            calibration_duration_seconds=(
                float(request.calibration_duration_seconds)
                if request and request.calibration_enabled
                else 0.0
            ),
            derived_speech_start_rms=(thresholds.speech_start_rms if thresholds else 0.0),
            derived_speech_continue_rms=(
                thresholds.speech_continue_rms if thresholds else 0.0
            ),
            derived_silence_rms=thresholds.silence_rms if thresholds else 0.0,
            maximum_duration_reached=status == VAD_STATUS_MAXIMUM_DURATION,
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
    if request.speech_start_rms <= request.speech_continue_rms:
        raise ValueError("speech_start_rms_must_exceed_speech_continue_rms")
    if request.speech_continue_rms <= request.silence_rms:
        raise ValueError("speech_continue_rms_must_exceed_silence_rms")
    if request.silence_rms < 0:
        raise ValueError("silence_rms_must_be_non_negative")
    if not 1 <= request.required_speech_frames <= 50:
        raise ValueError("required_speech_frames_out_of_range")
    if not 1 <= request.required_continue_frames <= 50:
        raise ValueError("required_continue_frames_out_of_range")
    if not 1 <= request.required_silence_frames <= 100:
        raise ValueError("required_silence_frames_out_of_range")
    if request.calibration_enabled and not 0.1 <= request.calibration_duration_seconds <= 10.0:
        raise ValueError("calibration_duration_seconds_out_of_range")
    threshold_bounds = (
        (request.minimum_speech_start_rms, request.maximum_speech_start_rms),
        (request.minimum_speech_continue_rms, request.maximum_speech_continue_rms),
        (request.minimum_silence_rms, request.maximum_silence_rms),
    )
    if any(minimum <= 0 or minimum > maximum for minimum, maximum in threshold_bounds):
        raise ValueError("voice_activity_threshold_bounds_invalid")
    if not (
        request.minimum_silence_rms
        < request.maximum_speech_continue_rms
        < request.maximum_speech_start_rms
    ):
        raise ValueError("voice_activity_threshold_order_invalid")
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


def _median_or_zero(levels: list[float]) -> float:
    if not levels:
        return 0.0
    ordered = sorted(levels)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float((ordered[middle - 1] + ordered[middle]) / 2.0)


def _record_transition(
    transitions: list[Dict[str, Any]],
    previous: str,
    current: str,
    frame_number: int,
    rms: float,
) -> None:
    if len(transitions) >= 50:
        return
    transitions.append(
        {
            "from": previous,
            "to": current,
            "frame": int(frame_number),
            "rms": round(float(rms), 3),
        }
    )


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
