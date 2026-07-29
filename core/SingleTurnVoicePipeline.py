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
    FinalizedAudioHook,
    PIPELINE_CLEANUP_DELETE_ALWAYS,
    PIPELINE_CLEANUP_KEEP,
    PreBrainHook,
    RawTranscriptHook,
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
from core.TranscriptNormalization import TranscriptNormalizer
from core.VoiceCommandRouter import VoiceCommandRouter
from memory.schema_migrations import MigrationError


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
CaptureReadyCallback = Callable[[Dict[str, Any]], None]


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
        transcript_normalizer: Optional[TranscriptNormalizer] = None,
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
        self.transcript_normalizer = transcript_normalizer or TranscriptNormalizer()
        self.stage_callback = stage_callback
        self._stage_observers: List[StageCallback] = []
        self._capture_ready_observers: List[CaptureReadyCallback] = []
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
        pre_brain_hook: Optional[PreBrainHook] = None,
        raw_transcript_hook: Optional[RawTranscriptHook] = None,
        finalized_audio_hook: Optional[FinalizedAudioHook] = None,
    ) -> SingleTurnVoiceResultV1:
        state = SingleTurnRunState(request=request, started_at=self.clock())
        self._emit(state, EVENT_SINGLE_TURN_STARTED, "pipeline", "started", True)
        return self._execute_ready(
            request,
            state,
            cancellation_token,
            pre_brain_hook,
            raw_transcript_hook,
            finalized_audio_hook,
        )

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
        pre_brain_hook: Optional[PreBrainHook] = None,
        raw_transcript_hook: Optional[RawTranscriptHook] = None,
        finalized_audio_hook: Optional[FinalizedAudioHook] = None,
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
                    result = self._execute_ready(
                        normalized,
                        state,
                        cancellation_token,
                        pre_brain_hook,
                        raw_transcript_hook,
                        finalized_audio_hook,
                    )
        except KeyboardInterrupt:
            if cancellation_token is not None and cancellation_token.supports_cancellation:
                cancellation_token.cancel("keyboard_interrupt")
            self._stop_components()
            raise
        finally:
            stop_result = self.stop(normalized)
            cleanup = self._cleanup_files(normalized, result)

        assert result is not None
        final_data = {
            **dict(result.data),
            "lifecycle": {
                "start": start_result.to_dict() if start_result else {},
                "health_check": health_result.to_dict() if health_result else {},
                "stop": stop_result.to_dict() if stop_result else {},
            },
            "cleanup": cleanup,
            "coordinator": self.coordinator.to_dict(),
            "resource_usage": self.resource_manager.current_usage(),
        }
        if result.data.get("protected_input_redacted"):
            final_data = _redact_protected_input_data(final_data, normalized.text_input)
        return replace(
            result,
            total_processing_time_seconds=elapsed(self.clock, state.started_at),
            events=[dict(event) for event in state.events],
            data=final_data,
        )

    def run_local_output(
        self,
        request: SingleTurnVoiceRequestV1,
        text: str,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> SingleTurnVoiceResultV1:
        clean_text = str(text or "").strip()
        if not clean_text:
            return contract_failure_result(request, "local_output_text_required")
        try:
            normalized = validated_single_turn_request(request)
        except ValueError as error:
            return contract_failure_result(request, str(error))
        local_request = replace(
            normalized,
            text_input=clean_text,
            playback_enabled=True,
            metadata={
                **dict(normalized.metadata or {}),
                "local_output": True,
            },
        )
        self._active_request = local_request
        state = SingleTurnRunState(request=local_request, started_at=self.clock())
        state.brain_execution_status = "local_output"
        state.brain_text_response = clean_text
        state.data["local_output"] = {
            "source": "configured_local_phrase",
            "input_turn_created": False,
        }
        result: Optional[SingleTurnVoiceResultV1] = None
        start_result: Optional[LifecycleResult] = None
        health_result: Optional[LifecycleResult] = None
        stop_result: Optional[LifecycleResult] = None
        try:
            start_result = self.start(local_request)
            if not start_result.success:
                result = self._failure(
                    state,
                    "lifecycle_start",
                    start_result.error_message or start_result.status,
                    "start_failed",
                )
            else:
                health_result = self.health_check(local_request)
                self._apply_health_state(state, health_result)
                if not health_result.success:
                    result = self._failure(
                        state,
                        "health_check",
                        health_result.error_message or health_result.status,
                        "health_check_failed",
                    )
                else:
                    result = self._execute_ready(
                        local_request,
                        state,
                        cancellation_token,
                        stage_runner=self._run_local_output_stages,
                    )
        except KeyboardInterrupt:
            if cancellation_token is not None and cancellation_token.supports_cancellation:
                cancellation_token.cancel("keyboard_interrupt")
            self._stop_components()
            raise
        finally:
            stop_result = self.stop(local_request)
            cleanup = self._cleanup_files(local_request, result)

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

    def add_stage_observer(self, observer: StageCallback) -> Callable[[], None]:
        if not callable(observer):
            raise TypeError("stage observer must be callable")
        self._stage_observers.append(observer)

        def unsubscribe() -> None:
            if observer in self._stage_observers:
                self._stage_observers.remove(observer)

        return unsubscribe

    def add_capture_ready_observer(
        self,
        observer: CaptureReadyCallback,
    ) -> Callable[[], None]:
        """Observe the safe owner-speech boundary for a real recording.

        Unlike the generic ``Recording/running`` stage, this notification is
        emitted only after the microphone transport is open and any requested
        ambient calibration has completed.  Callers may therefore print their
        owner prompt without sacrificing speech to setup or calibration.
        """

        if not callable(observer):
            raise ValueError("capture ready observer must be callable")
        self._capture_ready_observers.append(observer)

        def unsubscribe() -> None:
            if observer in self._capture_ready_observers:
                self._capture_ready_observers.remove(observer)

        return unsubscribe

    def _notify_capture_ready(self, diagnostics: Dict[str, Any]) -> None:
        payload = dict(diagnostics or {})
        for observer in list(self._capture_ready_observers):
            try:
                observer(dict(payload))
            except (OSError, RuntimeError, TypeError, ValueError):
                # A terminal-only diagnostic observer cannot invalidate audio.
                continue

    def coordination_status(self) -> Dict[str, Any]:
        status = self.coordinator.to_dict()
        status["speaker_playing"] = bool(getattr(self.speaker_adapter, "playing", False))
        status["idle"] = not (
            status["capture_active"]
            or status["playback_active"]
            or status["heavy_stage"]
            or status["speaker_playing"]
        )
        return status

    def lifecycle_status(self) -> Dict[str, Any]:
        return self.lifecycle_manager.status(SINGLE_TURN_MODULE_NAME).to_dict()

    def _execute_ready(
        self,
        request: SingleTurnVoiceRequestV1,
        state: SingleTurnRunState,
        cancellation_token: Optional[CancellationToken],
        pre_brain_hook: Optional[PreBrainHook] = None,
        raw_transcript_hook: Optional[RawTranscriptHook] = None,
        finalized_audio_hook: Optional[FinalizedAudioHook] = None,
        stage_runner: Optional[Callable[..., SingleTurnVoiceResultV1]] = None,
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
                runner = stage_runner or self._run_stages
                return runner(
                    request,
                    state,
                    cancellation_token,
                    pre_brain_hook,
                    raw_transcript_hook,
                    finalized_audio_hook,
                )
            except KeyboardInterrupt:
                if cancellation_token is not None and cancellation_token.supports_cancellation:
                    cancellation_token.cancel("keyboard_interrupt")
                raise

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
        response_data = {
            **dict(response.data),
            "task_slot": task_slot.to_dict(),
            "task_release": task_release.to_dict(),
            "lifecycle_execute": lifecycle.to_dict(),
        }
        if response.data.get("protected_input_redacted"):
            response_data = _redact_protected_input_data(response_data, request.text_input)
        return replace(
            response,
            data=response_data,
        )

    def _run_local_output_stages(
        self,
        request: SingleTurnVoiceRequestV1,
        state: SingleTurnRunState,
        cancellation_token: Optional[CancellationToken],
        _pre_brain_hook: Optional[PreBrainHook] = None,
        _raw_transcript_hook: Optional[RawTranscriptHook] = None,
        _finalized_audio_hook: Optional[FinalizedAudioHook] = None,
    ) -> SingleTurnVoiceResultV1:
        cancelled = self._cancelled(state, cancellation_token, "before_synthesis")
        if cancelled:
            return cancelled
        self._stage(5, "Synthesizing response", "running")
        synthesis_failure = self._synthesize(request, state, emit_event=False)
        if synthesis_failure:
            return synthesis_failure
        self._stage(5, "Synthesizing response", "completed")

        cancelled = self._cancelled(state, cancellation_token, "before_playback")
        if cancelled:
            return cancelled
        self._stage(6, "Playing response", "running")
        playback_failure = self._playback(request, state, emit_event=False)
        if playback_failure:
            return playback_failure
        self._stage(6, "Playing response", "completed")
        return self._result(state, success=True, status="completed_local_output")

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
            ("speech_to_text", self.speech_to_text_adapter),
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
            stop = getattr(adapter, "stop", None)
            stopped = (
                self._safe_adapter_call(adapter, "stop")
                if callable(stop)
                else {
                    "success": True,
                    "status": "no_persistent_resources",
                    "metadata": {
                        "safe": True,
                        "source": "single_turn_voice_pipeline",
                    },
                }
            )
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
        preserve = (
            request.cleanup_policy == PIPELINE_CLEANUP_KEEP
            or (
                request.cleanup_policy != PIPELINE_CLEANUP_DELETE_ALWAYS
                and (result is None or not result.success)
            )
        )
        removed: List[str] = []
        preserved: List[str] = []
        recording = dict(result.data.get("recording") or {}) if result else {}
        recording_data = dict(recording.get("data") or {})
        candidate_paths = [
            request.recording_output_path,
            result.recorded_wav_path if result else "",
            result.generated_speech_wav_path if result else "",
            recording.get("raw_wav_path", ""),
            recording.get("assembled_wav_path", ""),
            recording.get("normalized_wav_path", ""),
            recording_data.get("raw_wav_path", ""),
            recording_data.get("assembled_wav_path", ""),
            recording_data.get("normalized_wav_path", ""),
            recording_data.get("final_whisper_input_path", ""),
        ]
        stage_directories = set()
        for raw_path in dict.fromkeys(str(item or "") for item in candidate_paths):
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
                if ".turn." in path.parent.name:
                    stage_directories.add(path.parent)
            except OSError:
                preserved.append(str(path))
        for directory in sorted(stage_directories, key=str, reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
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
            raw_transcript=state.raw_transcript,
            cleaned_transcript=state.cleaned_transcript,
            normalized_command=state.normalized_command,
            extracted_calculator_expression=state.extracted_calculator_expression,
            repetition_detected=state.repetition_detected,
            repetitions_removed=state.repetitions_removed,
            transcript_cleanup_rule=state.transcript_cleanup_rule,
            transcription_processing_time_seconds=state.transcription_processing_time_seconds,
            brain_execution_status=state.brain_execution_status,
            detected_intent=state.detected_intent,
            candidate_skills=[dict(candidate) for candidate in state.candidate_skills],
            routed_skill=state.routed_skill,
            planner_decision=state.planner_decision,
            execution_result=state.execution_result,
            rejection_reason=state.rejection_reason,
            routing_diagnostics=dict(state.routing_diagnostics),
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
                correlation_id=state.request.correlation_id,
                session_id=state.request.session_id,
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
        except (MigrationError, OSError) as error:
            state.data.setdefault("event_history_failures", []).append(
                {"event_type": event_type, "error": safe_exception(error)}
            )

    def _stage(self, index: int, label: str, status: str) -> None:
        if self.stage_callback is not None:
            self.stage_callback(index, 6, label, status)
        for observer in list(self._stage_observers):
            observer(index, 6, label, status)

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


def _redact_protected_input_data(value: Any, sensitive_text: str) -> Any:
    sensitive = str(sensitive_text or "").strip()
    if isinstance(value, dict):
        return {
            key: _redact_protected_input_data(item, sensitive)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_protected_input_data(item, sensitive) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_protected_input_data(item, sensitive) for item in value)
    if isinstance(value, str) and sensitive and sensitive.casefold() in value.casefold():
        return "[REDACTED]"
    return value
