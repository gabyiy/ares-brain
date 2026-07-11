from dataclasses import replace
from datetime import datetime, timedelta, timezone

from core import (
    ConfirmationManager,
    CoreService,
    DANGER_FORBIDDEN,
    ExecutionPipeline,
    LIFECYCLE_UNLOADED,
    LocalDeviceActionAdapter,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockVoiceInputAdapter,
    MockVoiceOutputAdapter,
    PC_SERVICE_NAME,
    RESOURCE_ERROR_HEAVY_MODULE_LIMIT,
    RESOURCE_ERROR_NO_EVICTION_CANDIDATE,
    ResourceDeclaration,
    ResourceManager,
    ResourcePolicy,
    VOICE_SERVICE_NAME,
    VoicePipeline,
    VoiceSessionLoop,
    build_service_manifest,
)
from events import EventBus
from memory import NotesStore
from skills import SkillManager
from skills.builtin.device_action import DeviceActionSkill
from skills.builtin.notes import NotesSkill


class ManualClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value


class InactiveCity:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def get_status(self):
        self.calls.append("get_status")
        return {"success": True, "status": "healthy"}

    def get_capabilities(self):
        self.calls.append("get_capabilities")
        return {"success": True, "data": {"source": self.name}}

    def handle(self, text):
        self.calls.append(text)
        return {"success": True, "text": text}


def _manifest(name, capability, resources=None):
    return build_service_manifest(
        module_name=name,
        capabilities=[capability],
        resources=resources or ResourceDeclaration(),
    )


def _voice_pipeline(command_handler, transcript, core_service=None, output=None):
    return VoicePipeline(
        microphone_adapter=MockMicrophoneAdapter(chunks=[b"\x01"]),
        speech_to_text_adapter=MockSpeechToTextAdapter(transcripts=[transcript], confidence=0.95),
        output_adapter=output or MockVoiceOutputAdapter(),
        command_handler=command_handler,
        core_service=core_service,
    )


def _payload_text(events):
    return repr([event["payload"] for event in events])


def test_brain_path_cannot_execute_shell_or_unknown_actions():
    manager = SkillManager(event_bus=EventBus())
    manager.register(DeviceActionSkill())

    response = manager.handle("run command del C:\\important.txt", run_before_intents=True)

    assert response.skill == "device_action"
    assert response.metadata["results"][0]["returned_data"]["metadata"]["danger_classification"] == DANGER_FORBIDDEN
    assert response.metadata["results"][0]["returned_data"]["metadata"]["executed"] is False
    assert "forbidden" in response.text


def test_voice_command_cannot_bypass_confirmation_for_lock_pc():
    calls = []
    adapter = LocalDeviceActionAdapter(
        lock_impl=lambda: calls.append("locked") or True,
        platform_system=lambda: "Windows",
    )
    manager = SkillManager(event_bus=EventBus(), device_action_adapter=adapter)
    manager.register(DeviceActionSkill())
    pipeline = _voice_pipeline(
        lambda text: manager.handle(text, run_before_intents=True),
        "lock pc",
        core_service=manager.core_service,
    )

    result = pipeline.run_once(session_id="safety-confirm", correlation_id="safety-confirm")

    assert result.success is True
    assert result.response_text.startswith("Confirmation required")
    assert calls == []
    assert manager.confirmation_manager.pending() is not None


def test_expired_malformed_and_reused_confirmation_are_rejected():
    calls = []
    adapter = LocalDeviceActionAdapter(
        lock_impl=lambda: calls.append("locked") or True,
        platform_system=lambda: "Windows",
    )
    manager = SkillManager(event_bus=EventBus(), device_action_adapter=adapter)
    manager.register(DeviceActionSkill())

    manager.handle("lock pc", run_before_intents=True)
    pending = manager.confirmation_manager.pending()
    assert pending is not None
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    manager.confirmation_manager._pending = replace(pending, expires_at=expired_at)
    expired = manager.handle("yes")

    assert expired.skill == "confirmation"
    assert expired.metadata["error"] == "missing_confirmation"
    assert calls == []

    manager.handle("lock pc", run_before_intents=True)
    malformed = manager.handle("proceed without confirmation")
    assert malformed is None
    assert calls == []
    confirmed = manager.handle("yes")
    reused = manager.handle("yes")

    assert confirmed.text == "Windows session lock requested."
    assert reused.metadata["error"] == "missing_confirmation"
    assert calls == ["locked"]


