from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from core.Contracts import (
    CONTRACT_MICROPHONE_CAPTURE_RESULT,
    CONTRACT_SPEECH_TO_TEXT_RESULT,
    CONTRACT_VERSION_V1,
    CONTRACT_VOICE_PIPELINE_REQUEST,
    CONTRACT_VOICE_PIPELINE_RESULT,
    MicrophoneCaptureRequestV1,
    SpeechToTextRequestV1,
    VoicePipelineRequestV1,
    utc_contract_timestamp,
    validate_contract,
)
from core.CoreService import CoreService
from core.EventBus import Event, EventBus, PRIORITY_NORMAL
from core.Health import (
    RETRY_SAFE,
    AdapterCandidate,
    AdapterFallbackPolicy,
)
from core.Microphone import AudioChunk, MicrophoneAdapter, MicrophoneResult
from core.SpeechToText import SpeechToTextAdapter, TranscriptionResult
from core.VoiceCommandRouter import (
    DEFAULT_VOICE_COMMAND_CONFIDENCE_THRESHOLD,
    DEFAULT_VOICE_ROUTE_CAPABILITY,
    VoiceCommandRouter,
    VoiceCommandRoutingResult,
)
from core.VoiceService import VoiceOutputAdapter, VoiceServiceResult


VOICE_PIPELINE_AUDIO_CAPTURED_EVENT = "voice_pipeline.audio_captured"
VOICE_PIPELINE_TRANSCRIPTION_ACCEPTED_EVENT = "voice_pipeline.transcription_accepted"
VOICE_PIPELINE_TRANSCRIPTION_REJECTED_EVENT = "voice_pipeline.transcription_rejected"
VOICE_PIPELINE_COMMAND_ROUTED_EVENT = "voice_pipeline.command_routed"
VOICE_PIPELINE_COMMAND_REJECTED_EVENT = "voice_pipeline.command_rejected"
VOICE_PIPELINE_CITY_ACTIVATED_EVENT = "voice_pipeline.city_activated"
VOICE_PIPELINE_EXECUTION_COMPLETED_EVENT = "voice_pipeline.execution_completed"
VOICE_PIPELINE_EXECUTION_FAILED_EVENT = "voice_pipeline.execution_failed"
VOICE_PIPELINE_OUTPUT_PRODUCED_EVENT = "voice_pipeline.output_produced"

VoiceCommandHandler = Callable[[str], Any]


@dataclass(frozen=True)
class VoicePipelineResult:
    success: bool
    status: str
    text: str = ""
    response_text: str = ""
    session_id: str = ""
    correlation_id: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    contract_name: str = CONTRACT_VOICE_PIPELINE_RESULT
    contract_version: str = CONTRACT_VERSION_V1
    created_at: str = field(default_factory=utc_contract_timestamp)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "success": self.success,
            "status": self.status,
            "text": self.text,
            "response_text": self.response_text,
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "error_message": self.error_message,
            "data": dict(self.data),
            "events": [dict(event) for event in self.events],
            "metadata": dict(self.metadata),
        }


