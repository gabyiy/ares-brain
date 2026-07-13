from __future__ import annotations

from dataclasses import replace
import time
from typing import Any, Callable, Dict, List, Optional

from core.CapabilityManifest import (
    CapabilityManifest,
    build_multi_turn_voice_session_manifest,
)
from core.Contracts import (
    MultiTurnVoiceSessionRequestV1,
    MultiTurnVoiceSessionResultV1,
)
from core.EventBus import EventBus
from core.ModuleLifecycle import (
    LIFECYCLE_READY,
    LifecyclePolicy,
    LifecycleRequest,
    LifecycleResult,
    ModuleLifecycleManager,
)
from core.MultiTurnVoiceSupport import (
    SessionStateMachine,
    multi_turn_contract_failure,
    validated_multi_turn_request,
)
from core.ResourceBudget import CancellationToken, ResourceManager
from core.SingleTurnVoicePipeline import SingleTurnVoicePipeline
from core.MultiTurnVoiceExecution import MultiTurnVoiceExecutionMixin
from core.MultiTurnVoiceRuntime import (
    EVENT_CLEANUP_COMPLETED,
    EVENT_GREETING_COMPLETED,
    EVENT_GREETING_STARTED,
    EVENT_SESSION_CANCELLED,
    EVENT_SESSION_COMPLETED,
    EVENT_SESSION_FAILED,
    EVENT_SESSION_STARTED,
    EVENT_STOP_PHRASE,
    EVENT_STOP_REQUESTED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    EVENT_TURN_STARTED,
    MULTI_TURN_MODULE_NAME,
    MultiTurnVoiceRuntimeMixin,
)


Clock = Callable[[], float]
Sleeper = Callable[[float], None]
TextInputProvider = Callable[[int], Optional[str]]
ProgressCallback = Callable[[str, Dict[str, Any]], None]


class _SessionLifecycleDelegate:
    def __init__(self, manager: "MultiTurnVoiceSession"):
        self.manager = manager

    def start(self) -> Dict[str, Any]:
        return {
            "success": True,
            "status": "started",
            "owner_triggered_only": True,
            "bounded": True,
        }

    def health_check(self) -> Dict[str, Any]:
        return self.manager._health_components()

    def stop(self) -> Dict[str, Any]:
        return self.manager._stop_dependencies()


