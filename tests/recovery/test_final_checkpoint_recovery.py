import pytest

from core import (
    AdapterCandidate,
    AdapterFallbackPolicy,
    CapabilityManifest,
    CancellationToken,
    CoreService,
    Event,
    EventBus,
    LIFECYCLE_FAILED,
    LIFECYCLE_READY,
    LIFECYCLE_UNLOADED,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockVoiceOutputAdapter,
    PC_SERVICE_NAME,
    RESOURCE_ERROR_CANCELLATION_UNSUPPORTED,
    RESOURCE_ERROR_GLOBAL_TASK_LIMIT,
    RESOURCE_ERROR_RAM_BUDGET_EXCEEDED,
    RESOURCE_PROFILE_TEST,
    ResourceDeclaration,
    ResourceManager,
    ResourcePolicy,
    VOICE_SERVICE_NAME,
    VoicePipeline,
    build_service_manifest,
    resource_policy_for_profile,
)


class ManualClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class RouteCity:
    def __init__(self, name="city", fail_start=False, fail_health=False, fail_stop=False):
        self.name = name
        self.fail_start = fail_start
        self.fail_health = fail_health
        self.fail_stop = fail_stop
        self.calls = []
        self.start_count = 0
        self.stop_count = 0

    def start(self):
        self.start_count += 1
        if self.fail_start:
            return {"success": False, "error_message": "start_failed"}
        return {"success": True, "status": "started"}

    def get_status(self):
        if self.fail_health:
            return {"success": False, "error_message": "health_failed"}
        return {"success": True, "status": "healthy", "metadata": {"safe": True}}

    def get_capabilities(self):
        return {
            "success": True,
            "data": {"source": self.name, "capabilities": [f"{self.name}.run"]},
            "metadata": {"safe": True},
        }

    def stop(self):
        self.stop_count += 1
        if self.fail_stop:
            return {"success": False, "error_message": "stop_failed"}
        return {"success": True, "status": "stopped"}

    def handle(self, text):
        self.calls.append(text)
        return {"success": True, "text": f"{self.name}:{text}"}


class FailingEventHistoryStore:
    def __init__(self):
        self.calls = 0

    def add(self, event, result):
        self.calls += 1
        raise OSError("event history unavailable")


def _voice_pipeline(
    transcript="hello",
    command_handler=lambda text: "handled",
    core_service=None,
    microphone=None,
    stt=None,
    output=None,
    fallback_policy=None,
    microphone_candidates=None,
    speech_to_text_candidates=None,
):
    pipeline = VoicePipeline(
        microphone_adapter=microphone or MockMicrophoneAdapter(chunks=[b"\x01"]),
        speech_to_text_adapter=stt or MockSpeechToTextAdapter(transcripts=[transcript]),
        output_adapter=output or MockVoiceOutputAdapter(),
        command_handler=command_handler,
        core_service=core_service,
        fallback_policy=fallback_policy,
        microphone_candidates=microphone_candidates,
        speech_to_text_candidates=speech_to_text_candidates,
    )
    return pipeline


def _manifest(name, capability, resources=None):
    return build_service_manifest(
        module_name=name,
        capabilities=[capability],
        resources=resources or ResourceDeclaration(),
    )


def test_primary_microphone_health_failure_falls_back_to_secondary_without_leaks():
    primary = MockMicrophoneAdapter(source="primary_mic", available=False)
    secondary = MockMicrophoneAdapter(source="secondary_mic", chunks=[b"\x01"])
    fallback = AdapterFallbackPolicy()
    pipeline = _voice_pipeline(
        microphone=primary,
        stt=MockSpeechToTextAdapter(transcripts=["hello"]),
        fallback_policy=fallback,
        microphone_candidates=[
            AdapterCandidate("primary_mic", primary, capabilities=["voice.capture"]),
            AdapterCandidate("secondary_mic", secondary, capabilities=["voice.capture"]),
        ],
    )

    result = pipeline.run_once(session_id="recover-mic", correlation_id="recover-mic")

    assert result.success is True
    assert primary.start_count == 0
    assert secondary.start_count == 1
    assert secondary.read_count == 1
    assert result.data["microphone"]["adapter_selection"]["selected_adapter_name"] == "secondary_mic"
    assert pipeline.core_service.resource_manager.current_usage()["active_task_count"] == 0


