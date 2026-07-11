from core import (
    PC_SERVICE_NAME,
    VOICE_PIPELINE_AUDIO_CAPTURED_EVENT,
    VOICE_PIPELINE_CITY_ACTIVATED_EVENT,
    VOICE_PIPELINE_COMMAND_REJECTED_EVENT,
    VOICE_PIPELINE_COMMAND_ROUTED_EVENT,
    VOICE_PIPELINE_EXECUTION_COMPLETED_EVENT,
    VOICE_PIPELINE_EXECUTION_FAILED_EVENT,
    VOICE_PIPELINE_OUTPUT_PRODUCED_EVENT,
    VOICE_PIPELINE_TRANSCRIPTION_ACCEPTED_EVENT,
    VOICE_PIPELINE_TRANSCRIPTION_REJECTED_EVENT,
    VOICE_SERVICE_NAME,
    CoreService,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockVoiceOutputAdapter,
    VoicePipeline,
)
from skills.base import SkillResponse


def _pipeline(
    transcripts=None,
    chunks=None,
    confidence=0.95,
    command_handler=None,
    core_service=None,
    microphone_adapter=None,
    stt_adapter=None,
    output_adapter=None,
):
    microphone = microphone_adapter or MockMicrophoneAdapter(
        chunks=chunks if chunks is not None else [b"\x01\x02"]
    )
    stt = stt_adapter or MockSpeechToTextAdapter(
        transcripts=transcripts if transcripts is not None else ["hello"],
        confidence=confidence,
    )
    output = output_adapter or MockVoiceOutputAdapter()
    pipeline = VoicePipeline(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
        output_adapter=output,
        command_handler=command_handler,
        core_service=core_service,
    )
    return pipeline, microphone, stt, output


def _event_types(result):
    return [event["type"] for event in result.events]


def _event_payloads(result, event_type):
    return [event["payload"] for event in result.events if event["type"] == event_type]


def test_voice_pipeline_runs_successful_complete_simulated_command():
    handled = []

    def handle(text):
        handled.append(text)
        return SkillResponse(text="Hello from the text path.", skill="test")

    pipeline, microphone, stt, output = _pipeline(
        transcripts=["hello ARES"],
        command_handler=handle,
    )

    result = pipeline.run_once(session_id="session-1", correlation_id="corr-1")

    assert result.success is True
    assert result.status == "completed"
    assert result.response_text == "Hello from the text path."
    assert result.session_id == "session-1"
    assert result.correlation_id == "corr-1"
    assert handled == ["hello ARES"]
    assert microphone.start_count == 1
    assert microphone.read_count == 1
    assert microphone.stop_count == 1
    assert stt.transcription_count == 1
    assert output.spoken_texts == ["Hello from the text path."]
    assert result.data["activated_city"] == VOICE_SERVICE_NAME
    assert _event_types(result) == [
        VOICE_PIPELINE_AUDIO_CAPTURED_EVENT,
        VOICE_PIPELINE_TRANSCRIPTION_ACCEPTED_EVENT,
        VOICE_PIPELINE_COMMAND_ROUTED_EVENT,
        VOICE_PIPELINE_CITY_ACTIVATED_EVENT,
        VOICE_PIPELINE_EXECUTION_COMPLETED_EVENT,
        VOICE_PIPELINE_OUTPUT_PRODUCED_EVENT,
    ]


def test_voice_pipeline_empty_audio_is_ignored_safely():
    handled = []
    pipeline, _, _, output = _pipeline(
        chunks=[b""],
        transcripts=["unused"],
        command_handler=lambda text: handled.append(text),
    )

    result = pipeline.run_once(session_id="session-empty", correlation_id="corr-empty")

    assert result.success is True
    assert result.status == "empty_transcription_ignored"
    assert result.data["audio"]["byte_count"] == 0
    assert result.data["transcription"]["status"] == "empty_audio"
    assert handled == []
    assert output.spoken_texts == ["Voice command ignored because transcription was empty."]
    assert VOICE_PIPELINE_TRANSCRIPTION_REJECTED_EVENT in _event_types(result)
    assert VOICE_PIPELINE_COMMAND_REJECTED_EVENT in _event_types(result)


