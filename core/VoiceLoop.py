import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.VoiceService import (
    MockVoiceInputAdapter,
    MockVoiceOutputAdapter,
    NullVoiceInput,
    NullVoiceOutput,
    PlaceholderVoiceService,
    VoiceInput,
    VoiceInputAdapter,
    VoiceOutput,
    VoiceOutputAdapter,
    VoiceService,
    VoiceServiceResult,
)


TextHandler = Callable[[str], Any]
DEFAULT_VOICE_SESSION_MAX_TURNS = 5
VOICE_SESSION_STOP_PHRASES = {"stop", "exit", "goodbye"}


@dataclass(frozen=True)
class VoiceLoopResult:
    success: bool
    status: str
    text: str = ""
    input_text: str = ""
    response_text: str = ""
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
            "error_message": self.error_message,
            "data": dict(self.data),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class VoiceTextRequest:
    text: str
    source: str
    data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "source": self.source,
            "data": dict(self.data),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class VoiceSessionTurn:
    turn_number: int
    success: bool
    status: str
    input_text: str = ""
    response_text: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_number": self.turn_number,
            "success": self.success,
            "status": self.status,
            "input_text": self.input_text,
            "response_text": self.response_text,
            "error_message": self.error_message,
            "data": dict(self.data),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class VoiceSessionResult:
    success: bool
    status: str
    turns: List[VoiceSessionTurn] = field(default_factory=list)
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "turns": [turn.to_dict() for turn in self.turns],
            "transcript": [dict(entry) for entry in self.transcript],
            "history": [dict(entry) for entry in self.history],
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "data": dict(self.data),
            "metadata": dict(self.metadata),
        }


