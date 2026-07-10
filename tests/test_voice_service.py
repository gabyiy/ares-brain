from core import (
    PC_SERVICE_NAME,
    VOICE_SERVICE_NAME,
    CoreService,
    MockVoiceInputAdapter,
    MockVoiceOutputAdapter,
    NullVoiceInput,
    NullVoiceOutput,
    PlaceholderVoiceService,
    VoiceLoop,
    VoiceOutput,
    VoiceSessionLoop,
    VoiceSingleTurnLoop,
    VoiceServiceResult,
)
from skills.base import SkillResponse


class StaticVoiceInput(NullVoiceInput):
    def __init__(self, transcript: str):
        super().__init__()
        self.transcript = transcript
        self.listen_count = 0

    def listen_once(self) -> VoiceServiceResult:
        self.listen_count += 1
        return VoiceServiceResult(
            success=True,
            text="Static test voice input.",
            data={
                "source": "static_voice_input",
                "transcript": self.transcript,
                "microphone": "disabled",
                "stt": "disabled",
                "audio_hardware_access": "disabled",
            },
            metadata={
                "safe": True,
                "audio_hardware_accessed": self.audio_hardware_accessed,
            },
        )


class RaisingVoiceInput(NullVoiceInput):
    def listen_once(self) -> VoiceServiceResult:
        raise RuntimeError("input failure")


class RecordingVoiceOutput(VoiceOutput):
    def __init__(self, fail=False):
        self.spoken_texts = []
        self.fail = fail
        self.audio_hardware_accessed = False

    def speak(self, text: str) -> VoiceServiceResult:
        self.spoken_texts.append(text)
        if self.fail:
            return VoiceServiceResult(
                success=False,
                text="Voice output failed safely.",
                error_message="output failure",
                data={"speaker": "disabled"},
                metadata={"safe": True, "audio_hardware_accessed": False},
            )
        return VoiceServiceResult(
            success=True,
            text="Recorded output without audio.",
            data={
                "accepted_text": text,
                "speaker": "disabled",
                "tts": "disabled",
                "audio_hardware_access": "disabled",
            },
            metadata={"safe": True, "audio_hardware_accessed": False},
        )

    def get_status(self) -> VoiceServiceResult:
        return VoiceServiceResult(success=True, data={"status": "recording_test"})

    def get_capabilities(self) -> VoiceServiceResult:
        return VoiceServiceResult(success=True, data={"voice_output": "recording_test"})


def test_voice_service_registers_through_core_service():
    core_service = CoreService()

    voice_service = core_service.get_service(VOICE_SERVICE_NAME)

    assert isinstance(voice_service, PlaceholderVoiceService)
    assert {service["name"] for service in core_service.list_services()} == {
        PC_SERVICE_NAME,
        VOICE_SERVICE_NAME,
    }


def test_voice_service_owns_input_and_output_components():
    voice_service = PlaceholderVoiceService()

    assert isinstance(voice_service.voice_input, NullVoiceInput)
    assert isinstance(voice_service.voice_output, NullVoiceOutput)
    assert isinstance(voice_service.voice_input.adapter, MockVoiceInputAdapter)
    assert isinstance(voice_service.voice_output.adapter, MockVoiceOutputAdapter)


def test_voice_service_can_use_injected_voice_adapters():
    input_adapter = MockVoiceInputAdapter(transcripts=["hello adapter"])
    output_adapter = MockVoiceOutputAdapter()
    voice_service = PlaceholderVoiceService(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
    )

    input_result = voice_service.voice_input.listen_once()
    output_result = voice_service.voice_output.speak("adapter response")

    assert voice_service.voice_input.adapter is input_adapter
    assert voice_service.voice_output.adapter is output_adapter
    assert input_result.success is True
    assert input_result.data["transcript"] == "hello adapter"
    assert input_result.data["microphone"] == "disabled"
    assert output_result.success is True
    assert output_result.data["accepted_text"] == "adapter response"
    assert output_result.data["speaker"] == "disabled"
    assert output_adapter.spoken_texts == ["adapter response"]


def test_null_voice_input_returns_safe_placeholder_result():
    voice_input = NullVoiceInput()

    result = voice_input.listen_once()

    assert result.success is False
    assert result.error_message == "voice_input_unavailable"
    assert result.text == "Voice input is a placeholder. No microphone was accessed."
    assert result.data["transcript"] == ""
    assert result.data["voice_input"] == "placeholder"
    assert result.data["microphone"] == "disabled"
    assert result.data["stt"] == "disabled"
    assert result.data["background_listening"] == "disabled"
    assert result.data["audio_hardware_access"] == "disabled"
    assert result.metadata["audio_hardware_accessed"] is False
    assert voice_input.audio_hardware_accessed is False