class VoicePipeline:
    """End-to-end simulated Voice City command pipeline with injected adapters."""

    def __init__(
        self,
        microphone_adapter: MicrophoneAdapter,
        speech_to_text_adapter: SpeechToTextAdapter,
        output_adapter: VoiceOutputAdapter,
        command_handler: Optional[VoiceCommandHandler] = None,
        core_service: Optional[CoreService] = None,
        command_router: Optional[VoiceCommandRouter] = None,
        event_bus: Optional[EventBus] = None,
        fallback_policy: Optional[AdapterFallbackPolicy] = None,
        microphone_candidates: Optional[List[AdapterCandidate]] = None,
        speech_to_text_candidates: Optional[List[AdapterCandidate]] = None,
        confidence_threshold: float = DEFAULT_VOICE_COMMAND_CONFIDENCE_THRESHOLD,
        route_capability: str = DEFAULT_VOICE_ROUTE_CAPABILITY,
    ):
        if microphone_adapter is None:
            raise ValueError("microphone_adapter is required")
        if speech_to_text_adapter is None:
            raise ValueError("speech_to_text_adapter is required")
        if output_adapter is None:
            raise ValueError("output_adapter is required")

        self.microphone_adapter = microphone_adapter
        self.speech_to_text_adapter = speech_to_text_adapter
        self.output_adapter = output_adapter
        self._active_microphone_adapter = microphone_adapter
        self._active_speech_to_text_adapter = speech_to_text_adapter
        self.fallback_policy = fallback_policy
        self.microphone_candidates = list(microphone_candidates or [])
        self.speech_to_text_candidates = list(speech_to_text_candidates or [])
        self.event_bus = event_bus
        self.core_service = core_service or CoreService(register_default_pc=False)
        self.command_router = command_router or VoiceCommandRouter(
            command_handler=command_handler,
            core_service=self.core_service,
            confidence_threshold=confidence_threshold,
            route_capability=route_capability,
        )

    def run_once(
        self,
        session_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        request: Optional[Any] = None,
        contract_version: str = CONTRACT_VERSION_V1,
    ) -> VoicePipelineResult:
        pipeline_request = _voice_pipeline_request(
            request=request,
            session_id=session_id,
            correlation_id=correlation_id,
            timeout_seconds=timeout_seconds,
            contract_version=contract_version,
        )
        request_payload = _contract_payload(pipeline_request)
        clean_session_id = _stable_id(request_payload.get("session_id") or session_id, "voice-session")
        clean_correlation_id = _stable_id(
            request_payload.get("correlation_id") or correlation_id,
            "voice-correlation",
        )
        request_compatibility = validate_contract(
            pipeline_request,
            expected_contract_name=CONTRACT_VOICE_PIPELINE_REQUEST,
        )
        if not request_compatibility.success:
            return VoicePipelineResult(
                success=False,
                status="contract_rejected",
                text="Voice pipeline request rejected by compatibility gate.",
                response_text="",
                session_id=clean_session_id,
                correlation_id=clean_correlation_id,
                error_message=request_compatibility.error_message or request_compatibility.status,
                data={
                    "request": request_payload,
                    "compatibility": request_compatibility.to_dict(),
                    "microphone_started": False,
                    "speech_to_text_started": False,
                    "core_service_routed": False,
                },
                metadata=_metadata(),
            )

        timeout_seconds = request_payload.get("timeout_seconds")
        run_events: List[Dict[str, Any]] = []
        self._active_microphone_adapter = self.microphone_adapter
        self._active_speech_to_text_adapter = self.speech_to_text_adapter
        microphone_request = MicrophoneCaptureRequestV1(
            timeout_seconds=timeout_seconds,
            session_id=clean_session_id,
            correlation_id=clean_correlation_id,
            metadata={"source": "voice_pipeline"},
        )
        microphone_compatibility = validate_contract(microphone_request)
        if not microphone_compatibility.success:
            return VoicePipelineResult(
                success=False,
                status="contract_rejected",
                text="Microphone capture request rejected by compatibility gate.",
                session_id=clean_session_id,
                correlation_id=clean_correlation_id,
                error_message=microphone_compatibility.error_message or microphone_compatibility.status,
                data={
                    "request": request_payload,
                    "microphone_request": microphone_request.to_dict(),
                    "compatibility": microphone_compatibility.to_dict(),
                },
                metadata=_metadata(),
            )
        microphone_data: Dict[str, Any] = {"request": microphone_request.to_dict()}

        microphone_selection = self._select_microphone_adapter()
        if microphone_selection is not None:
            microphone_data["adapter_selection"] = microphone_selection.to_dict()
            if not microphone_selection.success:
                return self._finish_with_output(
                    success=False,
                    status="microphone_failed",
                    text=microphone_selection.message or "No healthy microphone adapter is available.",
                    response_text=microphone_selection.message
                    or "No healthy microphone adapter is available.",
                    error_message=microphone_selection.error_code
                    or "microphone_adapter_unavailable",
                    session_id=clean_session_id,
                    correlation_id=clean_correlation_id,
                    run_events=run_events,
                    data={"microphone": microphone_data},
                    execution_failed_stage="microphone_health",
                )

        start_result = self._start_microphone()
        microphone_data["start"] = start_result.to_dict()
        start_compatibility = validate_contract(
            start_result,
            expected_contract_name=CONTRACT_MICROPHONE_CAPTURE_RESULT,
        )
        if not start_compatibility.success:
            return VoicePipelineResult(
                success=False,
                status="contract_rejected",
                text="Microphone start result rejected by compatibility gate.",
                session_id=clean_session_id,
                correlation_id=clean_correlation_id,
                error_message=start_compatibility.error_message or start_compatibility.status,
                data={
                    "request": request_payload,
                    "microphone": microphone_data,
                    "compatibility": start_compatibility.to_dict(),
                },
                metadata=_metadata(),
            )
        if not start_result.success:
            return self._finish_with_output(
                success=False,
                status="microphone_failed",
                text=start_result.text or "Voice pipeline failed at microphone start.",
                response_text=start_result.text or "Voice pipeline failed safely.",
                error_message=start_result.error_message or start_result.status,
                session_id=clean_session_id,
                correlation_id=clean_correlation_id,
                run_events=run_events,
                data={"microphone": microphone_data},
                execution_failed_stage="microphone_start",
            )

        read_result = self._read_microphone(timeout_seconds)
        microphone_data["read"] = read_result.to_dict()
        stop_result = self._stop_microphone()
        microphone_data["stop"] = stop_result.to_dict()
        read_compatibility = validate_contract(
            read_result,
            expected_contract_name=CONTRACT_MICROPHONE_CAPTURE_RESULT,
        )
        if not read_compatibility.success:
            return VoicePipelineResult(
                success=False,
                status="contract_rejected",
                text="Microphone read result rejected by compatibility gate.",
                session_id=clean_session_id,
                correlation_id=clean_correlation_id,
                error_message=read_compatibility.error_message or read_compatibility.status,
                data={
                    "request": request_payload,
                    "microphone": microphone_data,
                    "compatibility": read_compatibility.to_dict(),
                },
                metadata=_metadata(),
            )

        if not read_result.success:
            return self._finish_with_output(
                success=False,
                status="microphone_failed",
                text=read_result.text or "Voice pipeline failed at microphone read.",
                response_text=read_result.text or "Voice pipeline failed safely.",
                error_message=read_result.error_message or read_result.status,
                session_id=clean_session_id,
                correlation_id=clean_correlation_id,
                run_events=run_events,
                data={"microphone": microphone_data},
                execution_failed_stage="microphone_read",
            )

        audio_chunk = read_result.chunk or _empty_audio_chunk(
            session_id=clean_session_id,
            correlation_id=clean_correlation_id,
        )
        self._emit_event(
            run_events,
            VOICE_PIPELINE_AUDIO_CAPTURED_EVENT,
            clean_session_id,
            clean_correlation_id,
            {
                "stage": "audio_capture",
                "status": read_result.status,
                "success": True,
                "byte_count": audio_chunk.byte_count,
                "audio": audio_chunk.to_dict(),
            },
        )

        stt_request = SpeechToTextRequestV1(
            audio_chunk=audio_chunk.to_dict(),
            session_id=clean_session_id,
            correlation_id=clean_correlation_id,
            metadata={"source": "voice_pipeline"},
        )
        stt_request_compatibility = validate_contract(stt_request)
        if not stt_request_compatibility.success:
            return VoicePipelineResult(
                success=False,
                status="contract_rejected",
                text="Speech-to-text request rejected by compatibility gate.",
                session_id=clean_session_id,
                correlation_id=clean_correlation_id,
                error_message=stt_request_compatibility.error_message or stt_request_compatibility.status,
                data={
                    "request": request_payload,
                    "speech_to_text_request": stt_request.to_dict(),
                    "compatibility": stt_request_compatibility.to_dict(),
                },
                metadata=_metadata(),
            )

        transcription = self._transcribe(audio_chunk)
        transcription_data = transcription.to_dict()
        transcription_compatibility = validate_contract(
            transcription,
            expected_contract_name=CONTRACT_SPEECH_TO_TEXT_RESULT,
        )
        if not transcription_compatibility.success:
            return self._finish_with_output(
                success=False,
                status="contract_rejected",
                text="Speech-to-text result rejected by compatibility gate.",
                response_text="Speech-to-text result rejected by compatibility gate.",
                error_message=transcription_compatibility.error_message or transcription_compatibility.status,
                session_id=clean_session_id,
                correlation_id=clean_correlation_id,
                run_events=run_events,
                data={
                    "request": request_payload,
                    "microphone": microphone_data,
                    "audio": audio_chunk.to_dict(),
                    "speech_to_text_request": stt_request.to_dict(),
                    "transcription": transcription_data,
                    "compatibility": transcription_compatibility.to_dict(),
                },
                execution_failed_stage="speech_to_text_contract",
            )
        transcription_data["request"] = stt_request.to_dict()
        transcription_event_type = (
            VOICE_PIPELINE_TRANSCRIPTION_ACCEPTED_EVENT
            if transcription.success and bool(transcription.text)
            else VOICE_PIPELINE_TRANSCRIPTION_REJECTED_EVENT
        )
        self._emit_event(
            run_events,
            transcription_event_type,
            clean_session_id,
            clean_correlation_id,
            {
                "stage": "transcription",
                "status": transcription.status,
                "success": transcription.success,
                "confidence": transcription.confidence,
                "text_length": len(transcription.text),
                "error_message": transcription.error_message,
            },
        )

        routing_result = self.command_router.route(
            transcription,
            session_id=clean_session_id,
            correlation_id=clean_correlation_id,
        )
        routing_data = routing_result.to_dict()
        command_event_type = (
            VOICE_PIPELINE_COMMAND_ROUTED_EVENT
            if routing_result.success and routing_result.status == "routed"
            else VOICE_PIPELINE_COMMAND_REJECTED_EVENT
        )
        self._emit_event(
            run_events,
            command_event_type,
            clean_session_id,
            clean_correlation_id,
            {
                "stage": "command_routing",
                "status": routing_result.status,
                "success": routing_result.success,
                "route": routing_result.route,
                "confidence": routing_result.confidence,
                "text_length": len(routing_result.input_text),
                "error_message": routing_result.error_message,
            },
        )

        activated_city = _activated_city(routing_result)
        if activated_city:
            self._emit_event(
                run_events,
                VOICE_PIPELINE_CITY_ACTIVATED_EVENT,
                clean_session_id,
                clean_correlation_id,
                {
                    "stage": "city_activation",
                    "status": "activated",
                    "success": True,
                    "city": activated_city,
                    "city_lifecycle": _city_lifecycle(routing_result),
                    "city_statuses": _city_statuses(routing_result),
                },
            )

        execution_event_type = (
            VOICE_PIPELINE_EXECUTION_COMPLETED_EVENT
            if routing_result.success
            else VOICE_PIPELINE_EXECUTION_FAILED_EVENT
        )
        self._emit_event(
            run_events,
            execution_event_type,
            clean_session_id,
            clean_correlation_id,
            {
                "stage": "execution",
                "status": routing_result.status,
                "success": routing_result.success,
                "city": activated_city,
                "error_message": routing_result.error_message,
            },
        )

        final_response_text = _final_response_text(routing_result)
        return self._finish_with_output(
            success=routing_result.success,
            status="completed" if routing_result.success and routing_result.status == "routed" else routing_result.status,
            text=routing_result.text,
            response_text=final_response_text,
            error_message=routing_result.error_message,
            session_id=clean_session_id,
            correlation_id=clean_correlation_id,
            run_events=run_events,
            data={
                "microphone": microphone_data,
                "audio": audio_chunk.to_dict(),
                "transcription": transcription_data,
                "routing": routing_data,
                "activated_city": activated_city,
                "city_statuses": _city_statuses(routing_result),
            },
        )

    def _finish_with_output(
        self,
        success: bool,
        status: str,
        text: str,
        response_text: str,
        error_message: str,
        session_id: str,
        correlation_id: str,
        run_events: List[Dict[str, Any]],
        data: Dict[str, Any],
        execution_failed_stage: str = "",
    ) -> VoicePipelineResult:
        if execution_failed_stage:
            self._emit_event(
                run_events,
                VOICE_PIPELINE_EXECUTION_FAILED_EVENT,
                session_id,
                correlation_id,
                {
                    "stage": execution_failed_stage,
                    "status": status,
                    "success": False,
                    "error_message": error_message,
                },
            )

        output_result = self._speak(response_text)
        output_data = _voice_result_to_dict(output_result)
        final_data = {
            **dict(data),
            "output": output_data,
            "session_id": session_id,
            "correlation_id": correlation_id,
        }

        if not output_result.success:
            output_error = output_result.error_message or "voice_output_failed"
            self._emit_event(
                run_events,
                VOICE_PIPELINE_EXECUTION_FAILED_EVENT,
                session_id,
                correlation_id,
                {
                    "stage": "output",
                    "status": "output_failed",
                    "success": False,
                    "error_message": output_error,
                },
            )
            return VoicePipelineResult(
                success=False,
                status="output_failed",
                text=output_result.text,
                response_text=response_text,
                session_id=session_id,
                correlation_id=correlation_id,
                error_message=output_error,
                data=final_data,
                events=[dict(event) for event in run_events],
                metadata=_metadata(),
            )

        self._emit_event(
            run_events,
            VOICE_PIPELINE_OUTPUT_PRODUCED_EVENT,
            session_id,
            correlation_id,
            {
                "stage": "output",
                "status": "output_produced",
                "success": True,
                "text_length": len(response_text),
                "output": output_data,
            },
        )
        return VoicePipelineResult(
            success=success,
            status=status,
            text=text,
            response_text=response_text,
            session_id=session_id,
            correlation_id=correlation_id,
            error_message=error_message,
            data=final_data,
            events=[dict(event) for event in run_events],
            metadata=_metadata(),
        )

    def _start_microphone(self) -> MicrophoneResult:
        try:
            return self._active_microphone_adapter.start()
        except Exception as error:
            return _microphone_failure("start_failed", error)

    def _read_microphone(self, timeout_seconds: Optional[float]) -> MicrophoneResult:
        try:
            return self._active_microphone_adapter.read_chunk(timeout_seconds=timeout_seconds)
        except Exception as error:
            return _microphone_failure("read_failed", error)

    def _stop_microphone(self) -> MicrophoneResult:
        try:
            return self._active_microphone_adapter.stop()
        except Exception as error:
            return _microphone_failure("stop_failed", error)

    def _transcribe(self, audio_chunk: AudioChunk) -> TranscriptionResult:
        if self.fallback_policy is not None and self.speech_to_text_candidates:
            execution = self.fallback_policy.execute(
                self.speech_to_text_candidates,
                "voice.transcribe",
                lambda adapter: adapter.transcribe(audio_chunk),
                retry_safety=RETRY_SAFE,
                required_interface_version=CONTRACT_VERSION_V1,
            )
            if execution.success:
                return _transcription_from_execution(execution, audio_chunk)
            return TranscriptionResult(
                success=False,
                status=execution.status,
                text="",
                confidence=0.0,
                error_message=execution.original_error
                or execution.error_message
                or "speech_to_text_unavailable",
                data={
                    "audio_chunk": audio_chunk.to_dict(),
                    "source": "voice_pipeline",
                    "speech_engine_access": "disabled",
                    "fallback_execution": execution.to_dict(),
                },
                metadata=_metadata(),
            )
        try:
            return self._active_speech_to_text_adapter.transcribe(audio_chunk)
        except Exception as error:
            return TranscriptionResult(
                success=False,
                status="adapter_failed",
                text="",
                confidence=0.0,
                error_message=f"{type(error).__name__}: {error}",
                data={
                    "audio_chunk": audio_chunk.to_dict(),
                    "source": "voice_pipeline",
                    "speech_engine_access": "disabled",
                },
                metadata=_metadata(),
            )

    def _select_microphone_adapter(self):
        if self.fallback_policy is None or not self.microphone_candidates:
            return None
        selection = self.fallback_policy.select(
            self.microphone_candidates,
            "voice.capture",
            required_interface_version=CONTRACT_VERSION_V1,
        )
        if selection.success:
            self._active_microphone_adapter = selection.selected_adapter
        return selection

    def _speak(self, text: str) -> VoiceServiceResult:
        try:
            return self.output_adapter.speak(text)
        except Exception as error:
            return VoiceServiceResult(
                success=False,
                text="Voice output failed safely.",
                error_message=f"{type(error).__name__}: {error}",
                data={
                    "accepted_text": str(text or ""),
                    "speaker": "disabled",
                    "tts": "disabled",
                    "source": "voice_pipeline",
                },
                metadata=_metadata(),
            )

    def _emit_event(
        self,
        run_events: List[Dict[str, Any]],
        event_type: str,
        session_id: str,
        correlation_id: str,
        payload: Dict[str, Any],
    ) -> Event:
        event_payload = {
            "session_id": session_id,
            "correlation_id": correlation_id,
            **dict(payload),
        }
        if self.event_bus is not None:
            event = self.event_bus.publish(
                source="voice",
                type=event_type,
                payload=event_payload,
                priority=PRIORITY_NORMAL,
            )
        else:
            event = Event(
                source="voice",
                type=event_type,
                payload=event_payload,
                priority=PRIORITY_NORMAL,
            )
        run_events.append(event.to_dict())
        return event


