from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Deque, Dict, Optional, Protocol, Tuple
import wave

from core.Contracts import (
    CONTRACT_VOICE_ACTIVITY_CAPTURE_REQUEST,
    VoiceActivityCaptureRequestV1,
    VoiceActivityCaptureResultV1,
    validate_contract,
)
from core.WavAudio import analyze_pcm_audio, analyze_wav_audio, pcm_frame_sample_count
from core.VoiceActivityCalibration import (
    AmbientStatistics,
    VoiceActivityThresholds,
    adapt_post_speech_thresholds,
    analyze_robust_ambient_calibration,
    calculate_ambient_statistics,
    derive_thresholds,
    derive_wake_thresholds,
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


@dataclass(frozen=True)
class VoiceActivityStreamCalibration:
    success: bool
    status: str
    frame_count: int = 0
    duration_seconds: float = 0.0
    ambient_statistics: Optional[AmbientStatistics] = None
    thresholds: Optional[VoiceActivityThresholds] = None
    rejected_speech_frames: int = 0
    restart_count: int = 0
    maximum_frame_count: int = 0
    attempt_count: int = 1
    diagnostics: Optional["VoiceActivityCalibrationDiagnostics"] = None
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class VoiceActivityCalibrationDiagnostics:
    frame_count: int
    frame_duration_seconds: float
    minimum_rms: float
    median_rms: float
    percentile_20_rms: float
    percentile_80_rms: float
    maximum_rms: float
    speech_frame_count: int
    non_speech_frame_count: int
    longest_non_speech_sequence: int
    bootstrap_threshold_rms: float
    selected_noise_floor_rms: float
    quiet_sample_count: int
    quiet_sample_fraction: float
    clipped_frame_count: int
    clipped_frame_fraction: float
    zero_frame_count: int
    quality_passed: bool
    quality_reason: str
    rms_summary: Tuple[Dict[str, float | int], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calibration_method": "rms_percentile_bootstrap_v1",
            "speech_classifier": "bounded_rms_outlier",
            "frame_count": self.frame_count,
            "frame_duration_seconds": self.frame_duration_seconds,
            "minimum_rms": self.minimum_rms,
            "median_rms": self.median_rms,
            "percentile_20_rms": self.percentile_20_rms,
            "percentile_80_rms": self.percentile_80_rms,
            "maximum_rms": self.maximum_rms,
            "speech_frame_count": self.speech_frame_count,
            "non_speech_frame_count": self.non_speech_frame_count,
            "longest_non_speech_sequence": self.longest_non_speech_sequence,
            "bootstrap_threshold_rms": self.bootstrap_threshold_rms,
            "selected_noise_floor_rms": self.selected_noise_floor_rms,
            "quiet_sample_count": self.quiet_sample_count,
            "quiet_sample_fraction": self.quiet_sample_fraction,
            "clipped_frame_count": self.clipped_frame_count,
            "clipped_frame_fraction": self.clipped_frame_fraction,
            "zero_frame_count": self.zero_frame_count,
            "quality_passed": self.quality_passed,
            "quality_reason": self.quality_reason,
            "rms_summary": [dict(item) for item in self.rms_summary],
        }


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

    def calibrate_stream(
        self,
        request: VoiceActivityCaptureRequestV1,
        frame_source: PcmFrameSource,
        cancel_requested: Optional[CancelCheck | Any] = None,
    ) -> VoiceActivityStreamCalibration:
        """Calibrate one already-open PCM stream without closing or capturing it."""

        try:
            request = validate_voice_activity_request(request)
        except (TypeError, ValueError) as error:
            return VoiceActivityStreamCalibration(
                False,
                "invalid_calibration_request",
                error_code="invalid_calibration_request",
                error_message=str(error),
            )
        if self.state != VAD_STATE_READY:
            return VoiceActivityStreamCalibration(
                False,
                "not_ready",
                error_code="voice_activity_capture_not_ready",
                error_message="voice activity capture is not ready",
            )
        if not request.calibration_enabled or request.calibration_duration_seconds <= 0:
            return VoiceActivityStreamCalibration(
                True,
                "manual_thresholds",
                thresholds=manual_thresholds(request),
            )

        self.state = VAD_STATE_BUSY
        frame_seconds = request.frame_duration_ms / 1000.0
        frame_count = max(
            1,
            math.ceil(request.calibration_duration_seconds / frame_seconds),
        )
        calibration_metadata = dict(request.metadata or {})
        guarded = bool(
            calibration_metadata.get("calibration_confirm_non_speech", False)
        )
        maximum_seconds = _metadata_number(
            calibration_metadata,
            "calibration_maximum_seconds",
            request.calibration_duration_seconds,
            request.calibration_duration_seconds,
            10.0,
        )
        maximum_frame_count = max(
            frame_count,
            math.ceil(maximum_seconds / frame_seconds),
        )
        samples_per_frame = pcm_frame_sample_count(
            request.sample_rate_hz,
            request.frame_duration_ms,
        )
        frame_bytes = samples_per_frame * request.channels * request.sample_width_bytes
        levels: list[float] = []
        peaks: list[int] = []
        frames_read = 0
        calibration_started = self.clock()
        wall_timeout_seconds = maximum_seconds + min(
            2.0,
            request.frame_read_timeout_seconds * 2.0,
        )
        try:
            for _ in range(frame_count):
                if self.clock() - calibration_started > wall_timeout_seconds:
                    return VoiceActivityStreamCalibration(
                        False,
                        VAD_STATUS_TIMEOUT,
                        frame_count=frames_read,
                        duration_seconds=round(frames_read * frame_seconds, 6),
                        maximum_frame_count=maximum_frame_count,
                        error_code=VAD_STATUS_TIMEOUT,
                        error_message="standby calibration exceeded its wall-clock bound",
                    )
                if _is_cancelled(cancel_requested):
                    return VoiceActivityStreamCalibration(
                        False,
                        VAD_STATUS_CANCELLED,
                        frame_count=frames_read,
                        duration_seconds=round(frames_read * frame_seconds, 6),
                        maximum_frame_count=maximum_frame_count,
                        error_code=VAD_STATUS_CANCELLED,
                        error_message="voice activity calibration cancelled",
                    )
                try:
                    frame = frame_source.read_frame(
                        frame_bytes,
                        request.frame_read_timeout_seconds,
                    )
                except TimeoutError:
                    return VoiceActivityStreamCalibration(
                        False,
                        VAD_STATUS_TIMEOUT,
                        frame_count=len(levels),
                        duration_seconds=round(len(levels) * frame_seconds, 6),
                        error_code=VAD_STATUS_TIMEOUT,
                        error_message="pcm frame read timeout during calibration",
                    )
                except (EOFError, OSError, RuntimeError) as error:
                    return VoiceActivityStreamCalibration(
                        False,
                        VAD_STATUS_DEVICE_ERROR,
                        frame_count=len(levels),
                        duration_seconds=round(len(levels) * frame_seconds, 6),
                        error_code=VAD_STATUS_DEVICE_ERROR,
                        error_message=f"pcm_stream_error:{error.__class__.__name__}",
                    )
                if not isinstance(frame, (bytes, bytearray)) or len(frame) != frame_bytes:
                    return VoiceActivityStreamCalibration(
                        False,
                        VAD_STATUS_INVALID_AUDIO,
                        frame_count=len(levels),
                        duration_seconds=round(len(levels) * frame_seconds, 6),
                        error_code=VAD_STATUS_INVALID_AUDIO,
                        error_message="invalid PCM frame during calibration",
                    )
                try:
                    signal = analyze_pcm_audio(bytes(frame), request.sample_width_bytes)
                except ValueError as error:
                    return VoiceActivityStreamCalibration(
                        False,
                        VAD_STATUS_INVALID_AUDIO,
                        frame_count=len(levels),
                        duration_seconds=round(len(levels) * frame_seconds, 6),
                        error_code=VAD_STATUS_INVALID_AUDIO,
                        error_message=str(error),
                    )
                frames_read += 1
                rms = float(signal["rms_amplitude"])
                levels.append(rms)
                peaks.append(int(signal["peak_amplitude"]))
            if len(levels) < frame_count:
                return VoiceActivityStreamCalibration(
                    False,
                    "calibration_frames_incomplete",
                    frame_count=frames_read,
                    duration_seconds=round(frames_read * frame_seconds, 6),
                    maximum_frame_count=maximum_frame_count,
                    error_code="calibration_frames_incomplete",
                    error_message="bounded calibration did not receive enough PCM frames",
                )
            if guarded:
                metadata = dict(request.metadata or {})
                analysis = analyze_robust_ambient_calibration(
                    levels,
                    peaks,
                    quiet_sample_fraction=_metadata_number(
                        metadata,
                        "calibration_quiet_sample_fraction",
                        0.25,
                        0.10,
                        0.50,
                    ),
                    minimum_quiet_frame_fraction=_metadata_number(
                        metadata,
                        "calibration_minimum_quiet_frame_fraction",
                        0.20,
                        0.10,
                        0.80,
                    ),
                    maximum_speech_frame_fraction=_metadata_number(
                        metadata,
                        "calibration_maximum_speech_frame_fraction",
                        0.75,
                        0.20,
                        0.95,
                    ),
                    maximum_noise_floor_rms=_metadata_number(
                        metadata,
                        "calibration_maximum_noise_floor_rms",
                        600.0,
                        50.0,
                        5000.0,
                    ),
                    maximum_clipped_frame_fraction=_metadata_number(
                        metadata,
                        "calibration_maximum_clipped_frame_fraction",
                        0.10,
                        0.0,
                        0.50,
                    ),
                    bootstrap_speech_multiplier=_metadata_number(
                        metadata,
                        "calibration_bootstrap_speech_multiplier",
                        3.0,
                        1.5,
                        10.0,
                    ),
                    bootstrap_speech_margin_rms=_metadata_number(
                        metadata,
                        "calibration_bootstrap_speech_margin_rms",
                        180.0,
                        20.0,
                        5000.0,
                    ),
                    minimum_bootstrap_speech_rms=float(request.speech_start_rms),
                )
                diagnostics = _calibration_diagnostics(
                    levels,
                    frame_seconds,
                    analysis,
                    summary_interval=_metadata_integer(
                        metadata,
                        "calibration_diagnostic_interval_frames",
                        10,
                        1,
                        50,
                    ),
                )
                if not analysis.success:
                    return VoiceActivityStreamCalibration(
                        False,
                        analysis.quality_reason,
                        frame_count=frames_read,
                        duration_seconds=round(frames_read * frame_seconds, 6),
                        ambient_statistics=analysis.statistics,
                        rejected_speech_frames=analysis.speech_frame_count,
                        maximum_frame_count=maximum_frame_count,
                        diagnostics=diagnostics,
                        error_code=analysis.quality_reason,
                        error_message=_calibration_quality_message(
                            analysis.quality_reason
                        ),
                    )
                statistics = analysis.statistics
                if metadata.get("vad_profile") == "standby_wake_short_v1":
                    thresholds = derive_wake_thresholds(
                        statistics,
                        request,
                        str(metadata.get("wake_vad_sensitivity", "normal")),
                    )
                else:
                    thresholds = derive_thresholds(statistics, request)
                return VoiceActivityStreamCalibration(
                    True,
                    "calibrated",
                    frame_count=frames_read,
                    duration_seconds=round(frames_read * frame_seconds, 6),
                    ambient_statistics=statistics,
                    thresholds=thresholds,
                    rejected_speech_frames=analysis.speech_frame_count,
                    maximum_frame_count=maximum_frame_count,
                    diagnostics=diagnostics,
                )
            statistics = calculate_ambient_statistics(levels)
            thresholds = derive_thresholds(statistics, request)
            return VoiceActivityStreamCalibration(
                True,
                "calibrated",
                frame_count=frames_read,
                duration_seconds=round(frames_read * frame_seconds, 6),
                ambient_statistics=statistics,
                thresholds=thresholds,
                maximum_frame_count=maximum_frame_count,
            )
        except ValueError as error:
            return VoiceActivityStreamCalibration(
                False,
                VAD_STATUS_INVALID_AUDIO,
                frame_count=len(levels),
                duration_seconds=round(len(levels) * frame_seconds, 6),
                error_code=VAD_STATUS_INVALID_AUDIO,
                error_message=str(error),
            )
        finally:
            self.state = VAD_STATE_READY

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
        samples_per_frame = pcm_frame_sample_count(
            request.sample_rate_hz,
            request.frame_duration_ms,
        )
        frame_bytes = samples_per_frame * request.channels * request.sample_width_bytes
        wait_frames = max(1, math.ceil(request.speech_wait_timeout_seconds / frame_seconds))
        maximum_frames = max(1, math.ceil(request.maximum_utterance_seconds / frame_seconds))
        silence_frames = max(1, math.ceil(request.silence_duration_seconds / frame_seconds))
        speech_end_padding_frames = max(
            0,
            math.ceil(request.speech_end_padding_seconds / frame_seconds),
        )
        request_metadata = dict(request.metadata or {})
        wake_short_profile = (
            request_metadata.get("vad_profile") == "standby_wake_short_v1"
        )
        minimum_speech_seconds = float(
            request_metadata.get("minimum_speech_duration_seconds", 0.0) or 0.0
        )
        diagnostic_rms_interval = max(
            1,
            int(request_metadata.get("diagnostic_rms_interval_frames", 5) or 5),
        )
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
        captured_frame_indexes: list[int] = []
        appended_frame_indexes: set[int] = set()
        duplicate_frame_append_count = 0
        calibration_levels: list[float] = []
        ambient_levels: list[float] = []
        speech_levels: list[float] = []
        all_levels: list[float] = []
        transitions: list[Dict[str, Any]] = []
        rms_trace: list[Dict[str, Any]] = []
        frame_trace: list[Dict[str, Any]] = []
        thresholds = manual_thresholds(request)
        ambient_statistics: Optional[AmbientStatistics] = None
        noise_floor = 0.0
        speech_detected = False
        consecutive_speech = 0
        maximum_consecutive_speech_evidence = 0
        speech_start_threshold_crossing_count = 0
        consecutive_resume = 0
        consecutive_below_silence = 0
        active_frames = 0
        listening_frames = 0
        total_frames = 0
        speech_frame_count = 0
        pre_roll_frames_retained = 0
        trailing_silence_frames_trimmed = 0
        leading_silence_frames_trimmed = 0
        last_confirmed_speech_frame = 0
        speech_start_frame = 0
        stop_reason = ""
        silence_at_stop = 0.0
        terminal_quiet_frame_count = 0
        post_roll_frames_retained = 0
        detection_state = (
            DETECTION_STATE_CALIBRATING
            if request.calibration_enabled
            else DETECTION_STATE_WAITING
        )
        source_start_snapshot = _pcm_source_snapshot(frame_source)

        def observability_data() -> Dict[str, Any]:
            source_end_snapshot = _pcm_source_snapshot(frame_source)
            source_observable = bool(source_start_snapshot or source_end_snapshot)
            read_start = int(source_start_snapshot.get("read_sequence", 0) or 0)
            read_end = int(source_end_snapshot.get("read_sequence", 0) or 0)
            live_start = int(source_start_snapshot.get("live_frame_count", 0) or 0)
            live_end = int(source_end_snapshot.get("live_frame_count", 0) or 0)
            bytes_start = int(
                source_start_snapshot.get("total_bytes_returned", 0) or 0
            )
            bytes_end = int(source_end_snapshot.get("total_bytes_returned", 0) or 0)
            live_bytes_start = int(
                source_start_snapshot.get("total_live_bytes_read", 0) or 0
            )
            live_bytes_end = int(
                source_end_snapshot.get("total_live_bytes_read", 0) or 0
            )
            frames_delta = (
                max(0, read_end - read_start)
                if source_observable
                else total_frames
            )
            live_frames_delta = (
                max(0, live_end - live_start) if source_observable else total_frames
            )
            bytes_delta = (
                max(0, bytes_end - bytes_start)
                if source_observable
                else total_frames * frame_bytes
            )
            live_bytes_delta = (
                max(0, live_bytes_end - live_bytes_start)
                if source_observable
                else total_frames * frame_bytes
            )
            return {
                "source_observability_available": source_observable,
                "source_read_sequence_start": read_start,
                "source_read_sequence_end": read_end,
                "source_frames_read_delta": frames_delta,
                "source_live_frame_sequence_start": live_start,
                "source_live_frame_sequence_end": live_end,
                "source_live_frames_read_delta": live_frames_delta,
                "source_bytes_read_delta": bytes_delta,
                "source_live_bytes_read_delta": live_bytes_delta,
                "source_last_frame_sequence": int(
                    source_end_snapshot.get("last_source_frame_sequence", 0) or 0
                ),
                "source_last_read_timestamp": float(
                    source_end_snapshot.get("last_read_timestamp", 0.0) or 0.0
                ),
                "source_stream_closed": bool(
                    source_end_snapshot.get("closed", False)
                ),
                "listening_frame_count": listening_frames,
                "listening_duration_seconds": round(
                    listening_frames * frame_seconds,
                    6,
                ),
                "speech_start_threshold_crossing_count": (
                    speech_start_threshold_crossing_count
                ),
                "maximum_consecutive_speech_evidence": (
                    maximum_consecutive_speech_evidence
                ),
                "maximum_observed_rms": round(max(all_levels or [0.0]), 3),
                "frame_trace": _enrich_frame_trace(frame_trace, transitions),
            }

        def append_captured_frame(value: bytes, frame_index: int) -> None:
            nonlocal duplicate_frame_append_count
            if frame_index in appended_frame_indexes:
                duplicate_frame_append_count += 1
                return
            appended_frame_indexes.add(frame_index)
            captured_frame_indexes.append(frame_index)
            captured.append(value)

        def append_captured_items(
            values: list[Tuple[bytes, float, int]],
        ) -> None:
            for value, _, frame_index in values:
                append_captured_frame(value, frame_index)

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
                timeout_observability = observability_data()
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
                    data={
                        **timeout_observability,
                        "transitions": transitions,
                        "capture_failure_stage": (
                            "post_calibration_input_absent"
                            if timeout_observability["source_frames_read_delta"] == 0
                            else "speech_candidate_assembly_failed"
                        ),
                    },
                )
            except (EOFError, OSError, RuntimeError) as error:
                device_observability = observability_data()
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
                    data={
                        **device_observability,
                        "transitions": transitions,
                        "capture_failure_stage": (
                            "post_calibration_input_absent"
                            if device_observability["source_frames_read_delta"] == 0
                            else "speech_candidate_assembly_failed"
                        ),
                    },
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
            source_frame = _pcm_source_snapshot(frame_source)
            if request.frame_debug_enabled and len(frame_trace) < 512:
                predicted_evidence = consecutive_speech
                if detection_state == DETECTION_STATE_WAITING:
                    predicted_evidence = (
                        consecutive_speech + 1
                        if rms >= thresholds.speech_start_rms
                        else 0
                    )
                frame_trace.append(
                    {
                        "frame": total_frames,
                        "source_frame_sequence": int(
                            source_frame.get("last_source_frame_sequence", 0)
                            or total_frames
                        ),
                        "source_read_sequence": int(
                            source_frame.get("read_sequence", 0) or total_frames
                        ),
                        "rms": round(rms, 3),
                        "speech_start_threshold": round(
                            thresholds.speech_start_rms,
                            3,
                        ),
                        "speech_continue_threshold": round(
                            thresholds.speech_continue_rms,
                            3,
                        ),
                        "exceeded_speech_start": (
                            rms >= thresholds.speech_start_rms
                        ),
                        "exceeded_speech_continue": (
                            rms >= thresholds.speech_continue_rms
                        ),
                        "consecutive_speech_evidence": predicted_evidence,
                        "state_before": detection_state,
                        "state_after": detection_state,
                        "vad_state_transition": "none",
                        "bytes_read": len(frame),
                        "read_timestamp": float(
                            source_frame.get("last_read_timestamp", 0.0)
                            or self.clock()
                        ),
                        "replayed_pre_roll": bool(
                            source_frame.get("last_frame_was_replay", False)
                        ),
                    }
                )
            if request.frame_debug_enabled and (
                total_frames == 1 or total_frames % diagnostic_rms_interval == 0
            ) and len(rms_trace) < 128:
                rms_trace.append(
                    {
                        "frame": total_frames,
                        "rms": round(rms, 3),
                        "state": detection_state,
                    }
                )

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
                    speech_start_threshold_crossing_count += 1
                else:
                    consecutive_speech = 0
                    ambient_levels.append(rms)
                maximum_consecutive_speech_evidence = max(
                    maximum_consecutive_speech_evidence,
                    consecutive_speech,
                )
                if consecutive_speech >= request.required_speech_frames:
                    speech_detected = True
                    retained_pre_roll = list(pre_roll)
                    append_captured_items(retained_pre_roll)
                    speech_start_frame = total_frames - request.required_speech_frames + 1
                    pre_roll_frames_retained = sum(
                        frame_index < speech_start_frame
                        for _, _, frame_index in retained_pre_roll
                    )
                    if retained_pre_roll:
                        leading_silence_frames_trimmed = max(
                            0,
                            retained_pre_roll[0][2] - 1,
                        )
                    qualifying = [
                        level
                        for _, level, frame_index in retained_pre_roll
                        if frame_index >= speech_start_frame
                        and level >= thresholds.speech_continue_rms
                    ]
                    speech_levels.extend(qualifying)
                    speech_frame_count += len(qualifying)
                    active_frames = request.required_speech_frames
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
                    no_speech_observability = observability_data()
                    post_calibration_frames = no_speech_observability[
                        "source_frames_read_delta"
                    ]
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
                        data={
                            **no_speech_observability,
                            "frame_duration_ms": request.frame_duration_ms,
                            "pre_roll_frames": pre_roll_frames,
                            "required_speech_frames": request.required_speech_frames,
                            "transitions": transitions,
                            "rms_trace": rms_trace,
                            "capture_failure_stage": (
                                "post_calibration_input_absent"
                                if post_calibration_frames == 0
                                else (
                                    "speech_threshold_not_crossed"
                                    if speech_start_threshold_crossing_count == 0
                                    else "speech_start_evidence_incomplete"
                                )
                            ),
                            "vad_profile": (
                                "standby_wake_short_v1"
                                if wake_short_profile
                                else "default"
                            ),
                        },
                    )
                continue

            active_frames += 1
            if detection_state == DETECTION_STATE_SPEECH:
                if rms >= thresholds.speech_continue_rms:
                    append_captured_frame(frame, total_frames)
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
                    consecutive_below_silence = (
                        1
                        if (
                            rms < thresholds.speech_continue_rms
                            if wake_short_profile
                            else rms <= thresholds.silence_rms
                        )
                        else 0
                    )
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
                    if wake_short_profile or rms <= thresholds.silence_rms:
                        consecutive_below_silence += 1
                    else:
                        consecutive_below_silence = 0

                if consecutive_resume >= request.required_continue_frames:
                    append_captured_items(pending_silence)
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
                elif consecutive_below_silence >= max(
                    silence_frames,
                    request.required_silence_frames,
                ):
                    if wake_short_profile:
                        trailing_silence_frames_trimmed = max(
                            0,
                            len(pending_silence) - speech_end_padding_frames,
                        )
                        retained_pending = (
                            pending_silence[-speech_end_padding_frames:]
                            if speech_end_padding_frames
                            else []
                        )
                    else:
                        trailing_silence_frames_trimmed = max(
                            0,
                            consecutive_below_silence - speech_end_padding_frames,
                        )
                        retained_pending = (
                            pending_silence[:-trailing_silence_frames_trimmed]
                            if trailing_silence_frames_trimmed
                            else list(pending_silence)
                        )
                    post_roll_frames_retained = len(retained_pending)
                    append_captured_items(retained_pending)
                    retained_speech_levels = [
                        level
                        for _, level, _ in retained_pending
                        if level >= thresholds.speech_continue_rms
                    ]
                    speech_levels.extend(retained_speech_levels)
                    speech_frame_count += len(retained_speech_levels)
                    if retained_pending:
                        last_confirmed_speech_frame = max(
                            last_confirmed_speech_frame,
                            retained_pending[-1][2],
                        )
                    stop_reason = VAD_STATUS_COMPLETED_AFTER_SILENCE
                    silence_at_stop = consecutive_below_silence * frame_seconds
                    terminal_quiet_frame_count = consecutive_below_silence
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
                silence_at_stop = consecutive_below_silence * frame_seconds
                terminal_quiet_frame_count = consecutive_below_silence
                retained_at_maximum = list(pending_silence)
                if wake_short_profile and pending_silence:
                    retained_at_maximum = (
                        pending_silence[-speech_end_padding_frames:]
                        if speech_end_padding_frames
                        else []
                    )
                    trailing_silence_frames_trimmed = max(
                        0,
                        len(pending_silence) - len(retained_at_maximum),
                    )
                    post_roll_frames_retained = len(retained_at_maximum)
                append_captured_items(retained_at_maximum)
                retained_speech_levels = [
                    level
                    for _, level, _ in retained_at_maximum
                    if level >= thresholds.speech_continue_rms
                ]
                speech_levels.extend(retained_speech_levels)
                speech_frame_count += len(retained_speech_levels)
                pending_silence.clear()
                break

        if (
            wake_short_profile
            and speech_frame_count * frame_seconds < minimum_speech_seconds
        ):
            return self._failure(
                request,
                VAD_STATUS_INVALID_AUDIO,
                "wake_speech_below_minimum_duration",
                started_at,
                speech_detected=False,
                frame_count=total_frames,
                levels=all_levels,
                ambient_levels=ambient_levels or calibration_levels,
                ambient_statistics=ambient_statistics,
                thresholds=thresholds,
                data={
                    **observability_data(),
                    "transitions": transitions,
                    "rms_trace": rms_trace,
                    "speech_frame_count": speech_frame_count,
                    "minimum_speech_duration_seconds": minimum_speech_seconds,
                    "capture_failure_stage": "speech_candidate_assembly_failed",
                },
            )

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
                data={
                    **observability_data(),
                    "capture_failure_stage": "speech_candidate_assembly_failed",
                },
            )

        assembled_frame_count = len(captured)
        assembled_sample_count = assembled_frame_count * samples_per_frame
        assembled_byte_count = len(pcm)
        expected_assembled_bytes = (
            assembled_sample_count * request.channels * request.sample_width_bytes
        )
        if assembled_byte_count != expected_assembled_bytes:
            return self._failure(
                request,
                VAD_STATUS_INVALID_AUDIO,
                "assembled_pcm_byte_count_mismatch",
                started_at,
                speech_detected=True,
                frame_count=total_frames,
                levels=all_levels,
            )
        possible_silence_frames_retained = max(
            0,
            assembled_frame_count - pre_roll_frames_retained - speech_frame_count,
        )
        assembled_duration_seconds = assembled_sample_count / request.sample_rate_hz
        untrimmed_duration_seconds = (
            assembled_frame_count + trailing_silence_frames_trimmed
        ) * frame_seconds

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
            trailing_silence_frame_count=trailing_silence_frames_trimmed,
            selected_device=request.microphone_device,
            assembled_wav_path=str(wav_path),
            normalized_wav_path=str(wav_path),
            raw_duration_seconds=round(total_frames * frame_seconds, 6),
            untrimmed_duration_seconds=round(untrimmed_duration_seconds, 6),
            assembled_duration_seconds=round(assembled_duration_seconds, 6),
            normalized_duration_seconds=float(wav.get("duration_seconds", 0.0)),
            leading_silence_trimmed_seconds=round(
                leading_silence_frames_trimmed * frame_seconds,
                6,
            ),
            trailing_silence_trimmed_seconds=round(
                trailing_silence_frames_trimmed * frame_seconds,
                6,
            ),
            total_frames_read=total_frames,
            total_raw_samples=total_frames * samples_per_frame,
            raw_byte_count=total_frames * frame_bytes,
            pre_roll_frames_retained=pre_roll_frames_retained,
            speech_frames_retained=speech_frame_count,
            possible_silence_frames_retained=possible_silence_frames_retained,
            final_assembled_frame_count=assembled_frame_count,
            final_assembled_sample_count=assembled_sample_count,
            final_assembled_byte_count=assembled_byte_count,
            normalized_sample_count=int(wav.get("frames", 0)),
            normalized_byte_count=max(0, int(wav.get("byte_count", 0)) - 44),
            whisper_input_duration_seconds=float(wav.get("duration_seconds", 0.0)),
            duration_invariant_status="assembled_only",
            final_whisper_input_path=str(wav_path),
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
                **observability_data(),
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
                "speech_end_padding_frames": speech_end_padding_frames,
                "pre_roll_frames_retained": pre_roll_frames_retained,
                "speech_frames_retained": speech_frame_count,
                "possible_silence_frames_retained": possible_silence_frames_retained,
                "trailing_silence_frames_trimmed": trailing_silence_frames_trimmed,
                "terminal_quiet_frame_count": terminal_quiet_frame_count,
                "post_roll_frames_retained": post_roll_frames_retained,
                "duplicate_frame_append_count": duplicate_frame_append_count,
                "captured_frame_indexes_unique": (
                    len(captured_frame_indexes) == len(set(captured_frame_indexes))
                ),
                "leading_silence_frames_trimmed": leading_silence_frames_trimmed,
                "total_frames_read": total_frames,
                "total_raw_samples": total_frames * samples_per_frame,
                "raw_byte_count": total_frames * frame_bytes,
                "final_assembled_frame_count": assembled_frame_count,
                "final_assembled_sample_count": assembled_sample_count,
                "final_assembled_byte_count": assembled_byte_count,
                "untrimmed_duration_seconds": round(untrimmed_duration_seconds, 6),
                "assembled_duration_seconds": round(assembled_duration_seconds, 6),
                "leading_silence_trimmed_seconds": round(
                    leading_silence_frames_trimmed * frame_seconds,
                    6,
                ),
                "trailing_silence_trimmed_seconds": round(
                    trailing_silence_frames_trimmed * frame_seconds,
                    6,
                ),
                "terminal_silence_trimmed": True,
                "transitions": transitions,
                "rms_trace": rms_trace,
                "vad_profile": (
                    "standby_wake_short_v1" if wake_short_profile else "default"
                ),
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


