from core import (
    PC_SERVICE_NAME,
    VOICE_SERVICE_NAME,
    CoreService,
    PlaceholderVoiceService,
)


def test_voice_service_registers_through_core_service():
    core_service = CoreService()

    voice_service = core_service.get_service(VOICE_SERVICE_NAME)

    assert isinstance(voice_service, PlaceholderVoiceService)
    assert {service["name"] for service in core_service.list_services()} == {
        PC_SERVICE_NAME,
        VOICE_SERVICE_NAME,
    }


def test_voice_service_exposes_capabilities():
    voice_service = PlaceholderVoiceService()

    result = voice_service.get_capabilities()

    assert result.success is True
    assert result.text == "Voice City capabilities discovered."
    assert result.data["source"] == "voice_service"
    assert result.data["supported_voice_actions"] == []
    assert result.data["supported_input_modes"] == []
    assert result.data["supported_output_modes"] == []
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
