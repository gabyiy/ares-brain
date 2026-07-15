from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from core import (
    BRAIN_ACTIVE,
    BRAIN_BOOTING,
    BRAIN_ERROR,
    BRAIN_INITIALIZING,
    BRAIN_PROCESSING,
    BRAIN_RESPONDING,
    BRAIN_RETURNING_TO_STANDBY,
    BRAIN_SHUTTING_DOWN,
    BRAIN_STANDBY,
    BRAIN_STOPPED,
    CONTRACT_BRAIN_SESSION_SNAPSHOT,
    CONTRACT_BRAIN_SESSION_TRANSITION_REQUEST,
    DEFAULT_CONTRACT_REGISTRY,
    EVENT_BRAIN_BOOT_STARTED,
    EVENT_BRAIN_INITIALIZATION_STARTED,
    EVENT_BRAIN_LIFECYCLE_ERROR,
    EVENT_BRAIN_PROCESSING_STARTED,
    EVENT_BRAIN_RESPONSE_STARTED,
    EVENT_BRAIN_RETURNING_TO_STANDBY,
    EVENT_BRAIN_SESSION_ACTIVATED,
    EVENT_BRAIN_SESSION_ACTIVITY_RECORDED,
    EVENT_BRAIN_SHUTDOWN_STARTED,
    EVENT_BRAIN_STANDBY_ENTERED,
    EVENT_BRAIN_STATE_TRANSITION_REJECTED,
    EVENT_BRAIN_STOPPED,
    BrainSessionConfig,
    BrainSessionManager,
    BrainSessionSnapshotV1,
    BrainSessionTransitionRequestV1,
    CoreService,
)
from events import EventHistoryStore
from scripts.manual_verify_brain_session_manager import run_verification


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class SequenceIds:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> str:
        self.count += 1
        return f"brain-session-test-{self.count}"


def _manager(**kwargs) -> BrainSessionManager:
    return BrainSessionManager(session_id_factory=SequenceIds(), **kwargs)


def _boot_to_standby(manager: BrainSessionManager) -> None:
    assert manager.begin_boot().success
    assert manager.begin_initialization().success
    assert manager.enter_standby().success


def _boot_to_active(manager: BrainSessionManager) -> None:
    _boot_to_standby(manager)
    assert manager.activate_session().success


def _manager_in_state(state: str) -> BrainSessionManager:
    manager = _manager()
    if state == BRAIN_STOPPED:
        return manager
    manager.begin_boot()
    if state == BRAIN_BOOTING:
        return manager
    manager.begin_initialization()
    if state == BRAIN_INITIALIZING:
        return manager
    manager.enter_standby()
    if state == BRAIN_STANDBY:
        return manager
    manager.activate_session()
    if state == BRAIN_ACTIVE:
        return manager
    manager.begin_processing()
    if state == BRAIN_PROCESSING:
        return manager
    manager.begin_responding()
    if state == BRAIN_RESPONDING:
        return manager
    if state == BRAIN_RETURNING_TO_STANDBY:
        manager.request_return_to_standby()
        return manager
    if state == BRAIN_ERROR:
        manager.report_failure(reason="test_unrecoverable", unrecoverable=True)
        return manager
    if state == BRAIN_SHUTTING_DOWN:
        manager.begin_shutdown()
        return manager
    raise AssertionError(f"unsupported fixture state: {state}")


def test_initial_snapshot_is_versioned_stopped_state():
    result = _manager().snapshot()

    assert isinstance(result, BrainSessionSnapshotV1)
    assert result.contract_name == CONTRACT_BRAIN_SESSION_SNAPSHOT
    assert result.contract_version == "v1"
    assert result.current_state == BRAIN_STOPPED
    assert result.session_id == ""
    assert result.metadata == {"safe": True, "source": "brain_session_manager"}


