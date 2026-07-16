from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
from threading import RLock
import tempfile
import time
from typing import Any, Callable, Dict, Optional
import wave

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
    WakeLocalDiagnostics,
    WakeListenerConfig,
    clean_wake_transcript,
    normalize_wake_phrase,
)
from core.WakeRecognizer import WakeRecognizerLocalDiagnostics
from core.WakeAudio import WakeAudioTrimResult, trim_canonical_wake_wav
from core.VoiceActivityDetection import (
    VAD_STATUS_CANCELLED,
    VAD_STATUS_DEVICE_ERROR,
    VAD_STATUS_NO_SPEECH_TIMEOUT,
    VAD_STATUS_TIMEOUT,
)


STANDBY_STREAM_OWNER = "standby_wake_listener"


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
        self._pending_diagnostics: Optional[WakeLocalDiagnostics] = None
        self._stream_handle: Any = None
        self._stream_open_count = 0
        self._stream_close_count = 0
        self._calibration_count = 0
        self._candidate_count = 0
        self._calibration_at: Optional[float] = None
        self._calibration_thresholds: Any = None
        self._calibration_statistics: Any = None
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
        self.last_result: Optional[StandbyListenResultV1] = None
        self.last_diagnostics: Optional[WakeLocalDiagnostics] = None

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
        health = self.health(runtime_id=self._runtime_id)
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
        return self._result(True, "started", data=self._stream_metrics())

    def health(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        with self._lock:
            if runtime_id:
                self._runtime_id = str(runtime_id)
        microphone = _safe_call(self.microphone_adapter, "health_check")
        if not _result_success(microphone):
            return self._health_failure("microphone_unhealthy", _result_error(microphone))
        recognizer = _safe_call(self.wake_recognizer, "health_check")
        if not _result_success(recognizer):
            return self._health_failure("wake_recognizer_unhealthy", _result_error(recognizer))
        return self._result(
            True,
            "healthy",
            data={
                "microphone_status": _result_status(microphone),
                "recognizer_status": _result_status(recognizer),
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
        try:
            open_reason = str(reason or "standby_entered")[:80]
            handle = self.microphone_adapter.open_persistent_stream(
                owner=STANDBY_STREAM_OWNER,
                device=self.config.microphone_device,
            )
            with self._lock:
                self._stream_handle = handle
                self._stream_open_count += 1
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
                self._close_standby_stream("calibration_failed")
                return self._result(
                    False,
                    "calibration_failed",
                    str(getattr(calibration, "error_code", "") or "wake_calibration_failed"),
                    str(
                        getattr(calibration, "error_message", "")
                        or getattr(calibration, "status", "")
                        or "wake calibration failed"
                    ),
                )
            return self._result(
                True,
                "standby_stream_ready",
                data=self._stream_metrics(),
            )
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            self._close_standby_stream("stream_open_failed")
            return self._result(
                False,
                "stream_open_failed",
                "wake_stream_open_failed",
                f"{error.__class__.__name__}:{str(error)[:120]}",
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

    def listen_once(self, request: WakeListenerRequestV1) -> StandbyListenResultV1:
        with self._lock:
            self._last_cleanup_status = "not_required"
            self._pending_diagnostics = None
            self._last_candidate_stale_frames = 0
        try:
            result = self._listen_once_unfinalized(request)
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            result = self._listen_failure(
                request,
                "wake_listener_exception",
                f"{error.__class__.__name__}:{str(error)[:120]}",
            )
        with self._lock:
            cleanup_status = self._last_cleanup_status
        result = replace(result, cleanup_status=cleanup_status)
        self._finalize_local_diagnostics(result)
        self.last_result = result
        return result

    def _listen_once_unfinalized(self, request: WakeListenerRequestV1) -> StandbyListenResultV1:
        if not isinstance(request, WakeListenerRequestV1):
            raise TypeError("request must be WakeListenerRequestV1")
        if request.retain_diagnostic_audio and not (
            request.diagnostic_wake or self.config.diagnostic_wake
        ):
            return self._listen_failure(
                request,
                "wake_diagnostic_retention_not_authorized",
                "wake diagnostic retention requires diagnostic wake mode",
            )
        with self._lock:
            self._listen_count += 1
            if self._state == WAKE_LISTENER_STOPPED:
                return self._listen_failure(request, "listener_not_started", "wake listener is stopped")
            if self._cancelled:
                return self._cancelled_result(request)
            self._state = WAKE_LISTENER_LISTENING
            self._candidate_count += 1
        started_at = self.clock()
        turn_directory = self._create_turn_directory()
        output_path = turn_directory / "wake_candidate.wav"
        with self._lock:
            self._active_turn_directory = turn_directory
        try:
            stream_error = self._ensure_standby_stream(request.runtime_id)
            if stream_error:
                return self._listen_failure(
                    request,
                    "wake_stream_unavailable",
                    stream_error,
                )
            with self._lock:
                handle = self._stream_handle
                thresholds = self._calibration_thresholds
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
                return self._cancelled_result(request)
            capture_status = str(getattr(capture, "status", "") or "")
            if capture_status == VAD_STATUS_NO_SPEECH_TIMEOUT and not bool(
                getattr(capture, "speech_detected", False)
            ):
                return self._standby_result(
                    request,
                    success=True,
                    status=WAKE_STATUS_NO_SPEECH,
                    stop_reason=capture_status,
                    capture=capture,
                    started_at=started_at,
                )
            if not _result_success(capture) or not bool(getattr(capture, "speech_detected", False)):
                if capture_status in {VAD_STATUS_DEVICE_ERROR, VAD_STATUS_TIMEOUT}:
                    self._close_standby_stream(
                        capture_status,
                        handoff_source=STANDBY_STREAM_OWNER,
                        handoff_destination="device_recovery",
                    )
                return self._capture_failure(request, capture, started_at)
            with self._lock:
                self._speech_count += 1
            self._reset_candidate_stream(handle)
            wav_path = str(
                getattr(capture, "normalized_wav_path", "")
                or getattr(capture, "final_whisper_input_path", "")
                or getattr(capture, "wav_path", "")
            )
            if not wav_path or not Path(wav_path).is_file():
                return self._listen_failure(request, "wake_audio_missing", "validated wake WAV is missing")
            duration_error = self._validate_capture_durations(
                capture,
                Path(wav_path),
                listener_timeout_seconds=request.listener_timeout_seconds,
            )
            if duration_error:
                return self._standby_result(
                    request,
                    success=False,
                    status=WAKE_STATUS_FAILED,
                    stop_reason="wake_audio_duration_exceeded",
                    error_code="wake_audio_duration_exceeded",
                    error_message=duration_error,
                    capture=capture,
                    started_at=started_at,
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
                return self._standby_result(
                    request,
                    success=False,
                    status=WAKE_STATUS_FAILED,
                    stop_reason="wake_audio_trim_failed",
                    error_code="wake_audio_trim_failed",
                    error_message=f"{error.__class__.__name__}:{str(error)[:120]}",
                    capture=capture,
                    started_at=started_at,
                )
            recognition = self.wake_recognizer.recognize_wav(
                WakeRecognizerRequestV1(
                    runtime_id=request.runtime_id,
                    lifecycle_state=request.lifecycle_state,
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
                        self.config.maximum_duplicate_collapse_audio_seconds
                    ),
                    correlation_id=request.correlation_id,
                    metadata={"safe": True, "contains_transcript": False},
                )
            )
            self._reset_candidate_stream(handle)
            if not _result_success(recognition):
                return self._recognition_failure(
                    request,
                    recognition,
                    capture,
                    started_at,
                    trim=trim,
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
            )
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            self._close_standby_stream(
                "capture_exception",
                handoff_source=STANDBY_STREAM_OWNER,
                handoff_destination="device_recovery",
            )
            return self._listen_failure(
                request,
                "wake_listener_exception",
                f"{error.__class__.__name__}:{str(error)[:120]}",
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
                    self._state = WAKE_LISTENER_READY

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
        self._close_standby_stream("recalibration_failed")
        return str(
            getattr(calibration, "error_message", "")
            or getattr(calibration, "status", "")
            or "wake recalibration failed"
        )

    def _reset_candidate_stream(self, handle: Any) -> None:
        reset = getattr(self.microphone_adapter, "reset_persistent_candidate", None)
        if not callable(reset):
            clear = getattr(getattr(handle, "frame_source", None), "clear_history", None)
            if callable(clear):
                clear()
            return
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

    def _calibrate_stream(self, handle: Any, *, reason: str) -> Any:
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
                "calibration_confirm_non_speech": True,
                "calibration_maximum_seconds": self.config.calibration_maximum_seconds,
            },
        )
        result = self.microphone_adapter.calibrate_persistent_stream(
            handle,
            request,
            cancel_requested=self._is_cancelled,
        )
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
        return result

    def _close_standby_stream(
        self,
        reason: str,
        *,
        handoff_source: str = "",
        handoff_destination: str = "",
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
        if gate_owned:
            self._end_capture_gate()
        return success

    def _stream_metrics(self) -> Dict[str, Any]:
        with self._lock:
            handle = self._stream_handle
            thresholds = self._calibration_thresholds
            statistics = self._calibration_statistics
            return {
                "stream_active": bool(
                    handle is not None and not bool(getattr(handle, "closed", False))
                ),
                "capture_owner": (
                    STANDBY_STREAM_OWNER if handle is not None else ""
                ),
                "stream_open_count": self._stream_open_count,
                "stream_close_count": self._stream_close_count,
                "calibration_count": self._calibration_count,
                "candidate_count": self._candidate_count,
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

    def _health_failure(self, code: str, message: str) -> WakeListenerResultV1:
        with self._lock:
            self._failure_count += 1
        return self._result(False, "unhealthy", code, message)

    def _capture_failure(self, request: WakeListenerRequestV1, capture: Any, started_at: float) -> StandbyListenResultV1:
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
        )

    def _recognition_failure(
        self,
        request: WakeListenerRequestV1,
        recognition: Any,
        capture: Any,
        started_at: float,
        *,
        trim: Optional[WakeAudioTrimResult] = None,
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
            data={"recognition_status": _result_status(recognition)},
        )

    def _cancelled_result(self, request: WakeListenerRequestV1) -> StandbyListenResultV1:
        result = StandbyListenResultV1(
            success=False,
            status=WAKE_STATUS_CANCELLED,
            runtime_id=request.runtime_id,
            lifecycle_state=request.lifecycle_state,
            listener_state=WAKE_LISTENER_CANCELLING,
            stop_reason="cancelled",
            error_code="wake_listener_cancelled",
            correlation_id=request.correlation_id,
            metadata={"safe": True, "contains_transcript": False},
        )
        self.last_result = result
        return result

    def _listen_failure(self, request: WakeListenerRequestV1, code: str, message: str) -> StandbyListenResultV1:
        with self._lock:
            self._failure_count += 1
        self._bounded_retry_pause()
        result = StandbyListenResultV1(
            success=False,
            status=WAKE_STATUS_FAILED,
            runtime_id=request.runtime_id,
            lifecycle_state=request.lifecycle_state,
            listener_state=self._state,
            stop_reason=code,
            error_code=code,
            error_message=str(message or code)[:160],
            correlation_id=request.correlation_id,
            metadata={"safe": True, "contains_transcript": False},
        )
        self.last_result = result
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
    ) -> StandbyListenResultV1:
        audio = _capture_audio_metadata(capture)
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
            terminal_silence_duration_seconds=float(
                audio.get("terminal_silence_duration_seconds", 0.0) or 0.0
            ),
            terminal_quiet_frame_count=int(
                audio.get("terminal_quiet_frame_count", 0) or 0
            ),
            speech_frame_count=int(audio.get("speech_frame_count", 0) or 0),
            post_roll_frame_count=int(
                audio.get("post_roll_frame_count", 0) or 0
            ),
            duplicate_pcm_frame_count=int(
                audio.get("duplicate_pcm_frame_count", 0) or 0
            ),
            stale_pcm_frames_discarded=self._last_candidate_stale_frames,
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
        if bool(request.diagnostic_wake or self.config.diagnostic_wake) and bool(
            getattr(capture, "speech_detected", False)
        ):
            self._remember_local_diagnostics(
                request=request,
                result=result,
                audio=audio,
                capture=capture,
                recognition=recognition_metadata,
                recognizer_input_path=(trim.output_path if trim is not None else ""),
            )
        self.last_result = result
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

    def _remember_local_diagnostics(
        self,
        *,
        request: WakeListenerRequestV1,
        result: StandbyListenResultV1,
        audio: Dict[str, Any],
        capture: Any,
        recognition: Dict[str, Any],
        recognizer_input_path: str,
    ) -> None:
        stream = self._stream_metrics()
        recognizer_input = str(
            recognizer_input_path
            or getattr(capture, "normalized_wav_path", "")
            or getattr(capture, "final_whisper_input_path", "")
            or getattr(capture, "wav_path", "")
            or ""
        )
        local = getattr(self.wake_recognizer, "last_diagnostics", None)
        raw_text = str(getattr(local, "recognized_text", "") or "")
        self._pending_diagnostics = WakeLocalDiagnostics(
            raw_transcript=raw_text,
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
            classification=(
                "accepted"
                if result.command_category != WAKE_CATEGORY_NON_WAKE
                else "rejected"
            ),
            rejection_reason=(
                result.rejection_reason
                or (
                    ""
                    if result.command_category != WAKE_CATEGORY_NON_WAKE
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
            wake_model_path=str(recognition.get("model_path", "") or self.config.vosk_model_path),
            lifecycle_state=request.lifecycle_state,
            retained_audio_path=recognizer_input,
            recognizer_name=str(recognition.get("recognizer_name", "")),
            raw_recognition_result=str(
                getattr(local, "raw_recognition_result", "") or ""
            ),
            recognition_status=str(recognition.get("status", "")),
            recognition_confidence=recognition.get("confidence"),
            recognition_confidence_available=bool(
                recognition.get("confidence_available", False)
            ),
            minimum_word_confidence=recognition.get("minimum_word_confidence"),
            mean_word_confidence=recognition.get("mean_word_confidence"),
            canonical_confidence=recognition.get("canonical_confidence"),
            duplicate_collapse_used=bool(
                recognition.get("duplicate_collapse_used", False)
            ),
            recognition_processing_time_seconds=float(
                recognition.get("processing_time_seconds", 0.0)
            ),
            recognizer_model_path=str(
                recognition.get("model_path", "") or self.config.vosk_model_path
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
            pre_roll_frames_retained=int(
                audio.get("pre_roll_frames_retained", 0) or 0
            ),
            expected_pre_roll_frames=int(
                audio.get("expected_pre_roll_frames", 0) or 0
            ),
            beginning_clipped=(
                int(audio.get("pre_roll_frames_retained", 0) or 0)
                < int(audio.get("expected_pre_roll_frames", 0) or 0)
            ),
            first_speech_frame=int(audio.get("first_speech_frame", 0) or 0),
            terminal_silence_duration_seconds=float(
                audio.get("terminal_silence_duration_seconds", 0.0) or 0.0
            ),
            terminal_quiet_frame_count=int(
                audio.get("terminal_quiet_frame_count", 0) or 0
            ),
            speech_frame_count=int(audio.get("speech_frame_count", 0) or 0),
            post_roll_frame_count=int(
                audio.get("post_roll_frame_count", 0) or 0
            ),
            duplicate_pcm_frame_count=int(
                audio.get("duplicate_pcm_frame_count", 0) or 0
            ),
            stale_pcm_frames_discarded=self._last_candidate_stale_frames,
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

    def _finalize_local_diagnostics(self, result: StandbyListenResultV1) -> None:
        with self._lock:
            diagnostics = self._pending_diagnostics
            self._pending_diagnostics = None
        if diagnostics is None:
            return
        retained_path = diagnostics.retained_audio_path
        if result.cleanup_status != "retained_by_explicit_request" or not Path(retained_path).is_file():
            retained_path = ""
        diagnostics = replace(
            diagnostics,
            retained_audio_path=retained_path,
            cleanup_status=result.cleanup_status,
        )
        self.last_diagnostics = diagnostics
        if callable(self.diagnostic_callback):
            try:
                self.diagnostic_callback(diagnostics)
            except (OSError, RuntimeError, TypeError, ValueError):
                return

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
    first_speech_frame = next(
        (
            int(item.get("frame", 0) or 0)
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
        "terminal_silence_duration_seconds": float(
            getattr(capture, "silence_duration_at_stop_seconds", 0.0) or 0.0
        ),
        "terminal_quiet_frame_count": int(
            data.get("terminal_quiet_frame_count", 0) or 0
        ),
        "speech_frame_count": int(
            getattr(capture, "speech_frame_count", 0)
            or data.get("speech_frames_retained", 0)
            or 0
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
