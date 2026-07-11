from pathlib import Path

import pytest

from core import (
    CIRCUIT_CLOSED,
    CIRCUIT_HALF_OPEN,
    CIRCUIT_OPEN,
    HEALTH_ERROR_CIRCUIT_OPEN,
    HEALTH_ERROR_DISABLED_ADAPTER,
    HEALTH_ERROR_EXCEPTION,
    HEALTH_ERROR_FALLBACK_EXHAUSTED,
    HEALTH_ERROR_INCOMPATIBLE_INTERFACE_VERSION,
    HEALTH_ERROR_MALFORMED_RESULT,
    HEALTH_ERROR_MISSING_CAPABILITY,
    HEALTH_ERROR_NO_HEALTHY_ADAPTER,
    HEALTH_ERROR_RETRY_UNSAFE,
    HEALTH_ERROR_TIMEOUT,
    HEALTH_EVENT_FALLBACK_SELECTED,
    HEALTH_STATUS_DEGRADED,
    HEALTH_STATUS_HEALTHY,
    HEALTH_STATUS_UNAVAILABLE,
    LIFECYCLE_UNLOADED,
    PC_SERVICE_NAME,
    RETRY_SAFE,
    RETRY_UNSAFE,
    VOICE_SERVICE_NAME,
    AdapterCandidate,
    AdapterFallbackPolicy,
    CircuitBreaker,
    CoreService,
    HealthCache,
    HealthPolicyConfig,
    HealthResult,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockVoiceOutputAdapter,
    PCServiceResult,
    VoicePipeline,
    check_component_health,
)
from events import EventHistoryStore


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class FakeAdapter:
    def __init__(
        self,
        name,
        status=HEALTH_STATUS_HEALTHY,
        capabilities=("voice.transcribe",),
        health_exception=None,
        malformed=False,
        operation_success=True,
    ):
        self.name = name
        self.capabilities = tuple(capabilities)
        self.status = status
        self.health_exception = health_exception
        self.malformed = malformed
        self.operation_success = operation_success
        self.health_calls = 0
        self.operation_calls = 0

    def health_check(self):
        self.health_calls += 1
        if self.health_exception:
            raise self.health_exception
        if self.malformed:
            return "bad health"
        success = self.status in {HEALTH_STATUS_HEALTHY, HEALTH_STATUS_DEGRADED}
        return {
            "success": success,
            "status": self.status,
            "text": f"{self.name} health is {self.status}",
            "data": {
                "available": success,
                "status": self.status,
                "api_key": "SHOULD_NOT_LEAK",
                "transcript": "private words",
            },
            "metadata": {
                "safe": True,
                "api_key": "SHOULD_NOT_LEAK",
                "transcript": "private words",
            },
        }

    def execute(self):
        self.operation_calls += 1
        if self.operation_success:
            return {
                "success": True,
                "status": "handled",
                "text": f"{self.name} handled request",
                "data": {"adapter": self.name},
            }
        return {
            "success": False,
            "status": "runtime_failed",
            "error_message": f"{self.name} runtime failed",
        }


class CountingHealthService:
    def __init__(self):
        self.status_calls = 0
        self.handle_calls = 0

    def get_status(self):
        self.status_calls += 1
        return PCServiceResult(
            success=True,
            text="Counting service status ok.",
            data={"status": HEALTH_STATUS_HEALTHY, "source": "counting_service"},
            metadata={"safe": True},
        )

    def get_capabilities(self):
        return PCServiceResult(success=True, data={"source": "counting_service"})

    def handle(self):
        self.handle_calls += 1
        return PCServiceResult(success=True, text="handled")


def _candidate(adapter, **overrides):
    return AdapterCandidate(
        name=adapter.name,
        adapter=adapter,
        capabilities=list(adapter.capabilities),
        **overrides,
    )


def test_healthy_primary_selected():
    primary = FakeAdapter("primary")
    secondary = FakeAdapter("secondary")
    policy = AdapterFallbackPolicy()

    result = policy.select([_candidate(primary), _candidate(secondary)], "voice.transcribe")

    assert result.success is True
    assert result.selected_adapter_name == "primary"
    assert result.selected_health.status == HEALTH_STATUS_HEALTHY
    assert primary.health_calls == 1
    assert secondary.health_calls == 0