def test_mock_voice_input_adapter_captures_text_without_audio():
    adapter = MockVoiceInputAdapter(transcripts=["calculate 2 + 2"])

    result = adapter.capture()

    assert result.success is True
    assert result.text == "Mock voice input captured text."
    assert result.data["transcript"] == "calculate 2 + 2"
    assert result.data["voice_input"] == "mock"
    assert result.data["microphone"] == "disabled"
    assert result.data["stt"] == "mock"
    assert result.data["audio_hardware_access"] == "disabled"
    assert result.metadata["audio_hardware_accessed"] is False
    assert adapter.capture_count == 1
    assert adapter.audio_hardware_accessed is False


def test_mock_voice_input_adapter_capture_input_compatibility():
    adapter = MockVoiceInputAdapter(transcripts=["hello compatibility"])

    result = adapter.capture_input()

    assert result.success is True
    assert result.data["transcript"] == "hello compatibility"
    assert result.data["microphone"] == "disabled"
    assert adapter.capture_count == 1


def test_mock_voice_input_adapter_empty_input_is_safe():
    adapter = MockVoiceInputAdapter()

    result = adapter.capture_input()

    assert result.success is True
    assert result.text == "Mock voice input captured no text."
    assert result.data["transcript"] == ""
    assert result.data["microphone"] == "disabled"
    assert adapter.capture_count == 1
    assert adapter.audio_hardware_accessed is False


def test_null_voice_output_accepts_text_without_audio():
    voice_output = NullVoiceOutput()

    result = voice_output.speak("hello ARES")

    assert result.success is True
    assert result.text == "Voice output accepted as placeholder. No speaker audio was played."
    assert result.data["accepted_text"] == "hello ARES"
    assert result.data["voice_output"] == "placeholder"
    assert result.data["speaker"] == "disabled"
    assert result.data["tts"] == "disabled"
    assert result.data["audio_hardware_access"] == "disabled"
    assert result.metadata["audio_hardware_accessed"] is False
    assert voice_output.audio_hardware_accessed is False


def test_mock_voice_output_adapter_records_text_without_audio():
    adapter = MockVoiceOutputAdapter()

    result = adapter.speak("hello through adapter")

    assert result.success is True
    assert result.text == "Mock voice output accepted text. No speaker audio was played."
    assert result.data["accepted_text"] == "hello through adapter"
    assert result.data["voice_output"] == "mock"
    assert result.data["speaker"] == "disabled"
    assert result.data["tts"] == "mock"
    assert result.data["audio_hardware_access"] == "disabled"
    assert result.metadata["audio_hardware_accessed"] is False
    assert adapter.spoken_texts == ["hello through adapter"]
    assert adapter.audio_hardware_accessed is False


def test_voice_service_exposes_capabilities():
    voice_service = PlaceholderVoiceService()

    result = voice_service.get_capabilities()

    assert result.success is True
    assert result.text == "Voice City capabilities discovered."
    assert result.data["source"] == "voice_service"
    assert result.data["voice_input"] == "placeholder"
    assert result.data["voice_output"] == "placeholder"
    assert result.data["supported_voice_actions"] == []
    assert result.data["supported_input_modes"] == []
    assert result.data["supported_output_modes"] == []
    assert result.data["input_capabilities"]["voice_input"] == "placeholder"
    assert result.data["input_capabilities"]["microphone"] == "disabled"
    assert result.data["output_capabilities"]["voice_output"] == "placeholder"
    assert result.data["output_capabilities"]["speaker"] == "disabled"
    assert result.data["available_status_providers"] == ["voice_status"]
    assert result.data["available_services"] == [
        "voice_service",
        "placeholder_voice_service",
    ]
    assert result.data["safeguards"]["audio_hardware_access"] == "disabled"
    assert result.data["safeguards"]["microphone"] == "disabled"
    assert result.data["safeguards"]["speaker"] == "disabled"
    assert result.data["safeguards"]["stt"] == "disabled"
    assert result.data["safeguards"]["tts"] == "disabled"
    assert result.data["safeguards"]["wake_word"] == "disabled"
    assert result.data["safeguards"]["background_listening"] == "disabled"
    assert result.data["safeguards"]["internet"] == "disabled"
    assert result.data["safeguards"]["gpt"] == "disabled"
    assert result.metadata["audio_hardware_accessed"] is False


