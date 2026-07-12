from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional

from core.CapabilityManifest import CapabilityManifest, build_single_turn_voice_pipeline_manifest
from core.Contracts import SingleTurnVoiceRequestV1, SingleTurnVoiceResultV1
from core.CoreService import CoreService
from core.EventBus import Event, EventBus, PRIORITY_NORMAL
from core.Health import AdapterCandidate, AdapterFallbackPolicy
from core.LinuxAlsaSpeaker import SpeakerOutputAdapter
from core.Microphone import MicrophoneAdapter
from core.ModuleLifecycle import (
    LIFECYCLE_READY,
    LifecyclePolicy,
    LifecycleRequest,
    LifecycleResult,
    ModuleLifecycleManager,
)
from core.ResourceBudget import CancellationToken, ResourceManager
from core.SingleTurnVoiceStages import SingleTurnVoiceStageMixin
from core.SingleTurnVoiceSupport import (
    PIPELINE_CLEANUP_KEEP,
    SingleTurnRunState,
    VoiceStageCoordinator,
    contract_failure_result,
    elapsed,
    lifecycle_resource_failure,
    result_dict,
    result_success,
    safe_exception,
    validated_single_turn_request,
)
from core.SpeechToText import SpeechToTextAdapter
from core.TextToSpeech import TextToSpeechAdapter
from core.VoiceCommandRouter import VoiceCommandRouter


SINGLE_TURN_MODULE_NAME = "single_turn_voice_pipeline"
DEFAULT_BRAIN_FAILURE_RESPONSE = "I could not process that request."
DEFAULT_UNKNOWN_RESPONSE = "I cannot handle that request yet."

EVENT_SINGLE_TURN_STARTED = "voice_single_turn_started"
EVENT_RECORDING_STARTED = "recording_started"
EVENT_RECORDING_COMPLETED = "recording_completed"
EVENT_TRANSCRIPTION_COMPLETED = "transcription_completed"
EVENT_BRAIN_EXECUTION_COMPLETED = "brain_execution_completed"
EVENT_SYNTHESIS_COMPLETED = "synthesis_completed"
EVENT_PLAYBACK_COMPLETED = "playback_completed"
EVENT_SINGLE_TURN_COMPLETED = "voice_single_turn_completed"
EVENT_SINGLE_TURN_FAILED = "voice_single_turn_failed"

Clock = Callable[[], float]
StageCallback = Callable[[int, int, str, str], None]


class _LifecycleDelegate:
    def __init__(self, pipeline: "SingleTurnVoicePipeline"):
        self.pipeline = pipeline

    def start(self) -> Dict[str, Any]:
        return {"success": True, "status": "started", "owner_triggered_only": True}

    def health_check(self) -> Dict[str, Any]:
        return self.pipeline._health_components(self.pipeline._active_request)

    def stop(self) -> Dict[str, Any]:
        return self.pipeline._stop_components()