def test_failed_primary_falls_back_to_healthy_secondary():
    primary = FakeAdapter("primary", status="failed")
    secondary = FakeAdapter("secondary")
    policy = AdapterFallbackPolicy()

    result = policy.select([_candidate(primary), _candidate(secondary)], "voice.transcribe")

    assert result.success is True
    assert result.selected_adapter_name == "secondary"
    assert [rejection.reason for rejection in result.rejections] == ["failed"]


def test_unhealthy_secondary_skipped_when_primary_also_fails():
    primary = FakeAdapter("primary", status="failed")
    secondary = FakeAdapter("secondary", status=HEALTH_STATUS_UNAVAILABLE)
    policy = AdapterFallbackPolicy()

    result = policy.select([_candidate(primary), _candidate(secondary)], "voice.transcribe")

    assert result.success is False
    assert result.error_code == HEALTH_ERROR_NO_HEALTHY_ADAPTER
    assert [rejection.adapter_name for rejection in result.rejections] == [
        "primary",
        "secondary",
    ]


def test_disabled_adapter_skipped_before_health_check():
    adapter = FakeAdapter("primary")
    policy = AdapterFallbackPolicy()

    result = policy.select(
        [AdapterCandidate(name="primary", adapter=adapter, capabilities=["voice.transcribe"], enabled=False)],
        "voice.transcribe",
    )

    assert result.success is False
    assert result.rejections[0].reason == HEALTH_ERROR_DISABLED_ADAPTER
    assert adapter.health_calls == 0


def test_incompatible_interface_version_skipped():
    adapter = FakeAdapter("primary")
    policy = AdapterFallbackPolicy()

    result = policy.select(
        [_candidate(adapter, interface_version="v2")],
        "voice.transcribe",
        required_interface_version="v1",
    )

    assert result.success is False
    assert result.rejections[0].reason == HEALTH_ERROR_INCOMPATIBLE_INTERFACE_VERSION
    assert adapter.health_calls == 0


def test_missing_capability_skipped():
    adapter = FakeAdapter("primary", capabilities=("voice.capture",))
    policy = AdapterFallbackPolicy()

    result = policy.select([_candidate(adapter)], "voice.transcribe")

    assert result.success is False
    assert result.rejections[0].reason == HEALTH_ERROR_MISSING_CAPABILITY
    assert adapter.health_calls == 0


def test_degraded_adapter_rejected_by_strict_policy():
    adapter = FakeAdapter("primary", status=HEALTH_STATUS_DEGRADED)
    policy = AdapterFallbackPolicy(config=HealthPolicyConfig(allow_degraded=False))

    result = policy.select([_candidate(adapter)], "voice.transcribe")

    assert result.success is False
    assert result.rejections[0].reason == HEALTH_STATUS_DEGRADED


def test_degraded_adapter_accepted_by_permissive_policy():
    adapter = FakeAdapter("primary", status=HEALTH_STATUS_DEGRADED)
    policy = AdapterFallbackPolicy(config=HealthPolicyConfig(allow_degraded=True))

    result = policy.select([_candidate(adapter)], "voice.transcribe")

    assert result.success is True
    assert result.selected_adapter_name == "primary"
    assert result.selected_health.degraded is True


def test_all_candidates_unavailable_returns_structured_failure():
    adapter = FakeAdapter("primary", status=HEALTH_STATUS_UNAVAILABLE)
    policy = AdapterFallbackPolicy()

    result = policy.select([_candidate(adapter)], "voice.transcribe")

    assert result.success is False
    assert result.status == "selection_failed"
    assert result.error_code == HEALTH_ERROR_NO_HEALTHY_ADAPTER
    assert result.to_dict()["rejections"][0]["health"]["status"] == HEALTH_STATUS_UNAVAILABLE