def test_exactly_once_guard_blocks_duplicate_and_wrong_scope_confirmed_actions():
    calls = []
    sleep_calls = []
    adapter = LocalDeviceActionAdapter(
        lock_impl=lambda: calls.append("locked") or True,
        sleep_impl=lambda: sleep_calls.append("slept") or True,
        platform_system=lambda: "Windows",
    )
    params = {"confirmation_approved": True, "confirmation_id": "confirm-lock-1"}

    first = adapter.execute("lock_pc", params)
    duplicate = adapter.execute("lock_pc", params)
    wrong_scope = adapter.execute("sleep_pc", params)

    assert first.success is True
    assert duplicate.success is True
    assert duplicate.metadata["execution_guard"]["duplicate"] is True
    assert duplicate.metadata["execution_guard"]["executed_again"] is False
    assert wrong_scope.success is False
    assert wrong_scope.error_message == "idempotency_token_scope_mismatch"
    assert calls == ["locked"]
    assert sleep_calls == []


def test_response_retry_after_confirmation_execution_does_not_repeat_action():
    calls = []
    adapter = LocalDeviceActionAdapter(
        lock_impl=lambda: calls.append("locked") or True,
        platform_system=lambda: "Windows",
    )
    manager = SkillManager(event_bus=EventBus(), device_action_adapter=adapter)
    manager.register(DeviceActionSkill())
    manager.handle("lock pc", run_before_intents=True)
    request = manager.confirmation_manager.pending()
    context = manager.create_context()

    first = manager.execution_pipeline.execute_confirmed(request, context)
    second = manager.execution_pipeline.execute_confirmed(request, context)

    assert first.success is True
    assert second.success is True
    assert calls == ["locked"]
    assert second.step_results[0].returned_data["metadata"]["action_metadata"]["execution_guard"]["duplicate"] is True


def test_output_failure_after_confirmed_action_does_not_trigger_retry():
    calls = []
    adapter = LocalDeviceActionAdapter(
        lock_impl=lambda: calls.append("locked") or True,
        platform_system=lambda: "Windows",
    )
    params = {"confirmation_approved": True, "confirmation_id": "confirm-output-failure"}

    first = adapter.execute("lock_pc", params)
    retry = adapter.execute("lock_pc", params)

    assert first.success is True
    assert retry.success is True
    assert retry.metadata["execution_guard"]["duplicate"] is True
    assert calls == ["locked"]


def test_app_launch_remains_allowlist_only_and_disabled_apps_stay_disabled():
    calls = []
    adapter = LocalDeviceActionAdapter(app_launcher=lambda app: calls.append(app.app_id) or True)

    unknown = adapter.execute("open_app", {"app_id": "unknown", "confirmation_approved": True})
    notepad = adapter.execute("open_app", {"app_id": "notepad", "confirmation_approved": True})
    browser = adapter.execute("open_app", {"app_id": "browser", "confirmation_approved": True})
    path = adapter.execute(
        "open_app",
        {"app_id": "C:\\Windows\\System32\\cmd.exe", "confirmation_approved": True},
    )

    assert unknown.error_message == "unknown_app"
    assert notepad.error_message == "disabled_app"
    assert browser.error_message == "disabled_app"
    assert path.error_message == "invalid_app_id"
    assert calls == []


def test_fallback_and_retry_paths_do_not_bypass_confirmation():
    calls = []
    adapter = LocalDeviceActionAdapter(
        sleep_impl=lambda: calls.append("slept") or True,
        platform_system=lambda: "Windows",
    )
    manager = SkillManager(event_bus=EventBus(), device_action_adapter=adapter)
    manager.register(DeviceActionSkill())

    first = manager.handle("sleep pc", run_before_intents=True)
    second = manager.handle("sleep pc", run_before_intents=True)

    assert first.skill == "confirmation"
    assert second.skill == "confirmation"
    assert calls == []
    assert manager.confirmation_manager.pending() is not None


def test_inspection_does_not_activate_inactive_cities():
    core = CoreService()

    health = core.list_service_health(probe=False)
    resources = core.get_resource_status()
    capabilities = core.get_capabilities()

    assert health.success is True
    assert resources.success is True
    assert capabilities.success is True
    assert core.get_lifecycle_status(VOICE_SERVICE_NAME).data["lifecycle_status"]["state"] == LIFECYCLE_UNLOADED
    assert core.get_lifecycle_status(PC_SERVICE_NAME).data["lifecycle_status"]["state"] == LIFECYCLE_UNLOADED