def _stable_id(value: Optional[str], prefix: str) -> str:
    clean = str(value or "").strip()
    return clean or f"{prefix}-{uuid4()}"


def _empty_audio_chunk(session_id: str, correlation_id: str) -> AudioChunk:
    return AudioChunk(
        data=b"",
        source="voice_pipeline.empty_audio",
        metadata={
            "session_id": session_id,
            "correlation_id": correlation_id,
        },
    )


def _microphone_failure(status: str, error: Exception) -> MicrophoneResult:
    message = f"{type(error).__name__}: {error}"
    return MicrophoneResult(
        success=False,
        status=status,
        text=f"Microphone {status.replace('_', ' ')} safely.",
        error_message=message,
        data={"source": "voice_pipeline", "audio_hardware_access": "disabled"},
        metadata=_metadata(),
    )


def _transcription_from_execution(
    execution: Any,
    audio_chunk: AudioChunk,
) -> TranscriptionResult:
    data = dict(execution.data or {})
    transcription_data = dict(data.get("data") or {})
    metadata = dict(data.get("metadata") or {})
    return TranscriptionResult(
        success=bool(data.get("success", True)),
        status=str(data.get("status") or "transcribed"),
        text=str(data.get("text") or ""),
        confidence=float(data.get("confidence") or 0.0),
        error_message=str(data.get("error_message") or ""),
        data={
            **transcription_data,
            "audio_chunk": transcription_data.get("audio_chunk") or audio_chunk.to_dict(),
            "source": "voice_pipeline",
            "speech_engine_access": "disabled",
            "fallback_execution": execution.to_dict(),
            "selected_adapter_name": execution.selected_adapter_name,
        },
        metadata={**_metadata(), **metadata},
    )


