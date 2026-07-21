from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import shutil
from threading import Lock, RLock
import tempfile
import time
from typing import Any, Callable, Dict, Optional
import wave

from core.AresIdentity import canonicalize_ares_name_tokens
from core.Contracts import (
    StandbyListenResultV1,
    VoiceActivityCaptureRequestV1,
    WakeListenerRequestV1,
    WakeListenerResultV1,
    WakeListenerSnapshotV1,
    WakeRecognizerRequestV1,
    new_correlation_id,
)
from core.StandbyWakeListener import (
    WAKE_CATEGORY_NON_WAKE,
    WAKE_LISTENER_CANCELLING,
    WAKE_LISTENER_ERROR,
    WAKE_LISTENER_LISTENING,
    WAKE_LISTENER_READY,
    WAKE_LISTENER_STOPPED,
    WAKE_STATUS_CANCELLED,
    WAKE_STATUS_FAILED,
    WAKE_STATUS_NO_SPEECH,
    WAKE_STATUS_NON_WAKE_SPEECH,
    WakeAttemptResult,
    WakeLocalDiagnostics,
    WakeListenerConfig,
    clean_wake_transcript,
    normalize_wake_phrase,
)
from core.WakeRecognizer import WakeRecognitionAttempt, WakeRecognizerLocalDiagnostics
from core.WakeAudio import WakeAudioTrimResult, trim_canonical_wake_wav
from core.VoiceActivityDetection import (
    VAD_STATUS_CANCELLED,
    VAD_STATUS_DEVICE_ERROR,
    VAD_STATUS_NO_SPEECH_TIMEOUT,
    VAD_STATUS_TIMEOUT,
)


STANDBY_STREAM_OWNER = "standby_wake_listener"
STREAM_CLOSED = "CLOSED"
STREAM_OPENING = "OPENING"
STREAM_CALIBRATING = "CALIBRATING"
STREAM_HEALTHY = "HEALTHY"
STREAM_FAILED = "FAILED"


@dataclass
class _WakeAttemptContext:
    attempt_id: str
    candidate_id: str
    candidate_number: int
    lifecycle_state_before: str
    stream_generation: int = 0
    stream_instance_id: str = ""
    capture_valid: bool = False
    recognizer_invoked: bool = False
    infrastructure_failure: bool = False
    recognizer_input_path: str = ""
    recognition_diagnostics: Optional[WakeRecognizerLocalDiagnostics] = None
    cleanup_status: str = "not_required"


