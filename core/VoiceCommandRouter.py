from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.CoreService import CoreService, CoreServiceResult
from core.EventBus import Event, EventBus, PRIORITY_NORMAL
from core.SpeechToText import TranscriptionResult


VOICE_COMMAND_ROUTED_EVENT = "voice_command.routed"
VOICE_COMMAND_REJECTED_EVENT = "voice_command.rejected"
DEFAULT_VOICE_COMMAND_CONFIDENCE_THRESHOLD = 0.5
DEFAULT_VOICE_ROUTE_CAPABILITY = "voice.text_loop"

CommandHandler = Callable[[str], Any]


@dataclass(frozen=True)
class VoiceCommandRoutingResult:
    success: bool
    status: str
    text: str = ""
    input_text: str = ""
    response_text: str = ""
    confidence: float = 0.0
    route: str = DEFAULT_VOICE_ROUTE_CAPABILITY
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "text": self.text,
            "input_text": self.input_text,
            "response_text": self.response_text,
            "confidence": self.confidence,
            "route": self.route,
            "error_message": self.error_message,
            "data": dict(self.data),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class VoiceCommandRouterMetrics:
    total: int = 0
    routed: int = 0
    rejected: int = 0
    unknown: int = 0
    failed: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "total": self.total,
            "routed": self.routed,
            "rejected": self.rejected,
            "unknown": self.unknown,
            "failed": self.failed,
        }