def test_rejection_reasons_are_included():
    disabled = FakeAdapter("disabled")
    wrong_version = FakeAdapter("wrong_version")
    policy = AdapterFallbackPolicy()

    result = policy.select(
        [
            AdapterCandidate(
                name="disabled",
                adapter=disabled,
                capabilities=["voice.transcribe"],
                enabled=False,
            ),
            _candidate(wrong_version, interface_version="v2"),
        ],
        "voice.transcribe",
    )

    assert result.success is False
    assert [rejection.reason for rejection in result.rejections] == [
        HEALTH_ERROR_DISABLED_ADAPTER,
        HEALTH_ERROR_INCOMPATIBLE_INTERFACE_VERSION,
    ]


def test_fallback_attempt_count_is_bounded():
    adapters = [
        FakeAdapter("one", operation_success=False),
        FakeAdapter("two", operation_success=False),
        FakeAdapter("three", operation_success=False),
    ]
    policy = AdapterFallbackPolicy(config=HealthPolicyConfig(max_fallback_attempts=2))

    result = policy.execute(
        [_candidate(adapter) for adapter in adapters],
        "voice.transcribe",
        lambda adapter: adapter.execute(),
        retry_safety=RETRY_SAFE,
    )

    assert result.success is False
    assert result.status == HEALTH_ERROR_FALLBACK_EXHAUSTED
    assert result.attempts == 2
    assert sum(adapter.operation_calls for adapter in adapters) == 2


def test_runtime_retry_safe_failure_can_fall_back():
    primary = FakeAdapter("primary", operation_success=False)
    secondary = FakeAdapter("secondary", operation_success=True)
    policy = AdapterFallbackPolicy()

    result = policy.execute(
        [_candidate(primary), _candidate(secondary)],
        "voice.transcribe",
        lambda adapter: adapter.execute(),
        retry_safety=RETRY_SAFE,
    )

    assert result.success is True
    assert result.selected_adapter_name == "secondary"
    assert result.original_error == "primary runtime failed"
    assert primary.operation_calls == 1
    assert secondary.operation_calls == 1


def test_runtime_retry_unsafe_failure_does_not_fall_back():
    primary = FakeAdapter("primary", operation_success=False)
    secondary = FakeAdapter("secondary", operation_success=True)
    policy = AdapterFallbackPolicy()

    result = policy.execute(
        [_candidate(primary), _candidate(secondary)],
        "voice.transcribe",
        lambda adapter: adapter.execute(),
        retry_safety=RETRY_UNSAFE,
    )

    assert result.success is False
    assert result.status == HEALTH_ERROR_RETRY_UNSAFE
    assert result.attempts == 1
    assert secondary.operation_calls == 0


def test_circuit_opens_after_configured_failures():
    adapter = FakeAdapter("primary", status="failed")
    circuit = CircuitBreaker(failure_threshold=2)
    policy = AdapterFallbackPolicy(circuit_breaker=circuit)

    policy.select([_candidate(adapter)], "voice.transcribe")
    policy.select([_candidate(adapter)], "voice.transcribe")

    assert circuit.state("primary") == CIRCUIT_OPEN


def test_open_circuit_skips_adapter():
    adapter = FakeAdapter("primary")
    circuit = CircuitBreaker(failure_threshold=1)
    circuit.record_failure("primary", "boom")
    policy = AdapterFallbackPolicy(circuit_breaker=circuit)

    result = policy.select([_candidate(adapter)], "voice.transcribe")

    assert result.success is False
    assert result.rejections[0].reason == HEALTH_ERROR_CIRCUIT_OPEN
    assert adapter.health_calls == 0


def test_half_open_probe_succeeds_and_closes_circuit():
    clock = FakeClock()
    adapter = FakeAdapter("primary")
    circuit = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, clock=clock)
    circuit.record_failure("primary", "boom")
    clock.advance(10)
    policy = AdapterFallbackPolicy(circuit_breaker=circuit, clock=clock)

    result = policy.select([_candidate(adapter)], "voice.transcribe")

    assert result.success is True
    assert circuit.state("primary") == CIRCUIT_CLOSED


def test_half_open_probe_fails_and_reopens_circuit():
    clock = FakeClock()
    adapter = FakeAdapter("primary", status="failed")
    circuit = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, clock=clock)
    circuit.record_failure("primary", "boom")
    clock.advance(10)
    policy = AdapterFallbackPolicy(circuit_breaker=circuit, clock=clock)

    result = policy.select([_candidate(adapter)], "voice.transcribe")

    assert result.success is False
    assert circuit.state("primary") == CIRCUIT_OPEN