def test_voice_pipeline_microphone_failure_fails_safely():
    microphone = MockMicrophoneAdapter(fail_read=True, failure_message="read boom")
    pipeline, _, _, output = _pipeline(microphone_adapter=microphone)

    result = pipeline.run_once(session_id="session-mic", correlation_id="corr-mic")

    assert result.success is False
    assert result.status == "microphone_failed"
    assert result.error_message == "read boom"
    assert output.spoken_texts == [
        "Mock microphone read failed safely. No hardware was accessed."
    ]
    assert VOICE_PIPELINE_AUDIO_CAPTURED_EVENT not in _event_types(result)
    failure_events = _event_payloads(result, VOICE_PIPELINE_EXECUTION_FAILED_EVENT)
    assert failure_events[0]["stage"] == "microphone_read"
    assert failure_events[0]["correlation_id"] == "corr-mic"


def test_voice_pipeline_stt_failure_fails_safely():
    stt = MockSpeechToTextAdapter(fail=True, failure_message="stt boom")
    pipeline, _, _, output = _pipeline(
        stt_adapter=stt,
        command_handler=lambda text: f"unreachable {text}",
    )

    result = pipeline.run_once(session_id="session-stt", correlation_id="corr-stt")

    assert result.success is False
    assert result.status == "transcription_failed"
    assert result.error_message == "stt boom"
    assert output.spoken_texts == ["Voice command rejected because transcription failed."]
    assert result.data["transcription"]["success"] is False
    assert VOICE_PIPELINE_TRANSCRIPTION_REJECTED_EVENT in _event_types(result)
    assert VOICE_PIPELINE_EXECUTION_FAILED_EVENT in _event_types(result)


def test_voice_pipeline_empty_transcription_is_ignored_safely():
    handled = []
    pipeline, _, _, output = _pipeline(
        transcripts=[""],
        command_handler=lambda text: handled.append(text),
    )

    result = pipeline.run_once(session_id="session-no-text", correlation_id="corr-no-text")

    assert result.success is True
    assert result.status == "empty_transcription_ignored"
    assert result.data["transcription"]["status"] == "no_transcription"
    assert handled == []
    assert output.spoken_texts == ["Voice command ignored because transcription was empty."]
    assert VOICE_PIPELINE_TRANSCRIPTION_REJECTED_EVENT in _event_types(result)


def test_voice_pipeline_low_confidence_transcription_is_rejected():
    handled = []
    pipeline, _, _, output = _pipeline(
        transcripts=["hello"],
        confidence=0.2,
        command_handler=lambda text: handled.append(text),
    )

    result = pipeline.run_once(session_id="session-low", correlation_id="corr-low")

    assert result.success is False
    assert result.status == "low_confidence_rejected"
    assert result.error_message == "low_confidence"
    assert handled == []
    assert output.spoken_texts == [
        "Voice command rejected because transcription confidence is too low."
    ]
    assert VOICE_PIPELINE_COMMAND_REJECTED_EVENT in _event_types(result)
    assert not _event_payloads(result, VOICE_PIPELINE_CITY_ACTIVATED_EVENT)


def test_voice_pipeline_unknown_command_fails_safely_after_voice_city_route():
    pipeline, _, _, output = _pipeline(command_handler=None)

    result = pipeline.run_once(session_id="session-unknown", correlation_id="corr-unknown")

    assert result.success is False
    assert result.status == "unknown_command"
    assert result.error_message == "unknown_command"
    assert result.data["activated_city"] == VOICE_SERVICE_NAME
    assert output.spoken_texts == ["Voice command was not recognized."]
    assert VOICE_PIPELINE_CITY_ACTIVATED_EVENT in _event_types(result)


def test_voice_pipeline_core_service_routing_failure_fails_safely():
    core_service = CoreService(register_default_pc=False, register_default_voice=False)
    pipeline, _, _, output = _pipeline(
        transcripts=["hello"],
        command_handler=lambda text: f"handled {text}",
        core_service=core_service,
    )

    result = pipeline.run_once(session_id="session-route", correlation_id="corr-route")

    assert result.success is False
    assert result.status == "route_failed"
    assert result.error_message == "capability_not_available"
    assert result.data["activated_city"] == ""
    assert output.spoken_texts == ["Voice command route failed safely."]
    assert VOICE_PIPELINE_CITY_ACTIVATED_EVENT not in _event_types(result)