def test_required_boot_initialization_standby_flow():
    manager = _manager()

    assert manager.begin_boot().current_state == BRAIN_BOOTING
    assert manager.begin_initialization().current_state == BRAIN_INITIALIZING
    assert manager.enter_standby().current_state == BRAIN_STANDBY
    assert [(item.source_state, item.target_state) for item in manager.history()] == [
        (BRAIN_STOPPED, BRAIN_BOOTING),
        (BRAIN_BOOTING, BRAIN_INITIALIZING),
        (BRAIN_INITIALIZING, BRAIN_STANDBY),
    ]


def test_transition_timestamps_and_previous_state_use_injected_clock():
    clock = FakeClock()
    manager = _manager(clock=clock)
    initial = manager.snapshot()
    clock.advance(4)

    booting = manager.begin_boot(correlation_id="corr-clock")

    assert booting.previous_state == BRAIN_STOPPED
    assert booting.source_state == BRAIN_STOPPED
    assert booting.entered_at != initial.entered_at
    assert booting.entered_at == "2026-02-01T12:00:04Z"
    assert manager.history()[0].timestamp == booting.entered_at
    assert manager.events(EVENT_BRAIN_BOOT_STARTED)[0].payload["transitioned_at"] == booting.entered_at


def test_required_active_processing_responding_active_flow():
    manager = _manager()
    _boot_to_standby(manager)

    active = manager.activate_session()
    processing = manager.begin_processing()
    responding = manager.begin_responding()
    completed = manager.finish_response()

    assert active.session_id == "brain-session-test-1"
    assert processing.session_id == active.session_id
    assert responding.session_id == active.session_id
    assert completed.current_state == BRAIN_ACTIVE
    assert completed.session_id == active.session_id


@pytest.mark.parametrize(
    "source_state",
    [BRAIN_ACTIVE, BRAIN_PROCESSING, BRAIN_RESPONDING],
)
def test_operational_states_can_return_to_standby(source_state):
    manager = _manager_in_state(source_state)
    session_id = manager.session_id

    returning = manager.request_return_to_standby()
    standby = manager.complete_return_to_standby()

    assert returning.current_state == BRAIN_RETURNING_TO_STANDBY
    assert returning.session_id == session_id
    assert standby.current_state == BRAIN_STANDBY
    assert standby.session_id == ""


@pytest.mark.parametrize(
    "source_state",
    [
        BRAIN_BOOTING,
        BRAIN_INITIALIZING,
        BRAIN_STANDBY,
        BRAIN_ACTIVE,
        BRAIN_PROCESSING,
        BRAIN_RESPONDING,
        BRAIN_RETURNING_TO_STANDBY,
        BRAIN_ERROR,
    ],
)
def test_shutdown_is_supported_from_every_nonterminal_state(source_state):
    manager = _manager_in_state(source_state)

    shutdown = manager.begin_shutdown()
    stopped = manager.mark_stopped()

    assert shutdown.success is True
    assert shutdown.current_state == BRAIN_SHUTTING_DOWN
    assert stopped.success is True
    assert stopped.current_state == BRAIN_STOPPED
    assert stopped.session_id == ""


def test_illegal_transition_returns_failure_and_preserves_original_state():
    manager = _manager()
    before = manager.snapshot()

    result = manager.begin_processing(reason="illegal_test")

    assert result.success is False
    assert result.status == "transition_rejected"
    assert result.source_state == BRAIN_STOPPED
    assert result.requested_state == BRAIN_PROCESSING
    assert result.error_code == "illegal_state_transition"
    assert manager.state == before.current_state
    assert manager.history() == []


def test_direct_error_transition_is_rejected_in_favor_of_report_failure():
    manager = _manager()
    manager.begin_boot()
    request = BrainSessionTransitionRequestV1(
        requested_state=BRAIN_ERROR,
        reason="direct_error",
    )

    result = manager.transition(request)

    assert result.success is False
    assert result.error_code == "error_transition_requires_report_failure"
    assert manager.state == BRAIN_BOOTING


