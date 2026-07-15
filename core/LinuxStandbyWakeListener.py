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
    WakeListenerRequestV1,
    WakeListenerResultV1,
    WakeListenerSnapshotV1,
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
    classify_wake_transcript,
    clean_wake_transcript,
    normalize_wake_phrase,
)
from core.VoiceActivityDetection import (
    VAD_STATUS_CANCELLED,
    VAD_STATUS_NO_SPEECH_TIMEOUT,
    VAD_STATUS_TIMEOUT,
)


_EMPTY_TRANSCRIPTION_STATUSES = {
    "no_transcription",
    "no_usable_speech",
    "audio_silent",
    "audio_below_threshold",
}


class LinuxStandbyWakeListener:
    """Foreground ALSA/VAD candidate capture followed by bounded local Whisper."""

    def __init__(
        self,
        *,
        microphone_adapter: Any,
        speech_to_text_adapter: Any,
        config: Optional[WakeListenerConfig | Dict[str, Any]] = None,
        project_root: Optional[str | Path] = None,
        voice_io_gate: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        diagnostic_callback: Optional[Callable[[WakeLocalDiagnostics], None]] = None,
    ) -> None:
        if not callable(getattr(microphone_adapter, "record_until_silence", None)):
            raise ValueError("microphone_adapter must support record_until_silence")
        if not callable(getattr(speech_to_text_adapter, "transcribe_wav", None)):
            raise ValueError("speech_to_text_adapter must support transcribe_wav")
        self.microphone_adapter = microphone_adapter
        self.speech_to_text_adapter = speech_to_text_adapter
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
        self.last_result: Optional[StandbyListenResultV1] = None
        self.last_diagnostics: Optional[WakeLocalDiagnostics] = None

    def start(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        with self._lock:
            self._runtime_id = str(runtime_id or self._runtime_id)
            if self._state == WAKE_LISTENER_READY:
                return self._result(True, "already_started")
            if not self.config.enabled:
                return self._result(False, "disabled", "wake_listener_disabled", "wake listener is disabled")
            self._cancelled = False
        microphone = _safe_call(self.microphone_adapter, "start")
        if not _result_success(microphone):
            return self._start_failure("microphone_start_failed", _result_error(microphone))
        health = self.health(runtime_id=self._runtime_id)
        if not health.success:
            _safe_call(self.microphone_adapter, "stop")
            return health
        with self._lock:
            self._state = WAKE_LISTENER_READY
        return self._result(True, "started")

    def health(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        with self._lock:
            if runtime_id:
                self._runtime_id = str(runtime_id)
        microphone = _safe_call(self.microphone_adapter, "health_check")
        if not _result_success(microphone):
            return self._health_failure("microphone_unhealthy", _result_error(microphone))
        speech_to_text = _safe_call(self.speech_to_text_adapter, "health_check")
        if not _result_success(speech_to_text):
            return self._health_failure("wake_whisper_unhealthy", _result_error(speech_to_text))
        return self._result(
            True,
            "healthy",
            data={
                "microphone_status": _result_status(microphone),
                "speech_to_text_status": _result_status(speech_to_text),
                "offline": True,
                "background_thread": False,
            },
        )

    def listen_once(self, request: WakeListenerRequestV1) -> StandbyListenResultV1:
        with self._lock:
            self._last_cleanup_status = "not_required"
            self._pending_diagnostics = None
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
        started_at = self.clock()
        turn_directory = self._create_turn_directory()
        output_path = turn_directory / "wake_candidate.wav"
        with self._lock:
            self._active_turn_directory = turn_directory
        capture_started = False
        try:
            gate_result = self._begin_capture_gate(request.listener_timeout_seconds)
            if gate_result:
                return self._listen_failure(request, "capture_gate_unavailable", gate_result)
            capture_started = True
            capture = self.microphone_adapter.record_until_silence(
                output_path,
                device=request.microphone_device or self.config.microphone_device,
                calibration_enabled=self.config.calibration_enabled,
                calibration_duration_seconds=self.config.calibration_duration_seconds,
                speech_start_rms=self.config.speech_start_rms,
                speech_continue_rms=self.config.speech_continue_rms,
                silence_rms=self.config.silence_rms,
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
                frame_duration_ms=self.config.frame_duration_ms,
                frame_read_timeout_seconds=self.config.frame_read_timeout_seconds,
                minimum_speech_start_rms=self.config.minimum_speech_start_rms,
                maximum_speech_start_rms=self.config.maximum_speech_start_rms,
                minimum_speech_continue_rms=self.config.minimum_speech_continue_rms,
                maximum_speech_continue_rms=self.config.maximum_speech_continue_rms,
                minimum_silence_rms=self.config.minimum_silence_rms,
                maximum_silence_rms=self.config.maximum_silence_rms,
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
            if capture_status in {VAD_STATUS_NO_SPEECH_TIMEOUT, VAD_STATUS_TIMEOUT} and not bool(
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
                return self._capture_failure(request, capture, started_at)
            with self._lock:
                self._speech_count += 1
            wav_path = str(
                getattr(capture, "final_whisper_input_path", "")
                or getattr(capture, "normalized_wav_path", "")
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
            transcription = self.speech_to_text_adapter.transcribe_wav(
                wav_path,
                language=request.language or self.config.language,
                timeout_seconds=max(0.1, float(request.listener_timeout_seconds)),
            )
            transcript = str(getattr(transcription, "text", "") or "").strip()
            transcription_status = _result_status(transcription)
            if not _result_success(transcription):
                if transcription_status in _EMPTY_TRANSCRIPTION_STATUSES:
                    return self._standby_result(
                        request,
                        success=True,
                        status=WAKE_STATUS_NON_WAKE_SPEECH,
                        stop_reason="empty_wake_transcript",
                        capture=capture,
                        started_at=started_at,
                        transcription=transcription,
                        data={"transcription_status": transcription_status, "transcript_length": 0},
                    )
                return self._transcription_failure(request, transcription, capture, started_at)
            if not transcript:
                return self._standby_result(
                    request,
                    success=True,
                    status=WAKE_STATUS_NON_WAKE_SPEECH,
                    stop_reason="empty_wake_transcript",
                    capture=capture,
                    started_at=started_at,
                    transcription=transcription,
                )
            detection = classify_wake_transcript(
                transcript,
                wake_phrase_aliases=(
                    request.wake_phrase_aliases or self.config.wake_phrase_aliases
                ),
                wake_phrase_prefixes=(
                    request.wake_phrase_prefixes or self.config.wake_phrase_prefixes
                ),
                filler_prefixes=self.config.filler_prefixes,
                standby_phrases=request.standby_phrases,
                shutdown_phrases=request.shutdown_phrases,
                correlation_id=request.correlation_id,
                runtime_id=request.runtime_id,
            )
            if detection.wake_detected:
                with self._lock:
                    self._wake_count += 1
            return self._standby_result(
                request,
                success=detection.success,
                status=detection.status,
                speech_detected=True,
                wake_detected=detection.wake_detected,
                command_category=detection.command_category,
                normalized_wake_phrase=detection.normalized_wake_phrase,
                matched_phrase=detection.matched_phrase,
                selected_alias=detection.selected_alias,
                selected_wake_phrase=detection.selected_wake_phrase,
                canonical_wake_phrase=detection.canonical_wake_phrase,
                rejection_reason=detection.rejection_reason,
                stop_reason=detection.status,
                capture=capture,
                started_at=started_at,
                raw_transcript=transcript,
                transcription=transcription,
                data={
                    "transcription_status": transcription_status,
                    "transcript_length": len(transcript),
                    "contains_transcript": False,
                },
            )
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            return self._listen_failure(
                request,
                "wake_listener_exception",
                f"{error.__class__.__name__}:{str(error)[:120]}",
            )
        finally:
            if capture_started:
                self._end_capture_gate()
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
        cancel = getattr(self.microphone_adapter, "cancel_current", None)
        if callable(cancel):
            try:
                cancel()
            except (OSError, RuntimeError):
                pass
        return self._result(True, "cancelled", cleanup_status="capture_cancel_requested")

    def stop(self, reason: str = "stopped") -> WakeListenerResultV1:
        self.cancel(reason)
        stopped = _safe_call(self.microphone_adapter, "stop")
        with self._lock:
            self._state = WAKE_LISTENER_STOPPED
            self._last_stop_reason = str(reason or "stopped")[:80]
        return self._result(
            _result_success(stopped),
            "stopped" if _result_success(stopped) else "stop_failed",
            "" if _result_success(stopped) else "microphone_stop_failed",
            "" if _result_success(stopped) else _result_error(stopped),
            cleanup_status="complete" if _result_success(stopped) else "partial",
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
                last_stop_reason=self._last_stop_reason,
                metadata={
                    "safe": True,
                    "background_thread": False,
                    "retained_turn_count": len(self._retained_directories),
                    "last_cleanup_status": self._last_cleanup_status,
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

    def _transcription_failure(
        self,
        request: WakeListenerRequestV1,
        transcription: Any,
        capture: Any,
        started_at: float,
    ) -> StandbyListenResultV1:
        with self._lock:
            self._failure_count += 1
        self._bounded_retry_pause()
        return self._standby_result(
            request,
            success=False,
            status=WAKE_STATUS_FAILED,
            stop_reason=_result_status(transcription) or "wake_transcription_failed",
            error_code="wake_transcription_failed",
            error_message=_result_error(transcription),
            capture=capture,
            started_at=started_at,
            transcription=transcription,
            data={"transcription_status": _result_status(transcription)},
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
        rejection_reason: str = "",
        error_code: str = "",
        error_message: str = "",
        raw_transcript: str = "",
        transcription: Any = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> StandbyListenResultV1:
        audio = _capture_audio_metadata(capture)
        transcription_metadata = _transcription_metadata(transcription)
        candidate_duration = float(
            audio.get("whisper_input_duration_seconds", 0.0)
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
            whisper_processing_time_seconds=float(
                transcription_metadata.get("processing_time_seconds", 0.0)
            ),
            whisper_status=str(transcription_metadata.get("status", "")),
            whisper_exit_code=transcription_metadata.get("exit_code"),
            sample_rate_hz=int(audio.get("sample_rate_hz", 0)),
            channels=int(audio.get("channels", 0)),
            sample_width_bytes=int(audio.get("sample_width_bytes", 0)),
            capture_stop_reason=str(audio.get("capture_stop_reason", "")),
            error_code=error_code,
            error_message=str(error_message or "")[:160],
            correlation_id=request.correlation_id,
            audio_metadata=audio,
            data={"safe": True, "contains_transcript": False, **dict(data or {})},
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
                raw_transcript=raw_transcript,
                audio=audio,
                capture=capture,
                transcription=transcription_metadata,
            )
        self.last_result = result
        return result

    def _validate_capture_durations(
        self,
        capture: Any,
        whisper_input_path: Path,
        *,
        listener_timeout_seconds: float,
    ) -> str:
        try:
            whisper_wav = _read_wav_metadata(whisper_input_path)
        except (OSError, EOFError, ValueError, wave.Error) as error:
            return f"invalid_wake_wav:{error.__class__.__name__}"
        if (
            whisper_wav["sample_rate_hz"] != 16000
            or whisper_wav["channels"] != 1
            or whisper_wav["sample_width_bytes"] != 2
        ):
            return "wake_whisper_input_not_canonical_pcm"

        frame_seconds = self.config.frame_duration_ms / 1000.0
        candidate_limit = (
            self.config.maximum_utterance_seconds
            + self.config.pre_roll_seconds
            + (2.0 * frame_seconds)
            + self.config.duration_tolerance_seconds
        )
        candidate_durations = [
            float(whisper_wav["duration_seconds"]),
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
            (self.config.calibration_duration_seconds if self.config.calibration_enabled else 0.0)
            + min(
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
        raw_transcript: str,
        audio: Dict[str, Any],
        capture: Any,
        transcription: Dict[str, Any],
    ) -> None:
        whisper_input = str(
            getattr(capture, "final_whisper_input_path", "")
            or getattr(capture, "normalized_wav_path", "")
            or getattr(capture, "wav_path", "")
            or ""
        )
        self._pending_diagnostics = WakeLocalDiagnostics(
            raw_transcript=str(raw_transcript or ""),
            cleaned_transcript=clean_wake_transcript(raw_transcript),
            normalized_transcript=normalize_wake_phrase(raw_transcript),
            selected_alias=result.selected_alias,
            selected_wake_phrase=result.selected_wake_phrase,
            canonical_wake_phrase=result.canonical_wake_phrase,
            classification="accepted" if result.wake_detected else "rejected",
            rejection_reason=(
                result.rejection_reason
                or ("" if result.wake_detected else result.stop_reason or "wake_not_detected")
            ),
            capture_duration_seconds=result.duration_seconds,
            raw_capture_duration_seconds=float(audio.get("raw_duration_seconds", 0.0)),
            assembled_duration_seconds=float(audio.get("assembled_duration_seconds", 0.0)),
            normalized_duration_seconds=float(audio.get("normalized_duration_seconds", 0.0)),
            whisper_input_duration_seconds=float(
                audio.get("whisper_input_duration_seconds", 0.0)
            ),
            capture_stop_reason=result.capture_stop_reason,
            whisper_status=str(transcription.get("status", "")),
            whisper_exit_code=transcription.get("exit_code"),
            whisper_processing_time_seconds=float(
                transcription.get("processing_time_seconds", 0.0)
            ),
            wake_model_path=str(transcription.get("model_path", "") or self.config.whisper_model),
            lifecycle_state=request.lifecycle_state,
            retained_audio_path=whisper_input,
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


def _transcription_metadata(transcription: Any) -> Dict[str, Any]:
    if transcription is None:
        return {}
    data = (
        dict(transcription.get("data") or {})
        if isinstance(transcription, dict)
        else dict(getattr(transcription, "data", {}) or {})
    )
    process = dict(data.get("process") or {})
    exit_code = process.get("returncode")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    return {
        "status": _result_status(transcription),
        "exit_code": exit_code,
        "processing_time_seconds": float(data.get("processing_time_seconds", 0.0) or 0.0),
        "model_path": str(data.get("model_path", "") or ""),
    }


def _remove_turn_directory(path: Path) -> str:
    try:
        shutil.rmtree(path)
        return "removed"
    except FileNotFoundError:
        return "already_removed"
    except OSError:
        return "cleanup_failed"


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