class SingleTurnVoicePipeline(SingleTurnVoiceStageMixin):
    """One explicit microphone -> local Brain -> speaker turn."""

    DEFAULT_BRAIN_FAILURE_RESPONSE = DEFAULT_BRAIN_FAILURE_RESPONSE
    EVENT_RECORDING_STARTED = EVENT_RECORDING_STARTED
    EVENT_RECORDING_COMPLETED = EVENT_RECORDING_COMPLETED
    EVENT_TRANSCRIPTION_COMPLETED = EVENT_TRANSCRIPTION_COMPLETED
    EVENT_BRAIN_EXECUTION_COMPLETED = EVENT_BRAIN_EXECUTION_COMPLETED
    EVENT_SYNTHESIS_COMPLETED = EVENT_SYNTHESIS_COMPLETED
    EVENT_PLAYBACK_COMPLETED = EVENT_PLAYBACK_COMPLETED
    EVENT_SINGLE_TURN_COMPLETED = EVENT_SINGLE_TURN_COMPLETED

    def __init__(
        self,
        microphone_adapter: MicrophoneAdapter,
        speech_to_text_adapter: SpeechToTextAdapter,
        text_to_speech_adapter: TextToSpeechAdapter,
        speaker_adapter: SpeakerOutputAdapter,
        command_handler: Callable[[str], Any],
        core_service: Optional[CoreService] = None,
        command_router: Optional[VoiceCommandRouter] = None,
        lifecycle_manager: Optional[ModuleLifecycleManager] = None,
        resource_manager: Optional[ResourceManager] = None,
        manifest: Optional[CapabilityManifest] = None,
        event_bus: Optional[EventBus] = None,
        event_history_store: Any = None,
        fallback_policy: Optional[AdapterFallbackPolicy] = None,
        speech_to_text_candidates: Optional[List[AdapterCandidate]] = None,
        stage_callback: Optional[StageCallback] = None,
        clock: Clock = time.perf_counter,
    ):
        required = {
            "microphone_adapter": microphone_adapter,
            "speech_to_text_adapter": speech_to_text_adapter,
            "text_to_speech_adapter": text_to_speech_adapter,
            "speaker_adapter": speaker_adapter,
            "command_handler": command_handler,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(f"Missing single-turn dependencies: {', '.join(missing)}")

        self.microphone_adapter = microphone_adapter
        self.speech_to_text_adapter = speech_to_text_adapter
        self.text_to_speech_adapter = text_to_speech_adapter
        self.speaker_adapter = speaker_adapter
        self.core_service = core_service or CoreService(register_default_pc=False)
        self.command_router = command_router or VoiceCommandRouter(
            command_handler=command_handler,
            core_service=self.core_service,
        )
        self.lifecycle_manager = lifecycle_manager or ModuleLifecycleManager()
        self.resource_manager = resource_manager or self.core_service.resource_manager
        self.manifest = manifest or build_single_turn_voice_pipeline_manifest()
        self.event_bus = event_bus
        self.event_history_store = event_history_store
        self.fallback_policy = fallback_policy
        self.speech_to_text_candidates = list(speech_to_text_candidates or [])
        self.stage_callback = stage_callback
        self.clock = clock
        self.coordinator = VoiceStageCoordinator()
        self._active_request: Optional[SingleTurnVoiceRequestV1] = None
        self._health_verified_correlation_id = ""
        self._owns_reservation = False
        self._active_task_id = ""
        self._last_health: Dict[str, Any] = {}
        self.lifecycle_manager.register_module(
            SINGLE_TURN_MODULE_NAME,
            _LifecycleDelegate(self),
            LifecyclePolicy(stop_after_execute=False, inactivity_seconds=0),
        )

    def start(self, request: Optional[SingleTurnVoiceRequestV1] = None) -> LifecycleResult:
        if request is not None:
            self._active_request = request
        reservation = self.resource_manager.reserve(self.manifest)
        if not reservation.success:
            return lifecycle_resource_failure(
                "start", reservation, self._active_request, SINGLE_TURN_MODULE_NAME
            )
        self._owns_reservation = reservation.status == "reserved"
        lifecycle = self.lifecycle_manager.start(
            SINGLE_TURN_MODULE_NAME,
            self._lifecycle_request("start"),
        )
        if not lifecycle.success and self._owns_reservation:
            self.resource_manager.release(SINGLE_TURN_MODULE_NAME, force=True)
            self._owns_reservation = False
        return replace(
            lifecycle,
            data={**dict(lifecycle.data), "resource_reservation": reservation.to_dict()},
        )

    def health_check(
        self,
        request: Optional[SingleTurnVoiceRequestV1] = None,
    ) -> LifecycleResult:
        if request is not None:
            self._active_request = request
        result = self.lifecycle_manager.health_check(
            SINGLE_TURN_MODULE_NAME,
            self._lifecycle_request("health_check"),
        )
        self._last_health = dict(result.data.get("external_result") or {})
        self._health_verified_correlation_id = (
            self._active_request.correlation_id if result.success and self._active_request else ""
        )
        return result

    def execute(
        self,
        request: SingleTurnVoiceRequestV1,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> SingleTurnVoiceResultV1:
        state = SingleTurnRunState(request=request, started_at=self.clock())
        self._emit(state, EVENT_SINGLE_TURN_STARTED, "pipeline", "started", True)
        return self._execute_ready(request, state, cancellation_token)

    def stop(self, request: Optional[SingleTurnVoiceRequestV1] = None) -> LifecycleResult:
        if request is not None:
            self._active_request = request
        result = self.lifecycle_manager.stop(
            SINGLE_TURN_MODULE_NAME,
            self._lifecycle_request("stop"),
        )
        release = None
        if self._owns_reservation:
            release = self.resource_manager.release(SINGLE_TURN_MODULE_NAME, force=True)
            self._owns_reservation = False
        self._health_verified_correlation_id = ""
        return replace(
            result,
            data={
                **dict(result.data),
                "resource_release": release.to_dict() if release is not None else {},
            },
        )

    def run_once(
        self,
        request: SingleTurnVoiceRequestV1 | Dict[str, Any],
        cancellation_token: Optional[CancellationToken] = None,
    ) -> SingleTurnVoiceResultV1:
        try:
            normalized = validated_single_turn_request(request)
        except ValueError as error:
            return contract_failure_result(request, str(error))

        self._active_request = normalized
        state = SingleTurnRunState(request=normalized, started_at=self.clock())
        self._emit(state, EVENT_SINGLE_TURN_STARTED, "pipeline", "started", True)
        result: Optional[SingleTurnVoiceResultV1] = None
        start_result: Optional[LifecycleResult] = None
        health_result: Optional[LifecycleResult] = None
        stop_result: Optional[LifecycleResult] = None
        try:
            self._stage(1, "Checking components", "running")
            start_result = self.start(normalized)
            if not start_result.success:
                result = self._failure(
                    state,
                    "lifecycle_start",
                    start_result.error_message or start_result.status,
                    "start_failed",
                )
            else:
                health_result = self.health_check(normalized)
                self._apply_health_state(state, health_result)
                if not health_result.success:
                    result = self._failure(
                        state,
                        "health_check",
                        health_result.error_message or health_result.status,
                        "health_check_failed",
                    )
                else:
                    self._stage(1, "Checking components", "passed")
                    result = self._execute_ready(normalized, state, cancellation_token)
        except KeyboardInterrupt:
            if cancellation_token is not None and cancellation_token.supports_cancellation:
                cancellation_token.cancel("keyboard_interrupt")
            result = self._failure(state, "cancellation", "keyboard_interrupt", "cancelled")
        finally:
            stop_result = self.stop(normalized)
            cleanup = self._cleanup_files(normalized, result)

        assert result is not None
        return replace(
            result,
            total_processing_time_seconds=elapsed(self.clock, state.started_at),
            events=[dict(event) for event in state.events],
            data={
                **dict(result.data),
                "lifecycle": {
                    "start": start_result.to_dict() if start_result else {},
                    "health_check": health_result.to_dict() if health_result else {},
                    "stop": stop_result.to_dict() if stop_result else {},
                },
                "cleanup": cleanup,
                "coordinator": self.coordinator.to_dict(),
                "resource_usage": self.resource_manager.current_usage(),
            },
        )

    def lifecycle_status(self) -> Dict[str, Any]:
        return self.lifecycle_manager.status(SINGLE_TURN_MODULE_NAME).to_dict()

    def _execute_ready(
        self,
        request: SingleTurnVoiceRequestV1,
        state: SingleTurnRunState,
        cancellation_token: Optional[CancellationToken],
    ) -> SingleTurnVoiceResultV1:
        if self.lifecycle_manager.status(SINGLE_TURN_MODULE_NAME).state != LIFECYCLE_READY:
            return self._failure(state, "lifecycle", "module_not_ready", "not_ready")
        if self._health_verified_correlation_id != request.correlation_id:
            return self._failure(
                state,
                "health_check",
                "health_not_verified_for_request",
                "health_not_verified",
            )

        task_id = f"{SINGLE_TURN_MODULE_NAME}:{request.correlation_id or 'single-turn'}"
        task_slot = self.resource_manager.acquire_task(
            self.manifest,
            task_id=task_id,
            priority=self.manifest.resources.task_priority,
        )
        if not task_slot.success:
            return self._failure(
                state,
                "resource_reservation",
                task_slot.error_message or task_slot.status,
                "resource_denied",
                data={"task_slot": task_slot.to_dict()},
            )
        self._active_task_id = task_id

        def execute_stages(_: Any) -> SingleTurnVoiceResultV1:
            try:
                return self._run_stages(request, state, cancellation_token)
            except KeyboardInterrupt:
                if cancellation_token is not None and cancellation_token.supports_cancellation:
                    cancellation_token.cancel("keyboard_interrupt")
                return self._failure(
                    state,
                    "cancellation",
                    "keyboard_interrupt",
                    "cancelled",
                )

        try:
            lifecycle = self.lifecycle_manager.execute(
                SINGLE_TURN_MODULE_NAME,
                self._lifecycle_request("execute"),
                execute_stages,
            )
        finally:
            task_release = self.resource_manager.release_task(SINGLE_TURN_MODULE_NAME, task_id)
            self._active_task_id = ""
        if not lifecycle.success:
            return self._failure(
                state,
                "lifecycle_execute",
                lifecycle.error_message or lifecycle.status,
                "execution_failed",
                data={
                    "task_slot": task_slot.to_dict(),
                    "task_release": task_release.to_dict(),
                    "lifecycle_execute": lifecycle.to_dict(),
                },
            )
        response = lifecycle.data.get("response")
        if not isinstance(response, SingleTurnVoiceResultV1):
            return self._failure(
                state,
                "result_contract",
                "invalid_single_turn_result",
                "invalid_result",
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

    def _apply_health_state(
        self,
        state: SingleTurnRunState,
        health_result: LifecycleResult,
    ) -> None:
        external = dict(health_result.data.get("external_result") or {})
        components = dict(external.get("components") or {})
        state.data["health"] = external
        state.microphone_health_status = str(
            dict(components.get("microphone") or {}).get("status")
            or ("skipped" if state.request.text_input.strip() else "unknown")
        )

    def _health_components(
        self,
        request: Optional[SingleTurnVoiceRequestV1],
    ) -> Dict[str, Any]:
        if request is None:
            return {
                "success": False,
                "status": "request_missing",
                "error_message": "single_turn_request_missing",
                "components": {},
            }
        components: Dict[str, Any] = {
            "brain": {
                "success": callable(self.command_router.command_handler),
                "status": "healthy" if callable(self.command_router.command_handler) else "unavailable",
            }
        }
        if not request.text_input.strip():
            components["microphone"] = result_dict(
                self._safe_adapter_call(self.microphone_adapter, "health_check")
            )
            if self.fallback_policy is not None and self.speech_to_text_candidates:
                selection = self.fallback_policy.select(
                    self.speech_to_text_candidates,
                    "voice.transcribe",
                    required_interface_version="v1",
                )
                components["speech_to_text"] = {
                    "success": selection.success,
                    "status": selection.status,
                    "error_message": selection.error_code,
                    "selection": selection.to_dict(),
                }
            else:
                components["speech_to_text"] = result_dict(
                    self._safe_adapter_call(self.speech_to_text_adapter, "health_check")
                )
        else:
            components["microphone"] = {"success": True, "status": "skipped_simulated_input"}
            components["speech_to_text"] = {"success": True, "status": "skipped_simulated_input"}

        needs_synthesis = not request.text_input.strip() or request.playback_enabled
        if needs_synthesis:
            health_check = getattr(self.text_to_speech_adapter, "health_check")
            try:
                components["text_to_speech"] = result_dict(
                    health_check(request.tts_voice_profile)
                )
            except TypeError:
                components["text_to_speech"] = result_dict(health_check())
            components["speaker"] = result_dict(
                self._safe_adapter_call(self.speaker_adapter, "health_check")
            )
        else:
            components["text_to_speech"] = {"success": True, "status": "skipped"}
            components["speaker"] = {"success": True, "status": "skipped"}

        if bool(getattr(self.speaker_adapter, "playing", False)):
            components["speaker"] = {
                "success": False,
                "status": "playback_already_active",
                "error_message": "speaker_playback_already_active",
            }
        failed = [name for name, value in components.items() if not value.get("success")]
        return {
            "success": not failed,
            "status": "healthy" if not failed else "unhealthy_components",
            "error_message": "" if not failed else f"unhealthy:{','.join(failed)}",
            "components": components,
        }

    def _stop_components(self) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        success = True
        for name, adapter in (
            ("speaker", self.speaker_adapter),
            ("microphone", self.microphone_adapter),
            ("text_to_speech", self.text_to_speech_adapter),
        ):
            cancel = getattr(adapter, "cancel_current", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception as error:
                    results[f"{name}_cancel"] = {
                        "success": False,
                        "error_message": safe_exception(error),
                    }
                    success = False
            stopped = self._safe_adapter_call(adapter, "stop")
            results[name] = result_dict(stopped)
            success = success and result_success(stopped)
        self.coordinator.reset()
        return {
            "success": success,
            "status": "stopped" if success else "stop_failed",
            "error_message": "" if success else "component_stop_failed",
            "components": results,
        }

    def _cancelled(
        self,
        state: SingleTurnRunState,
        token: Optional[CancellationToken],
        stage: str,
    ) -> Optional[SingleTurnVoiceResultV1]:
        if token is None or not token.requested:
            return None
        return self._failure(
            state,
            "cancellation",
            token.reason or "cancelled",
            "cancelled",
            data={"cancelled_at": stage, "cancellation": token.to_dict()},
        )

    def _stage_timed_out(
        self,
        state: SingleTurnRunState,
        stage_started: float,
        stage_timeout: Optional[float],
    ) -> bool:
        total_elapsed = elapsed(self.clock, state.started_at)
        if total_elapsed > state.request.timeout_seconds:
            return True
        return stage_timeout is not None and elapsed(self.clock, stage_started) > stage_timeout

    def _cleanup_files(
        self,
        request: SingleTurnVoiceRequestV1,
        result: Optional[SingleTurnVoiceResultV1],
    ) -> Dict[str, Any]:
        preserve = request.cleanup_policy == PIPELINE_CLEANUP_KEEP or result is None or not result.success
        removed: List[str] = []
        preserved: List[str] = []
        for raw_path in (
            request.recording_output_path,
            result.generated_speech_wav_path if result else "",
        ):
            if not raw_path:
                continue
            path = Path(raw_path).expanduser()
            if not path.exists():
                continue
            if preserve:
                preserved.append(str(path))
                continue
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                preserved.append(str(path))
        return {"policy": request.cleanup_policy, "removed": removed, "preserved": preserved}

    def _failure(
        self,
        state: SingleTurnRunState,
        stage: str,
        reason: str,
        status: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> SingleTurnVoiceResultV1:
        self._emit(
            state,
            EVENT_SINGLE_TURN_FAILED,
            stage,
            status,
            False,
            {"error_reason": str(reason or status)[:200]},
        )
        return self._result(
            state,
            success=False,
            status=status,
            error_stage=stage,
            error_reason=reason,
            data=data,
        )

    def _result(
        self,
        state: SingleTurnRunState,
        success: bool,
        status: str,
        error_stage: str = "",
        error_reason: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> SingleTurnVoiceResultV1:
        return SingleTurnVoiceResultV1(
            success=success,
            status=status,
            correlation_id=state.request.correlation_id,
            session_id=state.request.session_id,
            microphone_health_status=state.microphone_health_status,
            recording_status=state.recording_status,
            recorded_wav_path=state.recorded_wav_path,
            recording_duration_seconds=state.recording_duration_seconds,
            peak_amplitude=state.peak_amplitude,
            rms_amplitude=state.rms_amplitude,
            transcription_status=state.transcription_status,
            recognized_text=state.recognized_text,
            transcription_processing_time_seconds=state.transcription_processing_time_seconds,
            brain_execution_status=state.brain_execution_status,
            detected_intent=state.detected_intent,
            routed_skill=state.routed_skill,
            brain_text_response=state.brain_text_response,
            brain_fallback_used=state.brain_fallback_used,
            tts_status=state.tts_status,
            resolved_voice_profile=state.resolved_voice_profile,
            generated_speech_wav_path=state.generated_speech_wav_path,
            tts_processing_time_seconds=state.tts_processing_time_seconds,
            playback_status=state.playback_status,
            total_processing_time_seconds=elapsed(self.clock, state.started_at),
            error_stage=error_stage,
            error_reason=str(error_reason or ""),
            simulated_input=bool(state.request.text_input.strip()),
            data={**dict(state.data), **dict(data or {})},
            events=[dict(event) for event in state.events],
            metadata={
                "safe": True,
                "source": "single_turn_voice_pipeline",
                "owner_triggered_only": True,
                "background_listening": False,
            },
        )

    def _emit(
        self,
        state: SingleTurnRunState,
        event_type: str,
        stage: str,
        status: str,
        success: bool,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Event:
        payload = {
            "correlation_id": state.request.correlation_id,
            "session_id": state.request.session_id,
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
            )
            if self.event_bus is not None
            else Event(source="voice", type=event_type, payload=payload)
        )
        state.events.append(event.to_dict())
        self._store_event_history(state, event, event_type, stage, status, success)
        return event

    def _store_event_history(
        self,
        state: SingleTurnRunState,
        event: Event,
        event_type: str,
        stage: str,
        status: str,
        success: bool,
    ) -> None:
        if self.event_history_store is None:
            return
        try:
            self.event_history_store.add(
                event,
                {
                    "success": success,
                    "decision": "recorded",
                    "text": "Single-turn voice operational event recorded.",
                    "data": {"event_type": event_type, "stage": stage, "status": status},
                    "error_message": "" if success else status,
                    "metadata": {"safe": True, "source": "single_turn_voice_pipeline"},
                },
            )
        except (OSError, RuntimeError, ValueError) as error:
            state.data.setdefault("event_history_failures", []).append(
                {"event_type": event_type, "error": safe_exception(error)}
            )

    def _stage(self, index: int, label: str, status: str) -> None:
        if self.stage_callback is not None:
            self.stage_callback(index, 6, label, status)

    def _safe_adapter_call(self, adapter: Any, method_name: str) -> Any:
        method = getattr(adapter, method_name, None)
        if not callable(method):
            return {
                "success": False,
                "status": "operation_unsupported",
                "error_message": f"{method_name}_unsupported",
            }
        try:
            return method()
        except Exception as error:
            return {
                "success": False,
                "status": "operation_failed",
                "error_message": safe_exception(error),
            }

    def _lifecycle_request(self, operation: str) -> LifecycleRequest:
        request = self._active_request
        return LifecycleRequest(
            module_name=SINGLE_TURN_MODULE_NAME,
            operation=operation,
            payload={
                "single_turn_contract": request.to_dict() if request else {},
                "owner_triggered": True,
            },
            correlation_id=request.correlation_id if request else "",
            session_id=request.session_id if request else "",
            metadata={"source": "single_turn_voice_pipeline"},
        )
