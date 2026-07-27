from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
import time
from typing import Any, Callable, Optional
from uuid import uuid4

from core.BrainRuntimeAdapters import (
    RuntimeInputResult,
    RuntimeOutputMessage,
    RuntimeOutputResult,
)
from core.Contracts import (
    SingleTurnVoiceRequestV1,
    new_correlation_id,
    utc_contract_timestamp,
)
from core.LifecycleControl import (
    LIFECYCLE_ACTION_NONE,
    normalize_active_lifecycle_command,
)
from core.ResourceBudget import CancellationToken
from core.SingleTurnVoiceSupport import SingleTurnPreBrainDecision


_INPUT_TIMEOUT_STATUSES = {
    "end_of_input",
    "no_speech_timeout",
    "silent_audio",
    "blank_transcription",
    "transcription_timeout",
    "transcription_rejected",
    "transcript_rejected",
}


@dataclass(frozen=True)
class ActiveCommandLocalDiagnostics:
    """Ephemeral owner-terminal details; never published or persisted."""

    raw_transcript: str = ""
    cleaned_transcript: str = ""
    alias_canonicalized_transcript: str = ""
    lifecycle_normalized_transcript: str = ""
    matched_assistant_alias: str = ""
    assistant_alias_type: str = ""
    assistant_alias_removed: str = ""
    alias_position: str = "none"
    canonical_name: str = ""
    negation_detected: bool = False
    lifecycle_classification: str = "ordinary"
    selected_lifecycle_action: str = "none"
    matched_lifecycle_phrase: str = ""
    lifecycle_rejection_reason: str = ""
    core_service_bypassed: bool = False
    lifecycle_state_before: str = ""
    lifecycle_state_after: str = ""
    session_id_before: str = ""
    session_id_after: str = ""
    capture_stop_reason: str = ""
    raw_capture_duration_seconds: float = 0.0
    finalized_candidate_duration_seconds: float = 0.0
    whisper_processing_duration_seconds: float = 0.0
    terminal_silence_status: str = "unknown"
    audio_finalization_started_at: str = ""
    audio_finalization_completed_at: str = ""
    wav_path: str = ""
    wav_byte_size: int = 0
    wav_sample_rate_hz: int = 0
    wav_channels: int = 0
    wav_sample_width_bytes: int = 0
    transcription_backend: str = "whisper.cpp"
    transcription_started_at: str = ""
    transcription_completed_at: str = ""
    transcription_status: str = "not_started"
    transcription_timeout_seconds: float = 0.0
    whisper_process_pid: int = 0
    whisper_process_group_id: int = 0
    whisper_process_exit_code: Optional[int] = None
    whisper_process_elapsed_seconds: float = 0.0
    whisper_process_terminated: bool = False
    whisper_process_killed: bool = False
    whisper_process_reaped: bool = False
    whisper_process_cleanup_completed: bool = False
    whisper_output_handles_closed: bool = False
    transcript_parsing_status: str = "not_started"
    routing_started_at: str = ""
    routing_completed_at: str = ""
    temporary_audio_cleanup_status: str = "unknown"
    microphone_gate_released_before_inference: bool = False
    pipeline_status: str = "not_started"
    runtime_terminal: bool = False
    runtime_terminal_reason: str = "not_terminal"


