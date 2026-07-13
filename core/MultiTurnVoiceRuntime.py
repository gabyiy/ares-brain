from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from core.Contracts import (
    MultiTurnVoiceSessionRequestV1,
    MultiTurnVoiceSessionResultV1,
    SingleTurnVoiceResultV1,
    utc_contract_timestamp,
)
from core.EventBus import Event, PRIORITY_NORMAL
from core.ModuleLifecycle import LifecycleRequest, LifecycleResult
from core.MultiTurnVoiceSupport import (
    SESSION_CANCELLED,
    SESSION_FAILED,
    SESSION_LISTENING,
    SESSION_PROCESSING,
    SESSION_SPEAKING,
    SESSION_SYNTHESIZING,
    SESSION_TRANSCRIBING,
    SessionStateMachine,
    SessionStateTransitionError,
)
from core.SingleTurnVoiceSupport import safe_exception


MULTI_TURN_MODULE_NAME = "multi_turn_voice_session"

EVENT_SESSION_STARTED = "conversation_session_started"
EVENT_GREETING_STARTED = "conversation_greeting_started"
EVENT_GREETING_COMPLETED = "conversation_greeting_completed"
EVENT_TURN_STARTED = "conversation_turn_started"
EVENT_TURN_COMPLETED = "conversation_turn_completed"
EVENT_TURN_FAILED = "conversation_turn_failed"
EVENT_STOP_PHRASE = "conversation_stop_phrase_detected"
EVENT_STOP_REQUESTED = "conversation_session_stop_requested"
EVENT_SESSION_CANCELLED = "conversation_session_cancelled"
EVENT_SESSION_COMPLETED = "conversation_session_completed"
EVENT_SESSION_FAILED = "conversation_session_failed"
EVENT_CLEANUP_COMPLETED = "conversation_cleanup_completed"


