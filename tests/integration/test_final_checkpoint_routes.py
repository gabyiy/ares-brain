from core import (
    CoreService,
    LIFECYCLE_READY,
    LIFECYCLE_UNLOADED,
    LocalDeviceActionAdapter,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockVoiceOutputAdapter,
    PC_SERVICE_NAME,
    VOICE_SERVICE_NAME,
    VoicePipeline,
)
from events import EventBus
from skills import SkillManager
from skills.base import SkillResponse
from skills.builtin.calculator import CalculatorSkill
from skills.builtin.device_action import DeviceActionSkill


def _voice_pipeline_for_manager(manager, core_service, transcript):
    microphone = MockMicrophoneAdapter(chunks=[b"\x01\x02"])
    stt = MockSpeechToTextAdapter(transcripts=[transcript], confidence=0.95)
    output = MockVoiceOutputAdapter()
    pipeline = VoicePipeline(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
        output_adapter=output,
        command_handler=lambda text: manager.handle(text, run_before_intents=True),
        core_service=core_service,
    )
    return pipeline, microphone, stt, output


def _event_payload_text(result):
    return repr([event["payload"] for event in result.events])


def test_route_a_voice_text_request_reaches_planner_execution_and_releases_resources():
    core_service = CoreService()
    manager = SkillManager(
        event_bus=EventBus(),
        core_service=core_service,
    )
    manager.register(CalculatorSkill())
    pipeline, microphone, stt, output = _voice_pipeline_for_manager(
        manager,
        core_service,
        "calculate 2 + 2",
    )

    result = pipeline.run_once(session_id="route-a-session", correlation_id="route-a-corr")

    route_result = result.data["routing"]["data"]["route_result"]
    resource = route_result["resource_management"]

    assert result.success is True
    assert "Result: 4" in result.response_text
    assert output.spoken_texts == [result.response_text]
    assert microphone.start_count == 1
    assert microphone.read_count == 1
    assert microphone.stop_count == 1
    assert stt.transcription_count == 1
    assert result.data["activated_city"] == VOICE_SERVICE_NAME
    assert resource["reservation"]["success"] is True
    assert resource["task_slot"]["status"] == "task_slot_acquired"
    assert resource["task_release"]["status"] == "task_slot_released"
    assert resource["current_usage"]["active_task_count"] == 0
    assert core_service.resource_manager.current_usage()["active_task_count"] == 0
    assert core_service.get_lifecycle_status(VOICE_SERVICE_NAME).data["lifecycle_status"]["state"] == LIFECYCLE_READY
    assert core_service.get_lifecycle_status(PC_SERVICE_NAME).data["lifecycle_status"]["state"] == LIFECYCLE_UNLOADED
    assert core_service.get_service_status(PC_SERVICE_NAME) == "idle"
    assert "calculate 2 + 2" not in _event_payload_text(result)


def test_route_b_voice_text_status_request_can_use_core_service_pc_route_without_confirmation():
    core_service = CoreService()
    routed_pc_results = []

    def handle_command(text):
        if text != "system status":
            return SkillResponse(text="", skill="test")
        route = core_service.route_by_capability(
            "pc.status",
            lambda pc_service: pc_service.get_status(),
            session_id="route-b-session",
            correlation_id="route-b-pc-corr",
            request_payload={"request_type": "status"},
        )
        routed_pc_results.append(route)
        status = dict(route.data.get("response") or {})
        return SkillResponse(
            text=f"System status: {status.get('status', 'unknown')}.",
            skill="device_action",
            metadata={"pc_route": route.to_dict()},
        )

    microphone = MockMicrophoneAdapter(chunks=[b"\x03"])
    stt = MockSpeechToTextAdapter(transcripts=["system status"], confidence=0.95)
    output = MockVoiceOutputAdapter()
    pipeline = VoicePipeline(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
        output_adapter=output,
        command_handler=handle_command,
        core_service=core_service,
    )

    result = pipeline.run_once(session_id="route-b-session", correlation_id="route-b-corr")
    pc_route = routed_pc_results[0]
    pc_status = pc_route.data["response"]

    assert result.success is True
    assert output.spoken_texts == ["System status: ok."]
    assert pc_route.success is True
    assert pc_route.data["service"] == PC_SERVICE_NAME
    assert pc_status["source"] == "pc_service"
    assert pc_status["checks"]["shell_execution"] == "disabled"
    assert "confirmation_required" not in result.response_text.lower()
    assert core_service.resource_manager.current_usage()["active_task_count"] == 0
    assert sorted(core_service.resource_manager.current_usage()["reservation_names"]) == [
        PC_SERVICE_NAME,
        VOICE_SERVICE_NAME,
    ]


def test_route_c_voice_device_action_requires_confirmation_then_executes_once():
    calls = []
    adapter = LocalDeviceActionAdapter(
        lock_impl=lambda: calls.append("locked") or True,
        platform_system=lambda: "Windows",
    )
    manager = SkillManager(
        event_bus=EventBus(),
        device_action_adapter=adapter,
    )
    manager.register(DeviceActionSkill())
    pipeline, _, _, output = _voice_pipeline_for_manager(
        manager,
        manager.core_service,
        "lock pc",
    )

    prompt = pipeline.run_once(session_id="route-c-session", correlation_id="route-c-corr")

    assert prompt.success is True
    assert prompt.response_text.startswith("Confirmation required to lock Windows session")
    assert output.spoken_texts == [prompt.response_text]
    assert manager.confirmation_manager.pending() is not None
    assert calls == []

    confirmed = manager.handle("yes")
    repeated = manager.handle("yes")

    assert confirmed.skill == "device_action"
    assert confirmed.text == "Windows session lock requested."
    assert calls == ["locked"]
    assert repeated.skill == "confirmation"
    assert repeated.metadata["error"] == "missing_confirmation"
    assert calls == ["locked"]
