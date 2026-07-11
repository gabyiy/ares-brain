from core import (
    EventBus,
    TranscriptionResult,
    VOICE_COMMAND_REJECTED_EVENT,
    VOICE_COMMAND_ROUTED_EVENT,
    VoiceCommandRouter,
)
from skills.base import SkillResponse


def _transcription(text: str, confidence: float = 0.9) -> TranscriptionResult:
    return TranscriptionResult(
        success=True,
        status="transcribed",
        text=text,
        confidence=confidence,
        data={"source": "test_stt"},
    )


def test_voice_command_router_routes_valid_transcription_to_voice_city():
    handled_texts = []

    def handle(text):
        handled_texts.append(text)
        return SkillResponse(text="Handled by text path.", skill="test")

    router = VoiceCommandRouter(command_handler=handle, confidence_threshold=0.5)

    result = router.route(_transcription("what time is it", confidence=0.92))

    assert result.success is True
    assert result.status == "routed"
    assert result.input_text == "what time is it"
    assert result.response_text == "Handled by text path."
    assert result.confidence == 0.92
    assert handled_texts == ["what time is it"]
    assert result.data["route_result"]["service"] == "voice"
    assert result.data["command_result"]["voice_service"] == "PlaceholderVoiceService"
    assert router.metrics == {
        "total": 1,
        "routed": 1,
        "rejected": 0,
        "unknown": 0,
        "failed": 0,
    }
    assert router.events()[0].type == VOICE_COMMAND_ROUTED_EVENT


def test_voice_command_router_ignores_empty_transcription():
    handled_texts = []
    router = VoiceCommandRouter(command_handler=lambda text: handled_texts.append(text))

    result = router.route(_transcription("   ", confidence=1.0))

    assert result.success is True
    assert result.status == "empty_transcription_ignored"
    assert result.input_text == ""
    assert result.response_text == ""
    assert handled_texts == []
    assert router.metrics["rejected"] == 1
    assert router.events()[0].type == VOICE_COMMAND_REJECTED_EVENT
    assert router.events()[0].payload["text_length"] == 0


def test_voice_command_router_rejects_low_confidence_transcription():
    handled_texts = []
    router = VoiceCommandRouter(
        command_handler=lambda text: handled_texts.append(text),
        confidence_threshold=0.75,
    )

    result = router.route(_transcription("open notes", confidence=0.4))

    assert result.success is False
    assert result.status == "low_confidence_rejected"
    assert result.error_message == "low_confidence"
    assert result.confidence == 0.4
    assert handled_texts == []
    assert router.metrics["rejected"] == 1
    assert router.metrics["routed"] == 0
    assert router.events()[0].payload["status"] == "low_confidence_rejected"


def test_voice_command_router_handles_unknown_command_gracefully():
    router = VoiceCommandRouter(command_handler=None)

    result = router.route(_transcription("unmapped command", confidence=0.95))

    assert result.success is False
    assert result.status == "unknown_command"
    assert result.error_message == "unknown_command"
    assert result.data["route_result"]["service"] == "voice"
    assert result.data["command_result"]["success"] is False
    assert router.metrics["unknown"] == 1
    assert router.metrics["rejected"] == 1
    assert router.events()[0].type == VOICE_COMMAND_REJECTED_EVENT


def test_voice_command_router_propagates_transcription_adapter_failure():
    router = VoiceCommandRouter(command_handler=lambda text: "unreachable")
    transcription = TranscriptionResult(
        success=False,
        status="failed",
        text="",
        confidence=0.0,
        error_message="mock_stt_failure",
    )

    result = router.route(transcription)

    assert result.success is False
    assert result.status == "transcription_failed"
    assert result.error_message == "mock_stt_failure"
    assert result.data["transcription"]["status"] == "failed"
    assert router.metrics["failed"] == 1
    assert router.metrics["rejected"] == 1
    assert router.metrics["routed"] == 0
    assert router.events()[0].payload["error_message"] == "mock_stt_failure"


def test_voice_command_router_publishes_routed_and_rejected_events_to_event_bus():
    event_bus = EventBus()
    router = VoiceCommandRouter(
        command_handler=lambda text: f"handled {text}",
        event_bus=event_bus,
        confidence_threshold=0.5,
    )

    routed = router.route(_transcription("hello", confidence=0.9))
    rejected = router.route(_transcription("hello", confidence=0.1))

    routed_events = event_bus.history(VOICE_COMMAND_ROUTED_EVENT)
    rejected_events = event_bus.history(VOICE_COMMAND_REJECTED_EVENT)
    assert routed.success is True
    assert rejected.success is False
    assert len(routed_events) == 1
    assert len(rejected_events) == 1
    assert routed_events[0].payload["status"] == "routed"
    assert rejected_events[0].payload["status"] == "low_confidence_rejected"
    assert router.metrics == {
        "total": 2,
        "routed": 1,
        "rejected": 1,
        "unknown": 0,
        "failed": 0,
    }