class LinuxStandbyWakeListener:
    """Foreground ALSA/VAD capture followed by constrained local wake recognition."""

    def __init__(
        self,
        *,
        microphone_adapter: Any,
        wake_recognizer: Any,
        config: Optional[WakeListenerConfig | Dict[str, Any]] = None,
        project_root: Optional[str | Path] = None,
        voice_io_gate: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        diagnostic_callback: Optional[Callable[[WakeLocalDiagnostics], None]] = None,
    ) -> None:
        required_microphone_methods = (
            "open_persistent_stream",
            "calibrate_persistent_stream",
            "record_persistent_until_silence",
            "close_persistent_stream",
        )
        missing = [
            name
            for name in required_microphone_methods
            if not callable(getattr(microphone_adapter, name, None))
        ]
        if missing:
            raise ValueError(
                "microphone_adapter must support persistent standby capture: "
                + ", ".join(missing)
            )
        if not callable(getattr(wake_recognizer, "recognize_wav", None)):
            raise ValueError("wake_recognizer must support recognize_wav")
        self.microphone_adapter = microphone_adapter
        self.wake_recognizer = wake_recognizer
        self.config = WakeListenerConfig.from_mapping(config)
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.voice_io_gate = voice_io_gate
        self.clock = clock
        self.sleeper = sleeper
        self.diagnostic_callback = diagnostic_callback
        self._lock = RLock()
        self._attempt_lock = Lock()
        self._state = WAKE_LISTENER_STOPPED
        self._runtime_id = ""
        self._cancelled = False
        self._listen_count = 0
        self._speech_count = 0
        self._wake_count = 0
        self._failure_count = 0
        self._last_stop_reason = ""
        self._last_cleanup_status = "not_required"
        self._active_turn_directory: Optional[Path] = None
        self._retained_directories: list[str] = []
        self._stream_handle: Any = None
        self._stream_open_count = 0
        self._stream_close_count = 0
        self._calibration_count = 0
        self._calibration_attempt_count = 0
        self._candidate_count = 0
        self._stream_generation = 0
        self._stream_state = STREAM_CLOSED
        self._calibration_at: Optional[float] = None
        self._calibration_thresholds: Any = None
        self._calibration_statistics: Any = None
        self._last_calibration_diagnostics: Any = None
        self._calibration_attempt_summaries: list[Dict[str, Any]] = []
        self._last_calibration_error_code = ""
        self._last_calibration_error_message = ""
        self._last_alsa_open_succeeded = False
        self._last_alsa_closed_during_cleanup = False
        self._last_valid_pcm_received = False
        self._recalibration_requested = False
        self._capture_gate_owned = False
        self._last_stream_instance_id = ""
        self._last_alsa_handle_id = ""
        self._last_open_reason = ""
        self._last_close_reason = ""
        self._last_calibration_reason = ""
        self._last_handoff_source = ""
        self._last_handoff_destination = ""
        self._stream_open_reasons: list[str] = []
        self._stream_close_reasons: list[str] = []
        self._calibration_reasons: list[str] = []
        self._ownership_handoffs: list[str] = []
        self._last_candidate_stale_frames = 0
        self._prepared_prompt_stale_frames = 0
        self.last_result: Optional[StandbyListenResultV1] = None
        self.last_diagnostics: Optional[WakeLocalDiagnostics] = None
        self.last_attempt: Optional[WakeAttemptResult] = None

    def start(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        with self._lock:
            self._runtime_id = str(runtime_id or self._runtime_id)
            if self._state == WAKE_LISTENER_READY:
                already_started = True
            else:
                already_started = False
            if not self.config.enabled:
                return self._result(False, "disabled", "wake_listener_disabled", "wake listener is disabled")
            self._cancelled = False
        if already_started:
            entered = self.enter_standby(
                runtime_id=self._runtime_id,
                reason="listener_start_idempotent",
            )
            return self._result(
                entered.success,
                "already_started" if entered.success else entered.status,
                entered.error_code,
                entered.error_message,
                data=self._stream_metrics(),
            )
        recognizer = _safe_call(self.wake_recognizer, "start")
        if not _result_success(recognizer):
            return self._start_failure(
                "wake_recognizer_start_failed",
                _result_error(recognizer),
            )
        microphone = _safe_call(self.microphone_adapter, "start")
        if not _result_success(microphone):
            _safe_call(self.wake_recognizer, "stop")
            return self._start_failure("microphone_start_failed", _result_error(microphone))
        health = self._dependency_health(runtime_id=self._runtime_id)
        if not health.success:
            _safe_call(self.microphone_adapter, "stop")
            _safe_call(self.wake_recognizer, "stop")
            return health
        with self._lock:
            self._state = WAKE_LISTENER_READY
        entered = self.enter_standby(
            runtime_id=self._runtime_id,
            reason="listener_start",
        )
        if not entered.success:
            _safe_call(self.microphone_adapter, "stop")
            _safe_call(self.wake_recognizer, "stop")
            with self._lock:
                self._state = WAKE_LISTENER_ERROR
            return entered
        health = self.health(runtime_id=self._runtime_id)
        if not health.success:
            self._close_standby_stream("post_start_health_failed", final_state=STREAM_FAILED)
            _safe_call(self.microphone_adapter, "stop")
            _safe_call(self.wake_recognizer, "stop")
            return health
        return self._result(True, "started", data=health.data)

    def health(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        dependencies = self._dependency_health(runtime_id=runtime_id)
        if not dependencies.success:
            return dependencies
        stream = self._stream_metrics()
        stream_healthy = bool(
            stream["stream_state"] == STREAM_HEALTHY
            and stream["stream_active"]
            and stream["calibration_healthy"]
        )
        if not stream_healthy:
            return self._health_failure(
                "standby_stream_unhealthy",
                "standby microphone stream is not open and calibrated",
                data={
                    **dependencies.data,
                    **stream,
                    "failing_subsystem": (
                        "standby_calibration"
                        if stream.get("calibration_error_code")
                        else "standby_stream"
                    ),
                },
            )
        return self._result(
            True,
            "healthy",
            data={
                **dependencies.data,
                **stream,
                "wake_model_healthy": True,
                "microphone_adapter_healthy": True,
                "alsa_device_open": True,
                "calibration_healthy": True,
                "failing_subsystem": "",
            },
        )

    def component_health(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        """Report model, adapter, stream, and calibration health independently."""

        dependencies = self._dependency_health(runtime_id=runtime_id)
        stream = self._stream_metrics()
        return self._result(
            dependencies.success and stream["stream_state"] == STREAM_HEALTHY,
            "healthy" if dependencies.success and stream["stream_state"] == STREAM_HEALTHY else "unhealthy",
            dependencies.error_code,
            dependencies.error_message,
            data={
                **dependencies.data,
                **stream,
                "wake_model_healthy": bool(
                    dependencies.data.get("wake_model_healthy", False)
                ),
                "microphone_adapter_healthy": bool(
                    dependencies.data.get("microphone_adapter_healthy", False)
                ),
                "alsa_device_open": bool(stream["stream_active"]),
                "calibration_healthy": bool(stream["calibration_healthy"]),
                "failing_subsystem": (
                    dependencies.data.get("failing_subsystem", "")
                    or (
                        "standby_calibration"
                        if stream.get("calibration_error_code")
                        else (
                            "standby_stream"
                            if stream["stream_state"] != STREAM_HEALTHY
                            else ""
                        )
                    )
                ),
            },
        )

    def _dependency_health(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        with self._lock:
            if runtime_id:
                self._runtime_id = str(runtime_id)
        microphone = _safe_call(self.microphone_adapter, "health_check")
        recognizer = _safe_call(self.wake_recognizer, "health_check")
        microphone_healthy = _result_success(microphone)
        recognizer_healthy = _result_success(recognizer)
        health_data = {
            "microphone_status": _result_status(microphone),
            "recognizer_status": _result_status(recognizer),
            "wake_model_healthy": recognizer_healthy,
            "microphone_adapter_healthy": microphone_healthy,
            "recognizer_error_code": str(
                getattr(recognizer, "error_code", "") or ""
            ),
            "microphone_error_code": str(
                getattr(microphone, "error_code", "") or ""
            ),
        }
        if not recognizer_healthy:
            return self._health_failure(
                "wake_recognizer_unhealthy",
                _result_error(recognizer),
                data={
                    **health_data,
                    "failing_subsystem": "wake_recognizer_model",
                },
            )
        if not microphone_healthy:
            return self._health_failure(
                "microphone_unhealthy",
                _result_error(microphone),
                data={
                    **health_data,
                    "failing_subsystem": "microphone_adapter",
                },
            )
        return self._result(
            True,
            "dependencies_healthy",
            data={
                **health_data,
                "failing_subsystem": "",
                "recognizer_name": str(
                    getattr(self.wake_recognizer, "recognizer_name", "")
                    or "wake_recognizer"
                ),
                "offline": True,
                "background_thread": False,
            },
        )

    def enter_standby(
        self,
        *,
        runtime_id: str = "",
        reason: str = "standby_entered",
        handoff_source: str = "",
    ) -> WakeListenerResultV1:
        """Acquire one continuous ALSA stream for the current standby epoch."""

        with self._lock:
            if runtime_id:
                self._runtime_id = str(runtime_id)
            if self._state == WAKE_LISTENER_STOPPED:
                return self._result(
                    False,
                    "listener_not_started",
                    "wake_listener_not_started",
                    "wake listener must be started before entering standby",
                )
            if self._cancelled:
                return self._result(
                    False,
                    "cancelled",
                    "wake_listener_cancelled",
                    "wake listener is cancelled",
                )
            if self._stream_handle is not None and not bool(
                getattr(self._stream_handle, "closed", False)
            ):
                return self._result(
                    True,
                    "standby_stream_already_open",
                    data=self._stream_metrics(),
                )
        gate_error = self._begin_capture_gate(self.config.speech_wait_timeout_seconds)
        if gate_error:
            return self._result(
                False,
                "capture_gate_unavailable",
                "capture_gate_unavailable",
                gate_error,
            )
        with self._lock:
            self._capture_gate_owned = True
            self._stream_state = STREAM_OPENING
            self._last_alsa_open_succeeded = False
            self._last_alsa_closed_during_cleanup = False
        self._reset_recognizer_attempt_state("standby_stream_opening")
        try:
            open_reason = str(reason or "standby_entered")[:80]
            handle = self.microphone_adapter.open_persistent_stream(
                owner=STANDBY_STREAM_OWNER,
                device=self.config.microphone_device,
            )
            with self._lock:
                self._stream_handle = handle
                self._last_alsa_open_succeeded = True
                self._last_alsa_closed_during_cleanup = False
                self._stream_open_count += 1
                self._stream_generation += 1
                self._stream_state = STREAM_CALIBRATING
                self._last_stream_instance_id = str(
                    getattr(handle, "stream_id", "") or ""
                )[:96]
                self._last_alsa_handle_id = str(
                    getattr(handle, "alsa_handle_id", "")
                    or f"{self._last_stream_instance_id}-handle"
                )[:96]
                self._last_open_reason = open_reason
                _append_bounded(self._stream_open_reasons, open_reason)
                if handoff_source:
                    self._record_handoff_locked(
                        str(handoff_source)[:64],
                        STANDBY_STREAM_OWNER,
                        open_reason,
                    )
            calibration = self._calibrate_stream(
                handle,
                reason=f"{open_reason}:initial_calibration",
            )
            if not bool(getattr(calibration, "success", False)):
                self._reset_recognizer_attempt_state("calibration_failed")
                self._close_standby_stream(
                    "calibration_failed",
                    final_state=STREAM_FAILED,
                )
                return self._result(
                    False,
                    "calibration_failed",
                    str(getattr(calibration, "error_code", "") or "wake_calibration_failed"),
                    str(
                        getattr(calibration, "error_message", "")
                        or getattr(calibration, "status", "")
                        or "wake calibration failed"
                    ),
                    data={
                        **self._stream_metrics(),
                        "wake_model_healthy": True,
                        "microphone_adapter_healthy": True,
                        "alsa_device_open": False,
                        "alsa_device_open_attempt_succeeded": True,
                        "alsa_device_closed_during_cleanup": True,
                        "calibration_healthy": False,
                        "failing_subsystem": "standby_calibration",
                    },
                )
            with self._lock:
                self._stream_state = STREAM_HEALTHY
                self._state = WAKE_LISTENER_READY
            return self._result(
                True,
                "standby_stream_ready",
                data=self._stream_metrics(),
            )
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            self._reset_recognizer_attempt_state("stream_open_failed")
            self._close_standby_stream(
                "stream_open_failed",
                final_state=STREAM_FAILED,
            )
            return self._result(
                False,
                "stream_open_failed",
                "wake_stream_open_failed",
                f"{error.__class__.__name__}:{str(error)[:120]}",
                data={
                    **self._stream_metrics(),
                    "wake_model_healthy": True,
                    "microphone_adapter_healthy": True,
                    "alsa_device_open": False,
                    "calibration_healthy": False,
                    "failing_subsystem": "alsa_stream_open",
                },
            )

    def leave_standby(
        self,
        reason: str = "leaving_standby",
        *,
        handoff_destination: str = "active_command",
    ) -> WakeListenerResultV1:
        closed = self._close_standby_stream(
            reason,
            handoff_source=STANDBY_STREAM_OWNER,
            handoff_destination=handoff_destination,
        )
        return self._result(
            closed,
            "standby_stream_closed" if closed else "standby_stream_close_failed",
            "" if closed else "wake_stream_close_failed",
            "" if closed else "persistent standby stream did not close cleanly",
            data=self._stream_metrics(),
        )

    def request_recalibration(self) -> WakeListenerResultV1:
        with self._lock:
            self._recalibration_requested = True
        return self._result(True, "recalibration_requested")

    def prepare_for_owner_prompt(self) -> WakeListenerResultV1:
        """Discard only audio buffered before an owner-facing ready prompt."""

        with self._attempt_lock:
            with self._lock:
                handle = self._stream_handle
                stream_state = self._stream_state
            if (
                handle is None
                or bool(getattr(handle, "closed", False))
                or stream_state != STREAM_HEALTHY
            ):
                return self._result(
                    False,
                    "standby_stream_not_ready",
                    "wake_stream_not_ready_for_prompt",
                    "standby stream must be healthy before an owner prompt",
                    data=self._stream_metrics(),
                )
            stale_frames = self._reset_candidate_stream(handle)
            with self._lock:
                self._prepared_prompt_stale_frames = stale_frames
            return self._result(
                True,
                "owner_prompt_ready",
                data=self._stream_metrics(),
            )

    def listen_once(self, request: WakeListenerRequestV1) -> StandbyListenResultV1:
        return self.listen_attempt(request).result

    def listen_attempt(self, request: WakeListenerRequestV1) -> WakeAttemptResult:
        with self._attempt_lock:
            return self._listen_attempt_serialized(request)

    def _listen_attempt_serialized(
        self,
        request: WakeListenerRequestV1,
    ) -> WakeAttemptResult:
        if not isinstance(request, WakeListenerRequestV1):
            raise TypeError("request must be WakeListenerRequestV1")
        with self._lock:
            self._last_cleanup_status = "not_required"
            self._last_candidate_stale_frames = self._prepared_prompt_stale_frames
            self._prepared_prompt_stale_frames = 0
            self._listen_count += 1
            self._candidate_count += 1
            candidate_number = self._candidate_count
            generation = self._stream_generation
            stream_instance_id = (
                str(getattr(self._stream_handle, "stream_id", "") or "")
                if self._stream_handle is not None
                else self._last_stream_instance_id
            )
        attempt_id = new_correlation_id("wake-attempt")
        context = _WakeAttemptContext(
            attempt_id=attempt_id,
            candidate_id=f"wake-candidate-{candidate_number}-{attempt_id[-12:]}",
            candidate_number=candidate_number,
            lifecycle_state_before=request.lifecycle_state,
            stream_generation=generation,
            stream_instance_id=stream_instance_id,
        )
        try:
            result = self._listen_once_unfinalized(request, context)
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            context.infrastructure_failure = True
            self._reset_recognizer_attempt_state("wake_listener_exception")
            result = self._listen_failure(
                request,
                "wake_listener_exception",
                f"{error.__class__.__name__}:{str(error)[:120]}",
                context=context,
            )
        cleanup_status = context.cleanup_status
        result = replace(
            result,
            cleanup_status=cleanup_status,
            attempt_id=context.attempt_id,
            candidate_id=context.candidate_id,
            stream_generation=context.stream_generation,
            candidate_number=context.candidate_number,
            stream_instance_id=context.stream_instance_id,
            capture_valid=context.capture_valid,
            recognizer_invoked=context.recognizer_invoked,
            infrastructure_failure=context.infrastructure_failure,
        )
        diagnostics = None
        if bool(request.diagnostic_wake or self.config.diagnostic_wake):
            diagnostics = self._build_local_diagnostics(
                request=request,
                result=result,
                recognition=context.recognition_diagnostics,
                recognizer_input_path=context.recognizer_input_path,
            )
        attempt = WakeAttemptResult(
            attempt_id=context.attempt_id,
            candidate_id=context.candidate_id,
            stream_instance_id=result.stream_instance_id,
            stream_generation=context.stream_generation,
            candidate_number=context.candidate_number,
            capture_valid=context.capture_valid,
            recognizer_invoked=context.recognizer_invoked,
            infrastructure_failure=context.infrastructure_failure,
            lifecycle_state_before=request.lifecycle_state,
            lifecycle_state_after=request.lifecycle_state,
            cleanup_status=cleanup_status,
            result=result,
            diagnostics=diagnostics,
        )
        with self._lock:
            self.last_attempt = attempt
            self.last_result = result
            self.last_diagnostics = diagnostics
        if diagnostics is not None and callable(self.diagnostic_callback):
            try:
                self.diagnostic_callback(diagnostics)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        return attempt

    def completed_attempt(self, attempt_id: str) -> Optional[WakeAttemptResult]:
        """Return only the exact immutable attempt requested; never a fallback."""

        normalized = str(attempt_id or "").strip()
        with self._lock:
            attempt = self.last_attempt
        if not normalized or attempt is None or attempt.attempt_id != normalized:
            return None
        return attempt

    def complete_attempt_lifecycle(
        self,
        attempt_id: str,
        lifecycle_state_after: str,
    ) -> Optional[WakeAttemptResult]:
        """Attach the external lifecycle outcome to one exact completed attempt."""

        normalized = str(attempt_id or "").strip()
        lifecycle_after = str(lifecycle_state_after or "").strip()
        if not normalized or not lifecycle_after:
            return None
        with self._lock:
            current = self.last_attempt
            if current is None or current.attempt_id != normalized:
                return None
            diagnostics = current.diagnostics
            if diagnostics is not None:
                diagnostics = replace(diagnostics, lifecycle_state=lifecycle_after)
            completed = replace(
                current,
                lifecycle_state_after=lifecycle_after,
                diagnostics=diagnostics,
            )
            self.last_attempt = completed
            self.last_diagnostics = diagnostics
            return completed

    def _listen_once_unfinalized(
        self,
        request: WakeListenerRequestV1,
        context: _WakeAttemptContext,
    ) -> StandbyListenResultV1:
        if request.retain_diagnostic_audio and not (
            request.diagnostic_wake or self.config.diagnostic_wake
        ):
            return self._listen_failure(
                request,
                "wake_diagnostic_retention_not_authorized",
                "wake diagnostic retention requires diagnostic wake mode",
                context=context,
            )
        with self._lock:
            if self._state == WAKE_LISTENER_STOPPED:
                context.infrastructure_failure = True
                return self._listen_failure(
                    request,
                    "listener_not_started",
                    "wake listener is stopped",
                    context=context,
                )
            if self._cancelled:
                return self._cancelled_result(request, context=context)
            self._state = WAKE_LISTENER_LISTENING
        started_at = self.clock()
        turn_directory = self._create_turn_directory()
        output_path = turn_directory / "wake_candidate.wav"
        with self._lock:
            self._active_turn_directory = turn_directory
        try:
            stream_error = self._ensure_standby_stream(request.runtime_id)
            with self._lock:
                context.stream_generation = self._stream_generation
                context.stream_instance_id = str(
                    getattr(self._stream_handle, "stream_id", "")
                    or self._last_stream_instance_id
                )
            if stream_error:
                context.infrastructure_failure = True
                self._reset_recognizer_attempt_state("wake_stream_unavailable")
                return self._listen_failure(
                    request,
                    "wake_stream_unavailable",
                    stream_error,
                    context=context,
                )
            with self._lock:
                handle = self._stream_handle
                thresholds = self._calibration_thresholds
                stream_state = self._stream_state
            if handle is None or thresholds is None or stream_state != STREAM_HEALTHY:
                context.infrastructure_failure = True
                self._reset_recognizer_attempt_state("stream_not_calibrated")
                return self._listen_failure(
                    request,
                    "wake_stream_not_calibrated",
                    "standby stream is not healthy and calibrated",
                    context=context,
                )
            capture = self.microphone_adapter.record_persistent_until_silence(
                handle,
                output_path,
                device=request.microphone_device or self.config.microphone_device,
                calibration_enabled=False,
                calibration_duration_seconds=0.0,
                speech_start_rms=float(thresholds.speech_start_rms),
                speech_continue_rms=float(thresholds.speech_continue_rms),
                silence_rms=float(thresholds.silence_rms),
                required_speech_frames=self.config.required_speech_frames,
                required_continue_frames=self.config.required_continue_frames,
                required_silence_frames=self.config.required_silence_frames,
                silence_seconds=self.config.silence_duration_seconds,
                speech_wait_timeout_seconds=min(
                    self.config.speech_wait_timeout_seconds,
                    max(0.1, float(request.listener_timeout_seconds)),
                ),
                maximum_utterance_seconds=self.config.maximum_utterance_seconds,
                pre_roll_seconds=self.config.pre_roll_seconds,
                speech_end_padding_seconds=self.config.speech_end_padding_seconds,
                frame_duration_ms=self.config.frame_duration_ms,
                frame_read_timeout_seconds=self.config.frame_read_timeout_seconds,
                minimum_speech_start_rms=self.config.minimum_speech_start_rms,
                maximum_speech_start_rms=self.config.maximum_speech_start_rms,
                minimum_speech_continue_rms=self.config.minimum_speech_continue_rms,
                maximum_speech_continue_rms=self.config.maximum_speech_continue_rms,
                minimum_silence_rms=self.config.minimum_silence_rms,
                maximum_silence_rms=self.config.maximum_silence_rms,
                frame_debug_enabled=bool(
                    request.diagnostic_wake or self.config.diagnostic_wake
                ),
                capture_profile="standby_wake_short_v1",
                minimum_speech_duration_seconds=(
                    self.config.minimum_speech_duration_seconds
                ),
                diagnostic_rms_interval_frames=(
                    self.config.diagnostic_rms_interval_frames
                ),
                diagnostic_audio=bool(
                    request.retain_diagnostic_audio or self.config.retain_diagnostic_audio
                ),
                cancel_requested=self._is_cancelled,
                correlation_id=request.correlation_id,
                session_id=request.session_id,
            )
            if self._is_cancelled() or str(getattr(capture, "status", "")) == VAD_STATUS_CANCELLED:
                self._reset_recognizer_attempt_state("capture_cancelled")
                return self._cancelled_result(request, context=context)
            capture_status = str(getattr(capture, "status", "") or "")
            if capture_status == VAD_STATUS_NO_SPEECH_TIMEOUT and not bool(
                getattr(capture, "speech_detected", False)
            ):
                self._reset_recognizer_attempt_state("no_speech")
                return self._standby_result(
                    request,
                    success=True,
                    status=WAKE_STATUS_NO_SPEECH,
                    stop_reason=capture_status,
                    capture=capture,
                    started_at=started_at,
                    context=context,
                )
            if not _result_success(capture) or not bool(getattr(capture, "speech_detected", False)):
                context.infrastructure_failure = capture_status != VAD_STATUS_NO_SPEECH_TIMEOUT
                self._reset_recognizer_attempt_state(capture_status or "capture_failed")
                if capture_status in {VAD_STATUS_DEVICE_ERROR, VAD_STATUS_TIMEOUT}:
                    self._close_standby_stream(
                        capture_status,
                        handoff_source=STANDBY_STREAM_OWNER,
                        handoff_destination="device_recovery",
                        final_state=STREAM_FAILED,
                    )
                return self._capture_failure(
                    request,
                    capture,
                    started_at,
                    context=context,
                )
            with self._lock:
                self._speech_count += 1
            self._reset_candidate_stream(handle)
            wav_path = str(
                getattr(capture, "normalized_wav_path", "")
                or getattr(capture, "final_whisper_input_path", "")
                or getattr(capture, "wav_path", "")
            )
            if not wav_path or not Path(wav_path).is_file():
                context.infrastructure_failure = True
                self._reset_recognizer_attempt_state("wake_audio_missing")
                return self._listen_failure(
                    request,
                    "wake_audio_missing",
                    "validated wake WAV is missing",
                    context=context,
                )
            duration_error = self._validate_capture_durations(
                capture,
                Path(wav_path),
                listener_timeout_seconds=request.listener_timeout_seconds,
            )
            if duration_error:
                context.infrastructure_failure = True
                self._reset_recognizer_attempt_state("wake_audio_duration_invalid")
                return self._standby_result(
                    request,
                    success=False,
                    status=WAKE_STATUS_FAILED,
                    stop_reason="wake_audio_duration_exceeded",
                    error_code="wake_audio_duration_exceeded",
                    error_message=duration_error,
                    capture=capture,
                    started_at=started_at,
                    context=context,
                )
            recognizer_input_path = turn_directory / "wake_recognizer_input.wav"
            try:
                trim = trim_canonical_wake_wav(
                    wav_path,
                    recognizer_input_path,
                    speech_threshold_rms=float(thresholds.speech_continue_rms),
                    frame_duration_ms=self.config.frame_duration_ms,
                    leading_padding_seconds=self.config.trim_leading_padding_seconds,
                    trailing_padding_seconds=self.config.trim_trailing_padding_seconds,
                )
            except (OSError, ValueError, wave.Error) as error:
                context.infrastructure_failure = True
                self._reset_recognizer_attempt_state("wake_audio_trim_failed")
                return self._standby_result(
                    request,
                    success=False,
                    status=WAKE_STATUS_FAILED,
                    stop_reason="wake_audio_trim_failed",
                    error_code="wake_audio_trim_failed",
                    error_message=f"{error.__class__.__name__}:{str(error)[:120]}",
                    capture=capture,
                    started_at=started_at,
                    context=context,
                )
            context.capture_valid = True
            context.recognizer_input_path = str(recognizer_input_path)
            recognizer_request = WakeRecognizerRequestV1(
                runtime_id=request.runtime_id,
                lifecycle_state=request.lifecycle_state,
                attempt_id=context.attempt_id,
                stream_generation=context.stream_generation,
                candidate_number=context.candidate_number,
                audio_path=str(recognizer_input_path),
                sample_rate_hz=16000,
                channels=1,
                sample_width_bytes=2,
                wake_phrases=list(request.wake_phrases or self.config.wake_phrases),
                wake_phrase_aliases=list(
                    request.wake_phrase_aliases or self.config.wake_phrase_aliases
                ),
                standby_phrases=list(request.standby_phrases),
                shutdown_phrases=list(request.shutdown_phrases),
                canonical_wake_phrase=self.config.wake_phrase_aliases[0],
                minimum_confidence=self.config.minimum_recognition_confidence,
                medium_confidence=self.config.medium_recognition_confidence,
                allow_exact_wake_without_confidence=(
                    self.config.allow_exact_wake_without_confidence
                ),
                validated_speech_candidate=True,
                medium_confirmation_repetitions=(
                    self.config.medium_confidence_confirmation_count
                ),
                medium_confirmation_window_seconds=(
                    self.config.medium_confidence_window_seconds
                ),
                timeout_seconds=max(0.1, float(request.listener_timeout_seconds)),
                audio_duration_seconds=trim.trimmed_duration_seconds,
                maximum_duplicate_collapse_audio_seconds=(
                    min(
                        self.config.maximum_duplicate_collapse_audio_seconds,
                        self.config.maximum_utterance_seconds
                        + self.config.pre_roll_seconds,
                    )
                ),
                correlation_id=request.correlation_id,
                metadata={"safe": True, "contains_transcript": False},
            )
            context.recognizer_invoked = True
            recognize_attempt = getattr(self.wake_recognizer, "recognize_attempt", None)
            if callable(recognize_attempt):
                recognized_attempt = recognize_attempt(recognizer_request)
                if not isinstance(recognized_attempt, WakeRecognitionAttempt):
                    raise TypeError("wake recognizer returned malformed attempt")
                recognition = recognized_attempt.result
                context.recognition_diagnostics = recognized_attempt.diagnostics
            else:
                self._reset_recognizer_attempt_state(
                    "recognition_started",
                    preserve_medium_confirmation=True,
                )
                recognition = self.wake_recognizer.recognize_wav(recognizer_request)
                local_diagnostics = getattr(
                    self.wake_recognizer,
                    "last_diagnostics",
                    None,
                )
                if isinstance(local_diagnostics, WakeRecognizerLocalDiagnostics):
                    local_attempt_id = str(local_diagnostics.attempt_id or "")
                    local_generation = int(local_diagnostics.stream_generation or 0)
                    local_candidate = int(local_diagnostics.candidate_number or 0)
                    if (
                        local_attempt_id not in {"", context.attempt_id}
                        or local_generation not in {0, context.stream_generation}
                        or local_candidate not in {0, context.candidate_number}
                    ):
                        local_diagnostics = None
                    else:
                        local_diagnostics = replace(
                            local_diagnostics,
                            attempt_id=context.attempt_id,
                            stream_generation=context.stream_generation,
                            candidate_number=context.candidate_number,
                        )
                context.recognition_diagnostics = local_diagnostics
                try:
                    setattr(self.wake_recognizer, "last_diagnostics", None)
                except (AttributeError, TypeError):
                    pass
            self._reset_candidate_stream(handle)
            with self._lock:
                current_generation = self._stream_generation
            recognition_generation = int(
                getattr(recognition, "stream_generation", 0) or 0
            )
            recognition_attempt_id = str(
                getattr(recognition, "attempt_id", "") or ""
            )
            if (
                current_generation != context.stream_generation
                or recognition_generation not in {0, context.stream_generation}
                or recognition_attempt_id not in {"", context.attempt_id}
            ):
                context.infrastructure_failure = True
                context.recognition_diagnostics = None
                self._reset_recognizer_attempt_state("stale_stream_generation_result")
                return self._standby_result(
                    request,
                    success=False,
                    status=WAKE_STATUS_FAILED,
                    stop_reason="stale_stream_generation_result",
                    error_code="stale_stream_generation_result",
                    error_message="recognition result did not match the capture attempt generation",
                    capture=capture,
                    started_at=started_at,
                    context=context,
                    trim=trim,
                )
            recognition = replace(
                recognition,
                attempt_id=context.attempt_id,
                stream_generation=context.stream_generation,
                candidate_number=context.candidate_number,
            )
            if not _result_success(recognition):
                context.infrastructure_failure = True
                return self._recognition_failure(
                    request,
                    recognition,
                    capture,
                    started_at,
                    trim=trim,
                    context=context,
                )
            if bool(getattr(recognition, "wake_detected", False)):
                with self._lock:
                    self._wake_count += 1
            return self._standby_result(
                request,
                success=bool(getattr(recognition, "success", False)),
                status=str(getattr(recognition, "status", "") or WAKE_STATUS_NON_WAKE_SPEECH),
                speech_detected=True,
                wake_detected=bool(getattr(recognition, "wake_detected", False)),
                command_category=str(getattr(recognition, "command_category", WAKE_CATEGORY_NON_WAKE)),
                normalized_wake_phrase=str(getattr(recognition, "normalized_wake_phrase", "")),
                matched_phrase=str(getattr(recognition, "matched_phrase", "")),
                selected_alias=str(getattr(recognition, "selected_alias", "")),
                selected_wake_phrase=str(getattr(recognition, "selected_wake_phrase", "")),
                canonical_wake_phrase=str(getattr(recognition, "canonical_wake_phrase", "")),
                classification_path=(
                    "vosk_constrained_grammar_duplicate_collapse"
                    if bool(getattr(recognition, "duplicate_collapse_used", False))
                    else "vosk_constrained_grammar_exact"
                ),
                classification_reason=str(getattr(recognition, "classification_reason", "")),
                rejection_reason=str(getattr(recognition, "rejection_reason", "")),
                stop_reason=str(getattr(recognition, "status", "") or WAKE_STATUS_NON_WAKE_SPEECH),
                capture=capture,
                started_at=started_at,
                recognition=recognition,
                trim=trim,
                data={
                    "recognition_status": _result_status(recognition),
                    "recognizer_name": str(getattr(recognition, "recognizer_name", "")),
                    "contains_transcript": False,
                },
                context=context,
            )
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            self._close_standby_stream(
                "capture_exception",
                handoff_source=STANDBY_STREAM_OWNER,
                handoff_destination="device_recovery",
                final_state=STREAM_FAILED,
            )
            context.infrastructure_failure = True
            self._reset_recognizer_attempt_state("capture_exception")
            return self._listen_failure(
                request,
                "wake_listener_exception",
                f"{error.__class__.__name__}:{str(error)[:120]}",
                context=context,
            )
        finally:
            retain = bool(request.retain_diagnostic_audio or self.config.retain_diagnostic_audio)
            if retain:
                cleanup_status = "retained_by_explicit_request"
                with self._lock:
                    self._retained_directories.append(str(turn_directory))
                    while len(self._retained_directories) > self.config.maximum_retained_candidates:
                        expired = Path(self._retained_directories.pop(0))
                        _remove_turn_directory(expired)
            else:
                cleanup_status = _remove_turn_directory(turn_directory)
            with self._lock:
                self._last_cleanup_status = cleanup_status
                self._active_turn_directory = None
                if self._state != WAKE_LISTENER_STOPPED:
                    self._state = (
                        WAKE_LISTENER_READY
                        if self._stream_state == STREAM_HEALTHY
                        else WAKE_LISTENER_ERROR
                    )
            context.cleanup_status = cleanup_status

    def cancel(self, reason: str = "cancelled") -> WakeListenerResultV1:
        with self._lock:
            self._cancelled = True
            self._state = WAKE_LISTENER_CANCELLING
            self._last_stop_reason = str(reason or "cancelled")[:80]
        self._close_standby_stream(
            reason,
            handoff_source=STANDBY_STREAM_OWNER,
            handoff_destination="stopped",
        )
        cancel = getattr(self.microphone_adapter, "cancel_current", None)
        if callable(cancel):
            try:
                cancel()
            except (OSError, RuntimeError):
                pass
        _safe_call(self.wake_recognizer, "cancel")
        return self._result(True, "cancelled", cleanup_status="capture_cancel_requested")

    def stop(self, reason: str = "stopped") -> WakeListenerResultV1:
        self.cancel(reason)
        stopped = _safe_call(self.microphone_adapter, "stop")
        recognizer_stopped = _safe_call(self.wake_recognizer, "stop")
        cleanup_success = _result_success(stopped) and _result_success(recognizer_stopped)
        with self._lock:
            self._state = WAKE_LISTENER_STOPPED
            self._last_stop_reason = str(reason or "stopped")[:80]
        return self._result(
            cleanup_success,
            "stopped" if cleanup_success else "stop_failed",
            "" if cleanup_success else "wake_listener_stop_failed",
            "" if cleanup_success else (_result_error(stopped) or _result_error(recognizer_stopped)),
            cleanup_status="complete" if cleanup_success else "partial",
        )

    close = stop

    def snapshot(self, *, runtime_id: str = "") -> WakeListenerSnapshotV1:
        with self._lock:
            return WakeListenerSnapshotV1(
                runtime_id=runtime_id or self._runtime_id,
                listener_state=self._state,
                started=self._state != WAKE_LISTENER_STOPPED,
                listening=self._state == WAKE_LISTENER_LISTENING,
                cancelled=self._cancelled,
                listen_count=self._listen_count,
                speech_candidate_count=self._speech_count,
                wake_detection_count=self._wake_count,
                consecutive_failure_count=self._failure_count,
                stream_open_count=self._stream_open_count,
                stream_close_count=self._stream_close_count,
                calibration_count=self._calibration_count,
                candidate_count=self._candidate_count,
                stream_generation=self._stream_generation,
                stream_state=self._stream_state,
                calibration_healthy=bool(
                    self._stream_state == STREAM_HEALTHY
                    and self._calibration_thresholds is not None
                ),
                stream_active=bool(
                    self._stream_handle is not None
                    and not bool(getattr(self._stream_handle, "closed", False))
                ),
                capture_owner=(
                    STANDBY_STREAM_OWNER if self._stream_handle is not None else ""
                ),
                stream_instance_id=(
                    str(getattr(self._stream_handle, "stream_id", "") or "")
                    if self._stream_handle is not None
                    else self._last_stream_instance_id
                ),
                alsa_handle_id=(
                    str(getattr(self._stream_handle, "alsa_handle_id", "") or "")
                    if self._stream_handle is not None
                    else self._last_alsa_handle_id
                ),
                stream_open_reason=self._last_open_reason,
                stream_close_reason=self._last_close_reason,
                calibration_reason=self._last_calibration_reason,
                ownership_handoff_source=self._last_handoff_source,
                ownership_handoff_destination=self._last_handoff_destination,
                stream_open_reasons=list(self._stream_open_reasons),
                stream_close_reasons=list(self._stream_close_reasons),
                calibration_reasons=list(self._calibration_reasons),
                ownership_handoffs=list(self._ownership_handoffs),
                last_stop_reason=self._last_stop_reason,
                metadata={
                    "safe": True,
                    "background_thread": False,
                    "retained_turn_count": len(self._retained_directories),
                    "last_cleanup_status": self._last_cleanup_status,
                    "persistent_stream": True,
                },
            )

    @property
    def retained_directories(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._retained_directories)

    def _create_turn_directory(self) -> Path:
        if self.config.retain_diagnostic_audio:
            base = Path(self.config.diagnostic_output_directory).expanduser()
            if not base.is_absolute():
                base = self.project_root / base
            base.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="wake-turn-", dir=str(base)))
        return Path(tempfile.mkdtemp(prefix="ares-wake-turn-"))

    def _is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def _ensure_standby_stream(self, runtime_id: str) -> str:
        with self._lock:
            handle = self._stream_handle
            calibrated_at = self._calibration_at
            recalibration_requested = self._recalibration_requested
        if handle is None or bool(getattr(handle, "closed", False)):
            recovery_reason = (
                "device_recovery"
                if self._last_close_reason
                in {VAD_STATUS_DEVICE_ERROR, VAD_STATUS_TIMEOUT, "capture_exception"}
                else "standby_stream_missing_recovery"
            )
            entered = self.enter_standby(
                runtime_id=runtime_id,
                reason=recovery_reason,
                handoff_source="device_recovery",
            )
            return "" if entered.success else entered.error_message or entered.status
        now = self.clock()
        interval = self.config.recalibration_interval_seconds
        recalibration_due = bool(
            recalibration_requested
            or calibrated_at is None
            or now < calibrated_at
            or (interval > 0 and now - calibrated_at >= interval)
        )
        if not recalibration_due:
            return ""
        if recalibration_requested:
            calibration_reason = "manual_recalibration_request"
        elif calibrated_at is None:
            calibration_reason = "missing_calibration_state"
        elif now < calibrated_at:
            calibration_reason = "clock_rollback_recalibration"
        else:
            calibration_reason = "configured_interval_recalibration"
        calibration = self._calibrate_stream(handle, reason=calibration_reason)
        if bool(getattr(calibration, "success", False)):
            return ""
        self._close_standby_stream(
            "recalibration_failed",
            final_state=STREAM_FAILED,
        )
        return str(
            getattr(calibration, "error_message", "")
            or getattr(calibration, "status", "")
            or "wake recalibration failed"
        )

    def _reset_candidate_stream(self, handle: Any) -> int:
        reset = getattr(self.microphone_adapter, "reset_persistent_candidate", None)
        if not callable(reset):
            clear = getattr(getattr(handle, "frame_source", None), "clear_history", None)
            if callable(clear):
                clear()
            return 0
        result = reset(
            handle,
            frame_duration_ms=self.config.frame_duration_ms,
            maximum_discard_seconds=min(
                2.0,
                self.config.maximum_utterance_seconds,
            ),
        )
        if isinstance(result, dict):
            stale_frames = int(result.get("stale_pcm_frames_discarded", 0) or 0)
        else:
            stale_frames = int(
                getattr(result, "stale_pcm_frames_discarded", 0) or 0
            )
        with self._lock:
            self._last_candidate_stale_frames += max(0, stale_frames)
        return max(0, stale_frames)

    def _calibrate_stream(self, handle: Any, *, reason: str) -> Any:
        with self._lock:
            self._stream_state = STREAM_CALIBRATING
        request = VoiceActivityCaptureRequestV1(
            output_wav_path="wake-calibration-not-written.wav",
            microphone_device=self.config.microphone_device,
            sample_rate_hz=16000,
            channels=1,
            sample_width_bytes=2,
            frame_duration_ms=self.config.frame_duration_ms,
            calibration_enabled=self.config.calibration_enabled,
            calibration_duration_seconds=self.config.calibration_duration_seconds,
            speech_start_rms=self.config.speech_start_rms,
            speech_continue_rms=self.config.speech_continue_rms,
            silence_rms=self.config.silence_rms,
            required_speech_frames=self.config.required_speech_frames,
            required_continue_frames=self.config.required_continue_frames,
            required_silence_frames=self.config.required_silence_frames,
            silence_duration_seconds=self.config.silence_duration_seconds,
            speech_wait_timeout_seconds=self.config.speech_wait_timeout_seconds,
            maximum_utterance_seconds=self.config.maximum_utterance_seconds,
            pre_roll_seconds=self.config.pre_roll_seconds,
            speech_end_padding_seconds=self.config.speech_end_padding_seconds,
            frame_read_timeout_seconds=self.config.frame_read_timeout_seconds,
            minimum_speech_start_rms=self.config.minimum_speech_start_rms,
            maximum_speech_start_rms=self.config.maximum_speech_start_rms,
            minimum_speech_continue_rms=self.config.minimum_speech_continue_rms,
            maximum_speech_continue_rms=self.config.maximum_speech_continue_rms,
            minimum_silence_rms=self.config.minimum_silence_rms,
            maximum_silence_rms=self.config.maximum_silence_rms,
            metadata={
                "safe": True,
                "source": "persistent_standby_calibration",
                "vad_profile": "standby_wake_short_v1",
                "wake_vad_sensitivity": self.config.wake_vad_sensitivity,
                "calibration_confirm_non_speech": True,
                "calibration_maximum_seconds": self.config.calibration_maximum_seconds,
                "calibration_quiet_sample_fraction": (
                    self.config.calibration_quiet_sample_fraction
                ),
                "calibration_minimum_quiet_frame_fraction": (
                    self.config.calibration_minimum_quiet_frame_fraction
                ),
                "calibration_maximum_speech_frame_fraction": (
                    self.config.calibration_maximum_speech_frame_fraction
                ),
                "calibration_maximum_noise_floor_rms": (
                    self.config.calibration_maximum_noise_floor_rms
                ),
                "calibration_maximum_clipped_frame_fraction": (
                    self.config.calibration_maximum_clipped_frame_fraction
                ),
                "calibration_bootstrap_speech_multiplier": (
                    self.config.calibration_bootstrap_speech_multiplier
                ),
                "calibration_bootstrap_speech_margin_rms": (
                    self.config.calibration_bootstrap_speech_margin_rms
                ),
                "calibration_diagnostic_interval_frames": (
                    self.config.calibration_diagnostic_interval_frames
                ),
            },
        )
        retryable_quality_failures = {
            "calibration_speech_dominated",
            "calibration_quiet_samples_insufficient",
            "calibration_noise_floor_unusable",
        }
        result = None
        for attempt_number in range(1, self.config.calibration_retry_count + 2):
            with self._lock:
                self._calibration_attempt_count += 1
            result = self.microphone_adapter.calibrate_persistent_stream(
                handle,
                request,
                cancel_requested=self._is_cancelled,
            )
            if hasattr(result, "__dataclass_fields__") and hasattr(
                result,
                "attempt_count",
            ):
                result = replace(result, attempt_count=attempt_number)
            diagnostics = getattr(result, "diagnostics", None)
            error_code = str(getattr(result, "error_code", "") or "")
            with self._lock:
                self._last_calibration_diagnostics = diagnostics
                self._last_calibration_error_code = error_code
                self._last_calibration_error_message = str(
                    getattr(result, "error_message", "") or ""
                )[:200]
                self._last_valid_pcm_received = bool(
                    bool(getattr(result, "success", False))
                    or int(getattr(result, "frame_count", 0) or 0) > 0
                )
                summary = {
                    "attempt": attempt_number,
                    "success": bool(getattr(result, "success", False)),
                    "status": str(getattr(result, "status", "") or ""),
                    "error_code": error_code,
                    "frame_count": int(getattr(result, "frame_count", 0) or 0),
                    "quality": (
                        diagnostics.to_dict() if diagnostics is not None else {}
                    ),
                }
                self._calibration_attempt_summaries.append(summary)
                if len(self._calibration_attempt_summaries) > 4:
                    del self._calibration_attempt_summaries[:-4]
            if bool(getattr(result, "success", False)):
                break
            if (
                attempt_number > self.config.calibration_retry_count
                or error_code not in retryable_quality_failures
                or self._is_cancelled()
            ):
                break
            self._reset_recognizer_attempt_state("calibration_retry")
            clear_history = getattr(
                getattr(handle, "frame_source", None),
                "clear_history",
                None,
            )
            if callable(clear_history):
                clear_history()
            if self.config.calibration_retry_delay_seconds > 0:
                self.sleeper(self.config.calibration_retry_delay_seconds)
            self._reset_candidate_stream(handle)
        if result is None:
            raise RuntimeError("standby_calibration_did_not_run")
        if bool(getattr(result, "success", False)):
            clear_history = getattr(
                getattr(handle, "frame_source", None),
                "clear_history",
                None,
            )
            if callable(clear_history):
                clear_history()
            with self._lock:
                self._calibration_count += 1
                self._last_calibration_reason = str(reason or "calibration")[:96]
                _append_bounded(
                    self._calibration_reasons,
                    self._last_calibration_reason,
                )
                self._calibration_at = self.clock()
                self._calibration_thresholds = getattr(result, "thresholds", None)
                self._calibration_statistics = getattr(
                    result,
                    "ambient_statistics",
                    None,
                )
                self._recalibration_requested = False
                self._stream_state = STREAM_HEALTHY
                self._last_calibration_error_code = ""
                self._last_calibration_error_message = ""
        else:
            with self._lock:
                self._stream_state = STREAM_FAILED
        return result

    def _close_standby_stream(
        self,
        reason: str,
        *,
        handoff_source: str = "",
        handoff_destination: str = "",
        final_state: str = STREAM_CLOSED,
    ) -> bool:
        close_reason = str(reason or "leaving_standby")[:80]
        with self._lock:
            handle = self._stream_handle
            self._stream_handle = None
            self._calibration_at = None
            self._calibration_thresholds = None
            self._calibration_statistics = None
            gate_owned = self._capture_gate_owned
            self._capture_gate_owned = False
            self._last_stop_reason = close_reason
            self._last_close_reason = close_reason
            self._stream_state = final_state
            if handle is not None:
                _append_bounded(self._stream_close_reasons, close_reason)
            if handoff_source or handoff_destination:
                self._record_handoff_locked(
                    str(handoff_source or STANDBY_STREAM_OWNER)[:64],
                    str(handoff_destination or "released")[:64],
                    close_reason,
                )
        success = True
        if handle is not None and not bool(getattr(handle, "closed", False)):
            try:
                clear_history = getattr(
                    getattr(handle, "frame_source", None),
                    "clear_history",
                    None,
                )
                if callable(clear_history):
                    clear_history()
                result = self.microphone_adapter.close_persistent_stream(
                    handle,
                    owner=STANDBY_STREAM_OWNER,
                )
                success = _result_success(result)
            except (OSError, RuntimeError, TimeoutError, TypeError, ValueError):
                success = False
            finally:
                with self._lock:
                    self._stream_close_count += 1
                    self._last_alsa_closed_during_cleanup = True
        if gate_owned:
            self._end_capture_gate()
        self._reset_recognizer_attempt_state(close_reason)
        return success

    def _stream_metrics(self) -> Dict[str, Any]:
        with self._lock:
            handle = self._stream_handle
            thresholds = self._calibration_thresholds
            statistics = self._calibration_statistics
            diagnostics = self._last_calibration_diagnostics
            stream_active = bool(
                handle is not None and not bool(getattr(handle, "closed", False))
            )
            calibration_healthy = bool(
                self._stream_state == STREAM_HEALTHY and thresholds is not None
            )
            return {
                "stream_active": stream_active,
                "alsa_device_open": stream_active,
                "alsa_device_open_attempt_succeeded": self._last_alsa_open_succeeded,
                "alsa_device_closed_during_cleanup": (
                    self._last_alsa_closed_during_cleanup
                ),
                "valid_pcm_received": self._last_valid_pcm_received,
                "capture_owner": (
                    STANDBY_STREAM_OWNER if handle is not None else ""
                ),
                "stream_open_count": self._stream_open_count,
                "stream_close_count": self._stream_close_count,
                "calibration_count": self._calibration_count,
                "wake_vad_sensitivity": self.config.wake_vad_sensitivity,
                "candidate_count": self._candidate_count,
                "stream_generation": self._stream_generation,
                "stream_state": self._stream_state,
                "calibration_healthy": calibration_healthy,
                "calibration_quality_passed": bool(
                    diagnostics is not None
                    and bool(getattr(diagnostics, "quality_passed", False))
                ),
                "standby_listener_healthy": bool(
                    stream_active and calibration_healthy
                ),
                "calibration_attempt_count": self._calibration_attempt_count,
                "calibration_error_code": self._last_calibration_error_code,
                "calibration_error_message": self._last_calibration_error_message,
                "stream_instance_id": (
                    str(getattr(handle, "stream_id", "") or "")
                    if handle is not None
                    else self._last_stream_instance_id
                ),
                "alsa_handle_id": (
                    str(getattr(handle, "alsa_handle_id", "") or "")
                    if handle is not None
                    else self._last_alsa_handle_id
                ),
                "stream_open_reason": self._last_open_reason,
                "stream_close_reason": self._last_close_reason,
                "calibration_reason": self._last_calibration_reason,
                "ownership_handoff_source": self._last_handoff_source,
                "ownership_handoff_destination": self._last_handoff_destination,
                "stream_open_reasons": list(self._stream_open_reasons),
                "stream_close_reasons": list(self._stream_close_reasons),
                "calibration_reasons": list(self._calibration_reasons),
                "ownership_handoffs": list(self._ownership_handoffs),
                "calibration_thresholds": (
                    thresholds.to_dict() if thresholds is not None else {}
                ),
                "ambient_statistics": (
                    statistics.to_dict() if statistics is not None else {}
                ),
                "calibration_diagnostics": (
                    diagnostics.to_dict() if diagnostics is not None else {}
                ),
                "calibration_attempts": list(self._calibration_attempt_summaries),
                "safe": True,
            }

    def _record_handoff_locked(
        self,
        source: str,
        destination: str,
        reason: str,
    ) -> None:
        self._last_handoff_source = source
        self._last_handoff_destination = destination
        _append_bounded(
            self._ownership_handoffs,
            f"{source}->{destination}:{reason}"[:192],
        )

    def _begin_capture_gate(self, timeout_seconds: float) -> str:
        if self.voice_io_gate is None:
            return ""
        wait = getattr(self.voice_io_gate, "wait_for_capture", None)
        if callable(wait) and not bool(wait(timeout_seconds=max(0.1, float(timeout_seconds)))):
            return "speaker_playback_or_settle_active"
        begin = getattr(self.voice_io_gate, "begin_capture", None)
        if callable(begin):
            try:
                begin("standby_wake")
            except (RuntimeError, ValueError) as error:
                return str(error)[:160]
        return ""

    def _end_capture_gate(self) -> None:
        if self.voice_io_gate is None:
            return
        end = getattr(self.voice_io_gate, "end_capture", None)
        if callable(end):
            try:
                end("standby_wake")
            except (RuntimeError, ValueError):
                pass

    def _start_failure(self, code: str, message: str) -> WakeListenerResultV1:
        with self._lock:
            self._state = WAKE_LISTENER_ERROR
            self._failure_count += 1
        return self._result(False, "start_failed", code, message)

    def _health_failure(
        self,
        code: str,
        message: str,
        *,
        data: Optional[Dict[str, Any]] = None,
    ) -> WakeListenerResultV1:
        with self._lock:
            self._failure_count += 1
        return self._result(False, "unhealthy", code, message, data=data)

    def _capture_failure(
        self,
        request: WakeListenerRequestV1,
        capture: Any,
        started_at: float,
        *,
        context: _WakeAttemptContext,
    ) -> StandbyListenResultV1:
        with self._lock:
            self._failure_count += 1
        self._bounded_retry_pause()
        return self._standby_result(
            request,
            success=False,
            status=WAKE_STATUS_FAILED,
            stop_reason=_result_status(capture) or "capture_failed",
            error_code="wake_capture_failed",
            error_message=_result_error(capture),
            capture=capture,
            started_at=started_at,
            context=context,
        )

    def _recognition_failure(
        self,
        request: WakeListenerRequestV1,
        recognition: Any,
        capture: Any,
        started_at: float,
        *,
        trim: Optional[WakeAudioTrimResult] = None,
        context: _WakeAttemptContext,
    ) -> StandbyListenResultV1:
        with self._lock:
            self._failure_count += 1
        self._bounded_retry_pause()
        return self._standby_result(
            request,
            success=False,
            status=WAKE_STATUS_FAILED,
            stop_reason=_result_status(recognition) or "wake_recognition_failed",
            error_code=str(getattr(recognition, "error_code", "") or "wake_recognition_failed"),
            error_message=_result_error(recognition),
            capture=capture,
            started_at=started_at,
            recognition=recognition,
            trim=trim,
            context=context,
            data={"recognition_status": _result_status(recognition)},
        )

    def _cancelled_result(
        self,
        request: WakeListenerRequestV1,
        *,
        context: _WakeAttemptContext,
    ) -> StandbyListenResultV1:
        result = StandbyListenResultV1(
            success=False,
            status=WAKE_STATUS_CANCELLED,
            runtime_id=request.runtime_id,
            lifecycle_state=request.lifecycle_state,
            listener_state=WAKE_LISTENER_CANCELLING,
            attempt_id=context.attempt_id,
            candidate_id=context.candidate_id,
            stream_generation=context.stream_generation,
            candidate_number=context.candidate_number,
            stop_reason="cancelled",
            error_code="wake_listener_cancelled",
            correlation_id=request.correlation_id,
            metadata={"safe": True, "contains_transcript": False},
        )
        return result

    def _listen_failure(
        self,
        request: WakeListenerRequestV1,
        code: str,
        message: str,
        *,
        context: _WakeAttemptContext,
    ) -> StandbyListenResultV1:
        with self._lock:
            self._failure_count += 1
        self._bounded_retry_pause()
        result = StandbyListenResultV1(
            success=False,
            status=WAKE_STATUS_FAILED,
            runtime_id=request.runtime_id,
            lifecycle_state=request.lifecycle_state,
            listener_state=self._state,
            attempt_id=context.attempt_id,
            candidate_id=context.candidate_id,
            stream_generation=context.stream_generation,
            candidate_number=context.candidate_number,
            capture_valid=context.capture_valid,
            recognizer_invoked=context.recognizer_invoked,
            infrastructure_failure=True,
            stop_reason=code,
            error_code=code,
            error_message=str(message or code)[:160],
            correlation_id=request.correlation_id,
            metadata={"safe": True, "contains_transcript": False},
        )
        return result

    def _bounded_retry_pause(self) -> None:
        delay = self.config.retry_delay_seconds
        if delay <= 0 or self._is_cancelled():
            return
        try:
            self.sleeper(delay)
        except (OSError, RuntimeError, ValueError):
            pass

    def _standby_result(
        self,
        request: WakeListenerRequestV1,
        *,
        success: bool,
        status: str,
        stop_reason: str,
        capture: Any,
        started_at: float,
        speech_detected: Optional[bool] = None,
        wake_detected: bool = False,
        command_category: str = WAKE_CATEGORY_NON_WAKE,
        normalized_wake_phrase: str = "",
        matched_phrase: str = "",
        selected_alias: str = "",
        selected_wake_phrase: str = "",
        canonical_wake_phrase: str = "",
        classification_path: str = "",
        classification_reason: str = "",
        collapsed_wake_representation: str = "",
        wake_vocabulary_only: bool = False,
        wake_token_count: int = 0,
        alias_repetition_count: int = 0,
        maximum_prefix_repetition_count: int = 0,
        rejection_reason: str = "",
        error_code: str = "",
        error_message: str = "",
        recognition: Any = None,
        trim: Optional[WakeAudioTrimResult] = None,
        data: Optional[Dict[str, Any]] = None,
        context: _WakeAttemptContext,
    ) -> StandbyListenResultV1:
        audio = _capture_audio_metadata(capture)
        if context.recognizer_invoked and not wake_detected:
            audio["capture_failure_stage"] = "recognizer_rejected"
        elif context.capture_valid and not audio.get("capture_failure_stage"):
            audio["capture_failure_stage"] = "candidate_assembled"
        if trim is not None:
            audio.update(
                {
                    "trimmed_duration_seconds": trim.trimmed_duration_seconds,
                    "leading_trimmed_seconds": trim.leading_trimmed_seconds,
                    "trailing_trimmed_seconds": trim.trailing_trimmed_seconds,
                    "trim_first_speech_frame": trim.first_speech_frame,
                    "trim_last_speech_frame": trim.last_speech_frame,
                    "trim_speech_threshold_rms": trim.speech_threshold_rms,
                }
            )
        recognition_metadata = _recognition_metadata(recognition)
        stream = self._stream_metrics()
        candidate_duration = float(
            audio.get("trimmed_duration_seconds", 0.0)
            or audio.get("whisper_input_duration_seconds", 0.0)
            or audio.get("normalized_duration_seconds", 0.0)
            or audio.get("assembled_duration_seconds", 0.0)
            or audio.get("duration_seconds", 0.0)
            or 0.0
        )
        processing_time = round(max(0.0, self.clock() - started_at), 6)
        result = StandbyListenResultV1(
            success=success,
            status=status,
            runtime_id=request.runtime_id,
            lifecycle_state=request.lifecycle_state,
            listener_state=WAKE_LISTENER_READY,
            attempt_id=context.attempt_id,
            candidate_id=context.candidate_id,
            stream_generation=context.stream_generation,
            capture_valid=context.capture_valid,
            recognizer_invoked=context.recognizer_invoked,
            infrastructure_failure=context.infrastructure_failure,
            speech_detected=(
                bool(getattr(capture, "speech_detected", False))
                if speech_detected is None
                else speech_detected
            ),
            wake_detected=wake_detected,
            command_category=command_category,
            normalized_wake_phrase=normalized_wake_phrase,
            matched_phrase=matched_phrase,
            selected_alias=selected_alias,
            selected_wake_phrase=selected_wake_phrase,
            canonical_wake_phrase=canonical_wake_phrase,
            classification_path=classification_path,
            classification_reason=classification_reason,
            collapsed_wake_representation=collapsed_wake_representation,
            wake_vocabulary_only=wake_vocabulary_only,
            wake_token_count=wake_token_count,
            alias_repetition_count=alias_repetition_count,
            maximum_prefix_repetition_count=maximum_prefix_repetition_count,
            rejection_reason=rejection_reason,
            stop_reason=stop_reason,
            duration_seconds=round(candidate_duration, 6),
            processing_time_seconds=processing_time,
            raw_capture_duration_seconds=float(audio.get("raw_duration_seconds", 0.0)),
            assembled_duration_seconds=float(audio.get("assembled_duration_seconds", 0.0)),
            normalized_duration_seconds=float(audio.get("normalized_duration_seconds", 0.0)),
            whisper_input_duration_seconds=float(
                audio.get("whisper_input_duration_seconds", 0.0)
            ),
            trimmed_duration_seconds=float(
                audio.get("trimmed_duration_seconds", 0.0)
            ),
            leading_trimmed_seconds=float(
                audio.get("leading_trimmed_seconds", 0.0)
            ),
            trailing_trimmed_seconds=float(
                audio.get("trailing_trimmed_seconds", 0.0)
            ),
            whisper_processing_time_seconds=0.0,
            whisper_status="",
            whisper_exit_code=None,
            recognizer_name=str(recognition_metadata.get("recognizer_name", "")),
            recognition_status=str(recognition_metadata.get("status", "")),
            recognition_confidence=recognition_metadata.get("confidence"),
            recognition_confidence_available=bool(
                recognition_metadata.get("confidence_available", False)
            ),
            minimum_word_confidence=recognition_metadata.get(
                "minimum_word_confidence"
            ),
            mean_word_confidence=recognition_metadata.get(
                "mean_word_confidence"
            ),
            canonical_confidence=recognition_metadata.get(
                "canonical_confidence"
            ),
            duplicate_collapse_used=bool(
                recognition_metadata.get("duplicate_collapse_used", False)
            ),
            recognition_processing_time_seconds=float(
                recognition_metadata.get("processing_time_seconds", 0.0)
            ),
            confidence_tier=str(recognition_metadata.get("confidence_tier", "")),
            confirmation_required=bool(
                recognition_metadata.get("confirmation_required", False)
            ),
            confirmation_count=int(
                recognition_metadata.get("confirmation_count", 0) or 0
            ),
            confirmation_required_count=int(
                recognition_metadata.get("confirmation_required_count", 0) or 0
            ),
            stream_open_count=int(stream["stream_open_count"]),
            stream_close_count=int(stream["stream_close_count"]),
            calibration_count=int(stream["calibration_count"]),
            candidate_number=context.candidate_number,
            stream_instance_id=str(stream["stream_instance_id"]),
            alsa_handle_id=str(stream["alsa_handle_id"]),
            stream_open_reason=str(stream["stream_open_reason"]),
            stream_close_reason=str(stream["stream_close_reason"]),
            calibration_reason=str(stream["calibration_reason"]),
            ownership_handoff_source=str(stream["ownership_handoff_source"]),
            ownership_handoff_destination=str(
                stream["ownership_handoff_destination"]
            ),
            stream_open_reasons=list(stream["stream_open_reasons"]),
            stream_close_reasons=list(stream["stream_close_reasons"]),
            calibration_reasons=list(stream["calibration_reasons"]),
            ownership_handoffs=list(stream["ownership_handoffs"]),
            pre_roll_frames_retained=int(
                audio.get("pre_roll_frames_retained", 0) or 0
            ),
            expected_pre_roll_frames=int(
                audio.get("expected_pre_roll_frames", 0) or 0
            ),
            first_speech_frame=int(audio.get("first_speech_frame", 0) or 0),
            waiting_duration_before_speech_seconds=float(
                audio.get("waiting_duration_before_speech_seconds", 0.0) or 0.0
            ),
            speech_start_timestamp_monotonic=float(
                audio.get("speech_start_timestamp_monotonic", 0.0) or 0.0
            ),
            speech_duration_seconds=float(
                audio.get("speech_duration_seconds", 0.0) or 0.0
            ),
            active_speech_window_seconds=float(
                audio.get("active_speech_window_seconds", 0.0) or 0.0
            ),
            terminal_silence_duration_seconds=float(
                audio.get("terminal_silence_duration_seconds", 0.0) or 0.0
            ),
            terminal_silence_confirmed=bool(
                audio.get("terminal_silence_confirmed", False)
            ),
            terminal_silence_reset_count=int(
                audio.get("terminal_silence_reset_count", 0) or 0
            ),
            terminal_quiet_frame_count=int(
                audio.get("terminal_quiet_frame_count", 0) or 0
            ),
            last_speech_frame=int(audio.get("last_speech_frame", 0) or 0),
            capture_completion_reason=str(
                audio.get("capture_completion_reason", "") or ""
            ),
            speech_frame_count=int(audio.get("speech_frame_count", 0) or 0),
            post_roll_frame_count=int(
                audio.get("post_roll_frame_count", 0) or 0
            ),
            duplicate_pcm_frame_count=int(
                audio.get("duplicate_pcm_frame_count", 0) or 0
            ),
            stale_pcm_frames_discarded=self._last_candidate_stale_frames,
            total_low_level_reads=int(
                audio.get("total_low_level_reads", 0) or 0
            ),
            valid_full_pcm_frames=int(
                audio.get("valid_full_pcm_frames", 0) or 0
            ),
            partial_reads=int(audio.get("partial_reads", 0) or 0),
            empty_reads=int(audio.get("empty_reads", 0) or 0),
            read_errors=int(audio.get("read_errors", 0) or 0),
            discarded_bytes=int(audio.get("discarded_bytes", 0) or 0),
            zero_filled_bytes=int(audio.get("zero_filled_bytes", 0) or 0),
            repeated_frame_hashes=int(
                audio.get("repeated_frame_hashes", 0) or 0
            ),
            mutable_buffer_reuse_detected=int(
                audio.get("mutable_buffer_reuse_detected", 0) or 0
            ),
            valid_microphone_bytes_delivered_to_vad=int(
                audio.get("valid_microphone_bytes_delivered_to_vad", 0) or 0
            ),
            fresh_microphone_bytes_delivered_to_vad=int(
                audio.get("fresh_microphone_bytes_delivered_to_vad", 0) or 0
            ),
            ambient_noise_floor=float(audio.get("ambient_noise_floor", 0.0) or 0.0),
            speech_start_threshold=float(
                audio.get("speech_start_threshold", 0.0) or 0.0
            ),
            speech_continue_threshold=float(
                audio.get("speech_continue_threshold", 0.0) or 0.0
            ),
            speech_end_threshold=float(
                audio.get("speech_end_threshold", 0.0) or 0.0
            ),
            speech_to_activation_seconds=(
                round(
                    max(
                        0.0,
                        processing_time
                        - float(audio.get("speech_start_offset_seconds", 0.0) or 0.0),
                    ),
                    6,
                )
                if wake_detected
                else 0.0
            ),
            sample_rate_hz=int(audio.get("sample_rate_hz", 0)),
            channels=int(audio.get("channels", 0)),
            sample_width_bytes=int(audio.get("sample_width_bytes", 0)),
            capture_stop_reason=str(audio.get("capture_stop_reason", "")),
            error_code=error_code,
            error_message=str(error_message or "")[:160],
            correlation_id=request.correlation_id,
            audio_metadata=audio,
            data={
                "safe": True,
                "contains_transcript": False,
                "stream": stream,
                **dict(data or {}),
            },
            metadata={"safe": True, "contains_transcript": False, "contains_audio": False},
        )
        if success:
            with self._lock:
                self._failure_count = 0
        return result

    def _validate_capture_durations(
        self,
        capture: Any,
        recognizer_input_path: Path,
        *,
        listener_timeout_seconds: float,
    ) -> str:
        try:
            recognizer_wav = _read_wav_metadata(recognizer_input_path)
        except (OSError, EOFError, ValueError, wave.Error) as error:
            return f"invalid_wake_wav:{error.__class__.__name__}"
        if (
            recognizer_wav["sample_rate_hz"] != 16000
            or recognizer_wav["channels"] != 1
            or recognizer_wav["sample_width_bytes"] != 2
        ):
            return "wake_recognizer_input_not_canonical_pcm"

        frame_seconds = self.config.frame_duration_ms / 1000.0
        candidate_limit = (
            self.config.maximum_utterance_seconds
            + self.config.pre_roll_seconds
            + (2.0 * frame_seconds)
            + self.config.duration_tolerance_seconds
        )
        candidate_durations = [
            float(recognizer_wav["duration_seconds"]),
            float(getattr(capture, "assembled_duration_seconds", 0.0) or 0.0),
            float(getattr(capture, "normalized_duration_seconds", 0.0) or 0.0),
            float(getattr(capture, "whisper_input_duration_seconds", 0.0) or 0.0),
        ]
        if max(candidate_durations) > candidate_limit:
            return (
                "wake_candidate_duration_exceeded:"
                f"actual={max(candidate_durations):.3f}:limit={candidate_limit:.3f}"
            )

        raw_duration = float(getattr(capture, "raw_duration_seconds", 0.0) or 0.0)
        raw_path = Path(str(getattr(capture, "raw_wav_path", "") or ""))
        if raw_path.is_file():
            try:
                raw_duration = max(
                    raw_duration,
                    float(_read_wav_metadata(raw_path)["duration_seconds"]),
                )
            except (OSError, EOFError, ValueError, wave.Error) as error:
                return f"invalid_raw_wake_wav:{error.__class__.__name__}"
        raw_limit = (
            min(
                self.config.speech_wait_timeout_seconds,
                max(0.1, float(listener_timeout_seconds)),
            )
            + self.config.maximum_utterance_seconds
            + (2.0 * frame_seconds)
            + self.config.duration_tolerance_seconds
        )
        if raw_duration > raw_limit:
            return f"wake_raw_duration_exceeded:actual={raw_duration:.3f}:limit={raw_limit:.3f}"
        return ""

    def _build_local_diagnostics(
        self,
        *,
        request: WakeListenerRequestV1,
        result: StandbyListenResultV1,
        recognition: Optional[WakeRecognizerLocalDiagnostics],
        recognizer_input_path: str,
    ) -> WakeLocalDiagnostics:
        """Build terminal diagnostics only from one completed attempt."""

        audio = dict(result.audio_metadata or {})
        stream = self._stream_metrics()
        same_attempt = bool(
            recognition is not None
            and recognition.attempt_id == result.attempt_id
            and recognition.stream_generation == result.stream_generation
            and recognition.candidate_number == result.candidate_number
        )
        if not same_attempt:
            recognition = None
        raw_text = str(recognition.recognized_text if recognition is not None else "")
        retained_path = str(recognizer_input_path or "")
        if (
            result.cleanup_status != "retained_by_explicit_request"
            or not retained_path
            or not Path(retained_path).is_file()
        ):
            retained_path = ""
        classification = (
            "accepted"
            if result.command_category != WAKE_CATEGORY_NON_WAKE
            else "rejected"
        )
        original_tokens = tuple(
            part for part in normalize_wake_phrase(raw_text).split(" ") if part
        )
        canonicalized_tokens = tuple(
            part
            for part in canonicalize_ares_name_tokens(
                raw_text,
                self.config.wake_phrase_aliases,
            ).split(" ")
            if part
        )
        canonical_tokens = (
            (result.canonical_wake_phrase,)
            if result.duplicate_collapse_used and result.canonical_wake_phrase
            else canonicalized_tokens
        )
        return WakeLocalDiagnostics(
            raw_transcript=raw_text,
            attempt_id=result.attempt_id,
            candidate_id=result.candidate_id,
            stream_generation=result.stream_generation,
            capture_valid=result.capture_valid,
            recognizer_invoked=result.recognizer_invoked,
            infrastructure_failure=result.infrastructure_failure,
            cleaned_transcript=clean_wake_transcript(raw_text),
            normalized_transcript=normalize_wake_phrase(raw_text),
            selected_alias=result.selected_alias,
            selected_wake_phrase=result.selected_wake_phrase,
            canonical_wake_phrase=result.canonical_wake_phrase,
            classification_path=result.classification_path,
            classification_reason=result.classification_reason,
            collapsed_wake_representation=result.collapsed_wake_representation,
            wake_vocabulary_only=result.wake_vocabulary_only,
            wake_token_count=result.wake_token_count,
            alias_repetition_count=result.alias_repetition_count,
            maximum_prefix_repetition_count=(
                result.maximum_prefix_repetition_count
            ),
            classification=classification,
            rejection_reason=(
                result.rejection_reason
                or (
                    ""
                    if classification == "accepted"
                    else result.stop_reason or "wake_not_detected"
                )
            ),
            capture_duration_seconds=result.duration_seconds,
            raw_capture_duration_seconds=float(audio.get("raw_duration_seconds", 0.0)),
            assembled_duration_seconds=float(audio.get("assembled_duration_seconds", 0.0)),
            normalized_duration_seconds=float(audio.get("normalized_duration_seconds", 0.0)),
            whisper_input_duration_seconds=float(
                audio.get("whisper_input_duration_seconds", 0.0)
            ),
            capture_stop_reason=result.capture_stop_reason,
            wake_model_path=str(
                recognition.model_path if recognition is not None else self.config.vosk_model_path
            ),
            lifecycle_state=request.lifecycle_state,
            retained_audio_path=retained_path,
            cleanup_status=result.cleanup_status,
            recognizer_name=str(
                recognition.recognizer_name if recognition is not None else ""
            ),
            raw_recognition_result=str(
                recognition.raw_recognition_result if recognition is not None else ""
            ),
            recognition_status=result.recognition_status,
            recognition_confidence=(
                recognition.confidence if recognition is not None else None
            ),
            recognition_confidence_available=bool(
                recognition.confidence_available if recognition is not None else False
            ),
            minimum_word_confidence=(
                recognition.minimum_word_confidence if recognition is not None else None
            ),
            mean_word_confidence=(
                recognition.mean_word_confidence if recognition is not None else None
            ),
            canonical_confidence=(
                recognition.canonical_confidence if recognition is not None else None
            ),
            duplicate_collapse_used=bool(
                recognition.duplicate_collapse_used if recognition is not None else False
            ),
            recognition_processing_time_seconds=float(
                recognition.processing_time_seconds if recognition is not None else 0.0
            ),
            recognizer_model_path=str(
                recognition.model_path if recognition is not None else self.config.vosk_model_path
            ),
            stream_open_count=int(stream["stream_open_count"]),
            stream_close_count=int(stream["stream_close_count"]),
            calibration_count=int(stream["calibration_count"]),
            candidate_number=int(stream["candidate_count"]),
            stream_instance_id=str(stream["stream_instance_id"]),
            alsa_handle_id=str(stream["alsa_handle_id"]),
            stream_open_reason=str(stream["stream_open_reason"]),
            stream_close_reason=str(stream["stream_close_reason"]),
            calibration_reason=str(stream["calibration_reason"]),
            ownership_handoff_source=str(stream["ownership_handoff_source"]),
            ownership_handoff_destination=str(
                stream["ownership_handoff_destination"]
            ),
            stream_open_reasons=tuple(stream["stream_open_reasons"]),
            stream_close_reasons=tuple(stream["stream_close_reasons"]),
            calibration_reasons=tuple(stream["calibration_reasons"]),
            ownership_handoffs=tuple(stream["ownership_handoffs"]),
            wake_vad_sensitivity=self.config.wake_vad_sensitivity,
            pre_roll_frames_retained=int(
                audio.get("pre_roll_frames_retained", 0) or 0
            ),
            expected_pre_roll_frames=int(
                audio.get("expected_pre_roll_frames", 0) or 0
            ),
            beginning_clipped=(
                bool(audio.get("speech_detected", False))
                and int(audio.get("pre_roll_frames_retained", 0) or 0)
                < int(audio.get("expected_pre_roll_frames", 0) or 0)
            ),
            beginning_clipped_status=(
                "not_applicable"
                if not bool(audio.get("speech_detected", False))
                else (
                    "yes"
                    if int(audio.get("pre_roll_frames_retained", 0) or 0)
                    < int(audio.get("expected_pre_roll_frames", 0) or 0)
                    else "no"
                )
            ),
            first_speech_frame=int(audio.get("first_speech_frame", 0) or 0),
            waiting_duration_before_speech_seconds=float(
                audio.get("waiting_duration_before_speech_seconds", 0.0) or 0.0
            ),
            speech_start_timestamp_monotonic=float(
                audio.get("speech_start_timestamp_monotonic", 0.0) or 0.0
            ),
            speech_duration_seconds=float(
                audio.get("speech_duration_seconds", 0.0) or 0.0
            ),
            active_speech_window_seconds=float(
                audio.get("active_speech_window_seconds", 0.0) or 0.0
            ),
            terminal_silence_duration_seconds=float(
                audio.get("terminal_silence_duration_seconds", 0.0) or 0.0
            ),
            terminal_silence_confirmed=bool(
                audio.get("terminal_silence_confirmed", False)
            ),
            terminal_silence_reset_count=int(
                audio.get("terminal_silence_reset_count", 0) or 0
            ),
            terminal_quiet_frame_count=int(
                audio.get("terminal_quiet_frame_count", 0) or 0
            ),
            last_speech_frame=int(audio.get("last_speech_frame", 0) or 0),
            capture_completion_reason=str(
                audio.get("capture_completion_reason", "") or ""
            ),
            original_vosk_tokens=original_tokens,
            canonical_tokens_after_collapse=canonical_tokens,
            speech_frame_count=int(audio.get("speech_frame_count", 0) or 0),
            post_roll_frame_count=int(
                audio.get("post_roll_frame_count", 0) or 0
            ),
            duplicate_pcm_frame_count=int(
                audio.get("duplicate_pcm_frame_count", 0) or 0
            ),
            stale_pcm_frames_discarded=self._last_candidate_stale_frames,
            total_low_level_reads=int(
                audio.get("total_low_level_reads", 0) or 0
            ),
            valid_full_pcm_frames=int(
                audio.get("valid_full_pcm_frames", 0) or 0
            ),
            partial_reads=int(audio.get("partial_reads", 0) or 0),
            empty_reads=int(audio.get("empty_reads", 0) or 0),
            read_errors=int(audio.get("read_errors", 0) or 0),
            discarded_bytes=int(audio.get("discarded_bytes", 0) or 0),
            zero_filled_bytes=int(audio.get("zero_filled_bytes", 0) or 0),
            repeated_frame_hashes=int(
                audio.get("repeated_frame_hashes", 0) or 0
            ),
            mutable_buffer_reuse_detected=int(
                audio.get("mutable_buffer_reuse_detected", 0) or 0
            ),
            valid_microphone_bytes_delivered_to_vad=int(
                audio.get("valid_microphone_bytes_delivered_to_vad", 0) or 0
            ),
            fresh_microphone_bytes_delivered_to_vad=int(
                audio.get("fresh_microphone_bytes_delivered_to_vad", 0) or 0
            ),
            ambient_noise_floor=float(audio.get("ambient_noise_floor", 0.0) or 0.0),
            speech_start_threshold=float(
                audio.get("speech_start_threshold", 0.0) or 0.0
            ),
            speech_continue_threshold=float(
                audio.get("speech_continue_threshold", 0.0) or 0.0
            ),
            speech_end_threshold=float(
                audio.get("speech_end_threshold", 0.0) or 0.0
            ),
            rms_trace=tuple(audio.get("rms_trace", ())),
            frame_trace=tuple(audio.get("frame_trace", ())),
            source_observability_available=bool(
                audio.get("source_observability_available", False)
            ),
            source_read_sequence_start=int(
                audio.get("source_read_sequence_start", 0) or 0
            ),
            source_read_sequence_end=int(
                audio.get("source_read_sequence_end", 0) or 0
            ),
            source_frames_read_delta=int(
                audio.get("source_frames_read_delta", 0) or 0
            ),
            source_live_frame_sequence_start=int(
                audio.get("source_live_frame_sequence_start", 0) or 0
            ),
            source_live_frame_sequence_end=int(
                audio.get("source_live_frame_sequence_end", 0) or 0
            ),
            source_live_frames_read_delta=int(
                audio.get("source_live_frames_read_delta", 0) or 0
            ),
            source_bytes_read_delta=int(
                audio.get("source_bytes_read_delta", 0) or 0
            ),
            source_live_bytes_read_delta=int(
                audio.get("source_live_bytes_read_delta", 0) or 0
            ),
            listening_duration_seconds=float(
                audio.get("listening_duration_seconds", 0.0) or 0.0
            ),
            speech_start_threshold_crossing_count=int(
                audio.get("speech_start_threshold_crossing_count", 0) or 0
            ),
            maximum_consecutive_speech_evidence=int(
                audio.get("maximum_consecutive_speech_evidence", 0) or 0
            ),
            maximum_observed_rms=float(
                audio.get("maximum_observed_rms", 0.0) or 0.0
            ),
            capture_failure_stage=str(
                audio.get("capture_failure_stage", "") or ""
            ),
            trimmed_duration_seconds=float(
                audio.get("trimmed_duration_seconds", 0.0) or 0.0
            ),
            leading_trimmed_seconds=float(
                audio.get("leading_trimmed_seconds", 0.0) or 0.0
            ),
            trailing_trimmed_seconds=float(
                audio.get("trailing_trimmed_seconds", 0.0) or 0.0
            ),
            vad_transitions=tuple(audio.get("vad_transitions", ())),
            speech_to_activation_seconds=result.speech_to_activation_seconds,
            confidence_tier=result.confidence_tier,
            confirmation_count=result.confirmation_count,
            confirmation_required_count=result.confirmation_required_count,
        )

    def _reset_recognizer_attempt_state(
        self,
        reason: str,
        *,
        preserve_medium_confirmation: bool = False,
    ) -> None:
        reset = getattr(self.wake_recognizer, "reset_attempt_state", None)
        if callable(reset):
            reset(
                str(reason or "attempt_reset")[:96],
                preserve_medium_confirmation=preserve_medium_confirmation,
            )
            return
        try:
            setattr(self.wake_recognizer, "last_diagnostics", None)
        except (AttributeError, TypeError):
            pass

    def _result(
        self,
        success: bool,
        status: str,
        error_code: str = "",
        error_message: str = "",
        *,
        cleanup_status: str = "not_required",
        data: Optional[Dict[str, Any]] = None,
    ) -> WakeListenerResultV1:
        return WakeListenerResultV1(
            success=success,
            status=status,
            runtime_id=self._runtime_id,
            listener_state=self._state,
            error_code=error_code,
            error_message=str(error_message or "")[:160],
            cleanup_status=cleanup_status,
            correlation_id=new_correlation_id("wake-listener"),
            data={"safe": True, **dict(data or {})},
            metadata={"safe": True, "contains_transcript": False, "contains_audio": False},
        )


def _capture_audio_metadata(capture: Any) -> Dict[str, Any]:
    data = getattr(capture, "data", {})
    if not isinstance(data, dict):
        data = {}
    transitions = data.get("transitions", [])
    if not isinstance(transitions, list):
        transitions = []
    first_speech_frame = max(
        0,
        int(getattr(capture, "first_speech_frame", 0) or 0),
    )
    if first_speech_frame == 0:
        first_speech_frame = next(
            (
                max(0, int(item.get("frame", 0) or 0))
                for item in transitions
                if isinstance(item, dict) and item.get("to") == "SPEECH"
            ),
            0,
        )
    frame_duration_seconds = float(data.get("frame_duration_ms", 0) or 0) / 1000.0
    expected_pre_roll_frames = (
        int(round(float(data.get("pre_roll_frames", 0) or 0)))
        if data.get("pre_roll_frames") is not None
        else 0
    )
    metadata = {
        "sample_rate_hz": int(
            getattr(capture, "normalized_sample_rate_hz", 0)
            or getattr(capture, "sample_rate_hz", 0)
            or 0
        ),
        "channels": int(
            getattr(capture, "normalized_channels", 0)
            or getattr(capture, "channels", 0)
            or 0
        ),
        "sample_width_bytes": int(
            getattr(capture, "normalized_sample_width_bytes", 0)
            or getattr(capture, "sample_width_bytes", 0)
            or 0
        ),
        "duration_seconds": float(getattr(capture, "duration_seconds", 0.0) or 0.0),
        "raw_duration_seconds": float(
            getattr(capture, "raw_duration_seconds", 0.0) or 0.0
        ),
        "assembled_duration_seconds": float(
            getattr(capture, "assembled_duration_seconds", 0.0) or 0.0
        ),
        "normalized_duration_seconds": float(
            getattr(capture, "normalized_duration_seconds", 0.0) or 0.0
        ),
        "whisper_input_duration_seconds": float(
            getattr(capture, "whisper_input_duration_seconds", 0.0) or 0.0
        ),
        "total_frames_read": int(getattr(capture, "total_frames_read", 0) or 0),
        "final_assembled_sample_count": int(
            getattr(capture, "final_assembled_sample_count", 0) or 0
        ),
        "normalized_sample_count": int(
            getattr(capture, "normalized_sample_count", 0) or 0
        ),
        "capture_stop_reason": str(getattr(capture, "stop_reason", "") or ""),
        "pre_roll_frames_retained": int(
            getattr(capture, "pre_roll_frames_retained", 0) or 0
        ),
        "expected_pre_roll_frames": expected_pre_roll_frames,
        "first_speech_frame": first_speech_frame,
        "waiting_duration_before_speech_seconds": float(
            getattr(capture, "waiting_duration_before_speech_seconds", 0.0) or 0.0
        ),
        "speech_start_timestamp_monotonic": float(
            getattr(capture, "speech_start_timestamp_monotonic", 0.0) or 0.0
        ),
        "speech_duration_seconds": float(
            getattr(capture, "speech_duration_seconds", 0.0) or 0.0
        ),
        "active_speech_window_seconds": float(
            getattr(capture, "active_speech_window_seconds", 0.0) or 0.0
        ),
        "terminal_silence_duration_seconds": float(
            getattr(capture, "silence_duration_at_stop_seconds", 0.0) or 0.0
        ),
        "terminal_silence_confirmed": bool(
            getattr(capture, "terminal_silence_confirmed", False)
        ),
        "terminal_silence_reset_count": int(
            getattr(capture, "terminal_silence_reset_count", 0) or 0
        ),
        "terminal_quiet_frame_count": int(
            data.get("terminal_quiet_frame_count", 0) or 0
        ),
        "speech_frame_count": int(
            getattr(capture, "speech_frame_count", 0)
            or data.get("speech_frames_retained", 0)
            or 0
        ),
        "last_speech_frame": int(
            getattr(capture, "last_speech_frame", 0)
            or data.get("last_speech_frame", 0)
            or 0
        ),
        "capture_completion_reason": str(
            getattr(capture, "completion_reason", "")
            or data.get("completion_reason", "")
            or ""
        ),
        "post_roll_frame_count": int(
            data.get("post_roll_frames_retained", 0) or 0
        ),
        "duplicate_pcm_frame_count": int(
            data.get("duplicate_frame_append_count", 0) or 0
        ),
        "speech_start_offset_seconds": float(
            getattr(capture, "speech_start_offset_seconds", 0.0) or 0.0
        ),
        "frame_duration_seconds": frame_duration_seconds,
        "vad_transitions": tuple(
            dict(item) for item in transitions if isinstance(item, dict)
        ),
        "speech_detected": bool(getattr(capture, "speech_detected", False)),
        "ambient_rms": round(float(getattr(capture, "ambient_rms", 0.0) or 0.0), 3),
        "speech_rms": round(float(getattr(capture, "speech_rms", 0.0) or 0.0), 3),
        "peak_amplitude": int(getattr(capture, "peak_amplitude", 0) or 0),
        "derived_speech_start_rms": round(
            float(getattr(capture, "derived_speech_start_rms", 0.0) or 0.0), 3
        ),
        "derived_speech_continue_rms": round(
            float(getattr(capture, "derived_speech_continue_rms", 0.0) or 0.0), 3
        ),
        "derived_silence_rms": round(
            float(getattr(capture, "derived_silence_rms", 0.0) or 0.0), 3
        ),
        "ambient_noise_floor": round(
            float(getattr(capture, "ambient_noise_floor", 0.0) or 0.0),
            3,
        ),
        "speech_start_threshold": round(
            float(getattr(capture, "derived_speech_start_rms", 0.0) or 0.0),
            3,
        ),
        "speech_continue_threshold": round(
            float(getattr(capture, "derived_speech_continue_rms", 0.0) or 0.0),
            3,
        ),
        "speech_end_threshold": round(
            float(getattr(capture, "derived_speech_continue_rms", 0.0) or 0.0),
            3,
        ),
        "rms_trace": tuple(
            dict(item)
            for item in data.get("rms_trace", [])
            if isinstance(item, dict)
        ),
        "frame_trace": tuple(
            dict(item)
            for item in data.get("frame_trace", [])
            if isinstance(item, dict)
        ),
        "source_observability_available": bool(
            data.get("source_observability_available", False)
        ),
        "source_read_sequence_start": int(
            data.get("source_read_sequence_start", 0) or 0
        ),
        "source_read_sequence_end": int(
            data.get("source_read_sequence_end", 0) or 0
        ),
        "source_frames_read_delta": int(
            data.get("source_frames_read_delta", 0) or 0
        ),
        "source_live_frame_sequence_start": int(
            data.get("source_live_frame_sequence_start", 0) or 0
        ),
        "source_live_frame_sequence_end": int(
            data.get("source_live_frame_sequence_end", 0) or 0
        ),
        "source_live_frames_read_delta": int(
            data.get("source_live_frames_read_delta", 0) or 0
        ),
        "source_bytes_read_delta": int(
            data.get("source_bytes_read_delta", 0) or 0
        ),
        "source_live_bytes_read_delta": int(
            data.get("source_live_bytes_read_delta", 0) or 0
        ),
        "total_low_level_reads": int(
            data.get("total_low_level_reads", 0) or 0
        ),
        "valid_full_pcm_frames": int(
            data.get("valid_full_pcm_frames", 0) or 0
        ),
        "partial_reads": int(data.get("partial_reads", 0) or 0),
        "empty_reads": int(data.get("empty_reads", 0) or 0),
        "read_errors": int(data.get("read_errors", 0) or 0),
        "discarded_bytes": int(data.get("discarded_bytes", 0) or 0),
        "zero_filled_bytes": int(data.get("zero_filled_bytes", 0) or 0),
        "repeated_frame_hashes": int(
            data.get("repeated_frame_hashes", 0) or 0
        ),
        "mutable_buffer_reuse_detected": int(
            data.get("mutable_buffer_reuse_detected", 0) or 0
        ),
        "valid_microphone_bytes_delivered_to_vad": int(
            data.get("valid_microphone_bytes_delivered_to_vad", 0) or 0
        ),
        "fresh_microphone_bytes_delivered_to_vad": int(
            data.get("fresh_microphone_bytes_delivered_to_vad", 0) or 0
        ),
        "listening_duration_seconds": float(
            data.get("listening_duration_seconds", 0.0) or 0.0
        ),
        "speech_start_threshold_crossing_count": int(
            data.get("speech_start_threshold_crossing_count", 0) or 0
        ),
        "maximum_consecutive_speech_evidence": int(
            data.get("maximum_consecutive_speech_evidence", 0) or 0
        ),
        "maximum_observed_rms": float(
            data.get("maximum_observed_rms", 0.0) or 0.0
        ),
        "capture_failure_stage": str(data.get("capture_failure_stage", "") or ""),
    }
    whisper_path = Path(
        str(
            getattr(capture, "final_whisper_input_path", "")
            or getattr(capture, "normalized_wav_path", "")
            or getattr(capture, "wav_path", "")
            or ""
        )
    )
    if whisper_path.is_file():
        try:
            wav = _read_wav_metadata(whisper_path)
            metadata["normalized_duration_seconds"] = float(wav["duration_seconds"])
            metadata["whisper_input_duration_seconds"] = float(wav["duration_seconds"])
            metadata["normalized_sample_count"] = int(wav["sample_count"])
        except (OSError, EOFError, ValueError, wave.Error):
            pass
    assembled_path = Path(str(getattr(capture, "assembled_wav_path", "") or ""))
    if assembled_path.is_file():
        try:
            metadata["assembled_duration_seconds"] = float(
                _read_wav_metadata(assembled_path)["duration_seconds"]
            )
        except (OSError, EOFError, ValueError, wave.Error):
            pass
    raw_path = Path(str(getattr(capture, "raw_wav_path", "") or ""))
    if raw_path.is_file():
        try:
            metadata["raw_duration_seconds"] = float(
                _read_wav_metadata(raw_path)["duration_seconds"]
            )
        except (OSError, EOFError, ValueError, wave.Error):
            pass
    return metadata


def _read_wav_metadata(path: Path) -> Dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 44:
        raise ValueError("empty_or_missing_wav")
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        sample_count = source.getnframes()
    if channels <= 0 or sample_width <= 0 or sample_rate <= 0 or sample_count <= 0:
        raise ValueError("invalid_wav_header")
    return {
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "sample_count": sample_count,
        "byte_count": sample_count * channels * sample_width,
        "duration_seconds": sample_count / sample_rate,
    }


def _recognition_metadata(recognition: Any) -> Dict[str, Any]:
    if recognition is None:
        return {}
    return {
        "status": _result_status(recognition),
        "recognizer_name": str(getattr(recognition, "recognizer_name", "") or ""),
        "confidence": getattr(recognition, "confidence", None),
        "confidence_available": bool(
            getattr(recognition, "confidence_available", False)
        ),
        "minimum_word_confidence": getattr(
            recognition,
            "minimum_word_confidence",
            None,
        ),
        "mean_word_confidence": getattr(
            recognition,
            "mean_word_confidence",
            None,
        ),
        "canonical_confidence": getattr(
            recognition,
            "canonical_confidence",
            None,
        ),
        "duplicate_collapse_used": bool(
            getattr(recognition, "duplicate_collapse_used", False)
        ),
        "confidence_tier": str(
            getattr(recognition, "confidence_tier", "") or ""
        ),
        "confirmation_required": bool(
            getattr(recognition, "confirmation_required", False)
        ),
        "confirmation_count": int(
            getattr(recognition, "confirmation_count", 0) or 0
        ),
        "confirmation_required_count": int(
            getattr(recognition, "confirmation_required_count", 0) or 0
        ),
        "processing_time_seconds": float(
            getattr(recognition, "processing_time_seconds", 0.0) or 0.0
        ),
        "model_path": str(getattr(recognition, "model_path", "") or ""),
    }


def _remove_turn_directory(path: Path) -> str:
    try:
        shutil.rmtree(path)
        return "removed"
    except FileNotFoundError:
        return "already_removed"
    except OSError:
        return "cleanup_failed"


def _append_bounded(values: list[str], value: str, *, limit: int = 32) -> None:
    values.append(str(value or "")[:192])
    if len(values) > limit:
        del values[: len(values) - limit]


def _safe_call(adapter: Any, method_name: str) -> Any:
    method = getattr(adapter, method_name, None)
    if not callable(method):
        return {"success": False, "status": "unsupported", "error_message": f"{method_name}_unsupported"}
    try:
        return method()
    except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
        return {
            "success": False,
            "status": f"{method_name}_exception",
            "error_message": f"{error.__class__.__name__}:{str(error)[:120]}",
        }


def _result_success(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("success"))
    return bool(getattr(result, "success", False))


def _result_status(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("status") or "")
    return str(getattr(result, "status", "") or "")


def _result_error(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("error_message") or result.get("status") or "adapter_failed")[:160]
    return str(
        getattr(result, "error_message", "")
        or getattr(result, "status", "")
        or "adapter_failed"
    )[:160]