class VoiceRuntimeGate:
    """Serialized microphone/speaker exclusion with a bounded post-playback settle."""

    def __init__(
        self,
        *,
        settle_delay_seconds: float = 0.35,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(settle_delay_seconds, bool) or not isinstance(
            settle_delay_seconds, (int, float)
        ):
            raise ValueError("settle_delay_seconds must be numeric")
        if not 0.0 <= float(settle_delay_seconds) <= 3.0:
            raise ValueError("settle_delay_seconds must be between 0 and 3")
        self.settle_delay_seconds = float(settle_delay_seconds)
        self.clock = clock
        self.sleeper = sleeper
        self._lock = RLock()
        self._capture_owner = ""
        self._playback_owner = ""
        self._last_playback_completed_at: Optional[float] = None

    def wait_for_capture(self, *, timeout_seconds: float) -> bool:
        timeout = max(0.0, float(timeout_seconds))
        started = self.clock()
        while True:
            with self._lock:
                if self._playback_owner:
                    remaining = timeout - max(0.0, self.clock() - started)
                elif self._last_playback_completed_at is None:
                    return True
                else:
                    settle_remaining = self.settle_delay_seconds - max(
                        0.0, self.clock() - self._last_playback_completed_at
                    )
                    if settle_remaining <= 0:
                        return True
                    remaining = min(settle_remaining, timeout - max(0.0, self.clock() - started))
            if remaining <= 0:
                return False
            self.sleeper(remaining)

    def begin_capture(self, owner: str) -> None:
        clean_owner = str(owner or "capture").strip()[:64]
        with self._lock:
            if self._playback_owner:
                raise RuntimeError("speaker_playback_active_during_capture")
            if self._capture_owner and self._capture_owner != clean_owner:
                raise RuntimeError("microphone_capture_already_active")
            self._capture_owner = clean_owner

    def end_capture(self, owner: str = "") -> None:
        with self._lock:
            if not owner or self._capture_owner == str(owner)[:64]:
                self._capture_owner = ""

    def begin_playback(self, owner: str) -> None:
        clean_owner = str(owner or "playback").strip()[:64]
        with self._lock:
            if self._capture_owner:
                raise RuntimeError("microphone_capture_active_during_playback")
            if self._playback_owner and self._playback_owner != clean_owner:
                raise RuntimeError("speaker_playback_already_active")
            self._playback_owner = clean_owner

    def end_playback(self, owner: str = "") -> None:
        with self._lock:
            if not owner or self._playback_owner == str(owner)[:64]:
                self._playback_owner = ""
                self._last_playback_completed_at = self.clock()

    def reset(self) -> None:
        with self._lock:
            self._capture_owner = ""
            self._playback_owner = ""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "capture_active": bool(self._capture_owner),
                "capture_owner": self._capture_owner,
                "playback_active": bool(self._playback_owner),
                "playback_owner": self._playback_owner,
                "settle_delay_seconds": self.settle_delay_seconds,
                "settling": (
                    self._last_playback_completed_at is not None
                    and max(0.0, self.clock() - self._last_playback_completed_at)
                    < self.settle_delay_seconds
                ),
                "safe": True,
            }