def test_disabled_and_incompatible_modules_fail_closed():
    core = CoreService(register_default_pc=False, register_default_voice=False)
    city = InactiveCity("disabled")
    core.register_service("disabled", city, capabilities=["disabled.run"])
    core.disable_service("disabled")

    disabled = core.route_by_capability("disabled.run", lambda service: service.handle("x"))
    incompatible = core.route_by_capability(
        "disabled.run",
        lambda service: service.handle("x"),
        contract_version="v2",
    )

    assert disabled.success is False
    assert disabled.error_message == "capability_not_available"
    assert incompatible.success is False
    assert incompatible.error_message
    assert city.calls == []


def test_voice_operational_events_do_not_store_transcript_audio_or_secrets():
    secret_text = "my password is swordfish"
    pipeline = _voice_pipeline(lambda text: "safe response", secret_text)

    result = pipeline.run_once(session_id="redaction", correlation_id="redaction")
    payloads = _payload_text(result.events)

    assert result.success is True
    assert secret_text not in payloads
    assert "swordfish" not in payloads
    assert "password" not in payloads.lower()
    assert "data': b" not in payloads
    assert "accepted_text': 'safe response'" not in payloads
    assert "accepted_text_length" in payloads


def test_malformed_voice_input_does_not_create_resource_use():
    core = CoreService()
    pipeline = VoicePipeline(
        microphone_adapter=MockMicrophoneAdapter(chunks=[b"\x01"]),
        speech_to_text_adapter=MockSpeechToTextAdapter(transcripts=["ignored"], confidence=0.1),
        output_adapter=MockVoiceOutputAdapter(),
        command_handler=lambda text: "should not run",
        core_service=core,
    )

    result = pipeline.run_once(session_id="malformed", correlation_id="malformed")

    assert result.success is False
    assert result.status == "low_confidence_rejected"
    assert core.resource_manager.list_reservations() == []
    assert core.get_lifecycle_status(VOICE_SERVICE_NAME).data["lifecycle_status"]["state"] == LIFECYCLE_UNLOADED


def test_max_turn_and_task_concurrency_and_heavy_limits_are_enforced():
    session = VoiceSessionLoop(
        input_adapter=MockVoiceInputAdapter(transcripts=["one", "two", "three"]),
        output_adapter=MockVoiceOutputAdapter(),
        text_handler=lambda text: f"handled {text}",
        max_turns=2,
    ).run()

    manager = ResourceManager(policy=ResourcePolicy(maximum_concurrent_tasks=1, maximum_heavy_modules_loaded=1))
    light = _manifest("light", "light.run")
    heavy_one = _manifest("voice", "voice.run", ResourceDeclaration(heavy_module=True))
    heavy_two = _manifest("vision", "vision.run", ResourceDeclaration(heavy_module=True))
    assert manager.reserve(light).success is True
    assert manager.acquire_task(light, task_id="one").success is True
    blocked_task = manager.acquire_task(light, task_id="two")
    assert manager.release_task("light", "one").success is True
    assert manager.reserve(heavy_one).success is True
    blocked_heavy = manager.reserve(heavy_two)

    assert session.status == "max_turns_reached"
    assert len(session.turns) == 2
    assert blocked_task.status == "global_task_limit_exceeded"
    assert blocked_heavy.status == RESOURCE_ERROR_HEAVY_MODULE_LIMIT
    assert manager.current_usage()["active_task_count"] == 0


def test_eviction_and_cancellation_cannot_interrupt_active_or_dangerous_work():
    clock = ManualClock()
    manager = ResourceManager(
        policy=ResourcePolicy(eviction_enabled=True),
        clock=clock,
    )
    active = _manifest(
        "active_voice",
        "voice.run",
        ResourceDeclaration(estimated_ram_mb=32, task_priority="normal"),
    )
    incoming = _manifest(
        "incoming",
        "incoming.run",
        ResourceDeclaration(estimated_ram_mb=32, task_priority="high"),
    )
    assert manager.reserve(active).success is True
    assert manager.acquire_task(active, task_id="dangerous-action").success is True

    eviction = manager.select_eviction_candidate(incoming, priority="high")

    assert eviction.success is False
    assert eviction.status == RESOURCE_ERROR_NO_EVICTION_CANDIDATE
    assert manager.current_usage()["active_task_count"] == 1


def test_memory_migration_and_resource_metric_status_are_safe_summaries(tmp_path):
    notes_path = tmp_path / "notes.json"
    store = NotesStore(path=notes_path, event_bus=EventBus())
    note = store.add("private owner memory")
    reloaded = NotesStore(path=notes_path, event_bus=EventBus())
    core = CoreService()

    report = core.get_resource_status()

    assert reloaded.list()[0].id == note.id
    assert "private owner memory" not in repr(report.data)
    assert "Process metrics are process-level observations, not per-module exact measurements." in (
        report.data["observed_process_metrics"]["note"]
    )