def test_activation_creates_unique_session_ids_across_sessions():
    manager = _manager()
    _boot_to_standby(manager)
    first = manager.activate_session().session_id
    manager.request_return_to_standby()
    manager.complete_return_to_standby()
    second = manager.activate_session().session_id

    assert first == "brain-session-test-1"
    assert second == "brain-session-test-2"
    assert first != second


def test_standby_clears_active_session_but_retains_last_activity_timestamp():
    clock = FakeClock()
    manager = _manager(clock=clock)
    _boot_to_active(manager)
    clock.advance(2)
    activity = manager.record_activity(reason="owner_interaction")
    last_activity = activity.last_activity_at

    manager.request_return_to_standby()
    standby = manager.complete_return_to_standby()

    assert standby.session_id == ""
    assert standby.last_activity_at == last_activity
    assert standby.inactivity_deadline_at == ""
    assert standby.inactivity_expired is False


def test_record_activity_updates_time_deadline_and_correlation():
    clock = FakeClock()
    manager = _manager(clock=clock)
    _boot_to_active(manager)
    before = manager.snapshot()
    clock.advance(5)

    result = manager.record_activity(
        correlation_id="corr-activity-1",
        reason="owner_interaction",
    )

    assert result.success is True
    assert result.status == "activity_recorded"
    assert result.correlation_id == "corr-activity-1"
    assert result.last_activity_at != before.last_activity_at
    assert result.inactivity_deadline_at.endswith("Z")
    assert result.metadata["activity_reason"] == "owner_interaction"


def test_inactivity_expiry_is_false_before_and_true_at_boundary():
    clock = FakeClock()
    manager = _manager(
        clock=clock,
        config=BrainSessionConfig(inactivity_timeout_seconds=30),
    )
    _boot_to_active(manager)

    clock.advance(29.999)
    assert manager.inactivity_expired() is False
    clock.advance(0.001)
    assert manager.inactivity_expired() is True
    assert manager.snapshot().inactivity_expired is True


def test_inactivity_query_does_not_transition_or_start_background_work():
    clock = FakeClock()
    manager = _manager(clock=clock)
    _boot_to_active(manager)
    history_count = len(manager.history())
    clock.advance(60)

    assert manager.inactivity_expired() is True
    assert manager.state == BRAIN_ACTIVE
    assert len(manager.history()) == history_count


def test_inactivity_is_not_expired_without_an_active_session():
    manager = _manager()
    _boot_to_standby(manager)

    assert manager.inactivity_expired() is False


def test_config_defaults_are_conservative_and_serializable():
    config = BrainSessionConfig()

    assert config.to_dict() == {
        "inactivity_timeout_seconds": 30.0,
        "maximum_consecutive_failures": 3,
    }


@pytest.mark.parametrize(
    "value",
    [0, -1, 3600.1, True, False, float("nan"), float("inf"), "30", None],
)
def test_config_rejects_invalid_inactivity_timeout(value):
    with pytest.raises(ValueError):
        BrainSessionConfig(inactivity_timeout_seconds=value)


@pytest.mark.parametrize("value", [0, -1, 21, True, False, 3.0, "3", None])
def test_config_rejects_invalid_failure_limit(value):
    with pytest.raises(ValueError):
        BrainSessionConfig(maximum_consecutive_failures=value)


def test_config_rejects_unknown_or_nonmapping_fields():
    with pytest.raises(ValueError, match="Unknown brain_session"):
        BrainSessionConfig.from_mapping({"unexpected": 1})
    with pytest.raises(ValueError, match="must be a mapping"):
        BrainSessionConfig.from_mapping("invalid")
    with pytest.raises(ValueError, match="Unknown brain_session"):
        BrainSessionConfig.from_mapping({1: 30})