def test_primary_stt_runtime_failure_falls_back_when_retry_safe():
    primary = MockSpeechToTextAdapter(source="primary_stt", fail=True, failure_message="boom")
    fallback_stt = MockSpeechToTextAdapter(source="fallback_stt", transcripts=["hello from fallback"])
    fallback = AdapterFallbackPolicy()
    pipeline = _voice_pipeline(
        stt=primary,
        fallback_policy=fallback,
        speech_to_text_candidates=[
            AdapterCandidate("primary_stt", primary, capabilities=["voice.transcribe"]),
            AdapterCandidate("fallback_stt", fallback_stt, capabilities=["voice.transcribe"]),
        ],
    )

    result = pipeline.run_once(session_id="recover-stt", correlation_id="recover-stt")

    assert result.success is True
    assert result.data["transcription"]["data"]["selected_adapter_name"] == "fallback_stt"
    assert primary.transcription_count == 0
    assert fallback_stt.transcription_count == 1


def test_all_stt_adapters_fail_with_structured_failure_and_no_task_leak():
    first = MockSpeechToTextAdapter(source="first_stt", fail=True, failure_message="first")
    second = MockSpeechToTextAdapter(source="second_stt", fail=True, failure_message="second")
    pipeline = _voice_pipeline(
        stt=first,
        fallback_policy=AdapterFallbackPolicy(),
        speech_to_text_candidates=[
            AdapterCandidate("first_stt", first, capabilities=["voice.transcribe"]),
            AdapterCandidate("second_stt", second, capabilities=["voice.transcribe"]),
        ],
    )

    result = pipeline.run_once(session_id="recover-stt-all", correlation_id="recover-stt-all")

    assert result.success is False
    assert result.status == "transcription_failed"
    assert result.error_message
    assert pipeline.core_service.resource_manager.current_usage()["active_task_count"] == 0


def test_incompatible_fallback_adapter_is_rejected_before_use():
    bad = MockSpeechToTextAdapter(source="bad_version", transcripts=["ignored"])
    policy = AdapterFallbackPolicy()

    selection = policy.select(
        [AdapterCandidate("bad_version", bad, capabilities=["voice.transcribe"], interface_version="v2")],
        "voice.transcribe",
        required_interface_version="v1",
    )

    assert selection.success is False
    assert selection.rejections[0].reason == "incompatible_interface_version"
    assert bad.transcription_count == 0


def test_voice_city_start_failure_returns_structured_failure_and_releases_resources():
    city = RouteCity("voice", fail_start=True)
    core = CoreService(register_default_pc=False, register_default_voice=False)
    core.register_service(VOICE_SERVICE_NAME, city, capabilities=["voice.text_loop"])
    pipeline = _voice_pipeline(core_service=core)

    result = pipeline.run_once(session_id="start-failure", correlation_id="start-failure")

    assert result.success is False
    assert result.status == "route_failed"
    assert result.error_message == "start_failed"
    assert core.get_lifecycle_status(VOICE_SERVICE_NAME).data["lifecycle_status"]["state"] == LIFECYCLE_FAILED
    assert core.resource_manager.current_usage()["active_task_count"] == 0
    assert core.resource_manager.list_reservations() == []


