from __future__ import annotations

from dataclasses import replace
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
from core.Contracts import SingleTurnVoiceRequestV1, new_correlation_id
from core.ResourceBudget import CancellationToken
from core.SingleTurnVoiceSupport import SingleTurnPreBrainDecision


_INPUT_TIMEOUT_STATUSES = {
    "no_speech_timeout",
    "silent_audio",
    "blank_transcription",
    "transcription_rejected",
    "transcript_rejected",
}


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
        voice_io_gate: Optional[VoiceRuntimeGate] = None,
    ) -> None:
        if not callable(getattr(pipeline, "run_once", None)):
            raise ValueError("pipeline must support run_once")
        if not callable(session_id_provider):
            raise ValueError("session_id_provider must be callable")
        self.pipeline = pipeline
        self.base_request = base_request
        self.session_id_provider = session_id_provider
        self.voice_io_gate = voice_io_gate or VoiceRuntimeGate(settle_delay_seconds=0.0)
        self._lock = RLock()
        self._current_token: Optional[CancellationToken] = None
        self._closed = False
        self.last_result: Any = None
        self.capture_count = 0

    def wait_for_input(self, timeout_seconds: float) -> RuntimeInputResult:
        with self._lock:
            if self._closed:
                return RuntimeInputResult.end()
        if not self.voice_io_gate.wait_for_capture(timeout_seconds=max(0.0, float(timeout_seconds))):
            return RuntimeInputResult.timeout()
        correlation = new_correlation_id("runtime-voice-command")
        output_path = _unique_runtime_input_path(self.base_request.recording_output_path)
        request = replace(
            self.base_request,
            correlation_id=correlation,
            session_id=str(self.session_id_provider() or ""),
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

        def intercept(text: str) -> SingleTurnPreBrainDecision:
            captured["text"] = str(text or "").strip()
            return SingleTurnPreBrainDecision(
                handled=True,
                status="runtime_transport_captured",
                continue_to_output=False,
                data={"transport_only": True},
            )

        with self._lock:
            self._current_token = token
            self.capture_count += 1
        try:
            self.voice_io_gate.begin_capture("active_command")
            result = self.pipeline.run_once(
                request,
                cancellation_token=token,
                pre_brain_hook=intercept,
            )
            self.last_result = result
        except KeyboardInterrupt:
            token.cancel("keyboard_interrupt")
            return RuntimeInputResult.cancelled()
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            return RuntimeInputResult.failed(
                "active_voice_pipeline_exception",
                f"{error.__class__.__name__}:{str(error)[:120]}",
            )
        finally:
            self.voice_io_gate.end_capture("active_command")
            with self._lock:
                self._current_token = None
        status = str(getattr(result, "status", "") or "")
        if status == "cancelled" or str(getattr(result, "error_stage", "")) == "cancellation":
            return RuntimeInputResult.cancelled()
        text = captured.get("text") or str(getattr(result, "recognized_text", "") or "").strip()
        if text and bool(getattr(result, "success", False)):
            return RuntimeInputResult(
                status="input",
                text=text,
                metadata={
                    "safe": True,
                    "source": "single_turn_voice_pipeline",
                    "recognized_length": len(text),
                    "capture_stop_reason": _capture_stop_reason(result),
                    "contains_audio": False,
                },
            )
        if status in _INPUT_TIMEOUT_STATUSES or str(getattr(result, "error_stage", "")) in {
            "recording_validation",
            "transcription",
            "transcript_normalization",
        }:
            return RuntimeInputResult(
                status="timeout",
                metadata={
                    "safe": True,
                    "source": "single_turn_voice_pipeline",
                    "capture_status": status,
                    "contains_audio": False,
                },
            )
        return RuntimeInputResult.failed(
            "active_voice_pipeline_failed",
            str(getattr(result, "error_reason", "") or status or "voice input failed")[:160],
        )

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
