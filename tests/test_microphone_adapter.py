from threading import Event

import pytest

from core import (
    AudioChunk,
    MockMicrophoneAdapter,
    MockVoiceInputAdapter,
    NullVoiceInput,
    PlaceholderVoiceService,
)


def test_audio_chunk_serializes_stable_metadata():
    chunk = AudioChunk(
        data=b"\x01\x02\x03\x04",
        sample_rate_hz=2,
        channels=1,
        sample_width_bytes=2,
        sequence_number=7,
        source="test_microphone",
    )

    payload = chunk.to_dict()

    assert payload["byte_count"] == 4
    assert payload["duration_seconds"] == 1.0
    assert payload["sample_rate_hz"] == 2
    assert payload["channels"] == 1
    assert payload["sample_width_bytes"] == 2
    assert payload["sequence_number"] == 7
    assert payload["source"] == "test_microphone"
    assert "data" not in payload
    assert chunk.to_dict(include_bytes=True)["data"] == b"\x01\x02\x03\x04"


def test_audio_chunk_rejects_invalid_audio_format():
    with pytest.raises(ValueError, match="sample_rate_hz must be positive"):
        AudioChunk(data=b"", sample_rate_hz=0)

    with pytest.raises(ValueError, match="channels must be positive"):
        AudioChunk(data=b"", channels=0)

    with pytest.raises(ValueError, match="sample_width_bytes must be positive"):
        AudioChunk(data=b"", sample_width_bytes=0)


def test_mock_microphone_start_stop_lifecycle_is_safe():
    adapter = MockMicrophoneAdapter()

    start = adapter.start()
    stop = adapter.stop()

    assert start.success is True
    assert start.status == "started"
    assert stop.success is True
    assert stop.status == "stopped"
    assert adapter.started is False
    assert adapter.start_count == 1
    assert adapter.stop_count == 1
    assert adapter.audio_hardware_accessed is False
    assert start.metadata["audio_hardware_accessed"] is False
    assert stop.data["audio_hardware_access"] == "disabled"


def test_mock_microphone_reads_queued_audio_chunk():
    adapter = MockMicrophoneAdapter(chunks=[b"\x01\x02\x03\x04"])

    adapter.start()
    result = adapter.read_chunk(timeout_seconds=0.25)

    assert result.success is True
    assert result.status == "chunk"
    assert result.chunk is not None
    assert result.chunk.data == b"\x01\x02\x03\x04"
    assert result.chunk.sequence_number == 1
    assert result.data["remaining_chunks"] == 0
    assert result.data["timeout_seconds"] == 0.25
    assert result.metadata["audio_hardware_accessed"] is False


def test_mock_microphone_read_requires_start():
    adapter = MockMicrophoneAdapter(chunks=[b"\x01"])

    result = adapter.read_chunk()

    assert result.success is False
    assert result.status == "not_started"
    assert result.error_message == "microphone_not_started"
    assert result.chunk is None
    assert adapter.read_count == 1
    assert adapter.audio_hardware_accessed is False


def test_mock_microphone_timeout_returns_safe_result():
    adapter = MockMicrophoneAdapter()

    adapter.start()
    result = adapter.read_chunk(timeout_seconds=0.01)

    assert result.success is False
    assert result.status == "timeout"
    assert result.error_message == "microphone_read_timeout"
    assert result.data["timeout_seconds"] == 0.01
    assert result.chunk is None
    assert adapter.audio_hardware_accessed is False


def test_mock_microphone_cancellation_callable_is_supported():
    adapter = MockMicrophoneAdapter(chunks=[b"\x01"])

    adapter.start()
    result = adapter.read_chunk(cancel_requested=lambda: True)

    assert result.success is False
    assert result.status == "cancelled"
    assert result.error_message == "microphone_read_cancelled"
    assert adapter.read_count == 1
    assert adapter.audio_hardware_accessed is False


def test_mock_microphone_cancellation_event_is_supported():
    cancel_event = Event()
    cancel_event.set()
    adapter = MockMicrophoneAdapter(chunks=[b"\x01"])

    adapter.start()
    result = adapter.read_chunk(cancel_requested=cancel_event)

    assert result.success is False
    assert result.status == "cancelled"
    assert result.error_message == "microphone_read_cancelled"


def test_mock_microphone_failure_modes_fail_safely():
    start_adapter = MockMicrophoneAdapter(fail_start=True, failure_message="start boom")
    read_adapter = MockMicrophoneAdapter(fail_read=True, failure_message="read boom")
    stop_adapter = MockMicrophoneAdapter(fail_stop=True, failure_message="stop boom")

    start = start_adapter.start()
    read_adapter.start()
    read = read_adapter.read_chunk()
    stop = stop_adapter.stop()

    assert start.success is False
    assert start.status == "start_failed"
    assert start.error_message == "start boom"
    assert read.success is False
    assert read.status == "read_failed"
    assert read.error_message == "read boom"
    assert stop.success is False
    assert stop.status == "stop_failed"
    assert stop.error_message == "stop boom"
    assert start_adapter.audio_hardware_accessed is False
    assert read_adapter.audio_hardware_accessed is False
    assert stop_adapter.audio_hardware_accessed is False


def test_microphone_status_and_capabilities_are_structured():
    adapter = MockMicrophoneAdapter(chunks=[b"\x01"])

    adapter.start()
    status = adapter.get_status()
    capabilities = adapter.get_capabilities()

    assert status.success is True
    assert status.data["status"] == "started"
    assert status.data["queued_chunks"] == 1
    assert status.data["audio_hardware_access"] == "disabled"
    assert capabilities.success is True
    assert capabilities.data["supported_modes"] == ["mock_audio_chunks"]
    assert capabilities.data["timeout_handling"] == "safe_timeout_result"
    assert capabilities.data["cancellation"] == "supported"
    assert capabilities.data["audio_hardware_access"] == "disabled"


def test_voice_service_accepts_injected_microphone_adapter():
    microphone_adapter = MockMicrophoneAdapter(chunks=[b"\x01\x02"])
    voice_service = PlaceholderVoiceService(microphone_adapter=microphone_adapter)

    status = voice_service.get_status()
    capabilities = voice_service.get_capabilities()

    assert isinstance(voice_service.voice_input, NullVoiceInput)
    assert voice_service.microphone_adapter is microphone_adapter
    assert voice_service.voice_input.microphone_adapter is microphone_adapter
    assert status.success is True
    assert status.data["voice_input"]["microphone_adapter"]["data"]["source"] == (
        "mock_microphone_adapter"
    )
    assert capabilities.data["input_capabilities"]["microphone_adapter"]["data"][
        "cancellation"
    ] == "supported"
    assert voice_service.audio_hardware_accessed is False


def test_voice_input_adapter_can_receive_injected_microphone_adapter():
    microphone_adapter = MockMicrophoneAdapter(chunks=[b"\x01\x02"])
    input_adapter = MockVoiceInputAdapter(
        transcripts=["hello"],
        microphone_adapter=microphone_adapter,
    )

    result = input_adapter.capture()

    assert result.success is True
    assert input_adapter.microphone_adapter is microphone_adapter
    assert result.data["transcript"] == "hello"
    assert result.data["microphone_adapter"]["data"]["source"] == "mock_microphone_adapter"
    assert result.data["microphone"] == "disabled"
    assert result.metadata["audio_hardware_accessed"] is False