def test_voice_service_status_is_safe_placeholder():
    voice_service = PlaceholderVoiceService()

    result = voice_service.get_status()

    assert result.success is True
    assert result.text == "Voice City status: placeholder only. Audio hardware access is disabled."
    assert result.data["source"] == "voice_service"
    assert result.data["status"] == "placeholder"
    assert result.data["voice_input"]["voice_input"] == "placeholder"
    assert result.data["voice_input"]["microphone"] == "disabled"
    assert result.data["voice_output"]["voice_output"] == "placeholder"
    assert result.data["voice_output"]["speaker"] == "disabled"
    assert result.data["checks"]["audio_hardware_access"] == "disabled"
    assert result.data["checks"]["microphone"] == "disabled"
    assert result.data["checks"]["speaker"] == "disabled"
    assert result.data["checks"]["stt"] == "not_configured"
    assert result.data["checks"]["tts"] == "not_configured"
    assert result.data["checks"]["wake_word"] == "not_configured"
    assert result.data["checks"]["background_listening"] == "disabled"
    assert result.data["checks"]["internet"] == "disabled"
    assert result.data["checks"]["gpt"] == "disabled"
    assert result.metadata["placeholder"] is True
    assert result.metadata["audio_hardware_accessed"] is False


def test_core_service_aggregates_pc_and_voice_capabilities():
    core_service = CoreService()

    result = core_service.get_capabilities()

    assert result.success is True
    assert result.data["available_services"] == [PC_SERVICE_NAME, VOICE_SERVICE_NAME]
    assert result.data["capabilities_by_service"][PC_SERVICE_NAME]["source"] == "pc_service"
    assert result.data["capabilities_by_service"][VOICE_SERVICE_NAME]["source"] == "voice_service"
    assert result.data["capabilities_by_service"][VOICE_SERVICE_NAME]["voice_input"] == "placeholder"
    assert result.data["capabilities_by_service"][VOICE_SERVICE_NAME]["voice_output"] == "placeholder"
    assert result.data["capabilities_by_service"][VOICE_SERVICE_NAME]["safeguards"][
        "audio_hardware_access"
    ] == "disabled"


def test_voice_service_does_not_access_audio_hardware():
    voice_service = PlaceholderVoiceService()

    status = voice_service.get_status()
    capabilities = voice_service.get_capabilities()

    assert voice_service.audio_hardware_accessed is False
    assert status.metadata["audio_hardware_accessed"] is False
    assert capabilities.metadata["audio_hardware_accessed"] is False


def test_voice_loop_defaults_to_null_voice_components():
    loop = VoiceLoop(text_handler=lambda text: f"handled {text}")

    assert isinstance(loop.voice_input, NullVoiceInput)
    assert isinstance(loop.voice_output, NullVoiceOutput)


def test_voice_single_turn_loop_runs_adapter_backed_input_output():
    handled_texts = []
    input_adapter = MockVoiceInputAdapter(transcripts=["calculate 2 + 2"])
    output_adapter = MockVoiceOutputAdapter()

    def handle_text(text):
        handled_texts.append(text)
        return SkillResponse(text="Result: 4", skill="calculator")

    loop = VoiceSingleTurnLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=handle_text,
    )

    result = loop.run_once()

    assert result.success is True
    assert result.status == "completed"
    assert result.input_text == "calculate 2 + 2"
    assert result.response_text == "Result: 4"
    assert handled_texts == ["calculate 2 + 2"]
    assert input_adapter.capture_count == 1
    assert output_adapter.spoken_texts == ["Result: 4"]
    assert result.data["text_request"]["text"] == "calculate 2 + 2"
    assert result.data["text_request"]["source"] == "mock_voice_input_adapter"
    assert result.data["handler_response"]["skill"] == "calculator"
    assert result.data["voice_output"]["data"]["accepted_text"] == "Result: 4"
    assert result.data["voice_input"]["data"]["microphone"] == "disabled"
    assert result.data["voice_output"]["data"]["speaker"] == "disabled"