def test_recoverable_failures_increment_without_changing_state():
    manager = _manager(config={"maximum_consecutive_failures": 3})
    _boot_to_active(manager)

    first = manager.report_failure(reason="temporary_failure")
    second = manager.report_failure(reason="temporary_failure")

    assert first.status == "failure_recorded"
    assert second.consecutive_failure_count == 2
    assert manager.state == BRAIN_ACTIVE


def test_maximum_consecutive_failures_enters_error():
    manager = _manager(config={"maximum_consecutive_failures": 2})
    _boot_to_active(manager)
    manager.report_failure(reason="temporary_failure")

    result = manager.report_failure(reason="temporary_failure")

    assert result.success is False
    assert result.current_state == BRAIN_ERROR
    assert result.error_code == "maximum_consecutive_failures_reached"
    assert result.consecutive_failure_count == 2


def test_explicit_unrecoverable_failure_enters_error_immediately():
    manager = _manager()
    _boot_to_active(manager)

    result = manager.report_failure(reason="lifecycle_corrupt", unrecoverable=True)

    assert result.current_state == BRAIN_ERROR
    assert result.error_code == "unrecoverable_lifecycle_failure"
    assert manager.history()[-1].target_state == BRAIN_ERROR


def test_activity_after_recoverable_failure_resets_consecutive_count():
    manager = _manager()
    _boot_to_active(manager)
    manager.report_failure(reason="temporary_failure")

    result = manager.record_activity(reason="successful_activity")

    assert result.consecutive_failure_count == 0


def test_unsafe_error_recovery_is_rejected_and_error_is_preserved():
    manager = _manager_in_state(BRAIN_ERROR)

    result = manager.recover_to_standby(recovery_safe=False)

    assert result.success is False
    assert result.error_code == "recovery_not_confirmed_safe"
    assert manager.state == BRAIN_ERROR


def test_explicit_safe_recovery_uses_returning_state_and_reaches_standby():
    manager = _manager_in_state(BRAIN_ERROR)

    result = manager.recover_to_standby(recovery_safe=True)

    assert result.success is True
    assert result.current_state == BRAIN_STANDBY
    assert result.session_id == ""
    assert [(item.source_state, item.target_state) for item in manager.history()][-2:] == [
        (BRAIN_ERROR, BRAIN_RETURNING_TO_STANDBY),
        (BRAIN_RETURNING_TO_STANDBY, BRAIN_STANDBY),
    ]


def test_activity_outside_active_session_is_rejected():
    manager = _manager()

    result = manager.record_activity()

    assert result.success is False
    assert result.error_code == "activity_not_allowed"
    assert manager.state == BRAIN_STOPPED


def test_unbounded_or_private_reason_is_rejected_without_event_leak():
    manager = _manager()
    private_reason = "favorite color is secret blue"

    result = manager.begin_boot(reason=private_reason)
    serialized_events = json.dumps([event.to_dict() for event in manager.events()])

    assert result.success is False
    assert result.error_code == "invalid_transition_reason"
    assert manager.state == BRAIN_STOPPED
    assert private_reason not in serialized_events


def test_unknown_target_is_redacted_from_rejection_event():
    manager = _manager()
    request = BrainSessionTransitionRequestV1(
        requested_state="private transcript favorite color blue",
        reason="bad_target",
    )

    result = manager.transition(request)
    event = manager.events(EVENT_BRAIN_STATE_TRANSITION_REJECTED)[0]

    assert result.requested_state == "INVALID"
    assert event.payload["requested_state"] == "INVALID"
    assert "favorite color" not in json.dumps(event.to_dict()).lower()