def test_city_health_failure_blocks_execution_and_keeps_unrelated_city_unloaded():
    city = RouteCity("voice", fail_health=True)
    core = CoreService(register_default_pc=True, register_default_voice=False)
    core.register_service(VOICE_SERVICE_NAME, city, capabilities=["voice.text_loop"])
    pipeline = _voice_pipeline(core_service=core)

    result = pipeline.run_once(session_id="health-failure", correlation_id="health-failure")

    assert result.success is False
    assert result.error_message == "health_failed"
    assert city.calls == []
    assert core.get_lifecycle_status(PC_SERVICE_NAME).data["lifecycle_status"]["state"] == LIFECYCLE_UNLOADED
    assert core.resource_manager.current_usage()["active_task_count"] == 0


def test_resource_reservation_failure_prevents_activation_and_preserves_core_service():
    manager = ResourceManager(policy=ResourcePolicy(maximum_estimated_loaded_ram_mb=4))
    city = RouteCity("voice")
    core = CoreService(
        register_default_pc=False,
        register_default_voice=False,
        resource_manager=manager,
    )
    core.register_service(
        VOICE_SERVICE_NAME,
        city,
        capabilities=["voice.text_loop"],
        manifest=_manifest(
            VOICE_SERVICE_NAME,
            "voice.text_loop",
            ResourceDeclaration(estimated_ram_mb=8),
        ),
    )

    result = core.route_by_capability("voice.text_loop", lambda service: service.handle("hello"))

    assert result.success is False
    assert result.error_message == RESOURCE_ERROR_RAM_BUDGET_EXCEEDED
    assert city.start_count == 0
    assert manager.current_usage()["active_task_count"] == 0
    assert manager.list_reservations() == []
    assert core.get_lifecycle_status(VOICE_SERVICE_NAME).success is True
    assert core.list_services()[0]["name"] == VOICE_SERVICE_NAME


def test_task_execution_exception_is_isolated_and_releases_capacity():
    core = CoreService(register_default_pc=True)
    pipeline = _voice_pipeline(
        core_service=core,
        command_handler=lambda text: (_ for _ in ()).throw(RuntimeError("handler boom")),
    )

    result = pipeline.run_once(session_id="execution-failure", correlation_id="execution-failure")

    assert result.success is False
    assert result.status == "handler_failed"
    assert "RuntimeError: handler boom" in result.error_message
    assert core.resource_manager.current_usage()["active_task_count"] == 0
    assert core.get_lifecycle_status(PC_SERVICE_NAME).data["lifecycle_status"]["state"] == LIFECYCLE_UNLOADED


def test_output_failure_after_success_does_not_corrupt_next_voice_session():
    output = MockVoiceOutputAdapter(fail=True, failure_message="output failed")
    pipeline = _voice_pipeline(
        output=output,
        command_handler=lambda text: "first response",
    )

    first = pipeline.run_once(session_id="output-failure", correlation_id="output-failure-1")
    pipeline.output_adapter = MockVoiceOutputAdapter()
    pipeline.speech_to_text_adapter = MockSpeechToTextAdapter(transcripts=["hello again"])
    pipeline.microphone_adapter = MockMicrophoneAdapter(chunks=[b"\x02"])
    second = pipeline.run_once(session_id="output-failure", correlation_id="output-failure-2")

    assert first.success is False
    assert first.status == "output_failed"
    assert second.success is True
    assert second.status == "completed"
    assert pipeline.core_service.resource_manager.current_usage()["active_task_count"] == 0


def test_event_history_persistence_failure_is_recorded_without_crashing_core_service():
    failing_store = FailingEventHistoryStore()
    core = CoreService(event_history_store=failing_store)

    result = core.handle_event(
        Event(source=VOICE_SERVICE_NAME, type="voice.test", priority="normal", payload={"status": "ok"})
    )

    assert result.success is True
    assert result.decision == "recorded"
    assert failing_store.calls == 1
    assert core.event_history_failures()[0]["error_type"] == "OSError"


