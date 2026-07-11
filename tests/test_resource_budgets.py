import pytest

from core import (
    CITY_STATE_IDLE,
    LIFECYCLE_READY,
    RESOURCE_ERROR_CANCELLATION_UNSUPPORTED,
    RESOURCE_ERROR_GLOBAL_TASK_LIMIT,
    RESOURCE_ERROR_HARDWARE_ACCELERATION_DENIED,
    RESOURCE_ERROR_HEAVY_MODULE_LIMIT,
    RESOURCE_ERROR_MODULE_TASK_LIMIT,
    RESOURCE_ERROR_NETWORK_REQUIRED_DENIED,
    RESOURCE_ERROR_NO_EVICTION_CANDIDATE,
    RESOURCE_ERROR_RAM_BUDGET_EXCEEDED,
    RESOURCE_EVENT_ACTIVATION_DENIED,
    RESOURCE_EVENT_RESERVATION_CREATED,
    RESOURCE_PROFILE_DESKTOP,
    RESOURCE_PROFILE_FUTURE_ORIN,
    RESOURCE_PROFILE_RASPBERRY_PI_5,
    RESOURCE_PROFILE_TEST,
    TASK_PRIORITY_CRITICAL,
    TASK_PRIORITY_HIGH,
    TASK_PRIORITY_LOW,
    TASK_PRIORITY_NORMAL,
    AdapterCandidate,
    AdapterFallbackPolicy,
    CancellationToken,
    CoreService,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockVoiceOutputAdapter,
    ResourceDeclaration,
    ResourceManager,
    ResourcePolicy,
    VoicePipeline,
    build_service_manifest,
    resource_policy_for_profile,
)
from events import EventHistoryStore


class ManualClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class BudgetCity:
    def __init__(self, name="city", fail_start=False, fail_stop=False):
        self.name = name
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.calls = []
        self.start_count = 0
        self.stop_count = 0

    def start(self):
        self.start_count += 1
        if self.fail_start:
            return {"success": False, "error_message": "start_failed"}
        return {"success": True, "status": "started"}

    def stop(self):
        self.stop_count += 1
        if self.fail_stop:
            return {"success": False, "error_message": "stop_failed"}
        return {"success": True, "status": "stopped"}

    def get_status(self):
        return {
            "success": True,
            "status": "healthy",
            "data": {"status": "healthy"},
            "metadata": {"safe": True},
        }

    def handle(self, text):
        self.calls.append(text)
        return {"success": True, "text": f"{self.name}:{text}"}


def _manifest(name, capability=None, resources=None):
    return build_service_manifest(
        module_name=name,
        capabilities=[capability or f"{name}.run"],
        resources=resources or ResourceDeclaration(),
    )


def _manager(policy=None, clock=None, history=None, metrics_provider=None):
    return ResourceManager(
        policy=policy or ResourcePolicy(),
        clock=clock or ManualClock(),
        event_history_store=history,
        metrics_provider=metrics_provider,
    )


def test_valid_manifest_resource_declaration_is_serialized():
    manifest = _manifest(
        "voice",
        resources=ResourceDeclaration(
            estimated_ram_mb=12,
            estimated_cpu_weight="low",
            startup_cost="light",
            shutdown_cost="light",
            heavy_module=True,
            inactivity_timeout_seconds=10,
            maximum_concurrent_tasks=2,
            task_priority=TASK_PRIORITY_HIGH,
        ),
    )

    data = manifest.to_dict()
    round_trip = type(manifest).from_dict(data)

    assert data["resources"]["estimated_ram_mb"] == 12
    assert data["resources"]["heavy_module"] is True
    assert round_trip.resources.task_priority == TASK_PRIORITY_HIGH


def test_invalid_negative_ram_is_rejected():
    with pytest.raises(ValueError, match="estimated_ram_mb"):
        ResourceDeclaration(estimated_ram_mb=-1)