def _pcm_source_snapshot(frame_source: Any) -> Dict[str, Any]:
    snapshot = getattr(frame_source, "snapshot", None)
    if not callable(snapshot):
        return {}
    try:
        value = snapshot()
    except (OSError, RuntimeError, TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _enrich_frame_trace(
    frame_trace: list[Dict[str, Any]],
    transitions: list[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    transition_by_frame = {
        int(item.get("frame", 0) or 0): item
        for item in transitions
        if isinstance(item, dict)
    }
    enriched: list[Dict[str, Any]] = []
    for item in frame_trace:
        current = dict(item)
        transition = transition_by_frame.get(int(current.get("frame", 0) or 0))
        if transition is not None:
            source = str(transition.get("from", "") or "")
            target = str(transition.get("to", "") or "")
            current["state_after"] = target or current.get("state_before", "")
            current["vad_state_transition"] = (
                f"{source}->{target}" if source and target else "none"
            )
        enriched.append(current)
    return enriched


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
    if not 0.0 <= request.speech_end_padding_seconds <= request.silence_duration_seconds:
        raise ValueError("speech_end_padding_seconds_out_of_range")
    if not 0.01 <= request.frame_read_timeout_seconds <= 30.0:
        raise ValueError("frame_read_timeout_seconds_out_of_range")
    if not 0.0 <= request.duration_loss_tolerance_seconds <= 2.0:
        raise ValueError("duration_loss_tolerance_seconds_out_of_range")
    if not str(request.output_wav_path or "").lower().endswith(".wav"):
        raise ValueError("voice_activity_output_path_must_be_wav")
    return request


def _metadata_number(
    metadata: Dict[str, Any],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = metadata.get(name, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError(f"{name}_out_of_range")
    return float(value)


def _metadata_integer(
    metadata: Dict[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = metadata.get(name, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name}_out_of_range")
    return int(value)


def _calibration_diagnostics(
    levels: list[float],
    frame_seconds: float,
    analysis: Any,
    *,
    summary_interval: int,
) -> VoiceActivityCalibrationDiagnostics:
    summaries: list[Dict[str, float | int]] = []
    for start in range(0, len(levels), summary_interval):
        chunk = levels[start : start + summary_interval]
        summaries.append(
            {
                "first_frame": start + 1,
                "last_frame": start + len(chunk),
                "minimum_rms": round(min(chunk), 6),
                "mean_rms": round(sum(chunk) / len(chunk), 6),
                "maximum_rms": round(max(chunk), 6),
            }
        )
    statistics = analysis.statistics
    return VoiceActivityCalibrationDiagnostics(
        frame_count=len(levels),
        frame_duration_seconds=round(frame_seconds, 6),
        minimum_rms=analysis.minimum_rms,
        median_rms=statistics.median_rms,
        percentile_20_rms=analysis.percentile_20_rms,
        percentile_80_rms=analysis.percentile_80_rms,
        maximum_rms=analysis.maximum_rms,
        speech_frame_count=analysis.speech_frame_count,
        non_speech_frame_count=analysis.non_speech_frame_count,
        longest_non_speech_sequence=analysis.longest_non_speech_sequence,
        bootstrap_threshold_rms=analysis.bootstrap_threshold_rms,
        selected_noise_floor_rms=statistics.noise_floor_rms,
        quiet_sample_count=analysis.quiet_sample_count,
        quiet_sample_fraction=analysis.quiet_sample_fraction,
        clipped_frame_count=analysis.clipped_frame_count,
        clipped_frame_fraction=analysis.clipped_frame_fraction,
        zero_frame_count=analysis.zero_frame_count,
        quality_passed=analysis.success,
        quality_reason=analysis.quality_reason,
        rms_summary=tuple(summaries),
    )


def _calibration_quality_message(reason: str) -> str:
    return {
        "calibration_all_zero_pcm": "standby calibration received only zero PCM",
        "calibration_pcm_clipped": "standby calibration PCM was severely clipped",
        "calibration_speech_dominated": (
            "standby calibration was dominated by speech-like energy"
        ),
        "calibration_quiet_samples_insufficient": (
            "standby calibration did not contain enough low-energy samples"
        ),
        "calibration_noise_floor_unusable": (
            "standby calibration noise floor exceeded the configured usable limit"
        ),
    }.get(reason, "standby calibration quality policy rejected the PCM sample")


def _write_pcm_wav_atomic(request: VoiceActivityCaptureRequestV1, pcm: bytes) -> Path:
    output = Path(request.output_wav_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=str(output.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with wave.open(str(temporary), "wb") as wav_file:
            wav_file.setnchannels(request.channels)
            wav_file.setsampwidth(request.sample_width_bytes)
            wav_file.setframerate(request.sample_rate_hz)
            wav_file.writeframes(pcm)
        validation = analyze_wav_audio(temporary)
        if not validation.get("success"):
            raise ValueError(str(validation.get("error_message") or "invalid_temporary_wav"))
        os.replace(temporary, output)
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