class MultiTurnVoiceRuntimeMixin:
    def _pipeline_idle(self) -> bool:
        status_method = getattr(self.single_turn_pipeline, "coordination_status", None)
        if not callable(status_method):
            return True
        status = status_method()
        return bool(dict(status or {}).get("idle"))

    def _on_single_turn_stage(self, index: int, total: int, label: str, status: str) -> None:
        if not self._observing_turn or self._state_machine is None:
            return
        if index == 2:
            self._transition(SESSION_LISTENING, f"turn_{self._current_turn_number}_recording")
        elif index == 3:
            self._transition(
                SESSION_TRANSCRIBING,
                f"turn_{self._current_turn_number}_transcribing",
            )
        elif index == 4:
            self._transition(SESSION_PROCESSING, f"turn_{self._current_turn_number}_processing")
        elif index == 5 and not str(status).startswith("skipped"):
            self._transition(
                SESSION_SYNTHESIZING,
                f"turn_{self._current_turn_number}_synthesizing",
            )
        elif index == 6 and not str(status).startswith("skipped"):
            self._transition(SESSION_SPEAKING, f"turn_{self._current_turn_number}_speaking")

    def _register_stage_observer(self) -> Callable[[], None]:
        register = getattr(self.single_turn_pipeline, "add_stage_observer", None)
        if not callable(register):
            return lambda: None
        return register(self._on_single_turn_stage)

    def _transition(self, state: str, reason: str) -> None:
        if self._state_machine is None:
            raise SessionStateTransitionError("session_state_machine_not_initialized")
        self._state_machine.transition(state, reason)

    def _state_history(self) -> List[Dict[str, Any]]:
        if self._state_machine is None:
            return []
        return [transition.to_dict() for transition in self._state_machine.history]

    def _reset_run_state(self, request: MultiTurnVoiceSessionRequestV1) -> None:
        self._active_request = request
        self._state_machine = SessionStateMachine(clock=self.clock)
        self._session_events = []
        self._event_history_failures = []
        self._stop_requested = False
        self._stop_reason = ""
        self._current_turn_number = 0

    def _health_components(self) -> Dict[str, Any]:
        required_methods = (
            "run_once",
            "run_local_output",
            "stop",
            "coordination_status",
        )
        methods = {
            name: callable(getattr(self.single_turn_pipeline, name, None))
            for name in required_methods
        }
        idle = self._pipeline_idle()
        success = all(methods.values()) and idle
        return {
            "success": success,
            "status": "healthy" if success else "unavailable",
            "error_message": "" if success else "single_turn_pipeline_unavailable_or_busy",
            "components": {
                "single_turn_pipeline": {
                    "success": all(methods.values()),
                    "status": "available" if all(methods.values()) else "incompatible",
                    "methods": methods,
                    "idle": idle,
                }
            },
        }

    def _stop_dependencies(self) -> Dict[str, Any]:
        token = self._active_token
        if token is not None and token.supports_cancellation and not token.requested:
            token.cancel("session_stopping")
        try:
            stopped = self.single_turn_pipeline.stop()
        except (RuntimeError, ValueError, OSError) as error:
            return {
                "success": False,
                "status": "stop_failed",
                "error_message": safe_exception(error),
            }
        success = bool(getattr(stopped, "success", False))
        return {
            "success": success,
            "status": "stopped" if success else "stop_failed",
            "error_message": str(getattr(stopped, "error_message", "") or ""),
            "single_turn_pipeline": getattr(stopped, "to_dict", lambda: {})(),
        }

    def _emit(
        self,
        event_type: str,
        stage: str,
        status: str,
        success: bool,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Event:
        request = self._active_request
        payload = {
            "correlation_id": request.correlation_id if request else "",
            "session_id": request.session_id if request else "",
            "stage": stage,
            "status": status,
            "success": success,
            **dict(extra or {}),
        }
        event = (
            self.event_bus.publish(
                source="voice",
                type=event_type,
                payload=payload,
                priority=PRIORITY_NORMAL,
                correlation_id=str(
                    payload.get("turn_correlation_id")
                    or (request.correlation_id if request else "")
                ),
                session_id=request.session_id if request else "",
            )
            if self.event_bus is not None
            else Event(source="voice", type=event_type, payload=payload)
        )
        self._session_events.append(event.to_dict())
        if self.event_history_store is not None:
            try:
                self.event_history_store.add(
                    event,
                    {
                        "success": success,
                        "decision": "recorded",
                        "text": "Conversation session operational event recorded.",
                        "data": {"event_type": event_type, "stage": stage, "status": status},
                        "error_message": "" if success else status,
                        "metadata": {"safe": True, "source": "multi_turn_voice_session"},
                    },
                )
            except (OSError, RuntimeError, ValueError) as error:
                self._event_history_failures.append(
                    {"event_type": event_type, "error": safe_exception(error)}
                )
        return event

    def _notify(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event_type, dict(payload))
        except (OSError, RuntimeError, ValueError):
            return

    def _turn_correlation_id(
        self,
        request: MultiTurnVoiceSessionRequestV1,
        turn_number: int,
    ) -> str:
        return f"{request.correlation_id}:turn-{turn_number:03d}"

    def _resource_lifecycle_failure(self, operation: str, decision: Any) -> LifecycleResult:
        request = self._active_request
        return LifecycleResult(
            success=False,
            status="resource_denied",
            state="UNLOADED",
            text="Multi-turn voice session was denied by resource policy.",
            error_message=str(decision.error_message or decision.status),
            data={"resource_decision": decision.to_dict()},
            request=LifecycleRequest(
                module_name=MULTI_TURN_MODULE_NAME,
                operation=operation,
                correlation_id=request.correlation_id if request else "",
                session_id=request.session_id if request else "",
            ),
            metadata={"safe": True, "source": "multi_turn_voice_session"},
        )

    def _early_failure(
        self,
        request: MultiTurnVoiceSessionRequestV1,
        stage: str,
        reason: str,
        status: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> MultiTurnVoiceSessionResultV1:
        return MultiTurnVoiceSessionResultV1(
            success=False,
            status=status,
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            started_at=utc_contract_timestamp(),
            completed_at=utc_contract_timestamp(),
            final_state=SESSION_FAILED,
            error_stage=stage,
            error_reason=str(reason or status),
            resource_cleanup_status="pending_lifecycle_stop",
            data=dict(data or {}),
            events=[dict(event) for event in self._session_events],
            metadata={"safe": True, "source": "multi_turn_voice_session"},
        )

    def _cancelled_result(
        self,
        request: MultiTurnVoiceSessionRequestV1,
        reason: str,
    ) -> MultiTurnVoiceSessionResultV1:
        return MultiTurnVoiceSessionResultV1(
            success=False,
            status="cancelled",
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            started_at=utc_contract_timestamp(),
            completed_at=utc_contract_timestamp(),
            stop_reason=reason,
            cancelled=True,
            final_state=SESSION_CANCELLED,
            error_stage="cancellation",
            error_reason=reason,
            resource_cleanup_status="pending_lifecycle_stop",
            events=[dict(event) for event in self._session_events],
            metadata={"safe": True, "source": "multi_turn_voice_session"},
        )

    def _lifecycle_request(self, operation: str) -> LifecycleRequest:
        request = self._active_request
        return LifecycleRequest(
            module_name=MULTI_TURN_MODULE_NAME,
            operation=operation,
            payload={
                "session_contract": request.to_dict() if request else {},
                "owner_triggered": True,
                "bounded": True,
            },
            correlation_id=request.correlation_id if request else "",
            session_id=request.session_id if request else "",
            metadata={"source": "multi_turn_voice_session"},
        )