def test_invalid_resource_enum_is_rejected():
    with pytest.raises(ValueError, match="estimated_cpu_weight"):
        ResourceDeclaration(estimated_cpu_weight="huge")


def test_light_module_reservation_succeeds():
    manager = _manager(policy=ResourcePolicy(maximum_estimated_loaded_ram_mb=32))
    manifest = _manifest("notes", resources=ResourceDeclaration(estimated_ram_mb=4))

    result = manager.reserve(manifest)

    assert result.success is True
    assert result.status == "reserved"
    assert manager.current_usage()["declared_reserved_ram_mb"] == 4


def test_ram_limit_rejects_module():
    manager = _manager(policy=ResourcePolicy(maximum_estimated_loaded_ram_mb=8))
    manifest = _manifest("voice", resources=ResourceDeclaration(estimated_ram_mb=12))

    result = manager.reserve(manifest)

    assert result.success is False
    assert result.status == RESOURCE_ERROR_RAM_BUDGET_EXCEEDED
    assert manager.list_reservations() == []


def test_network_required_module_denied_by_policy():
    manager = _manager(policy=ResourcePolicy(allow_network_required_modules=False))
    manifest = _manifest(
        "weather_real",
        resources=ResourceDeclaration(network_required=True),
    )

    result = manager.reserve(manifest)

    assert result.success is False
    assert result.status == RESOURCE_ERROR_NETWORK_REQUIRED_DENIED


def test_hardware_acceleration_required_module_denied_by_policy():
    manager = _manager(policy=ResourcePolicy(allow_hardware_accelerated_modules=False))
    manifest = _manifest(
        "vision_gpu",
        resources=ResourceDeclaration(hardware_acceleration_required=True),
    )

    result = manager.reserve(manifest)

    assert result.success is False
    assert result.status == RESOURCE_ERROR_HARDWARE_ACCELERATION_DENIED


def test_heavy_module_limit_rejects_second_heavy_module():
    manager = _manager(policy=ResourcePolicy(maximum_heavy_modules_loaded=1))
    first = _manifest("voice", resources=ResourceDeclaration(heavy_module=True))
    second = _manifest("vision", resources=ResourceDeclaration(heavy_module=True))

    assert manager.reserve(first).success is True
    result = manager.reserve(second)

    assert result.success is False
    assert result.status == RESOURCE_ERROR_HEAVY_MODULE_LIMIT
    assert manager.current_usage()["active_heavy_module_count"] == 1


def test_release_permits_later_activation():
    manager = _manager(policy=ResourcePolicy(maximum_heavy_modules_loaded=1))
    first = _manifest("voice", resources=ResourceDeclaration(heavy_module=True))
    second = _manifest("vision", resources=ResourceDeclaration(heavy_module=True))

    assert manager.reserve(first).success is True
    assert manager.release("voice").success is True
    assert manager.reserve(second).success is True

    assert manager.list_loaded_modules() == ["vision"]


def test_core_service_budget_rejection_does_not_activate_unrelated_city():
    manager = _manager(policy=ResourcePolicy(maximum_estimated_loaded_ram_mb=4))
    blocked = BudgetCity("blocked")
    idle = BudgetCity("idle")
    core = CoreService(
        resource_manager=manager,
        register_default_pc=False,
        register_default_voice=False,
    )
    core.register_service(
        "blocked",
        blocked,
        capabilities=["blocked.run"],
        manifest=_manifest(
            "blocked",
            "blocked.run",
            ResourceDeclaration(estimated_ram_mb=8),
        ),
    )
    core.register_service("idle", idle, capabilities=["idle.run"])

    result = core.route_by_capability("blocked.run", lambda city: city.handle("go"))

    assert result.success is False
    assert result.error_message == RESOURCE_ERROR_RAM_BUDGET_EXCEEDED
    assert blocked.start_count == 0
    assert idle.start_count == 0
    assert core.get_lifecycle_status("idle").data["lifecycle_status"]["state"] == "UNLOADED"


