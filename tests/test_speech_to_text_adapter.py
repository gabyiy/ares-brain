from core import (
    AudioChunk,
    MockSpeechToTextAdapter,
    MockVoiceInputAdapter,
    PlaceholderVoiceService,
    TranscriptionResult,
)


def _chunk(data: bytes = b"\x01\x02") -> AudioChunk:
    return AudioChunk(data=data, source="test_microphone")


def test_transcription_result_clamps_confidence_and_strips_text():
    result = TranscriptionResult(
        success=True,
        status="transcribed",
        text="  hello ARES  ",
        confidence=1.7,
    )

    assert result.text == "hello ARES"
    assert result.confidence == 1.0
    assert result.to_dict()["confidence"] == 1.0


def test_mock_speech_to_text_transcribes_audio_chunk():
    adapter = MockSpeechToTextAdapter(transcripts=["hello ARES"], confidence=0.92)

    result = adapter.transcribe(_chunk())

    assert result.success is True
    assert result.status == "transcribed"
    assert result.text == "hello ARES"
    assert result.confidence == 0.92
    assert result.data["audio_chunk"]["byte_count"] == 2
    assert result.data["speech_engine_access"] == "disabled"
    assert result.metadata["speech_engine_accessed"] is False
    assert adapter.transcription_count == 1


def test_mock_speech_to_text_empty_audio_returns_safe_empty_result():
    adapter = MockSpeechToTextAdapter(transcripts=["ignored"])

    result = adapter.transcribe(_chunk(b""))

    assert result.success is True
    assert result.status == "empty_audio"
    assert result.text == ""
    assert result.confidence == 0.0
    assert result.data["audio_chunk"]["byte_count"] == 0
    assert result.error_message == ""
    assert adapter.transcription_count == 1


def test_mock_speech_to_text_low_confidence_is_reported():
    adapter = MockSpeechToTextAdapter(
        transcripts=["maybe hello"],
        confidence=0.24,
        low_confidence_threshold=0.5,
    )

    result = adapter.transcribe(_chunk())

    assert result.success is True
    assert result.status == "low_confidence"
    assert result.text == "maybe hello"
    assert result.confidence == 0.24
    assert result.error_message == ""


def test_mock_speech_to_text_adapter_failure_fails_safely():
    adapter = MockSpeechToTextAdapter(fail=True, failure_message="stt boom")

    result = adapter.transcribe(_chunk())

    assert result.success is False
    assert result.status == "failed"
    assert result.text == ""
    assert result.confidence == 0.0
    assert result.error_message == "stt boom"
    assert result.data["speech_engine_access"] == "disabled"
    assert result.metadata["speech_engine_accessed"] is False
    assert adapter.transcription_count == 1


def test_mock_speech_to_text_no_transcription_is_safe():
    adapter = MockSpeechToTextAdapter(transcripts=[], confidence=0.8)

    result = adapter.transcribe(_chunk())

    assert result.success is True
    assert result.status == "no_transcription"
    assert result.text == ""
    assert result.confidence == 0.8
    assert result.error_message == ""


def test_mock_speech_to_text_status_and_capabilities_are_structured():
    adapter = MockSpeechToTextAdapter(transcripts=["hello"])

    status = adapter.get_status()
    capabilities = adapter.get_capabilities()

    assert status.success is True
    assert status.status == "mock"
    assert status.data["queued_transcripts"] == 1
    assert status.data["stt"] == "mock"
    assert capabilities.success is True
    assert capabilities.status == "capabilities"
    assert capabilities.data["supported_input"] == "AudioChunk"
    assert capabilities.data["confidence"] == "supported"
    assert capabilities.data["empty_audio_handling"] == "safe_empty_result"
    assert capabilities.data["speech_engine"] == "disabled"


def test_voice_service_accepts_injected_speech_to_text_adapter():
    stt_adapter = MockSpeechToTextAdapter(transcripts=["hello"])
    voice_service = PlaceholderVoiceService(speech_to_text_adapter=stt_adapter)

    status = voice_service.get_status()
    capabilities = voice_service.get_capabilities()

    assert voice_service.speech_to_text_adapter is stt_adapter
    assert status.metadata["speech_engine_accessed"] is False
    assert status.data["voice_input"]["speech_to_text_adapter"]["data"]["source"] == (
        "mock_speech_to_text_adapter"
    )
    assert capabilities.data["input_capabilities"]["speech_to_text_adapter"]["data"][
        "supported_input"
    ] == "AudioChunk"
    assert capabilities.metadata["speech_engine_accessed"] is False


def test_voice_input_adapter_accepts_injected_speech_to_text_adapter():
    stt_adapter = MockSpeechToTextAdapter(transcripts=["hello from audio"])
    input_adapter = MockVoiceInputAdapter(
        transcripts=["hello from queued text"],
        speech_to_text_adapter=stt_adapter,
    )

    result = input_adapter.capture()

    assert input_adapter.speech_to_text_adapter is stt_adapter
    assert result.success is True
    assert result.data["transcript"] == "hello from queued text"
    assert result.data["speech_to_text_adapter"]["data"]["source"] == (
        "mock_speech_to_text_adapter"
    )
    assert result.metadata["speech_engine_accessed"] is False