def test_voice_single_turn_loop_empty_input_returns_safe_no_op():
    handled_texts = []
    input_adapter = MockVoiceInputAdapter(transcripts=["   "])
    output_adapter = MockVoiceOutputAdapter()
    loop = VoiceSingleTurnLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=lambda text: handled_texts.append(text),
    )

    result = loop.run_once()

    assert result.success is True
    assert result.status == "no_input"
    assert result.text == "No voice input detected."
    assert handled_texts == []
    assert input_adapter.capture_count == 1
    assert output_adapter.spoken_texts == []
    assert result.data["voice_input"]["data"]["transcript"] == "   "
    assert result.data["voice_input"]["data"]["microphone"] == "disabled"


def test_voice_single_turn_loop_input_adapter_failure_fails_safely():
    handled_texts = []
    input_adapter = MockVoiceInputAdapter(fail=True)
    output_adapter = MockVoiceOutputAdapter()
    loop = VoiceSingleTurnLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=lambda text: handled_texts.append(text),
    )

    result = loop.run_once()

    assert result.success is False
    assert result.status == "input_error"
    assert result.error_message == "mock_input_failure"
    assert handled_texts == []
    assert input_adapter.capture_count == 1
    assert output_adapter.spoken_texts == []
    assert result.data["voice_input"]["data"]["microphone"] == "disabled"


def test_voice_single_turn_loop_output_adapter_failure_fails_safely():
    input_adapter = MockVoiceInputAdapter(transcripts=["hello"])
    output_adapter = MockVoiceOutputAdapter(fail=True)
    loop = VoiceSingleTurnLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=lambda text: "Hello safely.",
    )

    result = loop.run_once()

    assert result.success is False
    assert result.status == "output_error"
    assert result.input_text == "hello"
    assert result.response_text == "Hello safely."
    assert result.error_message == "mock_output_failure"
    assert input_adapter.capture_count == 1
    assert output_adapter.spoken_texts == ["Hello safely."]
    assert result.data["voice_output"]["data"]["speaker"] == "disabled"


def test_voice_single_turn_loop_does_not_access_audio_hardware():
    input_adapter = MockVoiceInputAdapter(transcripts=["hello"])
    output_adapter = MockVoiceOutputAdapter()
    loop = VoiceSingleTurnLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=lambda text: "Hello.",
    )

    result = loop.run_once()

    assert result.success is True
    assert loop.audio_hardware_accessed is False
    assert input_adapter.audio_hardware_accessed is False
    assert output_adapter.audio_hardware_accessed is False
    assert result.data["voice_input"]["metadata"]["audio_hardware_accessed"] is False
    assert result.data["voice_output"]["metadata"]["audio_hardware_accessed"] is False
    assert result.metadata["audio_hardware_access"] == "disabled"


def test_voice_session_loop_runs_multiple_mock_turns():
    handled_texts = []
    input_adapter = MockVoiceInputAdapter(transcripts=["hello", "calculate 2 + 2"])
    output_adapter = MockVoiceOutputAdapter()

    def handle_text(text):
        handled_texts.append(text)
        return f"handled: {text}"

    session = VoiceSessionLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=handle_text,
        max_turns=2,
    )

    result = session.run()

    assert result.success is True
    assert result.status == "max_turns_reached"
    assert result.stop_reason == "max_turns"
    assert handled_texts == ["hello", "calculate 2 + 2"]
    assert output_adapter.spoken_texts == ["handled: hello", "handled: calculate 2 + 2"]
    assert input_adapter.capture_count == 2
    assert [turn.input_text for turn in result.turns] == ["hello", "calculate 2 + 2"]
    assert [turn.response_text for turn in result.turns] == [
        "handled: hello",
        "handled: calculate 2 + 2",
    ]
    assert result.transcript == result.history
    assert result.transcript[0]["user"] == "hello"
    assert result.transcript[0]["assistant"] == "handled: hello"
    assert result.data["turn_count"] == 2
    assert result.data["max_turns"] == 2


def test_voice_session_loop_stop_phrase_stops_before_handler():
    handled_texts = []
    input_adapter = MockVoiceInputAdapter(transcripts=["hello", "stop", "after stop"])
    output_adapter = MockVoiceOutputAdapter()
    session = VoiceSessionLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=lambda text: handled_texts.append(text) or f"handled: {text}",
        max_turns=3,
    )

    result = session.run()

    assert result.success is True
    assert result.status == "stopped"
    assert result.stop_reason == "stop_phrase"
    assert handled_texts == ["hello"]
    assert output_adapter.spoken_texts == ["handled: hello"]
    assert input_adapter.capture_count == 2
    assert len(result.turns) == 2
    assert result.turns[1].status == "stopped"
    assert result.turns[1].input_text == "stop"
    assert result.transcript[1]["assistant"] == ""