def _activated_city(result: VoiceCommandRoutingResult) -> str:
    route_result = dict(result.data.get("route_result", {}) or {})
    return str(route_result.get("service") or "")


def _city_lifecycle(result: VoiceCommandRoutingResult) -> Dict[str, Any]:
    route_result = dict(result.data.get("route_result", {}) or {})
    return dict(route_result.get("city_lifecycle", {}) or {})


def _city_statuses(result: VoiceCommandRoutingResult) -> Dict[str, Any]:
    route_result = dict(result.data.get("route_result", {}) or {})
    return dict(route_result.get("city_statuses", {}) or {})


def _final_response_text(result: VoiceCommandRoutingResult) -> str:
    return str(result.response_text or result.text or "Voice command completed.").strip()


def _voice_result_to_dict(result: VoiceServiceResult) -> Dict[str, Any]:
    return {
        "success": result.success,
        "text": result.text,
        "data": dict(result.data),
        "error_message": result.error_message,
        "metadata": dict(result.metadata),
    }


def _metadata() -> Dict[str, Any]:
    return {
        "safe": True,
        "source": "voice_pipeline",
        "audio_hardware_access": "disabled",
        "speaker_access": "disabled",
        "speech_engine_access": "disabled",
        "background_listening": "disabled",
        "internet": "disabled",
        "gpt": "disabled",
    }


def _voice_pipeline_request(
    request: Optional[Any],
    session_id: Optional[str],
    correlation_id: Optional[str],
    timeout_seconds: Optional[float],
    contract_version: str,
) -> Any:
    if request is not None:
        return request
    try:
        return VoicePipelineRequestV1(
            session_id=str(session_id or ""),
            correlation_id=str(correlation_id or ""),
            timeout_seconds=timeout_seconds,
            contract_version=contract_version,
            metadata={"source": "voice_pipeline"},
        )
    except ValueError:
        return {
            "contract_name": CONTRACT_VOICE_PIPELINE_REQUEST,
            "contract_version": str(contract_version or ""),
            "correlation_id": str(correlation_id or ""),
            "session_id": str(session_id or ""),
            "created_at": utc_contract_timestamp(),
            "metadata": {"source": "voice_pipeline"},
            "timeout_seconds": timeout_seconds,
        }


def _contract_payload(contract: Any) -> Dict[str, Any]:
    if isinstance(contract, dict):
        return dict(contract)
    to_dict = getattr(contract, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, dict):
            return dict(payload)
    return {}