def test_circuit_breaker_uses_deterministic_injected_clock():
    clock = FakeClock()
    circuit = CircuitBreaker(failure_threshold=1, cooldown_seconds=5, clock=clock)
    circuit.record_failure("primary", "boom")

    assert circuit.state("primary") == CIRCUIT_OPEN
    clock.advance(4.9)
    assert circuit.state("primary") == CIRCUIT_OPEN
    clock.advance(0.1)
    assert circuit.state("primary") == CIRCUIT_HALF_OPEN


def test_health_cache_reused_inside_ttl():
    clock = FakeClock()
    adapter = FakeAdapter("primary")
    cache = HealthCache(ttl_seconds=10, clock=clock)
    policy = AdapterFallbackPolicy(cache=cache, clock=clock)

    first = policy.select([_candidate(adapter)], "voice.transcribe")
    second = policy.select([_candidate(adapter)], "voice.transcribe")

    assert first.success is True
    assert second.success is True
    assert adapter.health_calls == 1
    assert second.selected_health.metadata["cache_hit"] is True


def test_expired_health_cache_causes_new_check():
    clock = FakeClock()
    adapter = FakeAdapter("primary")
    cache = HealthCache(ttl_seconds=10, clock=clock)
    policy = AdapterFallbackPolicy(cache=cache, clock=clock)

    policy.select([_candidate(adapter)], "voice.transcribe")
    clock.advance(10.1)
    policy.select([_candidate(adapter)], "voice.transcribe")

    assert adapter.health_calls == 2


def test_forced_refresh_bypasses_cache():
    clock = FakeClock()
    adapter = FakeAdapter("primary")
    cache = HealthCache(ttl_seconds=10, clock=clock)
    policy = AdapterFallbackPolicy(cache=cache, clock=clock)

    policy.select([_candidate(adapter)], "voice.transcribe")
    policy.select([_candidate(adapter)], "voice.transcribe", force_refresh=True)

    assert adapter.health_calls == 2


def test_disabling_adapter_invalidates_cached_health():
    clock = FakeClock()
    adapter = FakeAdapter("primary")
    cache = HealthCache(ttl_seconds=10, clock=clock)
    policy = AdapterFallbackPolicy(cache=cache, clock=clock)

    policy.select([_candidate(adapter)], "voice.transcribe")
    disabled_result = policy.select(
        [AdapterCandidate(name="primary", adapter=adapter, capabilities=["voice.transcribe"], enabled=False)],
        "voice.transcribe",
    )
    enabled_again = policy.select([_candidate(adapter)], "voice.transcribe")

    assert disabled_result.success is False
    assert adapter.health_calls == 2
    assert enabled_again.success is True


def test_malformed_health_response_rejected():
    adapter = FakeAdapter("primary", malformed=True)

    result = check_component_health(adapter, "primary", "adapter", ["voice.transcribe"])

    assert result.healthy is False
    assert result.error_code == HEALTH_ERROR_MALFORMED_RESULT


def test_timeout_converted_to_structured_failure():
    adapter = FakeAdapter("primary", health_exception=TimeoutError("too slow"))

    result = check_component_health(adapter, "primary", "adapter", ["voice.transcribe"])

    assert result.healthy is False
    assert result.error_code == HEALTH_ERROR_TIMEOUT


def test_exception_converted_to_structured_failure():
    adapter = FakeAdapter("primary", health_exception=RuntimeError("boom"))

    result = check_component_health(adapter, "primary", "adapter", ["voice.transcribe"])

    assert result.healthy is False
    assert result.error_code == HEALTH_ERROR_EXCEPTION
    assert "RuntimeError" in result.message


def test_fallback_event_stored_without_secret_or_transcript_content(tmp_path: Path):
    history = EventHistoryStore(path=tmp_path / "events.json")
    adapter = FakeAdapter("primary")
    policy = AdapterFallbackPolicy(event_history_store=history)

    result = policy.select([_candidate(adapter)], "voice.transcribe")

    assert result.success is True
    stored = history.recent(type=HEALTH_EVENT_FALLBACK_SELECTED)[0].to_dict()
    payload_text = repr(stored["event"]["payload"])
    assert "SHOULD_NOT_LEAK" not in payload_text
    assert "private words" not in payload_text
    assert stored["source"] == "health"


