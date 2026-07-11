import pytest

from core import (
    CONTRACT_CORE_EXECUTION_REQUEST,
    CONTRACT_MICROPHONE_CAPTURE_REQUEST,
    CONTRACT_MICROPHONE_CAPTURE_RESULT,
    CONTRACT_SPEECH_TO_TEXT_RESULT,
    CONTRACT_VERSION_V1,
    CONTRACT_VOICE_COMMAND_REQUEST,
    CONTRACT_VOICE_PIPELINE_REQUEST,
    DEFAULT_CONTRACT_REGISTRY,
    ContractRegistry,
    CoreExecutionRequestV1,
    MicrophoneCaptureRequestV1,
    MicrophoneCaptureResultV1,
    SpeechToTextResultV1,
    VoicePipelineRequestV1,
)


def test_contract_registry_accepts_valid_v1_request():
    request = MicrophoneCaptureRequestV1(
        correlation_id="corr-contract",
        session_id="session-contract",
    )

    result = DEFAULT_CONTRACT_REGISTRY.validate(
        request,
        expected_contract_name=CONTRACT_MICROPHONE_CAPTURE_REQUEST,
    )

    assert result.success is True
    assert result.status == "compatible"
    assert result.contract_version == CONTRACT_VERSION_V1
    assert result.current_version == CONTRACT_VERSION_V1


def test_contract_registry_accepts_valid_v1_result():
    result_contract = MicrophoneCaptureResultV1(
        success=True,
        status="chunk",
        correlation_id="corr-result",
        session_id="session-result",
    )

    result = DEFAULT_CONTRACT_REGISTRY.validate(
        result_contract,
        expected_contract_name=CONTRACT_MICROPHONE_CAPTURE_RESULT,
    )

    assert result.success is True
    assert result.contract_name == CONTRACT_MICROPHONE_CAPTURE_RESULT


def test_contract_registry_rejects_unsupported_v2_when_only_v1_supported():
    payload = MicrophoneCaptureRequestV1(correlation_id="corr-v2").to_dict()
    payload["contract_version"] = "v2"

    result = DEFAULT_CONTRACT_REGISTRY.validate(payload)

    assert result.success is False
    assert result.status == "unsupported_contract_version"
    assert result.contract_version == "v2"
    assert result.supported_versions == [CONTRACT_VERSION_V1]


def test_contract_registry_rejects_missing_contract_name():
    payload = MicrophoneCaptureRequestV1().to_dict()
    payload.pop("contract_name")

    result = DEFAULT_CONTRACT_REGISTRY.validate(payload)

    assert result.success is False
    assert result.status == "missing_contract_field"
    assert result.error_message == "missing_contract_name"


def test_contract_registry_rejects_missing_contract_version():
    payload = MicrophoneCaptureRequestV1().to_dict()
    payload.pop("contract_version")

    result = DEFAULT_CONTRACT_REGISTRY.validate(payload)

    assert result.success is False
    assert result.status == "missing_contract_field"
    assert result.error_message == "missing_contract_version"


def test_contract_registry_rejects_malformed_contract_version():
    payload = MicrophoneCaptureRequestV1().to_dict()
    payload["contract_version"] = "1"

    result = DEFAULT_CONTRACT_REGISTRY.validate(payload)

    assert result.success is False
    assert result.status == "malformed_contract_version"
    assert result.error_message == "malformed_contract_version:1"


def test_contract_registry_rejects_wrong_contract_type():
    request = CoreExecutionRequestV1(capability="voice.text_loop")

    result = DEFAULT_CONTRACT_REGISTRY.validate(
        request,
        expected_contract_name=CONTRACT_VOICE_COMMAND_REQUEST,
    )

    assert result.success is False
    assert result.status == "wrong_contract_type"
    assert CONTRACT_VOICE_COMMAND_REQUEST in result.error_message


def test_contract_serialization_preserves_correlation_id():
    request = VoicePipelineRequestV1(correlation_id="corr-stable")

    assert request.to_dict()["correlation_id"] == "corr-stable"


def test_contract_metadata_round_trips_through_from_dict():
    request = VoicePipelineRequestV1(
        correlation_id="corr-meta",
        metadata={"future_optional": {"kept": True}},
    )

    round_tripped = VoicePipelineRequestV1.from_dict(request.to_dict())

    assert round_tripped.correlation_id == "corr-meta"
    assert round_tripped.metadata == {"future_optional": {"kept": True}}


def test_contract_serialization_is_deterministic():
    request = VoicePipelineRequestV1(correlation_id="corr-deterministic")

    first = request.to_dict()
    second = request.to_dict()

    assert first == second
    assert list(first)[:6] == [
        "contract_name",
        "contract_version",
        "correlation_id",
        "session_id",
        "created_at",
        "metadata",
    ]


def test_contract_from_dict_rejects_unknown_top_level_fields():
    payload = VoicePipelineRequestV1().to_dict()
    payload["unexpected_required_field"] = True

    with pytest.raises(ValueError, match="Unknown contract fields"):
        VoicePipelineRequestV1.from_dict(payload)


def test_registry_reports_supported_contracts_and_current_version():
    assert CONTRACT_CORE_EXECUTION_REQUEST in DEFAULT_CONTRACT_REGISTRY.list_contracts()
    assert DEFAULT_CONTRACT_REGISTRY.current_version(CONTRACT_CORE_EXECUTION_REQUEST) == "v1"
    assert DEFAULT_CONTRACT_REGISTRY.supported_versions(CONTRACT_CORE_EXECUTION_REQUEST) == [
        "v1"
    ]
    assert "CoreService" in DEFAULT_CONTRACT_REGISTRY.consumers(CONTRACT_CORE_EXECUTION_REQUEST)


def test_duplicate_incompatible_registration_is_rejected():
    registry = ContractRegistry()
    registry.register(CONTRACT_SPEECH_TO_TEXT_RESULT, consumers=["VoicePipeline"])

    with pytest.raises(ValueError, match="Incompatible duplicate"):
        registry.register(
            CONTRACT_SPEECH_TO_TEXT_RESULT,
            current_version="v2",
            supported_versions=["v2"],
            consumers=["VoicePipeline"],
        )