def test_voice_pipeline_target_city_failure_is_reported_clearly():
    def fail_handler(text):
        raise RuntimeError(f"boom: {text}")

    pipeline, _, _, output = _pipeline(
        transcripts=["hello"],
        command_handler=fail_handler,
    )

    result = pipeline.run_once(session_id="session-target", correlation_id="corr-target")

    assert result.success is False
    assert result.status == "handler_failed"
    assert "RuntimeError: boom: hello" in result.error_message
    assert result.data["activated_city"] == VOICE_SERVICE_NAME
    assert output.spoken_texts == ["Voice command handler failed safely."]
    assert VOICE_PIPELINE_CITY_ACTIVATED_EVENT in _event_types(result)
    assert VOICE_PIPELINE_EXECUTION_FAILED_EVENT in _event_types(result)


def test_voice_pipeline_output_adapter_failure_fails_safely():
    output = MockVoiceOutputAdapter(fail=True, failure_message="output boom")
    pipeline, _, _, _ = _pipeline(
        transcripts=["hello"],
        output_adapter=output,
        command_handler=lambda text: "Hello.",
    )

    result = pipeline.run_once(session_id="session-output", correlation_id="corr-output")

    assert result.success is False
    assert result.status == "output_failed"
    assert result.response_text == "Hello."
    assert result.error_message == "output boom"
    assert output.spoken_texts == ["Hello."]
    output_failure_events = [
        payload
        for payload in _event_payloads(result, VOICE_PIPELINE_EXECUTION_FAILED_EVENT)
        if payload["stage"] == "output"
    ]
    assert output_failure_events[0]["correlation_id"] == "corr-output"


def test_voice_pipeline_session_remains_usable_after_failed_command():
    microphone = MockMicrophoneAdapter(chunks=[b"\x01", b"\x02"])
    stt = MockSpeechToTextAdapter(transcripts=["unknown", "hello"])
    output = MockVoiceOutputAdapter()

    def handle(text):
        if text == "hello":
            return "Recovered response."
        return None

    pipeline, _, _, _ = _pipeline(
        microphone_adapter=microphone,
        stt_adapter=stt,
        output_adapter=output,
        command_handler=handle,
    )

    first = pipeline.run_once(session_id="session-reuse", correlation_id="corr-first")
    second = pipeline.run_once(session_id="session-reuse", correlation_id="corr-second")

    assert first.success is False
    assert first.status == "unknown_command"
    assert second.success is True
    assert second.status == "completed"
    assert second.response_text == "Recovered response."
    assert first.session_id == second.session_id == "session-reuse"
    assert output.spoken_texts == [
        "Voice command was not recognized.",
        "Recovered response.",
    ]
    assert microphone.start_count == 2
    assert microphone.read_count == 2
    assert microphone.stop_count == 2


def test_voice_pipeline_only_requested_city_is_activated():
    core_service = CoreService()
    pipeline, _, _, _ = _pipeline(
        transcripts=["hello"],
        command_handler=lambda text: "Hello.",
        core_service=core_service,
    )

    result = pipeline.run_once(session_id="session-city", correlation_id="corr-city")

    assert result.success is True
    assert result.data["activated_city"] == VOICE_SERVICE_NAME
    assert result.data["city_statuses"][VOICE_SERVICE_NAME] == "idle"
    assert result.data["city_statuses"][PC_SERVICE_NAME] == "idle"
    city_events = _event_payloads(result, VOICE_PIPELINE_CITY_ACTIVATED_EVENT)
    assert [event["city"] for event in city_events] == [VOICE_SERVICE_NAME]
    assert core_service.get_service_status(PC_SERVICE_NAME) == "idle"


def test_voice_pipeline_preserves_correlation_id_across_every_event():
    pipeline, _, _, _ = _pipeline(command_handler=lambda text: "Hello.")

    result = pipeline.run_once(session_id="session-corr", correlation_id="corr-stable")

    assert result.correlation_id == "corr-stable"
    assert result.data["correlation_id"] == "corr-stable"
    assert result.data["session_id"] == "session-corr"
    assert result.events
    for event in result.events:
        assert event["payload"]["correlation_id"] == "corr-stable"
        assert event["payload"]["session_id"] == "session-corr"


def test_voice_pipeline_does_not_start_unrelated_city():
    core_service = CoreService()
    pipeline, _, _, _ = _pipeline(
        transcripts=["hello"],
        command_handler=lambda text: "Hello.",
        core_service=core_service,
    )

    result = pipeline.run_once(session_id="session-idle", correlation_id="corr-idle")

    assert result.success is True
    assert result.data["city_statuses"][PC_SERVICE_NAME] == "idle"
    assert all(
        event["payload"].get("city") != PC_SERVICE_NAME
        for event in result.events
        if event["type"] == VOICE_PIPELINE_CITY_ACTIVATED_EVENT
    )