class SingleTurnPipelineRuntimeInputAdapter:
    """Uses SingleTurnVoicePipeline only for one active-session capture/transcription."""

    def __init__(
        self,
        *,
        pipeline: Any,
        base_request: SingleTurnVoiceRequestV1,
        session_id_provider: Callable[[], str],
        lifecycle_state_provider: Optional[Callable[[], str]] = None,
        voice_io_gate: Optional[VoiceRuntimeGate] = None,
        diagnostic_callback: Optional[
            Callable[[ActiveCommandLocalDiagnostics], None]
        ] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not callable(getattr(pipeline, "run_once", None)):
            raise ValueError("pipeline must support run_once")
        if not callable(session_id_provider):
            raise ValueError("session_id_provider must be callable")
        if lifecycle_state_provider is not None and not callable(lifecycle_state_provider):
            raise ValueError("lifecycle_state_provider must be callable")
        self.pipeline = pipeline
        self.base_request = base_request
        self.session_id_provider = session_id_provider
        self.lifecycle_state_provider = lifecycle_state_provider or (lambda: "")
        self.voice_io_gate = voice_io_gate or VoiceRuntimeGate(settle_delay_seconds=0.0)
        self.diagnostic_callback = diagnostic_callback
        self.status_callback = status_callback
        self._lock = RLock()
        self._current_token: Optional[CancellationToken] = None
        self._closed = False
        self.last_result: Any = None
        self.last_diagnostics: Optional[ActiveCommandLocalDiagnostics] = None
        self.capture_count = 0
        self._routing_started_at = ""

    def wait_for_input(self, timeout_seconds: float) -> RuntimeInputResult:
        with self._lock:
            if self._closed:
                return RuntimeInputResult(
                    status="end_of_input",
                    metadata={
                        "safe": True,
                        "source": "single_turn_voice_pipeline",
                        "runtime_terminal": False,
                        "input_scope": "active_command",
                    },
                )
            self.last_diagnostics = None
            self._routing_started_at = ""
        lifecycle_state_before = _provided_text(self.lifecycle_state_provider)
        session_id_before = _provided_text(self.session_id_provider)
        emitted_statuses: set[str] = set()

        def emit_once(message: str) -> None:
            if message in emitted_statuses:
                return
            emitted_statuses.add(message)
            self._emit_status(message)

        if not self.voice_io_gate.wait_for_capture(timeout_seconds=max(0.0, float(timeout_seconds))):
            return RuntimeInputResult.timeout()
        correlation = new_correlation_id("runtime-voice-command")
        output_path = _unique_runtime_input_path(self.base_request.recording_output_path)
        request = replace(
            self.base_request,
            correlation_id=correlation,
            session_id=session_id_before,
            recording_output_path=str(output_path),
            text_input="",
            playback_enabled=False,
            metadata={
                **dict(self.base_request.metadata or {}),
                "source": "brain_runtime_active_voice_input",
                "runtime_transport_only": True,
            },
        )
        token = CancellationToken(task_id=f"runtime-voice-input:{correlation}")
        captured: dict[str, str] = {}

        def transport_intercept(text: str) -> SingleTurnPreBrainDecision:
            captured["text"] = str(text or "").strip()
            return SingleTurnPreBrainDecision(
                handled=True,
                status="runtime_transport_captured",
                continue_to_output=False,
                data={"transport_only": True},
            )

        def lifecycle_intercept(raw_text: str) -> SingleTurnPreBrainDecision:
            # Active capture is intentionally classified with ACTIVE semantics.
            # Wake activation is a STANDBY concern; here a name-only phrase is
            # attention-only and optional edge addressing is removed before
            # exact lifecycle matching.
            lifecycle = normalize_active_lifecycle_command(raw_text)
            if lifecycle.action == LIFECYCLE_ACTION_NONE:
                return SingleTurnPreBrainDecision(
                    handled=False,
                    status="not_lifecycle_command",
                    data={
                        "transport_only": True,
                        "lifecycle_action": lifecycle.action,
                    },
                )
            captured["text"] = str(raw_text or "").strip()
            return SingleTurnPreBrainDecision(
                handled=True,
                status="runtime_lifecycle_transport_captured",
                continue_to_output=False,
                data={
                    "transport_only": True,
                    "lifecycle_action": lifecycle.action,
                    "lifecycle_normalized_transcript": (
                        lifecycle.canonicalized_transcript
                    ),
                    "canonical_name": lifecycle.canonical_name,
                },
            )

        with self._lock:
            self._current_token = token
            self.capture_count += 1
        unsubscribe: Optional[Callable[[], None]] = None
        capture_gate_owned = False

        def observe_stage(_index: int, _total: int, label: str, status: str) -> None:
            nonlocal capture_gate_owned
            normalized_label = str(label or "").strip().casefold()
            normalized_status = str(status or "").strip().casefold()
            if normalized_label == "recording" and normalized_status == "completed":
                if capture_gate_owned:
                    self.voice_io_gate.end_capture("active_command")
                    capture_gate_owned = False
                emit_once("Speech detected")
                emit_once("Command captured")
            elif normalized_label == "transcribing" and normalized_status == "running":
                emit_once("Transcribing command")

        add_observer = getattr(self.pipeline, "add_stage_observer", None)
        if callable(add_observer):
            unsubscribe = add_observer(observe_stage)
        try:
            self.voice_io_gate.begin_capture("active_command")
            capture_gate_owned = True
            # Announce a real waiting phase only after playback and its settling
            # window have completed and capture ownership is secured. The runtime
            # polling interval can be shorter than that window; announcing before
            # the gate produced duplicate prompts even though no capture began.
            emit_once("ARES is waiting for your command...")
            emit_once("Active microphone capture started")
            result = self.pipeline.run_once(
                request,
                cancellation_token=token,
                pre_brain_hook=transport_intercept,
                raw_transcript_hook=lifecycle_intercept,
            )
            self.last_result = result
        except KeyboardInterrupt:
            token.cancel("keyboard_interrupt")
            self.last_diagnostics = ActiveCommandLocalDiagnostics(
                lifecycle_state_before=lifecycle_state_before,
                lifecycle_state_after=_provided_text(self.lifecycle_state_provider),
                session_id_before=session_id_before,
                session_id_after=_provided_text(self.session_id_provider),
                pipeline_status="cancelled",
                runtime_terminal=False,
                runtime_terminal_reason="not_terminal",
            )
            self._emit_local_diagnostics()
            return RuntimeInputResult.cancelled()
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            self.last_diagnostics = ActiveCommandLocalDiagnostics(
                lifecycle_state_before=lifecycle_state_before,
                lifecycle_state_after=_provided_text(self.lifecycle_state_provider),
                session_id_before=session_id_before,
                session_id_after=_provided_text(self.session_id_provider),
                pipeline_status="pipeline_exception",
                runtime_terminal=False,
                runtime_terminal_reason="not_terminal",
            )
            self._emit_local_diagnostics()
            return RuntimeInputResult.failed(
                "active_voice_pipeline_exception",
                f"{error.__class__.__name__}:{str(error)[:120]}",
            )
        finally:
            if unsubscribe is not None:
                unsubscribe()
            if capture_gate_owned:
                self.voice_io_gate.end_capture("active_command")
            with self._lock:
                self._current_token = None
        status = str(getattr(result, "status", "") or "")
        self.last_diagnostics = replace(
            _active_command_diagnostics(result),
            lifecycle_state_before=lifecycle_state_before,
            lifecycle_state_after=_provided_text(self.lifecycle_state_provider),
            session_id_before=session_id_before,
            session_id_after=_provided_text(self.session_id_provider),
        )
        if status == "cancelled" or str(getattr(result, "error_stage", "")) == "cancellation":
            self._emit_local_diagnostics()
            return RuntimeInputResult.cancelled()
        text = captured.get("text") or str(getattr(result, "recognized_text", "") or "").strip()
        if text and bool(getattr(result, "success", False)):
            emit_once("Speech detected")
            emit_once("Command captured")
            emit_once("Transcribing command")
            emit_once("Processing command")
            self._routing_started_at = utc_contract_timestamp()
            self.last_diagnostics = replace(
                self.last_diagnostics,
                routing_started_at=self._routing_started_at,
            )
            return RuntimeInputResult(
                status="input",
                text=text,
                metadata={
                    "safe": True,
                    "source": "single_turn_voice_pipeline",
                    "recognized_length": len(text),
                    "capture_stop_reason": _capture_stop_reason(result),
                    "raw_capture_duration_seconds": (
                        self.last_diagnostics.raw_capture_duration_seconds
                    ),
                    "finalized_candidate_duration_seconds": (
                        self.last_diagnostics.finalized_candidate_duration_seconds
                    ),
                    "whisper_processing_duration_seconds": (
                        self.last_diagnostics.whisper_processing_duration_seconds
                    ),
                    "contains_audio": False,
                },
            )
        if not text and bool(getattr(result, "success", False)):
            emit_once("No command heard; still active")
            self._emit_local_diagnostics()
            return RuntimeInputResult(
                status="timeout",
                metadata={
                    "safe": True,
                    "source": "single_turn_voice_pipeline",
                    "capture_status": status or "empty_transcript",
                    "runtime_terminal": False,
                    "contains_audio": False,
                },
            )
        if status in _INPUT_TIMEOUT_STATUSES or str(getattr(result, "error_stage", "")) in {
            "recording_validation",
            "transcription",
            "transcript_normalization",
        }:
            error_reason = str(
                getattr(result, "error_reason", "") or status or "transcription_failed"
            )
            if str(getattr(result, "error_stage", "")) == "transcription":
                if "timeout" in error_reason.casefold():
                    timeout = float(
                        getattr(self.base_request, "transcription_timeout_seconds", 0.0)
                        or 0.0
                    )
                    emit_once(
                        "Command transcription timeout handled"
                        + (f" after {timeout:g} seconds" if timeout else "")
                        + "; ARES remains active"
                    )
                    gate_released = not bool(
                        self.voice_io_gate.snapshot().get("capture_active")
                    )
                    emit_once(
                        "Microphone gate released: "
                        + ("yes" if gate_released else "no")
                    )
                    emit_once(
                        "Temporary audio cleanup: "
                        + self.last_diagnostics.temporary_audio_cleanup_status
                    )
                    emit_once("ARES: I could not transcribe that. Please try again.")
                else:
                    emit_once(
                        f"Command transcription failed: {error_reason[:120]}; still active"
                    )
            emit_once("No command heard; still active")
            self._emit_local_diagnostics()
            return RuntimeInputResult(
                status="timeout",
                metadata={
                    "safe": True,
                    "source": "single_turn_voice_pipeline",
                    "capture_status": status,
                    "transcription_failure_type": (
                        "transcription_timeout"
                        if status == "transcription_timeout"
                        or "timeout" in error_reason.casefold()
                        else "transcription_failed"
                    ),
                    "retryable": True,
                    "microphone_gate_released": not bool(
                        self.voice_io_gate.snapshot().get("capture_active")
                    ),
                    "temporary_audio_cleanup_status": (
                        self.last_diagnostics.temporary_audio_cleanup_status
                    ),
                    "runtime_terminal": False,
                    "contains_audio": False,
                },
            )
        self._emit_local_diagnostics()
        return RuntimeInputResult.failed(
            "active_voice_pipeline_failed",
            str(getattr(result, "error_reason", "") or status or "voice input failed")[:160],
        )

    def _emit_status(self, message: str) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(str(message))
        except (OSError, RuntimeError, TypeError, ValueError):
            return

    def _emit_local_diagnostics(self) -> None:
        if self.diagnostic_callback is None or self.last_diagnostics is None:
            return
        try:
            self.diagnostic_callback(self.last_diagnostics)
        except (OSError, RuntimeError, TypeError, ValueError):
            return

    def record_runtime_result(
        self,
        *,
        runtime_result: Any,
        lifecycle_state_before: str,
        session_id_before: str,
    ) -> None:
        """Complete local diagnostics after Capital/Core classification."""

        with self._lock:
            diagnostics = self.last_diagnostics
        if diagnostics is None:
            return
        category = str(getattr(runtime_result, "command_category", "") or "ordinary")
        data = dict(getattr(runtime_result, "data", {}) or {})
        lifecycle_data = dict(
            data.get("lifecycle_command")
            or data.get("lifecycle_normalization")
            or {}
        )
        action = str(
            lifecycle_data.get("action")
            or data.get("lifecycle_action")
            or (
                category
                if category
                in {"activation", "attention_only", "standby", "shutdown"}
                else "none"
            )
        )
        lifecycle_normalized = str(
            lifecycle_data["normalized_transcript"]
            if "normalized_transcript" in lifecycle_data
            else diagnostics.cleaned_transcript
        )
        lifecycle_canonicalized = str(
            lifecycle_data["canonicalized_transcript"]
            if "canonicalized_transcript" in lifecycle_data
            else getattr(runtime_result, "normalized_input", "")
            or lifecycle_normalized
        )
        lifecycle_cleaned = str(
            lifecycle_data["cleaned_transcript"]
            if "cleaned_transcript" in lifecycle_data
            else diagnostics.cleaned_transcript
        )
        canonical_name = str(lifecycle_data.get("canonical_name") or "")
        if (
            not canonical_name
            and action in {"activation", "attention_only", "standby", "shutdown"}
            and "ares" in lifecycle_canonicalized.split()
        ):
            canonical_name = "ares"
        current_state = str(
            getattr(runtime_result, "current_lifecycle_state", "") or ""
        )
        runtime_terminal = current_state == "STOPPED"
        terminal_reason = _diagnostic_terminal_reason(
            str(getattr(runtime_result, "stop_reason", "") or ""),
            runtime_terminal=runtime_terminal,
        )
        completed = replace(
            diagnostics,
            cleaned_transcript=lifecycle_cleaned,
            alias_canonicalized_transcript=lifecycle_canonicalized,
            lifecycle_normalized_transcript=lifecycle_normalized,
            matched_assistant_alias=str(
                lifecycle_data.get("matched_alias") or ""
            ),
            assistant_alias_type=str(
                lifecycle_data.get("alias_type") or ""
            ),
            assistant_alias_removed=str(
                lifecycle_data.get("assistant_alias_removed") or ""
            ),
            alias_position=str(
                lifecycle_data.get("alias_position") or "none"
            ),
            canonical_name=canonical_name,
            negation_detected=bool(
                lifecycle_data.get("negation_detected", False)
            ),
            lifecycle_classification=category,
            selected_lifecycle_action=action,
            matched_lifecycle_phrase=str(
                lifecycle_data.get("matched_phrase") or ""
            ),
            lifecycle_rejection_reason=str(
                lifecycle_data.get("rejection_reason") or ""
            ),
            core_service_bypassed=bool(
                data.get("core_service_bypassed")
                or category
                in {"activation", "attention_only", "standby", "shutdown"}
            ),
            lifecycle_state_before=str(lifecycle_state_before or ""),
            lifecycle_state_after=current_state,
            session_id_before=str(session_id_before or ""),
            session_id_after=str(getattr(runtime_result, "session_id", "") or ""),
            routing_started_at=(
                diagnostics.routing_started_at or self._routing_started_at
            ),
            routing_completed_at=utc_contract_timestamp(),
            runtime_terminal=runtime_terminal,
            runtime_terminal_reason=terminal_reason,
        )
        with self._lock:
            self.last_diagnostics = completed
        self._emit_local_diagnostics()

    def release_active_resources(self) -> None:
        with self._lock:
            token = self._current_token
        if token is not None:
            token.cancel("returning_to_standby")
        stop = getattr(self.pipeline, "stop", None)
        if callable(stop):
            try:
                stop(self.base_request)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        self.voice_io_gate.end_capture()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.release_active_resources()


class SingleTurnPipelineRuntimeOutputAdapter:
    """Speaks runtime output through the verified local TTS/speaker pipeline."""

    def __init__(
        self,
        *,
        pipeline: Any,
        base_request: SingleTurnVoiceRequestV1,
        voice_io_gate: Optional[VoiceRuntimeGate] = None,
        output_func: Optional[Callable[[str], None]] = print,
    ) -> None:
        if not callable(getattr(pipeline, "run_local_output", None)):
            raise ValueError("pipeline must support run_local_output")
        self.pipeline = pipeline
        self.base_request = base_request
        self.voice_io_gate = voice_io_gate or VoiceRuntimeGate(settle_delay_seconds=0.0)
        self.output_func = output_func
        self._lock = RLock()
        self._closed = False
        self.last_result: Any = None
        self.playback_count = 0

    def write(self, message: RuntimeOutputMessage) -> RuntimeOutputResult:
        if not isinstance(message, RuntimeOutputMessage):
            return RuntimeOutputResult(False, "malformed_output", "malformed_output", "invalid message")
        with self._lock:
            if self._closed:
                return RuntimeOutputResult(False, "output_closed", "output_closed", "voice output is closed")
        request = replace(
            self.base_request,
            correlation_id=message.correlation_id or new_correlation_id("runtime-voice-output"),
            session_id=message.session_id,
            recording_output_path=str(_unique_runtime_output_path(self.base_request.recording_output_path)),
            text_input=message.text,
            playback_enabled=True,
            cleanup_policy="delete_on_success",
            diagnostic_audio=False,
            metadata={
                **dict(self.base_request.metadata or {}),
                "source": "brain_runtime_voice_output",
                "output_category": message.category,
                "captured_audio_playback": False,
            },
        )
        token = CancellationToken(
            task_id=f"runtime-voice-output:{message.correlation_id or 'response'}"
        )
        try:
            self.voice_io_gate.begin_playback("runtime_response")
            result = self.pipeline.run_local_output(request, message.text, cancellation_token=token)
            self.last_result = result
            self.playback_count += 1
        except KeyboardInterrupt:
            token.cancel("keyboard_interrupt")
            return RuntimeOutputResult(False, "cancelled", "output_cancelled", "voice output cancelled")
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            return RuntimeOutputResult(
                False,
                "output_failed",
                "voice_output_exception",
                f"{error.__class__.__name__}:{str(error)[:120]}",
            )
        finally:
            self.voice_io_gate.end_playback("runtime_response")
        if self.output_func is not None:
            try:
                self.output_func(message.text)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        if bool(getattr(result, "success", False)):
            return RuntimeOutputResult(True, "spoken")
        return RuntimeOutputResult(
            False,
            "output_failed",
            "voice_pipeline_output_failed",
            str(getattr(result, "error_reason", "") or getattr(result, "status", "") or "voice output failed")[:160],
        )

    def release_active_resources(self) -> None:
        stop = getattr(self.pipeline, "stop", None)
        if callable(stop):
            try:
                stop(self.base_request)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        self.voice_io_gate.end_playback()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.release_active_resources()


def _unique_runtime_input_path(base_path: str) -> Path:
    base = Path(base_path).expanduser()
    return base.parent / f"runtime-command-{uuid4().hex}.wav"


def _unique_runtime_output_path(base_path: str) -> Path:
    base = Path(base_path).expanduser()
    return base.parent / f"runtime-response-{uuid4().hex}.wav"


def _capture_stop_reason(result: Any) -> str:
    data = dict(getattr(result, "data", {}) or {})
    recording = dict(data.get("recording") or {})
    return str(
        recording.get("stop_reason")
        or data.get("capture_stop_reason")
        or getattr(result, "recording_status", "")
        or ""
    )[:80]


def _active_command_diagnostics(result: Any) -> ActiveCommandLocalDiagnostics:
    data = dict(getattr(result, "data", {}) or {})
    recording = dict(data.get("recording") or {})
    audio_finalization = dict(data.get("audio_finalization") or {})
    transcription_contract = dict(data.get("transcription") or {})
    transcription = dict(transcription_contract.get("data") or {})
    process = dict(transcription.get("process") or {})
    process_metadata = dict(process.get("metadata") or {})
    transcription_boundary = dict(data.get("transcription_boundary") or {})
    cleanup = dict(data.get("cleanup") or {})
    stop_reason = _capture_stop_reason(result)
    raw_duration = float(recording.get("raw_duration_seconds", 0.0) or 0.0)
    candidate_duration = float(
        recording.get("normalized_duration_seconds", 0.0)
        or recording.get("assembled_duration_seconds", 0.0)
        or getattr(result, "recording_duration_seconds", 0.0)
        or 0.0
    )
    wav_path = str(
        audio_finalization.get("wav_path")
        or transcription.get("audio_path")
        or getattr(result, "recorded_wav_path", "")
        or ""
    )
    removed = {str(path) for path in list(cleanup.get("removed") or [])}
    preserved = {str(path) for path in list(cleanup.get("preserved") or [])}
    cleanup_status = (
        "removed"
        if wav_path and wav_path in removed
        else "preserved"
        if wav_path and wav_path in preserved
        else "not_applicable"
        if not wav_path
        else "missing_after_cleanup"
        if not Path(wav_path).exists()
        else "present"
    )
    return ActiveCommandLocalDiagnostics(
        raw_transcript=str(getattr(result, "raw_transcript", "") or ""),
        cleaned_transcript=str(
            getattr(result, "cleaned_transcript", "")
            or getattr(result, "recognized_text", "")
            or ""
        ),
        capture_stop_reason=stop_reason,
        raw_capture_duration_seconds=raw_duration,
        finalized_candidate_duration_seconds=candidate_duration,
        whisper_processing_duration_seconds=float(
            getattr(result, "transcription_processing_time_seconds", 0.0) or 0.0
        ),
        terminal_silence_status=(
            "confirmed_terminal_silence"
            if stop_reason == "completed_after_silence"
            else "maximum_duration_before_terminal_silence"
            if stop_reason == "maximum_duration_reached"
            else stop_reason or "unknown"
        ),
        audio_finalization_started_at=str(
            audio_finalization.get("started_at") or ""
        ),
        audio_finalization_completed_at=str(
            audio_finalization.get("completed_at") or ""
        ),
        wav_path=wav_path,
        wav_byte_size=int(audio_finalization.get("wav_byte_size", 0) or 0),
        wav_sample_rate_hz=int(
            audio_finalization.get("sample_rate_hz", 0) or 0
        ),
        wav_channels=int(audio_finalization.get("channels", 0) or 0),
        wav_sample_width_bytes=int(
            audio_finalization.get("sample_width_bytes", 0) or 0
        ),
        transcription_backend=str(
            transcription.get("transcription_backend") or "whisper.cpp"
        ),
        transcription_started_at=str(
            transcription.get("transcription_started_at")
            or transcription_boundary.get("started_at")
            or ""
        ),
        transcription_completed_at=str(
            transcription.get("transcription_completed_at")
            or transcription_boundary.get("completed_at")
            or ""
        ),
        transcription_status=str(
            transcription_contract.get("status")
            or getattr(result, "transcription_status", "")
            or "not_started"
        ),
        transcription_timeout_seconds=float(
            transcription.get("transcription_timeout_seconds")
            or transcription_boundary.get("timeout_seconds")
            or 0.0
        ),
        whisper_process_pid=int(process_metadata.get("pid", 0) or 0),
        whisper_process_group_id=int(process_metadata.get("pgid", 0) or 0),
        whisper_process_exit_code=(
            int(process.get("returncode"))
            if process.get("returncode") is not None
            else None
        ),
        whisper_process_elapsed_seconds=float(
            process_metadata.get("elapsed_seconds", 0.0) or 0.0
        ),
        whisper_process_terminated=bool(process_metadata.get("terminated")),
        whisper_process_killed=bool(process_metadata.get("killed")),
        whisper_process_reaped=bool(process_metadata.get("reaped")),
        whisper_process_cleanup_completed=bool(
            process_metadata.get("cleanup_completed")
        ),
        whisper_output_handles_closed=bool(
            process_metadata.get("output_handles_closed")
        ),
        transcript_parsing_status=str(
            transcription.get("transcript_parsing_status") or "not_started"
        ),
        temporary_audio_cleanup_status=cleanup_status,
        microphone_gate_released_before_inference=bool(
            transcription_boundary.get("microphone_capture_released")
        ),
        pipeline_status=str(getattr(result, "status", "") or "unknown"),
    )


def _provided_text(provider: Callable[[], str]) -> str:
    try:
        return str(provider() or "")
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""


def _diagnostic_terminal_reason(reason: str, *, runtime_terminal: bool) -> str:
    clean = str(reason or "").strip()
    if not runtime_terminal:
        return "not_terminal"
    if clean in {"explicit_shutdown_command", "owner_shutdown_phrase"}:
        return "explicit_shutdown_command"
    if clean in {"input_cancelled", "owner_cancellation"}:
        return "owner_cancellation"
    return clean or "unrecoverable_failure"