class VoiceCommandRouter:
    """Routes transcribed text to Voice City without speech engine dependencies."""

    def __init__(
        self,
        command_handler: Optional[CommandHandler] = None,
        core_service: Optional[CoreService] = None,
        event_bus: Optional[EventBus] = None,
        confidence_threshold: float = DEFAULT_VOICE_COMMAND_CONFIDENCE_THRESHOLD,
        route_capability: str = DEFAULT_VOICE_ROUTE_CAPABILITY,
    ):
        self.command_handler = command_handler
        self.core_service = core_service or CoreService(register_default_pc=False)
        self.event_bus = event_bus
        self.confidence_threshold = _clamp_confidence(confidence_threshold)
        self.route_capability = str(route_capability or DEFAULT_VOICE_ROUTE_CAPABILITY)
        self._metrics = VoiceCommandRouterMetrics()
        self._events: List[Event] = []

    @property
    def metrics(self) -> Dict[str, int]:
        return self._metrics.to_dict()

    def events(self, limit: Optional[int] = None) -> List[Event]:
        if limit is None:
            return list(self._events)
        return self._events[-max(0, int(limit)) :]

    def route(
        self,
        transcription: TranscriptionResult,
        session_id: str = "",
        correlation_id: str = "",
    ) -> VoiceCommandRoutingResult:
        self._increment(total=1)

        if not isinstance(transcription, TranscriptionResult):
            return self._rejected(
                status="invalid_transcription_result",
                text="Voice command rejected because transcription data is invalid.",
                confidence=0.0,
                input_text="",
                error_message="invalid_transcription_result",
                data={"transcription": repr(transcription)},
                failed=True,
            )

        transcription_data = transcription.to_dict()
        if not transcription.success:
            return self._rejected(
                status="transcription_failed",
                text="Voice command rejected because transcription failed.",
                confidence=transcription.confidence,
                input_text=transcription.text,
                error_message=transcription.error_message or "transcription_failed",
                data={"transcription": transcription_data},
                failed=True,
            )

        command_text = transcription.text.strip()
        if not command_text:
            return self._rejected(
                status="empty_transcription_ignored",
                text="Voice command ignored because transcription was empty.",
                confidence=transcription.confidence,
                input_text="",
                data={"transcription": transcription_data},
            )

        if transcription.confidence < self.confidence_threshold:
            return self._rejected(
                status="low_confidence_rejected",
                text="Voice command rejected because transcription confidence is too low.",
                confidence=transcription.confidence,
                input_text=command_text,
                error_message="low_confidence",
                data={
                    "transcription": transcription_data,
                    "confidence_threshold": self.confidence_threshold,
                },
            )

        route_result = self.core_service.route_by_capability(
            self.route_capability,
            lambda voice_service: self._handle_command(command_text, voice_service),
            session_id=session_id,
            correlation_id=correlation_id,
            request_payload={
                "text_length": len(command_text),
                "transcription_status": transcription.status,
            },
        )
        if not route_result.success:
            return self._rejected(
                status="route_failed",
                text="Voice command route failed safely.",
                confidence=transcription.confidence,
                input_text=command_text,
                error_message=route_result.error_message or "route_failed",
                data={
                    "transcription": transcription_data,
                    "route_result": route_result.data,
                },
                failed=True,
            )

        command_result = dict(route_result.data.get("response", {}) or {})
        if not command_result.get("success"):
            status = str(command_result.get("status") or "unknown_command")
            return self._rejected(
                status=status,
                text=str(command_result.get("text") or "Voice command was not handled."),
                confidence=transcription.confidence,
                input_text=command_text,
                error_message=str(command_result.get("error_message") or status),
                data={
                    "transcription": transcription_data,
                    "route_result": route_result.data,
                    "command_result": command_result,
                },
                unknown=status == "unknown_command",
            )

        result = VoiceCommandRoutingResult(
            success=True,
            status="routed",
            text=str(command_result.get("text") or "Voice command routed."),
            input_text=command_text,
            response_text=str(command_result.get("response_text") or ""),
            confidence=transcription.confidence,
            route=self.route_capability,
            data={
                "transcription": transcription_data,
                "route_result": route_result.data,
                "command_result": command_result,
            },
            metadata={
                "safe": True,
                "source": "voice_command_router",
                "confidence_threshold": self.confidence_threshold,
            },
        )
        self._increment(routed=1)
        self._emit_event(VOICE_COMMAND_ROUTED_EVENT, result)
        return result

    def _handle_command(self, command_text: str, voice_service: Any) -> CoreServiceResult:
        if self.command_handler is None:
            return _core_command_result(
                success=False,
                status="unknown_command",
                text="Voice command was not recognized.",
                response_text="",
                error_message="unknown_command",
                handler_response={},
                voice_service=voice_service,
            )

        try:
            handler_response = self.command_handler(command_text)
        except Exception as error:
            return _core_command_result(
                success=False,
                status="handler_failed",
                text="Voice command handler failed safely.",
                response_text="",
                error_message=f"{type(error).__name__}: {error}",
                handler_response={},
                voice_service=voice_service,
            )

        response_text = _extract_response_text(handler_response)
        if not response_text:
            return _core_command_result(
                success=False,
                status="unknown_command",
                text="Voice command was not recognized.",
                response_text="",
                error_message="unknown_command",
                handler_response=_handler_response_to_data(handler_response),
                voice_service=voice_service,
            )

        return _core_command_result(
            success=True,
            status="handled",
            text="Voice command routed to Voice City.",
            response_text=response_text,
            error_message="",
            handler_response=_handler_response_to_data(handler_response),
            voice_service=voice_service,
        )

    def _rejected(
        self,
        status: str,
        text: str,
        confidence: float,
        input_text: str,
        error_message: str = "",
        data: Optional[Dict[str, Any]] = None,
        failed: bool = False,
        unknown: bool = False,
    ) -> VoiceCommandRoutingResult:
        result = VoiceCommandRoutingResult(
            success=False if (failed or error_message or unknown) else True,
            status=status,
            text=text,
            input_text=input_text,
            confidence=confidence,
            route=self.route_capability,
            error_message=error_message,
            data=dict(data or {}),
            metadata={
                "safe": True,
                "source": "voice_command_router",
                "confidence_threshold": self.confidence_threshold,
            },
        )
        self._increment(
            rejected=1,
            failed=1 if failed else 0,
            unknown=1 if unknown else 0,
        )
        self._emit_event(VOICE_COMMAND_REJECTED_EVENT, result)
        return result

    def _emit_event(self, event_type: str, result: VoiceCommandRoutingResult) -> Event:
        payload = {
            "status": result.status,
            "success": result.success,
            "confidence": result.confidence,
            "route": result.route,
            "text_length": len(result.input_text),
            "error_message": result.error_message,
        }
        if self.event_bus is not None:
            event = self.event_bus.publish(
                source="voice",
                type=event_type,
                payload=payload,
                priority=PRIORITY_NORMAL,
            )
        else:
            event = Event(
                source="voice",
                type=event_type,
                payload=payload,
                priority=PRIORITY_NORMAL,
            )
        self._events.append(event)
        return event

    def _increment(
        self,
        total: int = 0,
        routed: int = 0,
        rejected: int = 0,
        unknown: int = 0,
        failed: int = 0,
    ) -> None:
        self._metrics = VoiceCommandRouterMetrics(
            total=self._metrics.total + total,
            routed=self._metrics.routed + routed,
            rejected=self._metrics.rejected + rejected,
            unknown=self._metrics.unknown + unknown,
            failed=self._metrics.failed + failed,
        )


def _core_command_result(
    success: bool,
    status: str,
    text: str,
    response_text: str,
    error_message: str,
    handler_response: Dict[str, Any],
    voice_service: Any,
) -> CoreServiceResult:
    return CoreServiceResult(
        success=success,
        text=text,
        error_message=error_message,
        data={
            "success": success,
            "status": status,
            "text": text,
            "response_text": response_text,
            "error_message": error_message,
            "handler_response": dict(handler_response),
            "voice_service": type(voice_service).__name__,
        },
        metadata={"safe": True, "source": "voice_command_router"},
    )


def _extract_response_text(response: Any) -> str:
    if isinstance(response, str):
        return response.strip()
    return str(getattr(response, "text", "") or "").strip()


def _handler_response_to_data(response: Any) -> Dict[str, Any]:
    if response is None:
        return {}
    if isinstance(response, str):
        return {"text": response}
    return {
        "text": getattr(response, "text", ""),
        "skill": getattr(response, "skill", ""),
        "metadata": dict(getattr(response, "metadata", {}) or {}),
    }


def _clamp_confidence(confidence: float) -> float:
    return max(0.0, min(1.0, float(confidence)))