def test_required_lifecycle_events_are_emitted_with_session_identifiers():
    manager = _manager()
    _boot_to_active(manager)
    session_id = manager.session_id
    manager.begin_processing(correlation_id="corr-process")
    manager.begin_responding()
    manager.finish_response()
    manager.request_return_to_standby()
    manager.complete_return_to_standby()
    manager.begin_shutdown()
    manager.mark_stopped()

    event_types = [event.type for event in manager.events()]
    assert event_types == [
        EVENT_BRAIN_BOOT_STARTED,
        EVENT_BRAIN_INITIALIZATION_STARTED,
        EVENT_BRAIN_STANDBY_ENTERED,
        EVENT_BRAIN_SESSION_ACTIVATED,
        EVENT_BRAIN_PROCESSING_STARTED,
        EVENT_BRAIN_RESPONSE_STARTED,
        EVENT_BRAIN_SESSION_ACTIVITY_RECORDED,
        EVENT_BRAIN_RETURNING_TO_STANDBY,
        EVENT_BRAIN_STANDBY_ENTERED,
        EVENT_BRAIN_SHUTDOWN_STARTED,
        EVENT_BRAIN_STOPPED,
    ]
    processing = manager.events(EVENT_BRAIN_PROCESSING_STARTED)[0]
    assert processing.session_id == session_id
    assert processing.correlation_id == "corr-process"


def test_rejected_transition_and_lifecycle_error_events_are_observable():
    manager = _manager()
    manager.begin_processing(reason="illegal_test")
    manager.begin_boot()
    manager.report_failure(reason="startup_failed", unrecoverable=True)

    assert len(manager.events(EVENT_BRAIN_STATE_TRANSITION_REJECTED)) == 1
    error_event = manager.events(EVENT_BRAIN_LIFECYCLE_ERROR)[0]
    assert error_event.payload["target_state"] == BRAIN_ERROR
    assert error_event.payload["reason"] == "startup_failed"


def test_event_payloads_exclude_transcripts_owner_memory_and_secrets():
    manager = _manager()
    _boot_to_active(manager)
    manager.record_activity(reason="owner_interaction")

    payload = json.dumps([event.to_dict() for event in manager.events()]).lower()
    for forbidden in (
        "transcript",
        "owner_memory_value",
        "microphone_audio",
        "api_key",
        "password",
    ):
        assert forbidden not in payload


def test_lifecycle_events_can_be_stored_in_existing_event_history(tmp_path):
    history_store = EventHistoryStore(path=tmp_path / "event_history.json")
    manager = _manager(event_history_store=history_store)

    manager.begin_boot(correlation_id="corr-history")

    records = history_store.list()
    assert len(records) == 1
    assert records[0].type == EVENT_BRAIN_BOOT_STARTED
    assert records[0].event["correlation_id"] == "corr-history"
    assert records[0].result["metadata"] == {"safe": True}


def test_event_history_failure_does_not_reverse_transition():
    class FailingHistory:
        def add(self, event, result):
            raise OSError("history unavailable")

    manager = _manager(event_history_store=FailingHistory())

    result = manager.begin_boot()

    assert result.success is True
    assert manager.state == BRAIN_BOOTING
    assert manager.event_history_failures()[0]["error_type"] == "OSError"


def test_brain_session_contracts_are_registered_and_round_trip():
    request = BrainSessionTransitionRequestV1(
        correlation_id="corr-contract",
        requested_state=BRAIN_BOOTING,
        reason="boot_requested",
    )
    snapshot = BrainSessionSnapshotV1(
        correlation_id="corr-snapshot",
        current_state=BRAIN_STANDBY,
    )

    assert DEFAULT_CONTRACT_REGISTRY.validate(request).success is True
    assert DEFAULT_CONTRACT_REGISTRY.validate(snapshot).success is True
    assert BrainSessionTransitionRequestV1.from_dict(request.to_dict()) == request
    assert BrainSessionSnapshotV1.from_dict(snapshot.to_dict()) == snapshot
    assert CONTRACT_BRAIN_SESSION_TRANSITION_REQUEST in DEFAULT_CONTRACT_REGISTRY.list_contracts()
    assert "BrainSessionManager" in DEFAULT_CONTRACT_REGISTRY.consumers(
        CONTRACT_BRAIN_SESSION_SNAPSHOT
    )