def test_voice_session_loop_respects_max_turns():
    input_adapter = MockVoiceInputAdapter(transcripts=["one", "two", "three"])
    output_adapter = MockVoiceOutputAdapter()
    session = VoiceSessionLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=lambda text: f"handled: {text}",
        max_turns=2,
    )

    result = session.run()

    assert result.success is True
    assert result.status == "max_turns_reached"
    assert input_adapter.capture_count == 2
    assert output_adapter.spoken_texts == ["handled: one", "handled: two"]
    assert [entry["user"] for entry in result.history] == ["one", "two"]


def test_voice_session_loop_empty_input_is_safe_no_op():
    handled_texts = []
    input_adapter = MockVoiceInputAdapter(transcripts=["", "hello"])
    output_adapter = MockVoiceOutputAdapter()
    session = VoiceSessionLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=lambda text: handled_texts.append(text) or f"handled: {text}",
        max_turns=2,
    )

    result = session.run()

    assert result.success is True
    assert result.status == "max_turns_reached"
    assert handled_texts == ["hello"]
    assert output_adapter.spoken_texts == ["handled: hello"]
    assert input_adapter.capture_count == 2
    assert result.turns[0].status == "no_input"
    assert result.turns[0].input_text == ""
    assert result.turns[0].response_text == ""
    assert result.turns[1].status == "completed"
    assert result.transcript[0]["status"] == "no_input"
    assert result.transcript[1]["assistant"] == "handled: hello"


def test_voice_session_loop_input_adapter_failure_fails_safely():
    handled_texts = []
    input_adapter = MockVoiceInputAdapter(fail=True)
    output_adapter = MockVoiceOutputAdapter()
    session = VoiceSessionLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=lambda text: handled_texts.append(text),
        max_turns=3,
    )

    result = session.run()

    assert result.success is False
    assert result.status == "failed"
    assert result.stop_reason == "failure"
    assert result.error_message == "mock_input_failure"
    assert handled_texts == []
    assert output_adapter.spoken_texts == []
    assert input_adapter.capture_count == 1
    assert len(result.turns) == 1
    assert result.turns[0].status == "input_error"
    assert result.turns[0].data["voice_input"]["data"]["microphone"] == "disabled"


def test_voice_session_loop_output_adapter_failure_fails_safely():
    input_adapter = MockVoiceInputAdapter(transcripts=["hello"])
    output_adapter = MockVoiceOutputAdapter(fail=True)
    session = VoiceSessionLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=lambda text: "Hello safely.",
        max_turns=2,
    )

    result = session.run()

    assert result.success is False
    assert result.status == "failed"
    assert result.stop_reason == "failure"
    assert result.error_message == "mock_output_failure"
    assert input_adapter.capture_count == 1
    assert output_adapter.spoken_texts == ["Hello safely."]
    assert len(result.turns) == 1
    assert result.turns[0].status == "output_error"
    assert result.turns[0].data["voice_output"]["data"]["speaker"] == "disabled"


def test_voice_session_loop_does_not_access_audio_hardware():
    input_adapter = MockVoiceInputAdapter(transcripts=["hello", "goodbye"])
    output_adapter = MockVoiceOutputAdapter()
    session = VoiceSessionLoop(
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        text_handler=lambda text: "Hello.",
        max_turns=2,
    )

    result = session.run()

    assert result.success is True
    assert session.audio_hardware_accessed is False
    assert input_adapter.audio_hardware_accessed is False
    assert output_adapter.audio_hardware_accessed is False
    assert result.metadata["audio_hardware_access"] == "disabled"
    assert result.metadata["background_loop"] == "disabled"
    assert result.metadata["audio_hardware_accessed"] is False
    assert result.turns[0].data["voice_input"]["metadata"]["audio_hardware_accessed"] is False
    assert result.turns[0].data["voice_output"]["metadata"]["audio_hardware_accessed"] is False


def test_voice_loop_empty_input_does_nothing_safely():
    calls = []
    voice_input = StaticVoiceInput("")
    voice_output = RecordingVoiceOutput()
    loop = VoiceLoop(
        voice_input=voice_input,
        voice_output=voice_output,
        text_handler=lambda text: calls.append(text),
    )

    result = loop.run_once()

    assert result.success is True
    assert result.status == "no_input"
    assert result.text == "No voice input detected."
    assert voice_input.listen_count == 1
    assert calls == []
    assert voice_output.spoken_texts == []
    assert result.data["voice_input"]["data"]["transcript"] == ""


