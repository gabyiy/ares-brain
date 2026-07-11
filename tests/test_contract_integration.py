from core import (
    CITY_STATE_IDLE,
    EVENT_DECISION_FAILED,
    LIFECYCLE_UNLOADED,
    CoreService,
    CoreServiceResult,
    LifecycleRequest,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockVoiceOutputAdapter,
    VoicePipeline,
    VoicePipelineRequestV1,
)
from events import EventHistoryStore


class ContractTestCity:
    def __init__(self):
        self.calls = []

    def handle(self, text):
        self.calls.append(text)
        return CoreServiceResult(
            success=True,
            text=f"handled {text}",
            data={
                "success": True,
                "status": "handled",
                "text": f"handled {text}",
                "response_text": f"handled {text}",
            },
            metadata={"safe": True, "source": "contract_test_city"},
        )


def test_core_service_rejects_unsupported_contract_before_city_activation(tmp_path):
    history = EventHistoryStore(path=tmp_path / "events.json")
    city = ContractTestCity()
    core_service = CoreService(
        event_history_store=history,
        register_default_pc=False,
        register_default_voice=False,
    )
    core_service.register_service("voice", city, capabilities=["voice.text_loop"])

    result = core_service.route_by_capability(
        "voice.text_loop",
        lambda service: service.handle("hello"),
        session_id="session-contract",
        correlation_id="corr-contract",
        contract_version="v2",
    )

    assert result.success is False
    assert result.error_message == "unsupported_contract_version:core.execution.request:v2"
    assert result.data["status"] == "contract_rejected"
    assert city.calls == []
    assert core_service.get_service_status("voice") == CITY_STATE_IDLE
    assert core_service.get_lifecycle_status("voice").data["lifecycle_status"]["state"] == (
        LIFECYCLE_UNLOADED
    )
    assert history.list()[0].decision == EVENT_DECISION_FAILED
    assert history.list()[0].type == "contract.compatibility_rejected"
    assert history.list()[0].event["correlation_id"] == "corr-contract"


def test_core_service_remains_usable_after_contract_failure():
    city = ContractTestCity()
    core_service = CoreService(register_default_pc=False, register_default_voice=False)
    core_service.register_service("voice", city, capabilities=["voice.text_loop"])

    failed = core_service.route_by_capability(
        "voice.text_loop",
        lambda service: service.handle("bad"),
        contract_version="v2",
    )
    recovered = core_service.route_by_capability(
        "voice.text_loop",
        lambda service: service.handle("good"),
        correlation_id="corr-good",
    )

    assert failed.success is False
    assert recovered.success is True
    assert recovered.data["service"] == "voice"
    assert recovered.data["module_lifecycle"]["execute"]["request"]["correlation_id"] == "corr-good"
    assert city.calls == ["good"]


def test_lifecycle_rejects_unsupported_contract_without_state_change():
    city = ContractTestCity()
    core_service = CoreService(register_default_pc=False, register_default_voice=False)
    core_service.register_service("voice", city, capabilities=["voice.text_loop"])

    failed = core_service.lifecycle_manager.start(
        "voice",
        LifecycleRequest(
            "voice",
            "start",
            contract_version="v2",
            correlation_id="corr-life",
        ),
    )

    assert failed.success is False
    assert failed.status == "contract_rejected"
    assert failed.request.correlation_id == "corr-life"
    assert core_service.get_lifecycle_status("voice").data["lifecycle_status"]["state"] == (
        LIFECYCLE_UNLOADED
    )
    assert core_service.get_lifecycle_history("voice").data["history"] == []


def test_voice_pipeline_rejects_unsupported_version_without_microphone_or_city_activation():
    microphone = MockMicrophoneAdapter(chunks=[b"\x01"])
    stt = MockSpeechToTextAdapter(transcripts=["hello"])
    output = MockVoiceOutputAdapter()
    core_service = CoreService()
    pipeline = VoicePipeline(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
        output_adapter=output,
        command_handler=lambda text: "handled",
        core_service=core_service,
    )

    result = pipeline.run_once(
        session_id="session-pipeline",
        correlation_id="corr-bad",
        contract_version="v2",
    )

    assert result.success is False
    assert result.status == "contract_rejected"
    assert result.error_message == "unsupported_contract_version:voice.pipeline.request:v2"
    assert microphone.start_count == 0
    assert stt.transcription_count == 0
    assert output.spoken_texts == []
    assert core_service.get_lifecycle_status("voice").data["lifecycle_status"]["state"] == (
        LIFECYCLE_UNLOADED
    )


def test_voice_session_remains_usable_after_contract_rejection():
    microphone = MockMicrophoneAdapter(chunks=[b"\x01"])
    stt = MockSpeechToTextAdapter(transcripts=["hello"])
    output = MockVoiceOutputAdapter()
    pipeline = VoicePipeline(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
        output_adapter=output,
        command_handler=lambda text: "handled after rejection",
    )

    rejected = pipeline.run_once(
        session_id="session-reuse",
        correlation_id="corr-rejected",
        request=VoicePipelineRequestV1(
            contract_version="v2",
            session_id="session-reuse",
            correlation_id="corr-rejected",
        ),
    )
    accepted = pipeline.run_once(session_id="session-reuse", correlation_id="corr-ok")

    assert rejected.success is False
    assert rejected.status == "contract_rejected"
    assert accepted.success is True
    assert accepted.status == "completed"
    assert accepted.response_text == "handled after rejection"
    assert output.spoken_texts == ["handled after rejection"]
    assert microphone.start_count == 1
    assert stt.transcription_count == 1


def test_voice_pipeline_still_completes_successfully_with_v1_contracts():
    pipeline = VoicePipeline(
        microphone_adapter=MockMicrophoneAdapter(chunks=[b"\x01"]),
        speech_to_text_adapter=MockSpeechToTextAdapter(transcripts=["hello"]),
        output_adapter=MockVoiceOutputAdapter(),
        command_handler=lambda text: "v1 handled",
    )

    result = pipeline.run_once(
        request=VoicePipelineRequestV1(
            session_id="session-v1",
            correlation_id="corr-v1",
        )
    )

    assert result.success is True
    assert result.status == "completed"
    assert result.contract_name == "voice.pipeline.result"
    assert result.contract_version == "v1"
    assert result.correlation_id == "corr-v1"
    assert result.data["routing"]["contract_name"] == "voice.command.result"
    assert result.data["routing"]["data"]["route_result"]["module_lifecycle"]["execute"][
        "contract_name"
    ] == "lifecycle.execution.result"