def test_failed_activation_does_not_leak_reservation():
    manager = _manager()
    city = BudgetCity("voice", fail_start=True)
    core = CoreService(
        resource_manager=manager,
        register_default_pc=False,
        register_default_voice=False,
    )
    core.register_service("voice", city, capabilities=["voice.text_loop"])

    result = core.route_by_capability("voice.text_loop", lambda service: service.handle("hi"))

    assert result.success is False
    assert manager.list_reservations() == []
    assert manager.current_usage()["active_task_count"] == 0


def test_execution_exception_releases_task_slot_and_reservation():
    manager = _manager()
    city = BudgetCity("voice")
    core = CoreService(
        resource_manager=manager,
        register_default_pc=False,
        register_default_voice=False,
    )
    core.register_service("voice", city, capabilities=["voice.text_loop"])

    result = core.route_by_capability(
        "voice.text_loop",
        lambda service: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert result.success is False
    assert manager.current_usage()["active_task_count"] == 0
    assert manager.list_reservations() == []


def test_global_task_limit_is_enforced():
    manager = _manager(policy=ResourcePolicy(maximum_concurrent_tasks=1))
    first = _manifest("first", resources=ResourceDeclaration(maximum_concurrent_tasks=2))
    second = _manifest("second", resources=ResourceDeclaration(maximum_concurrent_tasks=2))
    manager.reserve(first)
    manager.reserve(second)
    assert manager.acquire_task(first, "task-1").success is True

    result = manager.acquire_task(second, "task-2")

    assert result.success is False
    assert result.status == RESOURCE_ERROR_GLOBAL_TASK_LIMIT


def test_per_module_task_limit_is_enforced():
    manager = _manager()
    manifest = _manifest(
        "voice",
        resources=ResourceDeclaration(maximum_concurrent_tasks=1),
    )
    manager.reserve(manifest)
    assert manager.acquire_task(manifest, "task-1").success is True

    result = manager.acquire_task(manifest, "task-2")

    assert result.success is False
    assert result.status == RESOURCE_ERROR_MODULE_TASK_LIMIT


def test_persistent_module_is_not_idle_unloaded():
    clock = ManualClock()
    manager = _manager(
        policy=ResourcePolicy(default_inactivity_timeout_seconds=5),
        clock=clock,
    )
    manifest = _manifest(
        "memory",
        resources=ResourceDeclaration(persistent_module=True),
    )
    manager.reserve(manifest)
    clock.advance(60)

    assert manager.find_inactive_modules() == []


def test_active_module_is_not_idle_unloaded():
    clock = ManualClock()
    manager = _manager(
        policy=ResourcePolicy(default_inactivity_timeout_seconds=5),
        clock=clock,
    )
    manifest = _manifest("voice")
    manager.reserve(manifest)
    manager.acquire_task(manifest, "active-task")
    clock.advance(60)

    assert manager.find_inactive_modules() == []


def test_inactive_non_persistent_module_is_detected_deterministically():
    clock = ManualClock()
    manager = _manager(
        policy=ResourcePolicy(default_inactivity_timeout_seconds=5),
        clock=clock,
    )
    manager.reserve(_manifest("voice"))
    clock.advance(4.9)
    assert manager.find_inactive_modules() == []

    clock.advance(0.1)
    inactive = manager.find_inactive_modules()

    assert [item["module_name"] for item in inactive] == ["voice"]
    assert inactive[0]["idle_seconds"] == 5.0


def test_core_service_maintenance_tick_unloads_inactive_module():
    clock = ManualClock()
    manager = _manager(
        policy=ResourcePolicy(default_inactivity_timeout_seconds=5),
        clock=clock,
    )
    city = BudgetCity("voice")
    core = CoreService(
        resource_manager=manager,
        register_default_pc=False,
        register_default_voice=False,
    )
    core.register_service("voice", city, capabilities=["voice.text_loop"])
    assert core.route_by_capability("voice.text_loop", lambda service: service.handle("hi")).success
    clock.advance(5)

    maintenance = core.run_resource_maintenance()

    assert maintenance.success is True
    assert maintenance.data["unloaded"][0]["module_name"] == "voice"
    assert manager.list_reservations() == []
    assert city.stop_count == 1


def test_safe_eviction_selects_longest_inactive_low_priority_module():
    clock = ManualClock()
    manager = _manager(clock=clock)
    old = _manifest(
        "old",
        resources=ResourceDeclaration(
            estimated_ram_mb=2,
            task_priority=TASK_PRIORITY_LOW,
        ),
    )
    newer = _manifest(
        "newer",
        resources=ResourceDeclaration(
            estimated_ram_mb=20,
            task_priority=TASK_PRIORITY_LOW,
        ),
    )
    incoming = _manifest(
        "incoming",
        resources=ResourceDeclaration(task_priority=TASK_PRIORITY_HIGH),
    )
    manager.reserve(old)
    clock.advance(10)
    manager.reserve(newer)
    clock.advance(1)

    result = manager.select_eviction_candidate(incoming)

    assert result.success is True
    assert result.module_name == "old"


def test_active_module_is_never_evicted():
    manager = _manager()
    active = _manifest("active", resources=ResourceDeclaration(task_priority=TASK_PRIORITY_LOW))
    incoming = _manifest(
        "incoming",
        resources=ResourceDeclaration(task_priority=TASK_PRIORITY_HIGH),
    )
    manager.reserve(active)
    manager.acquire_task(active, "task")

    result = manager.select_eviction_candidate(incoming)

    assert result.success is False
    assert result.status == RESOURCE_ERROR_NO_EVICTION_CANDIDATE


def test_critical_module_is_never_evicted():
    manager = _manager()
    critical = _manifest(
        "critical",
        resources=ResourceDeclaration(task_priority=TASK_PRIORITY_CRITICAL),
    )
    incoming = _manifest(
        "incoming",
        resources=ResourceDeclaration(task_priority=TASK_PRIORITY_HIGH),
    )
    manager.reserve(critical)

    result = manager.select_eviction_candidate(incoming)

    assert result.success is False
    assert result.status == RESOURCE_ERROR_NO_EVICTION_CANDIDATE


def test_unsafe_stop_module_is_never_evicted():
    manager = _manager()
    unsafe = _manifest(
        "unsafe",
        resources=ResourceDeclaration(
            task_priority=TASK_PRIORITY_LOW,
            safe_to_stop=False,
        ),
    )
    incoming = _manifest(
        "incoming",
        resources=ResourceDeclaration(task_priority=TASK_PRIORITY_HIGH),
    )
    manager.reserve(unsafe)

    result = manager.select_eviction_candidate(incoming)

    assert result.success is False
    assert result.status == RESOURCE_ERROR_NO_EVICTION_CANDIDATE


def test_lower_priority_request_cannot_evict_higher_priority_module():
    manager = _manager()
    high = _manifest(
        "high",
        resources=ResourceDeclaration(task_priority=TASK_PRIORITY_HIGH),
    )
    incoming = _manifest(
        "incoming",
        resources=ResourceDeclaration(task_priority=TASK_PRIORITY_LOW),
    )
    manager.reserve(high)

    result = manager.select_eviction_candidate(incoming)

    assert result.success is False
    assert result.status == RESOURCE_ERROR_NO_EVICTION_CANDIDATE


def test_higher_priority_request_can_evict_lower_priority_when_enabled():
    clock = ManualClock()
    manager = _manager(
        policy=ResourcePolicy(
            maximum_estimated_loaded_ram_mb=10,
            eviction_enabled=True,
        ),
        clock=clock,
    )
    core = CoreService(
        resource_manager=manager,
        register_default_pc=False,
        register_default_voice=False,
    )
    low_city = BudgetCity("low")
    high_city = BudgetCity("high")
    core.register_service(
        "low",
        low_city,
        capabilities=["low.run"],
        manifest=_manifest(
            "low",
            "low.run",
            ResourceDeclaration(
                estimated_ram_mb=8,
                task_priority=TASK_PRIORITY_LOW,
            ),
        ),
    )
    core.register_service(
        "high",
        high_city,
        capabilities=["high.run"],
        manifest=_manifest(
            "high",
            "high.run",
            ResourceDeclaration(
                estimated_ram_mb=8,
                task_priority=TASK_PRIORITY_HIGH,
            ),
        ),
    )
    assert core.route_by_capability("low.run", lambda service: service.handle("old")).success
    clock.advance(10)

    result = core.route_by_capability("high.run", lambda service: service.handle("new"))

    assert result.success is True
    assert manager.list_loaded_modules() == ["high"]
    assert low_city.stop_count == 1
    assert result.data["resource_management"]["reservation"]["data"]["evicted_module"] == "low"


def test_eviction_disabled_means_no_automatic_eviction():
    manager = _manager(
        policy=ResourcePolicy(
            maximum_estimated_loaded_ram_mb=10,
            eviction_enabled=False,
        )
    )
    manager.reserve(
        _manifest(
            "low",
            resources=ResourceDeclaration(
                estimated_ram_mb=8,
                task_priority=TASK_PRIORITY_LOW,
            ),
        )
    )

    result = manager.reserve(
        _manifest(
            "high",
            resources=ResourceDeclaration(
                estimated_ram_mb=8,
                task_priority=TASK_PRIORITY_HIGH,
            ),
        )
    )

    assert result.success is False
    assert result.status == RESOURCE_ERROR_RAM_BUDGET_EXCEEDED


def test_cancellation_releases_task_slot():
    manager = _manager()
    manifest = _manifest("voice")
    token = CancellationToken(task_id="task-1", supports_cancellation=True)
    manager.reserve(manifest)
    manager.acquire_task(manifest, "task-1")

    result = manager.cancel_task("voice", token, reason="owner_cancelled")

    assert result.success is True
    assert manager.current_usage()["active_task_count"] == 0
    assert token.requested is True


def test_unsupported_cancellation_is_rejected_safely():
    token = CancellationToken(task_id="task-1", supports_cancellation=False)

    result = token.cancel("no")

    assert result.success is False
    assert result.status == RESOURCE_ERROR_CANCELLATION_UNSUPPORTED


def test_process_metrics_unavailable_path_remains_functional():
    manager = _manager(metrics_provider=lambda: {"process_rss_bytes": None})

    metrics = manager.observed_process_metrics()

    assert metrics["process_rss_bytes"] is None


def test_status_inspection_does_not_activate_modules():
    city = BudgetCity("voice")
    core = CoreService(register_default_pc=False, register_default_voice=False)
    core.register_service("voice", city, capabilities=["voice.text_loop"])

    status = core.get_resource_status()
    loaded = core.list_loaded_modules()

    assert status.success is True
    assert loaded.data["loaded_modules"] == []
    assert city.start_count == 0
    assert core.get_lifecycle_status("voice").data["lifecycle_status"]["state"] == "UNLOADED"


def test_event_records_contain_no_secrets_or_transcripts(tmp_path):
    history = EventHistoryStore(path=tmp_path / "events.json")
    manager = _manager(
        policy=ResourcePolicy(maximum_estimated_loaded_ram_mb=1),
        history=history,
    )
    manager.reserve(
        _manifest(
            "secret",
            resources=ResourceDeclaration(estimated_ram_mb=2),
        )
    )

    record = history.recent()[0]
    payload = record.event["payload"]

    assert record.type == RESOURCE_EVENT_ACTIVATION_DENIED
    assert "api_key" not in str(payload)
    assert "transcript" not in str(payload).lower()


def test_resource_events_record_reservation_creation(tmp_path):
    history = EventHistoryStore(path=tmp_path / "events.json")
    manager = _manager(history=history)

    manager.reserve(_manifest("voice"))

    assert history.recent()[0].type == RESOURCE_EVENT_RESERVATION_CREATED


def test_health_and_fallback_still_function_under_budgets():
    manager = _manager(policy=ResourcePolicy(maximum_estimated_loaded_ram_mb=64))
    manager.reserve(_manifest("voice", resources=ResourceDeclaration(estimated_ram_mb=4)))
    fallback = AdapterFallbackPolicy()
    failed = MockSpeechToTextAdapter(source="failed", fail=True)
    healthy = MockSpeechToTextAdapter(source="healthy", transcripts=["hello"])
    candidates = [
        AdapterCandidate("failed", failed, capabilities=["voice.transcribe"]),
        AdapterCandidate("healthy", healthy, capabilities=["voice.transcribe"]),
    ]

    result = fallback.select(candidates, "voice.transcribe")

    assert result.success is True
    assert result.selected_adapter_name == "healthy"
    assert manager.current_usage()["declared_reserved_ram_mb"] == 4


def test_simulated_voice_pipeline_still_works_with_resource_budgets():
    core = CoreService(register_default_pc=False)
    pipeline = VoicePipeline(
        microphone_adapter=MockMicrophoneAdapter(chunks=[b"\x01"]),
        speech_to_text_adapter=MockSpeechToTextAdapter(transcripts=["hello"]),
        output_adapter=MockVoiceOutputAdapter(),
        command_handler=lambda text: "Hello.",
        core_service=core,
    )

    result = pipeline.run_once(session_id="session", correlation_id="corr")

    assert result.success is True
    assert result.response_text == "Hello."
    assert core.get_resource_status().data["current_usage"]["active_module_count"] == 1
    assert core.get_resource_status().data["current_usage"]["active_task_count"] == 0


def test_text_fallback_still_works_when_primary_stt_unavailable():
    primary = MockSpeechToTextAdapter(source="primary", available=False)
    fallback = MockSpeechToTextAdapter(source="fallback", transcripts=["hello"])
    output = MockVoiceOutputAdapter()
    policy = AdapterFallbackPolicy()
    pipeline = VoicePipeline(
        microphone_adapter=MockMicrophoneAdapter(chunks=[b"\x01"]),
        speech_to_text_adapter=primary,
        output_adapter=output,
        command_handler=lambda text: "Fallback response.",
        fallback_policy=policy,
        speech_to_text_candidates=[
            AdapterCandidate("primary", primary, capabilities=["voice.transcribe"]),
            AdapterCandidate("fallback", fallback, capabilities=["voice.transcribe"]),
        ],
    )

    result = pipeline.run_once(session_id="session", correlation_id="corr")

    assert result.success is True
    assert result.response_text == "Fallback response."
    assert result.data["transcription"]["data"]["selected_adapter_name"] == "fallback"


def test_platform_profiles_are_available_and_bounded():
    profiles = {
        name: resource_policy_for_profile(name)
        for name in {
            RESOURCE_PROFILE_TEST,
            RESOURCE_PROFILE_RASPBERRY_PI_5,
            RESOURCE_PROFILE_DESKTOP,
            RESOURCE_PROFILE_FUTURE_ORIN,
        }
    }

    assert profiles[RESOURCE_PROFILE_RASPBERRY_PI_5].maximum_heavy_modules_loaded == 1
    assert profiles[RESOURCE_PROFILE_DESKTOP].maximum_concurrent_tasks > profiles[RESOURCE_PROFILE_TEST].maximum_concurrent_tasks
    assert profiles[RESOURCE_PROFILE_FUTURE_ORIN].platform_profile == RESOURCE_PROFILE_FUTURE_ORIN


def test_invalid_resource_policy_fails_safely():
    with pytest.raises(ValueError, match="maximum_concurrent_tasks"):
        ResourcePolicy(maximum_concurrent_tasks=0)