def test_voice_loop_routes_recognized_text_to_text_handler():
    handled_texts = []

    def handle_text(text):
        handled_texts.append(text)
        return SkillResponse(text="Result: 4", skill="calculator")

    voice_input = StaticVoiceInput("calculate 2 + 2")
    voice_output = RecordingVoiceOutput()
    loop = VoiceLoop(
        voice_input=voice_input,
        voice_output=voice_output,
        text_handler=handle_text,
    )

    result = loop.run_once()

    assert result.success is True
    assert result.status == "completed"
    assert result.input_text == "calculate 2 + 2"
    assert result.response_text == "Result: 4"
    assert handled_texts == ["calculate 2 + 2"]
    assert voice_output.spoken_texts == ["Result: 4"]
    assert result.data["handler_response"]["skill"] == "calculator"


def test_voice_loop_sends_response_text_to_null_voice_output():
    voice_input = StaticVoiceInput("what time is it")
    loop = VoiceLoop(
        voice_input=voice_input,
        voice_output=NullVoiceOutput(),
        text_handler=lambda text: "It is placeholder time.",
    )

    result = loop.run_once()

    assert result.success is True
    assert result.status == "completed"
    assert result.response_text == "It is placeholder time."
    assert result.data["voice_output"]["success"] is True
    assert result.data["voice_output"]["data"]["accepted_text"] == "It is placeholder time."
    assert result.data["voice_output"]["data"]["speaker"] == "disabled"
    assert result.data["voice_output"]["data"]["tts"] == "disabled"


def test_voice_loop_input_error_fails_safely():
    calls = []
    voice_output = RecordingVoiceOutput()
    loop = VoiceLoop(
        voice_input=RaisingVoiceInput(),
        voice_output=voice_output,
        text_handler=lambda text: calls.append(text),
    )

    result = loop.run_once()

    assert result.success is False
    assert result.status == "input_error"
    assert result.error_message == "RuntimeError: input failure"
    assert calls == []
    assert voice_output.spoken_texts == []


def test_voice_loop_input_adapter_failure_fails_safely():
    calls = []
    voice_output = RecordingVoiceOutput()
    voice_input = NullVoiceInput(adapter=MockVoiceInputAdapter(fail=True))
    loop = VoiceLoop(
        voice_input=voice_input,
        voice_output=voice_output,
        text_handler=lambda text: calls.append(text),
    )

    result = loop.run_once()

    assert result.success is False
    assert result.status == "input_error"
    assert result.error_message == "mock_input_failure"
    assert result.data["voice_input"]["data"]["microphone"] == "disabled"
    assert calls == []
    assert voice_output.spoken_texts == []


def test_voice_loop_handler_error_fails_safely():
    voice_output = RecordingVoiceOutput()

    def failing_handler(text):
        raise ValueError("handler failure")

    loop = VoiceLoop(
        voice_input=StaticVoiceInput("hello"),
        voice_output=voice_output,
        text_handler=failing_handler,
    )

    result = loop.run_once()

    assert result.success is False
    assert result.status == "handler_error"
    assert result.input_text == "hello"
    assert result.error_message == "ValueError: handler failure"
    assert voice_output.spoken_texts == []


def test_voice_loop_output_error_fails_safely():
    voice_output = RecordingVoiceOutput(fail=True)
    loop = VoiceLoop(
        voice_input=StaticVoiceInput("hello"),
        voice_output=voice_output,
        text_handler=lambda text: "Hello.",
    )

    result = loop.run_once()

    assert result.success is False
    assert result.status == "output_error"
    assert result.input_text == "hello"
    assert result.response_text == "Hello."
    assert result.error_message == "output failure"
    assert voice_output.spoken_texts == ["Hello."]


def test_voice_loop_output_adapter_failure_fails_safely():
    output_adapter = MockVoiceOutputAdapter(fail=True)
    loop = VoiceLoop(
        voice_input=StaticVoiceInput("hello"),
        voice_output=NullVoiceOutput(adapter=output_adapter),
        text_handler=lambda text: "Hello from adapter.",
    )

    result = loop.run_once()

    assert result.success is False
    assert result.status == "output_error"
    assert result.input_text == "hello"
    assert result.response_text == "Hello from adapter."
    assert result.error_message == "mock_output_failure"
    assert output_adapter.spoken_texts == ["Hello from adapter."]
    assert result.data["voice_output"]["data"]["speaker"] == "disabled"