def test_core_service_health_inspection_does_not_activate_unrelated_cities():
    core_service = CoreService()

    result = core_service.get_capability_health("voice.text_loop")

    assert result.success is True
    assert result.data["services"][0]["component_name"] == VOICE_SERVICE_NAME
    assert core_service.get_lifecycle_status(VOICE_SERVICE_NAME).data["lifecycle_status"][
        "state"
    ] == LIFECYCLE_UNLOADED
    assert core_service.get_lifecycle_status(PC_SERVICE_NAME).data["lifecycle_status"][
        "state"
    ] == LIFECYCLE_UNLOADED


def test_list_service_health_does_not_start_every_heavy_module():
    service = CountingHealthService()
    core_service = CoreService(register_default_pc=False, register_default_voice=False)
    core_service.register_service("heavy", service, capabilities=["heavy.capability"])

    result = core_service.list_service_health()

    assert result.success is True
    assert service.status_calls == 0
    assert core_service.get_lifecycle_status("heavy").data["lifecycle_status"]["state"] == (
        LIFECYCLE_UNLOADED
    )


def test_service_health_probe_calls_status_without_lifecycle_activation():
    service = CountingHealthService()
    core_service = CoreService(register_default_pc=False, register_default_voice=False)
    core_service.register_service("heavy", service, capabilities=["heavy.capability"])

    result = core_service.get_service_health("heavy", probe=True)

    assert result.success is True
    assert service.status_calls == 1
    assert service.handle_calls == 0
    assert core_service.get_lifecycle_status("heavy").data["lifecycle_status"]["state"] == (
        LIFECYCLE_UNLOADED
    )


def test_brain_path_remains_unaware_of_selected_adapter():
    core_service = CoreService()

    result = core_service.get_service_health(VOICE_SERVICE_NAME)

    assert result.success is True
    assert "selected_adapter_name" not in repr(result.data)
    assert result.data["health"]["metadata"]["active_probe"] is False


def test_current_mock_voice_pipeline_remains_functional():
    pipeline = VoicePipeline(
        microphone_adapter=MockMicrophoneAdapter(chunks=[b"\x01"]),
        speech_to_text_adapter=MockSpeechToTextAdapter(transcripts=["hello"]),
        output_adapter=MockVoiceOutputAdapter(),
        command_handler=lambda text: "Hello.",
    )

    result = pipeline.run_once(session_id="session-health", correlation_id="corr-health")

    assert result.success is True
    assert result.status == "completed"
    assert result.response_text == "Hello."


def test_text_fallback_available_when_primary_stt_is_unavailable():
    microphone = MockMicrophoneAdapter(chunks=[b"\x01"])
    primary_stt = MockSpeechToTextAdapter(
        source="primary_stt",
        available=False,
    )
    fallback_stt = MockSpeechToTextAdapter(
        source="text_fallback",
        transcripts=["hello from fallback"],
    )
    output = MockVoiceOutputAdapter()
    pipeline = VoicePipeline(
        microphone_adapter=microphone,
        speech_to_text_adapter=primary_stt,
        speech_to_text_candidates=[
            AdapterCandidate(
                name="primary_stt",
                adapter=primary_stt,
                capabilities=["voice.transcribe"],
            ),
            AdapterCandidate(
                name="text_fallback",
                adapter=fallback_stt,
                capabilities=["voice.transcribe"],
            ),
        ],
        fallback_policy=AdapterFallbackPolicy(),
        output_adapter=output,
        command_handler=lambda text: f"handled {text}",
    )

    result = pipeline.run_once(session_id="session-fallback", correlation_id="corr-fallback")

    assert result.success is True
    assert result.response_text == "handled hello from fallback"
    assert result.data["transcription"]["data"]["selected_adapter_name"] == "text_fallback"
    assert primary_stt.transcription_count == 0
    assert fallback_stt.transcription_count == 1


def test_health_policy_config_rejects_unbounded_values():
    with pytest.raises(ValueError, match="max_fallback_attempts"):
        HealthPolicyConfig(max_fallback_attempts=0)