class MultiTurnVoiceSession(MultiTurnVoiceExecutionMixin, MultiTurnVoiceRuntimeMixin):
    """Bounded owner-triggered session that delegates every turn to SingleTurnVoicePipeline."""

    def __init__(
        self,
        single_turn_pipeline: SingleTurnVoicePipeline,
        lifecycle_manager: Optional[ModuleLifecycleManager] = None,
        resource_manager: Optional[ResourceManager] = None,
        manifest: Optional[CapabilityManifest] = None,
        event_bus: Optional[EventBus] = None,
        event_history_store: Any = None,
        text_input_provider: Optional[TextInputProvider] = None,
        progress_callback: Optional[ProgressCallback] = None,
        clock: Clock = time.perf_counter,
        sleeper: Sleeper = time.sleep,
    ):
        if single_turn_pipeline is None:
            raise ValueError("single_turn_pipeline is required")
        self.single_turn_pipeline = single_turn_pipeline
        self.lifecycle_manager = lifecycle_manager or single_turn_pipeline.lifecycle_manager
        self.resource_manager = resource_manager or single_turn_pipeline.resource_manager
        self.manifest = manifest or build_multi_turn_voice_session_manifest()
        self.event_bus = event_bus if event_bus is not None else single_turn_pipeline.event_bus
        self.event_history_store = (
            event_history_store
            if event_history_store is not None
            else single_turn_pipeline.event_history_store
        )
        self.text_input_provider = text_input_provider
        self.progress_callback = progress_callback
        self.clock = clock
        self.sleeper = sleeper

        self._active_request: Optional[MultiTurnVoiceSessionRequestV1] = None
        self._active_token: Optional[CancellationToken] = None
        self._state_machine: Optional[SessionStateMachine] = None
        self._session_events: List[Dict[str, Any]] = []
        self._event_history_failures: List[Dict[str, str]] = []
        self._stop_requested = False
        self._stop_reason = ""
        self._health_verified_correlation_id = ""
        self._owns_reservation = False
        self._active_task_id = ""
        self._observing_turn = False
        self._current_turn_number = 0
        self._stage_unsubscribe = self._register_stage_observer()

        self.lifecycle_manager.register_module(
            MULTI_TURN_MODULE_NAME,
            _SessionLifecycleDelegate(self),
            LifecyclePolicy(stop_after_execute=False, inactivity_seconds=0),
        )

    def start(
        self,
        request: Optional[MultiTurnVoiceSessionRequestV1] = None,
    ) -> LifecycleResult:
        if request is not None:
            self._active_request = request
        reservation = self.resource_manager.reserve(self.manifest)
        if not reservation.success:
            return self._resource_lifecycle_failure("start", reservation)
        self._owns_reservation = reservation.status == "reserved"
        result = self.lifecycle_manager.start(
            MULTI_TURN_MODULE_NAME,
            self._lifecycle_request("start"),
        )
        if not result.success and self._owns_reservation:
            self.resource_manager.release(MULTI_TURN_MODULE_NAME, force=True)
            self._owns_reservation = False
        return replace(
            result,
            data={**dict(result.data), "resource_reservation": reservation.to_dict()},
        )

    def health_check(
        self,
        request: Optional[MultiTurnVoiceSessionRequestV1] = None,
    ) -> LifecycleResult:
        if request is not None:
            self._active_request = request
        result = self.lifecycle_manager.health_check(
            MULTI_TURN_MODULE_NAME,
            self._lifecycle_request("health_check"),
        )
        self._health_verified_correlation_id = (
            self._active_request.correlation_id
            if result.success and self._active_request is not None
            else ""
        )
        return result

    def execute(
        self,
        request: MultiTurnVoiceSessionRequestV1,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> MultiTurnVoiceSessionResultV1:
        if self.lifecycle_manager.status(MULTI_TURN_MODULE_NAME).state != LIFECYCLE_READY:
            return self._early_failure(request, "lifecycle", "module_not_ready", "not_ready")
        if self._health_verified_correlation_id != request.correlation_id:
            return self._early_failure(
                request,
                "health_check",
                "health_not_verified_for_request",
                "health_not_verified",
            )

        task_id = f"{MULTI_TURN_MODULE_NAME}:{request.correlation_id}"
        task_slot = self.resource_manager.acquire_task(
            self.manifest,
            task_id=task_id,
            priority=self.manifest.resources.task_priority,
        )
        if not task_slot.success:
            return self._early_failure(
                request,
                "resource_reservation",
                task_slot.error_message or task_slot.status,
                "resource_denied",
                data={"task_slot": task_slot.to_dict()},
            )
        self._active_task_id = task_id
        self._active_token = cancellation_token or CancellationToken(
            task_id=task_id,
            supports_cancellation=True,
        )

        def execute_session(_: Any) -> MultiTurnVoiceSessionResultV1:
            return self._run_session(request, self._active_token)

        try:
            lifecycle = self.lifecycle_manager.execute(
                MULTI_TURN_MODULE_NAME,
                self._lifecycle_request("execute"),
                execute_session,
            )
        finally:
            task_release = self.resource_manager.release_task(MULTI_TURN_MODULE_NAME, task_id)
            self._active_task_id = ""
            self._active_token = None
        response = lifecycle.data.get("response")
        if not lifecycle.success or not isinstance(response, MultiTurnVoiceSessionResultV1):
            return self._early_failure(
                request,
                "lifecycle_execute",
                lifecycle.error_message or "invalid_multi_turn_result",
                "execution_failed",
                data={
                    "task_slot": task_slot.to_dict(),
                    "task_release": task_release.to_dict(),
                    "lifecycle_execute": lifecycle.to_dict(),
                },
            )
        return replace(
            response,
            data={
                **dict(response.data),
                "task_slot": task_slot.to_dict(),
                "task_release": task_release.to_dict(),
                "lifecycle_execute": lifecycle.to_dict(),
            },
        )

    def request_stop(self, reason: str = "owner_requested") -> None:
        self._stop_requested = True
        self._stop_reason = str(reason or "owner_requested")
        token = self._active_token
        if token is not None and token.supports_cancellation and not token.requested:
            token.cancel(self._stop_reason)

    def stop(
        self,
        request: Optional[MultiTurnVoiceSessionRequestV1] = None,
    ) -> LifecycleResult:
        if request is not None:
            self._active_request = request
        result = self.lifecycle_manager.stop(
            MULTI_TURN_MODULE_NAME,
            self._lifecycle_request("stop"),
        )
        release = None
        if self._owns_reservation:
            release = self.resource_manager.release(MULTI_TURN_MODULE_NAME, force=True)
            self._owns_reservation = False
        self._health_verified_correlation_id = ""
        return replace(
            result,
            data={
                **dict(result.data),
                "resource_release": release.to_dict() if release is not None else {},
            },
        )

    def run_session(
        self,
        request: MultiTurnVoiceSessionRequestV1 | Dict[str, Any],
        cancellation_token: Optional[CancellationToken] = None,
    ) -> MultiTurnVoiceSessionResultV1:
        try:
            normalized = validated_multi_turn_request(request)
        except ValueError as error:
            return multi_turn_contract_failure(request, str(error))

        self._active_request = normalized
        self._session_events = []
        self._event_history_failures = []
        self._stop_requested = False
        self._stop_reason = ""
        result: Optional[MultiTurnVoiceSessionResultV1] = None
        start_result: Optional[LifecycleResult] = None
        health_result: Optional[LifecycleResult] = None
        stop_result: Optional[LifecycleResult] = None
        try:
            start_result = self.start(normalized)
            if not start_result.success:
                result = self._early_failure(
                    normalized,
                    "lifecycle_start",
                    start_result.error_message or start_result.status,
                    "start_failed",
                )
            else:
                health_result = self.health_check(normalized)
                if not health_result.success:
                    result = self._early_failure(
                        normalized,
                        "health_check",
                        health_result.error_message or health_result.status,
                        "health_check_failed",
                    )
                else:
                    result = self.execute(normalized, cancellation_token)
        except KeyboardInterrupt:
            self.request_stop("keyboard_interrupt")
            result = self._cancelled_result(normalized, "keyboard_interrupt")
        finally:
            stop_result = self.stop(normalized)

        assert result is not None
        cleanup_status = "completed" if stop_result.success else "failed"
        cleanup_event = self._emit(
            EVENT_CLEANUP_COMPLETED,
            "cleanup",
            cleanup_status,
            stop_result.success,
            {"resource_released": not self._owns_reservation},
        )
        events = [dict(event) for event in result.events]
        if not any(event.get("type") == cleanup_event.type for event in events):
            events.append(cleanup_event.to_dict())
        success = result.success and stop_result.success
        status = result.status if stop_result.success else "cleanup_failed"
        return replace(
            result,
            success=success,
            status=status,
            resource_cleanup_status=cleanup_status,
            events=events,
            data={
                **dict(result.data),
                "lifecycle": {
                    "start": start_result.to_dict() if start_result else {},
                    "health_check": health_result.to_dict() if health_result else {},
                    "stop": stop_result.to_dict() if stop_result else {},
                },
                "resource_usage": self.resource_manager.current_usage(),
            },
        )

    def lifecycle_status(self) -> Dict[str, Any]:
        return self.lifecycle_manager.status(MULTI_TURN_MODULE_NAME).to_dict()