class VoiceLoop:
    """One-shot Voice City text loop with no audio hardware access."""

    def __init__(
        self,
        voice_service: Optional[VoiceService] = None,
        text_handler: Optional[TextHandler] = None,
        voice_input: Optional[VoiceInput] = None,
        voice_output: Optional[VoiceOutput] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.voice_service = voice_service or PlaceholderVoiceService()
        self.voice_input = voice_input or getattr(self.voice_service, "voice_input", None) or NullVoiceInput()
        self.voice_output = voice_output or getattr(self.voice_service, "voice_output", None) or NullVoiceOutput()
        self.text_handler = text_handler
        self.logger = logger or logging.getLogger("ares.voice_loop")

    def run_once(self) -> VoiceLoopResult:
        captured = self.capture_text_request()
        if not captured.success or captured.status == "no_input":
            return captured

        if not self.text_handler:
            return VoiceLoopResult(
                success=False,
                status="missing_text_handler",
                text="Voice loop cannot process text because no text handler is configured.",
                input_text=captured.input_text,
                error_message="missing_text_handler",
                data=dict(captured.data),
                metadata={"safe": True, "source": "voice_loop"},
            )

        text_request = _text_request_from_data(captured.data)
        voice_input = dict(captured.data.get("voice_input", {}) or {})
        return self.process_text_request(text_request, voice_input)

    def capture_text_request(self) -> VoiceLoopResult:
        input_result = self._listen_once()
        if not input_result.success:
            return input_result

        voice_input = input_result.data["voice_input"]
        transcript = _extract_transcript(voice_input)
        if not transcript:
            return VoiceLoopResult(
                success=True,
                status="no_input",
                text="No voice input detected.",
                data={"voice_input": voice_input},
                metadata={
                    "safe": True,
                    "source": "voice_loop",
                    "audio_hardware_access": "disabled",
                    "background_loop": "disabled",
                },
            )

        text_request = _voice_input_to_text_request(transcript, voice_input)
        return VoiceLoopResult(
            success=True,
            status="text_request_created",
            text="Voice input was converted to a text request.",
            input_text=text_request.text,
            data={
                "voice_input": voice_input,
                "text_request": text_request.to_dict(),
            },
            metadata={
                "safe": True,
                "source": "voice_loop",
                "audio_hardware_access": "disabled",
                "background_loop": "disabled",
            },
        )

    def process_text_request(
        self,
        text_request: VoiceTextRequest,
        voice_input: Dict[str, Any],
    ) -> VoiceLoopResult:
        handled = self._handle_text(text_request, voice_input)
        if not handled.success:
            return handled

        return self._speak_response(handled)

    def _listen_once(self) -> VoiceLoopResult:
        try:
            voice_input = self.voice_input.listen_once()
        except Exception as error:
            return self._safe_error("input_error", error)

        voice_input_data = _voice_result_to_dict(voice_input)
        if not voice_input.success and voice_input.error_message != "voice_input_unavailable":
            return VoiceLoopResult(
                success=False,
                status="input_error",
                text=voice_input.text,
                error_message=voice_input.error_message or "voice_input_failed",
                data={"voice_input": voice_input_data},
                metadata={"safe": True, "source": "voice_loop"},
            )

        return VoiceLoopResult(
            success=True,
            status="input_received",
            text="Voice input was checked.",
            data={"voice_input": voice_input_data},
            metadata={"safe": True, "source": "voice_loop"},
        )

    def _handle_text(
        self,
        text_request: VoiceTextRequest,
        voice_input: Dict[str, Any],
    ) -> VoiceLoopResult:
        try:
            response = self.text_handler(text_request.text)
        except Exception as error:
            return self._safe_error(
                "handler_error",
                error,
                input_text=text_request.text,
                data={
                    "voice_input": voice_input,
                    "text_request": text_request.to_dict(),
                },
            )

        response_text = _extract_response_text(response)
        return VoiceLoopResult(
            success=True,
            status="text_handled",
            text=response_text,
            input_text=text_request.text,
            response_text=response_text,
            data={
                "voice_input": voice_input,
                "text_request": text_request.to_dict(),
                "handler_response": _handler_response_to_data(response),
            },
            metadata={"safe": True, "source": "voice_loop"},
        )

    def _speak_response(self, handled: VoiceLoopResult) -> VoiceLoopResult:
        try:
            output_result = self.voice_output.speak(handled.response_text)
        except Exception as error:
            return self._safe_error(
                "output_error",
                error,
                input_text=handled.input_text,
                response_text=handled.response_text,
                data=dict(handled.data),
            )

        output_data = _voice_result_to_dict(output_result)
        if not output_result.success:
            return VoiceLoopResult(
                success=False,
                status="output_error",
                text=output_result.text,
                input_text=handled.input_text,
                response_text=handled.response_text,
                error_message=output_result.error_message or "voice_output_failed",
                data={**dict(handled.data), "voice_output": output_data},
                metadata={"safe": True, "source": "voice_loop"},
            )

        return VoiceLoopResult(
            success=True,
            status="completed",
            text=handled.response_text,
            input_text=handled.input_text,
            response_text=handled.response_text,
            data={**dict(handled.data), "voice_output": output_data},
            metadata={
                "safe": True,
                "source": "voice_loop",
                "audio_hardware_access": "disabled",
                "background_loop": "disabled",
            },
        )

    def _safe_error(
        self,
        status: str,
        error: Exception,
        input_text: str = "",
        response_text: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> VoiceLoopResult:
        message = f"{type(error).__name__}: {error}"
        self.logger.error("Voice loop %s: %s", status, message)
        return VoiceLoopResult(
            success=False,
            status=status,
            text=f"Voice loop failed safely: {message}",
            input_text=input_text,
            response_text=response_text,
            error_message=message,
            data=dict(data or {}),
            metadata={"safe": True, "source": "voice_loop"},
        )


class VoiceSingleTurnLoop:
    """Adapter-backed one-turn Voice City loop with no audio hardware access."""

    def __init__(
        self,
        input_adapter: Optional[VoiceInputAdapter] = None,
        output_adapter: Optional[VoiceOutputAdapter] = None,
        text_handler: Optional[TextHandler] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.input_adapter = input_adapter or MockVoiceInputAdapter()
        self.output_adapter = output_adapter or MockVoiceOutputAdapter()
        self.voice_service = PlaceholderVoiceService(
            input_adapter=self.input_adapter,
            output_adapter=self.output_adapter,
        )
        self.voice_loop = VoiceLoop(
            voice_service=self.voice_service,
            text_handler=text_handler,
            logger=logger,
        )

    @property
    def audio_hardware_accessed(self) -> bool:
        return bool(
            getattr(self.input_adapter, "audio_hardware_accessed", False)
            or getattr(self.output_adapter, "audio_hardware_accessed", False)
        )

    def run_once(self) -> VoiceLoopResult:
        return self.voice_loop.run_once()


class VoiceSessionLoop:
    """Bounded multi-turn mock Voice City session with no audio hardware access."""

    def __init__(
        self,
        input_adapter: Optional[VoiceInputAdapter] = None,
        output_adapter: Optional[VoiceOutputAdapter] = None,
        text_handler: Optional[TextHandler] = None,
        max_turns: int = DEFAULT_VOICE_SESSION_MAX_TURNS,
        stop_phrases: Optional[set[str]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        self.input_adapter = input_adapter or MockVoiceInputAdapter()
        self.output_adapter = output_adapter or MockVoiceOutputAdapter()
        self.max_turns = max_turns
        self.stop_phrases = _normalize_stop_phrases(stop_phrases)
        self.voice_service = PlaceholderVoiceService(
            input_adapter=self.input_adapter,
            output_adapter=self.output_adapter,
        )
        self.voice_loop = VoiceLoop(
            voice_service=self.voice_service,
            text_handler=text_handler,
            logger=logger,
        )

    @property
    def audio_hardware_accessed(self) -> bool:
        return bool(
            getattr(self.input_adapter, "audio_hardware_accessed", False)
            or getattr(self.output_adapter, "audio_hardware_accessed", False)
        )

    def run(self) -> VoiceSessionResult:
        turns: List[VoiceSessionTurn] = []
        transcript: List[Dict[str, Any]] = []

        for turn_number in range(1, self.max_turns + 1):
            captured = self.voice_loop.capture_text_request()
            if not captured.success:
                turn = _turn_from_loop_result(turn_number, captured)
                turns.append(turn)
                transcript.append(_transcript_entry(turn))
                return _session_result(
                    success=False,
                    status="failed",
                    turns=turns,
                    transcript=transcript,
                    stop_reason="failure",
                    error_message=captured.error_message,
                    max_turns=self.max_turns,
                    audio_hardware_accessed=self.audio_hardware_accessed,
                )

            if captured.status == "no_input":
                turn = _turn_from_loop_result(turn_number, captured)
                turns.append(turn)
                transcript.append(_transcript_entry(turn))
                continue

            text_request = _text_request_from_data(captured.data)
            voice_input = dict(captured.data.get("voice_input", {}) or {})
            if _is_stop_phrase(text_request.text, self.stop_phrases):
                turn = VoiceSessionTurn(
                    turn_number=turn_number,
                    success=True,
                    status="stopped",
                    input_text=text_request.text,
                    response_text="",
                    data={
                        "voice_input": voice_input,
                        "text_request": text_request.to_dict(),
                    },
                    metadata={
                        "safe": True,
                        "source": "voice_session_loop",
                        "stop_phrase": text_request.text,
                    },
                )
                turns.append(turn)
                transcript.append(_transcript_entry(turn))
                return _session_result(
                    success=True,
                    status="stopped",
                    turns=turns,
                    transcript=transcript,
                    stop_reason="stop_phrase",
                    max_turns=self.max_turns,
                    audio_hardware_accessed=self.audio_hardware_accessed,
                )

            processed = self.voice_loop.process_text_request(text_request, voice_input)
            turn = _turn_from_loop_result(turn_number, processed)
            turns.append(turn)
            transcript.append(_transcript_entry(turn))
            if not processed.success:
                return _session_result(
                    success=False,
                    status="failed",
                    turns=turns,
                    transcript=transcript,
                    stop_reason="failure",
                    error_message=processed.error_message,
                    max_turns=self.max_turns,
                    audio_hardware_accessed=self.audio_hardware_accessed,
                )

        return _session_result(
            success=True,
            status="max_turns_reached",
            turns=turns,
            transcript=transcript,
            stop_reason="max_turns",
            max_turns=self.max_turns,
            audio_hardware_accessed=self.audio_hardware_accessed,
        )


def _extract_transcript(result: Dict[str, Any]) -> str:
    data = dict(result.get("data", {}) or {})
    return str(data.get("transcript") or data.get("recognized_text") or "").strip()


def _voice_input_to_text_request(
    transcript: str,
    voice_input: Dict[str, Any],
) -> VoiceTextRequest:
    data = dict(voice_input.get("data", {}) or {})
    metadata = dict(voice_input.get("metadata", {}) or {})
    source = str(data.get("source") or metadata.get("source") or "voice_input")
    return VoiceTextRequest(
        text=transcript,
        source=source,
        data=data,
        metadata=metadata,
    )


def _text_request_from_data(data: Dict[str, Any]) -> VoiceTextRequest:
    text_request_data = dict(data.get("text_request", {}) or {})
    return VoiceTextRequest(
        text=str(text_request_data.get("text") or ""),
        source=str(text_request_data.get("source") or "voice_input"),
        data=dict(text_request_data.get("data", {}) or {}),
        metadata=dict(text_request_data.get("metadata", {}) or {}),
    )


def _normalize_stop_phrases(stop_phrases: Optional[set[str]]) -> set[str]:
    phrases = stop_phrases if stop_phrases is not None else VOICE_SESSION_STOP_PHRASES
    return {str(phrase or "").strip().lower() for phrase in phrases if str(phrase or "").strip()}


def _is_stop_phrase(text: str, stop_phrases: set[str]) -> bool:
    return str(text or "").strip().lower() in stop_phrases


def _turn_from_loop_result(turn_number: int, result: VoiceLoopResult) -> VoiceSessionTurn:
    return VoiceSessionTurn(
        turn_number=turn_number,
        success=result.success,
        status=result.status,
        input_text=result.input_text,
        response_text=result.response_text,
        error_message=result.error_message,
        data=dict(result.data),
        metadata=dict(result.metadata),
    )


def _transcript_entry(turn: VoiceSessionTurn) -> Dict[str, Any]:
    return {
        "turn_number": turn.turn_number,
        "status": turn.status,
        "user": turn.input_text,
        "assistant": turn.response_text,
        "success": turn.success,
        "error_message": turn.error_message,
    }


def _session_result(
    success: bool,
    status: str,
    turns: List[VoiceSessionTurn],
    transcript: List[Dict[str, Any]],
    stop_reason: str,
    max_turns: int,
    audio_hardware_accessed: bool,
    error_message: str = "",
) -> VoiceSessionResult:
    history = [dict(entry) for entry in transcript]
    return VoiceSessionResult(
        success=success,
        status=status,
        turns=list(turns),
        transcript=[dict(entry) for entry in transcript],
        history=history,
        stop_reason=stop_reason,
        error_message=error_message,
        data={
            "turn_count": len(turns),
            "max_turns": max_turns,
            "transcript": [dict(entry) for entry in transcript],
            "history": history,
        },
        metadata={
            "safe": True,
            "source": "voice_session_loop",
            "audio_hardware_access": "disabled",
            "background_loop": "disabled",
            "audio_hardware_accessed": audio_hardware_accessed,
        },
    )


def _extract_response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    return str(getattr(response, "text", "") or "")


def _handler_response_to_data(response: Any) -> Dict[str, Any]:
    if isinstance(response, str):
        return {"text": response}
    return {
        "text": getattr(response, "text", ""),
        "skill": getattr(response, "skill", ""),
        "metadata": dict(getattr(response, "metadata", {}) or {}),
    }


def _voice_result_to_dict(result: VoiceServiceResult) -> Dict[str, Any]:
    return {
        "success": result.success,
        "text": result.text,
        "data": dict(result.data),
        "error_message": result.error_message,
        "metadata": dict(result.metadata),
    }