def test_generic_versioned_transition_request_executes_legal_transition():
    manager = _manager()
    request = BrainSessionTransitionRequestV1(
        requested_state=BRAIN_BOOTING,
        reason="boot_requested",
    )

    result = manager.transition(request.to_dict())

    assert result.success is True
    assert result.current_state == BRAIN_BOOTING


def test_malformed_transition_contract_fails_closed():
    manager = _manager()

    result = manager.transition({"requested_state": BRAIN_BOOTING})

    assert result.success is False
    assert result.error_code == "invalid_transition_contract"
    assert manager.state == BRAIN_STOPPED


def test_concurrent_activation_allows_exactly_one_transition():
    manager = _manager()
    _boot_to_standby(manager)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: manager.activate_session(), range(24)))

    assert sum(result.success for result in results) == 1
    assert manager.state == BRAIN_ACTIVE
    assert manager.session_id == "brain-session-test-1"
    assert len(manager.events(EVENT_BRAIN_SESSION_ACTIVATED)) == 1


def test_concurrent_snapshot_reads_are_consistent_during_activity_updates():
    manager = _manager()
    _boot_to_active(manager)

    def read_or_update(index):
        if index % 5 == 0:
            return manager.record_activity(reason="concurrent_activity")
        return manager.snapshot()

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(read_or_update, range(100)))

    assert all(result.current_state == BRAIN_ACTIVE for result in results)
    assert all(result.session_id == "brain-session-test-1" for result in results)
    assert manager.snapshot().consecutive_failure_count == 0


def test_transition_history_is_bounded():
    manager = _manager()
    for _ in range(26):
        assert manager.begin_boot().success
        assert manager.begin_initialization().success
        assert manager.enter_standby().success
        assert manager.activate_session().success
        assert manager.request_return_to_standby().success
        assert manager.complete_return_to_standby().success
        assert manager.begin_shutdown().success
        assert manager.mark_stopped().success

    assert len(manager.history()) == 200
    assert manager.history()[-1].target_state == BRAIN_STOPPED


def test_core_service_owns_manager_without_activating_registered_cities():
    core = CoreService(register_default_pc=False, register_default_voice=False)

    snapshot = core.get_brain_session_snapshot()

    assert isinstance(core.brain_session_manager, BrainSessionManager)
    assert snapshot.current_state == BRAIN_STOPPED
    assert core.list_services() == []


def test_core_service_accepts_injected_manager_and_config():
    injected = _manager(config={"inactivity_timeout_seconds": 45})
    core = CoreService(
        register_default_pc=False,
        register_default_voice=False,
        brain_session_manager=injected,
    )
    configured = CoreService(
        register_default_pc=False,
        register_default_voice=False,
        brain_session_config={
            "inactivity_timeout_seconds": 60,
            "maximum_consecutive_failures": 4,
        },
    )

    assert core.brain_session_manager is injected
    assert configured.get_brain_session_snapshot().inactivity_timeout_seconds == 60
    assert configured.get_brain_session_snapshot().maximum_consecutive_failures == 4


def test_manager_has_no_voice_hardware_memory_or_skill_imports():
    source = (Path(__file__).parents[1] / "core" / "BrainSessionManager.py").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "LinuxAlsa",
        "Whisper",
        "Piper",
        "MicrophoneAdapter",
        "OwnerProfileStore",
        "SkillManager",
        "Thread(",
        "while True",
    ):
        assert forbidden not in source


def test_manual_verification_script_demonstrates_required_flow():
    output = []

    exit_code = run_verification(output.append)

    rendered = "\n".join(output)
    assert exit_code == 0
    assert "Initial state: STOPPED" in rendered
    assert "Rejected: STOPPED -> PROCESSING" in rendered
    assert "Inactivity: active before 30s; expired exactly at 30s" in rendered
    assert "Session ID cleared after returning to standby: PASS" in rendered
    assert "Event privacy check: PASS" in rendered
    assert "Brain Session Manager verification passed." in rendered