def test_idle_unload_stop_failure_preserves_reservation_for_review():
    clock = ManualClock()
    manager = ResourceManager(
        policy=ResourcePolicy(default_inactivity_timeout_seconds=1),
        clock=clock,
    )
    city = RouteCity("voice", fail_stop=True)
    core = CoreService(
        register_default_pc=False,
        register_default_voice=False,
        resource_manager=manager,
    )
    core.register_service(VOICE_SERVICE_NAME, city, capabilities=["voice.text_loop"])
    assert core.route_by_capability("voice.text_loop", lambda service: service.handle("hello")).success is True
    clock.advance(2)

    maintenance = core.run_resource_maintenance()

    assert maintenance.success is False
    assert maintenance.error_message == "resource_maintenance_errors"
    assert manager.list_loaded_modules() == [VOICE_SERVICE_NAME]
    assert city.stop_count == 1


def test_contract_mismatch_does_not_activate_voice_city_or_alter_lifecycle():
    core = CoreService()
    pipeline = _voice_pipeline(core_service=core)

    result = pipeline.run_once(contract_version="v2", session_id="bad-contract", correlation_id="bad-contract")

    assert result.success is False
    assert result.status == "contract_rejected"
    assert result.data["microphone_started"] is False
    assert core.get_lifecycle_status(VOICE_SERVICE_NAME).data["lifecycle_status"]["state"] == LIFECYCLE_UNLOADED
    assert core.resource_manager.list_reservations() == []


def test_malformed_manifest_registration_is_rejected_and_core_service_remains_usable():
    core = CoreService(register_default_pc=False, register_default_voice=False)

    with pytest.raises(ValueError):
        CapabilityManifest.from_dict({"module_name": "", "module_type": "city"})

    core.register_service("healthy", RouteCity("healthy"), capabilities=["healthy.run"])
    result = core.route_by_capability("healthy.run", lambda service: service.handle("ok"))

    assert result.success is True
    assert result.data["service"] == "healthy"


def test_unknown_and_disabled_city_requests_fail_closed_without_activation():
    core = CoreService(register_default_pc=False, register_default_voice=False)
    disabled = RouteCity("disabled")
    core.register_service("disabled", disabled, capabilities=["disabled.run"])
    assert core.disable_service("disabled") is True

    unknown = core.route_by_capability("missing.run", lambda service: service.handle("x"))
    disabled_result = core.route_by_capability("disabled.run", lambda service: service.handle("x"))

    assert unknown.success is False
    assert unknown.error_message == "capability_not_available"
    assert disabled_result.success is False
    assert disabled_result.error_message == "capability_not_available"
    assert disabled.calls == []
    assert core.get_lifecycle_status("disabled").data["lifecycle_status"]["state"] == LIFECYCLE_UNLOADED


def test_cancellation_before_and_during_cooperative_task_releases_slots():
    manager = ResourceManager(policy=resource_policy_for_profile(RESOURCE_PROFILE_TEST))
    manifest = _manifest("voice", "voice.text_loop")
    assert manager.reserve(manifest).success is True
    assert manager.acquire_task(manifest, task_id="voice-task").success is True
    token = CancellationToken(task_id="voice-task", supports_cancellation=True)

    result = manager.cancel_task("voice", token, reason="owner_cancelled")

    assert result.success is True
    assert token.requested is True
    assert manager.current_usage()["active_task_count"] == 0


def test_unsupported_cancellation_and_global_task_limit_fail_safely():
    manager = ResourceManager(policy=ResourcePolicy(maximum_concurrent_tasks=1))
    manifest = _manifest("voice", "voice.text_loop")
    assert manager.reserve(manifest).success is True
    assert manager.acquire_task(manifest, task_id="first").success is True

    capacity = manager.acquire_task(manifest, task_id="second")
    unsupported = CancellationToken(task_id="first", supports_cancellation=False).cancel("stop")

    assert capacity.success is False
    assert capacity.status == RESOURCE_ERROR_GLOBAL_TASK_LIMIT
    assert unsupported.success is False
    assert unsupported.status == RESOURCE_ERROR_CANCELLATION_UNSUPPORTED
    assert manager.current_usage()["active_task_count"] == 1
