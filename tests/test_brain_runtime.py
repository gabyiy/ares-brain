from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import Event as ThreadEvent, Lock, Thread
import time
from types import SimpleNamespace

import pytest

from core import (
    BRAIN_ACTIVE,
    BRAIN_PROCESSING,
    BRAIN_RESPONDING,
    BRAIN_RETURNING_TO_STANDBY,
    BRAIN_STANDBY,
    BRAIN_STOPPED,
    CONTRACT_BRAIN_RUNTIME_COMMAND_CLASSIFICATION,
    CONTRACT_BRAIN_RUNTIME_LOOP_RESULT,
    CONTRACT_BRAIN_RUNTIME_REQUEST,
    CONTRACT_BRAIN_RUNTIME_RESULT,
    CONTRACT_BRAIN_RUNTIME_SNAPSHOT,
    DEFAULT_CONTRACT_REGISTRY,
    EVENT_ACTIVATION_ACCEPTED,
    EVENT_ACTIVATION_REJECTED,
    EVENT_ACTIVATION_REQUESTED,
    EVENT_RUNTIME_COMMAND_COMPLETED,
    EVENT_RUNTIME_COMMAND_FAILED,
    EVENT_RUNTIME_COMMAND_STARTED,
    EVENT_RUNTIME_INACTIVITY_EXPIRED,
    EVENT_RUNTIME_INPUT_RECEIVED,
    EVENT_RUNTIME_SHUTDOWN_REQUESTED,
    EVENT_RUNTIME_STANDBY_REQUESTED,
    EVENT_RUNTIME_STARTED,
    EVENT_RUNTIME_STOPPED,
    BrainRuntime,
    BrainRuntimeCommandClassificationV1,
    BrainRuntimeConfig,
    BrainRuntimeLoopResultV1,
    BrainRuntimeRequestV1,
    BrainRuntimeResultV1,
    BrainRuntimeSnapshotV1,
    BrainSessionConfig,
    BrainSessionManager,
    CollectingRuntimeOutputAdapter,
    ConsoleRuntimeInputAdapter,
    ConsoleRuntimeOutputAdapter,
    ConversationContextManager,
    CoreService,
    QueuedRuntimeInputAdapter,
    RUNTIME_COMMAND_ACTIVATION,
    RUNTIME_COMMAND_ATTENTION_ONLY,
    RUNTIME_COMMAND_ORDINARY,
    RUNTIME_COMMAND_SHUTDOWN,
    RUNTIME_COMMAND_STANDBY,
    RuntimeInputResult,
    RuntimeOutputMessage,
    RuntimeOutputResult,
    normalize_runtime_phrase,
    validate_contract,
)
from events import EventBus as SkillEventBus, EventHistoryStore
from memory import (
    GoalsStore,
    MemoryStore,
    NotesStore,
    OwnerMemoryService,
    TasksStore,
    UserProfileStore,
)
from scripts import manual_verify_brain_runtime
from scripts import manual_verify_single_turn_voice as single_turn
from scripts import run_ares_brain_runtime_text


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)

    def rollback(self, seconds: float) -> None:
        self.current -= timedelta(seconds=seconds)


class SequenceIds:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.count = 0

    def __call__(self) -> str:
        self.count += 1
        return f"{self.prefix}-{self.count}"


def _runtime(
    *,
    handler=None,
    inputs=None,
    output=None,
    clock=None,
    config=None,
    event_bus=None,
    event_history_store=None,
):
    clock = clock or FakeClock()
    config = config or BrainRuntimeConfig()
    manager = BrainSessionManager(
        config=BrainSessionConfig(
            inactivity_timeout_seconds=config.inactivity_timeout_seconds,
            maximum_consecutive_failures=config.maximum_consecutive_failures,
        ),
        clock=clock,
        session_id_factory=SequenceIds("brain-session-test"),
    )
    core_service = CoreService(
        brain_session_manager=manager,
        register_default_pc=False,
        register_default_voice=False,
    )
    runtime = BrainRuntime(
        core_service=core_service,
        command_handler=handler or (lambda text: f"handled:{text}"),
        input_adapter=inputs or QueuedRuntimeInputAdapter(),
        output_adapter=output or CollectingRuntimeOutputAdapter(),
        config=config,
        event_bus=event_bus,
        event_history_store=event_history_store,
        clock=clock,
        runtime_id_factory=lambda: "brain-runtime-test",
    )
    return runtime, clock


def _real_runtime(tmp_path: Path):
    clock = FakeClock()
    skill_bus = SkillEventBus()
    manager = BrainSessionManager(
        config=BrainSessionConfig(),
        clock=clock,
        session_id_factory=SequenceIds("brain-session-real-route"),
    )
    owner_service = OwnerMemoryService(
        tmp_path / "owner_profile.json",
        event_bus=skill_bus,
        pending_path=tmp_path / "pending.json",
    )
    core_service = CoreService(
        owner_memory_service=owner_service,
        brain_session_manager=manager,
        register_default_pc=False,
        register_default_voice=False,
    )
    support = tmp_path / "support"
    skill_manager = single_turn.create_skill_manager(
        core_service,
        event_history_store=EventHistoryStore(support / "events.json"),
        event_bus=skill_bus,
        memory_store=MemoryStore(
            short_path=support / "short.json",
            long_path=support / "long.json",
            event_bus=skill_bus,
        ),
        profile_store=UserProfileStore(support / "profile.json", event_bus=skill_bus),
        goals_store=GoalsStore(support / "goals.json", event_bus=skill_bus),
        notes_store=NotesStore(support / "notes.json", event_bus=skill_bus),
        tasks_store=TasksStore(support / "tasks.json", event_bus=skill_bus),
        conversation_context=ConversationContextManager(),
    )
    output = CollectingRuntimeOutputAdapter()
    runtime = BrainRuntime(
        core_service=core_service,
        command_handler=single_turn.build_existing_brain_handler(skill_manager),
        input_adapter=QueuedRuntimeInputAdapter(),
        output_adapter=output,
        clock=clock,
        runtime_id_factory=lambda: "brain-runtime-real-route",
    )
    return runtime, output, skill_manager, owner_service


def _start_active(runtime: BrainRuntime):
    assert runtime.start().success
    result = runtime.handle_text("Ares")
    assert result.success
    assert runtime.session_manager.state == BRAIN_ACTIVE
    return runtime.session_manager.session_id


@pytest.mark.parametrize(
    "contract_name,contract_type",
    [
        (CONTRACT_BRAIN_RUNTIME_REQUEST, BrainRuntimeRequestV1),
        (CONTRACT_BRAIN_RUNTIME_RESULT, BrainRuntimeResultV1),
        (CONTRACT_BRAIN_RUNTIME_SNAPSHOT, BrainRuntimeSnapshotV1),
        (CONTRACT_BRAIN_RUNTIME_COMMAND_CLASSIFICATION, BrainRuntimeCommandClassificationV1),
        (CONTRACT_BRAIN_RUNTIME_LOOP_RESULT, BrainRuntimeLoopResultV1),
    ],
)
def test_runtime_contracts_are_versioned_and_registered(contract_name, contract_type):
    contract = contract_type()

    assert contract.contract_name == contract_name
    assert contract.contract_version == "v1"
    assert DEFAULT_CONTRACT_REGISTRY.current_version(contract_name) == "v1"
    assert validate_contract(contract, expected_contract_name=contract_name).success


def test_runtime_configuration_defaults_are_bounded_and_explicit():
    config = BrainRuntimeConfig()

    assert config.ares_name_aliases == ("ares", "aris", "aries")
    assert config.activation_phrases == ("ares", "hey ares", "hello ares", "wake up ares")
    assert config.standby_phrases == (
        "goodbye ares",
        "ares goodbye",
        "bye ares",
        "ares bye",
        "go to standby ares",
        "ares go to standby",
        "go to sleep ares",
        "ares go to sleep",
        "standby ares",
        "ares standby",
        "sleep ares",
        "ares sleep",
    )
    assert config.shutdown_phrases == (
        "shutdown ares",
        "ares shutdown",
        "turn off ares",
        "ares turn off",
        "power down ares",
        "ares power down",
    )
    assert config.active_acknowledgement == "Yes Gabi."
    assert config.already_active_acknowledgement == ""
    assert config.inactivity_timeout_seconds == 30.0
    assert config.maximum_consecutive_failures == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"activation_phrases": []},
        {"ares_name_aliases": []},
        {"ares_name_aliases": ["aris"]},
        {"activation_phrases": ["ares", "ARES!"]},
        {"activation_phrases": [1]},
        {"activation_phrases": "ares"},
        {"activation_phrases": ["x" * 65]},
        {"active_acknowledgement": ""},
        {"already_active_acknowledgement": 1},
        {"standby_response": "x" * 257},
        {"inactivity_timeout_seconds": 0},
        {"inactivity_timeout_seconds": True},
        {"inactivity_timeout_seconds": float("nan")},
        {"inactivity_timeout_seconds": float("inf")},
        {"inactivity_timeout_seconds": 3601},
        {"maximum_consecutive_failures": 0},
        {"maximum_consecutive_failures": True},
        {"maximum_consecutive_failures": 3.0},
        {"input_polling_interval_seconds": 0},
        {"input_polling_interval_seconds": 6},
        {"command_timeout_seconds": 0},
        {"command_timeout_seconds": 601},
        {"shutdown_phrases": ["Ares"]},
        {"standby_phrases": ["please ares standby"]},
        {"shutdown_phrases": ["please ares shutdown"]},
        {"standby_phrases": ["shutdown ares"], "shutdown_phrases": ["shutdown ares"]},
        {"standby_phrases": ["shut down ares"], "shutdown_phrases": ["shutdown ares"]},
    ],
)
def test_runtime_configuration_rejects_malformed_or_ambiguous_values(changes):
    values = BrainRuntimeConfig().to_dict()
    values.update(changes)

    with pytest.raises(ValueError):
        BrainRuntimeConfig.from_mapping(values)


def test_runtime_configuration_rejects_unknown_fields():
    with pytest.raises(ValueError, match="Unknown brain_runtime"):
        BrainRuntimeConfig.from_mapping({"unknown": True})


def test_runtime_configuration_must_match_session_manager():
    manager = BrainSessionManager(config=BrainSessionConfig(inactivity_timeout_seconds=20))
    core = CoreService(
        brain_session_manager=manager,
        register_default_pc=False,
        register_default_voice=False,
    )

    with pytest.raises(ValueError, match="must match BrainSessionManager"):
        BrainRuntime(
            core_service=core,
            command_handler=lambda text: text,
            input_adapter=QueuedRuntimeInputAdapter(),
            output_adapter=CollectingRuntimeOutputAdapter(),
        )


@pytest.mark.parametrize(
    "text,normalized",
    [
        (" ARES ", "ares"),
        ("Hey, Ares!", "hey ares"),
        ("GOODBYE, ARES.", "goodbye ares"),
        ("shut-down Ares", "shut down ares"),
    ],
)
def test_runtime_phrase_normalization_is_case_and_punctuation_tolerant(text, normalized):
    assert normalize_runtime_phrase(text) == normalized


@pytest.mark.parametrize(
    "text",
    [
        "compare statistics",
        "I read about Ares yesterday",
        "address this issue",
        "nearest shop",
        "Ares please calculate two plus two",
    ],
)
def test_activation_does_not_use_loose_substring_matching(text):
    runtime, _ = _runtime()

    classification = runtime.classify_command(text)

    assert classification.command_category == RUNTIME_COMMAND_ORDINARY


@pytest.mark.parametrize(
    "text",
    ["Ares", "Aris", " Aries. ", "HEY ARES!", " hello, Ares ", "Wake up, Ares."],
)
def test_activation_phrase_classification_is_exact_and_bounded(text):
    runtime, _ = _runtime()

    assert runtime.classify_command(text).command_category == RUNTIME_COMMAND_ACTIVATION


def test_runtime_alias_canonicalization_is_whole_token_only():
    runtime, _ = _runtime()

    assert runtime.classify_command("Aris").normalized_input == "ares"
    assert runtime.classify_command("goodbye Aris").normalized_input == "goodbye ares"
    assert runtime.classify_command("shutdown Aris").normalized_input == "shutdown ares"
    rs_shutdown = runtime.classify_command("Shut down RS")
    assert rs_shutdown.normalized_input == "shutdown ares"
    assert rs_shutdown.command_category == RUNTIME_COMMAND_SHUTDOWN
    for value in ("Paris", "Harris", "various"):
        classification = runtime.classify_command(value)
        assert classification.normalized_input == value.casefold()
        assert classification.command_category == RUNTIME_COMMAND_ORDINARY
    assert runtime.classify_command("Aries").normalized_input == "ares"
    ordinary_rs = runtime.classify_command("What does RS mean?")
    assert ordinary_rs.normalized_input == "what does rs mean"
    assert ordinary_rs.command_category == RUNTIME_COMMAND_ORDINARY


def test_runtime_boots_once_through_manager_to_standby():
    runtime, _ = _runtime()

    result = runtime.start()
    second = runtime.start()

    assert result.status == "started"
    assert runtime.session_manager.state == BRAIN_STANDBY
    assert second.status == "already_started"
    assert [record.target_state for record in runtime.session_manager.history()] == [
        "BOOTING",
        "INITIALIZING",
        "STANDBY",
    ]


def test_ordinary_command_is_ignored_in_standby_without_session_creation():
    called = []
    runtime, _ = _runtime(handler=lambda text: called.append(text))
    runtime.start()

    result = runtime.handle_text("calculate 2 plus 2")

    assert result.success is True
    assert result.status == "ignored_in_standby"
    assert runtime.session_manager.session_id == ""
    assert called == []


def test_activation_creates_session_and_acknowledges_exactly_once():
    output = CollectingRuntimeOutputAdapter()
    runtime, _ = _runtime(output=output)
    runtime.start()

    result = runtime.handle_text("hello, Ares")

    assert result.status == "activated"
    assert runtime.session_manager.session_id == "brain-session-test-1"
    assert output.texts == ["Yes Gabi."]


@pytest.mark.parametrize("phrase", ["Ares", "Aris", "RS", "Hey Ares", "Hello Ares"])
def test_active_name_only_is_attention_without_activity_or_session_replacement(phrase):
    output = CollectingRuntimeOutputAdapter()
    runtime, clock = _runtime(output=output)
    session_id = _start_active(runtime)
    first_activity = runtime.session_manager.snapshot().last_activity_at
    clock.advance(10)

    result = runtime.handle_text(phrase)

    assert result.status == "attention_only"
    assert result.command_category == RUNTIME_COMMAND_ATTENTION_ONLY
    assert runtime.session_manager.session_id == session_id
    assert runtime.session_manager.snapshot().last_activity_at == first_activity
    assert result.data["core_service_bypassed"] is True
    assert output.texts == ["Yes Gabi."]


def test_attention_only_spoken_response_requires_explicit_configuration():
    output = CollectingRuntimeOutputAdapter()
    runtime, _ = _runtime(
        output=output,
        config=BrainRuntimeConfig(
            already_active_acknowledgement="ARES is already active.",
        ),
    )
    session_id = _start_active(runtime)

    result = runtime.handle_text("Ares")

    assert result.status == "attention_only"
    assert result.response_text == "ARES is already active."
    assert runtime.session_manager.session_id == session_id
    assert output.texts == ["Yes Gabi.", "ARES is already active."]


def test_multiple_commands_use_same_session_and_required_state_flow():
    runtime, _ = _runtime(handler=lambda text: f"response:{text}")
    session_id = _start_active(runtime)

    first = runtime.handle_text("first command")
    second = runtime.handle_text("second command")

    assert first.response_text == "response:first command"
    assert second.response_text == "response:second command"
    assert runtime.session_manager.session_id == session_id
    transitions = [(item.source_state, item.target_state) for item in runtime.session_manager.history()]
    assert transitions.count((BRAIN_ACTIVE, BRAIN_PROCESSING)) == 2
    assert transitions.count((BRAIN_PROCESSING, BRAIN_RESPONDING)) == 2
    assert transitions.count((BRAIN_RESPONDING, BRAIN_ACTIVE)) == 2


def test_calculator_uses_real_production_skill_route(tmp_path):
    runtime, output, skill_manager, _ = _real_runtime(tmp_path)
    _start_active(runtime)

    result = runtime.handle_text("calculate 2 plus 2")

    assert result.success
    assert result.response_text == "Result: 4"
    assert result.data["selected_skill"] == "calculator"
    assert skill_manager.core_service is runtime.core_service
    assert skill_manager.last_plan is not None
    assert skill_manager.last_execution is not None
    assert output.texts[-1] == "Result: 4"


def test_active_address_removal_preserves_calculator_operators(tmp_path):
    runtime, output, _, _ = _real_runtime(tmp_path)
    session_id = _start_active(runtime)

    result = runtime.handle_text("Ares calculate 2 + 2")

    assert result.success
    assert result.response_text == "Result: 4"
    assert result.data["selected_skill"] == "calculator"
    assert runtime.session_manager.session_id == session_id
    assert output.texts == ["Yes Gabi.", "Result: 4"]


def test_owner_memory_add_recall_remove_confirmation_uses_real_core_route(tmp_path):
    runtime, _, _, service = _real_runtime(tmp_path)
    session_id = _start_active(runtime)

    saved = runtime.handle_text("Remember in long-term memory that I like strategy games.")
    recalled = runtime.handle_text("What do I like?")
    requested = runtime.handle_text("Forget that I like strategy games.")
    confirmed = runtime.handle_text("Yes, delete it.")
    missing = runtime.handle_text("What do you remember about strategy games?")

    assert saved.data["selected_skill"] == "owner_memory"
    assert "strategy games" in recalled.response_text.lower()
    assert "should i delete" in requested.response_text.lower()
    assert "deleted" in confirmed.response_text.lower()
    assert "do not have" in missing.response_text.lower()
    assert runtime.session_manager.session_id == session_id
    assert service.inspect(include_values=True)["memory_count"] == 0


def test_unsupported_active_command_returns_safe_response_and_runtime_survives(tmp_path):
    runtime, _, _, _ = _real_runtime(tmp_path)
    session_id = _start_active(runtime)

    result = runtime.handle_text("tell me something unsupported and obscure")

    assert result.success
    assert result.response_text == "I cannot handle that request yet."
    assert runtime.session_manager.state == BRAIN_ACTIVE
    assert runtime.session_manager.session_id == session_id


@pytest.mark.parametrize(
    "phrase",
    [
        "goodbye",
        "goodbye Ares",
        "goodbye, Aris",
        "goodbye Aries",
        "goodbye RS",
        "Ares goodbye",
        "RS goodbye",
        "good bye Ares",
        "bye Ares",
        "standby",
        "go to standby",
        "go to standby Ares",
        "go to standby Aris",
        "go to standby RS",
        "go to sleep Ares",
        "Ares go to sleep",
        "stand by Ares",
        "standby Ares",
        "standby Aris",
        "standby Aries",
        "standby RS",
        "Ares standby",
        "RS standby",
        "sleep Ares",
    ],
)
def test_standby_phrases_return_to_standby_without_stopping_runtime(phrase):
    runtime, _ = _runtime()
    _start_active(runtime)

    result = runtime.handle_text(phrase)

    assert result.status == "standby_entered"
    assert runtime.session_manager.state == BRAIN_STANDBY
    assert runtime.session_manager.session_id == ""


def test_standby_phrase_while_standby_is_noop_and_creates_no_session():
    runtime, _ = _runtime()
    runtime.start()

    result = runtime.handle_text("goodbye Ares")

    assert result.status == "already_in_standby"
    assert runtime.session_manager.state == BRAIN_STANDBY
    assert runtime.session_manager.session_id == ""


@pytest.mark.parametrize(
    "phrase",
    [
        "shutdown",
        "shutdown Ares",
        "shutdown Aris",
        "shutdown Aries",
        "shut down Ares",
        "shut down Aris",
        "shut down Aries",
        "shutdown RS",
        "shut down RS",
        "RS shutdown",
        "RS shut down",
        "shut down R S",
        "R S shut down",
        "shut down Are S",
        "Are S shut down",
        "Ares shutdown",
        "Ares shut down",
        "Aris shutdown",
        "Aries shut down",
        "turn off Ares",
        "power down Ares",
    ],
)
def test_shutdown_phrases_stop_runtime_and_clear_session(phrase):
    runtime, _ = _runtime()
    _start_active(runtime)

    result = runtime.handle_text(phrase)

    assert result.status == "stopped"
    assert runtime.session_manager.state == BRAIN_STOPPED
    assert runtime.session_manager.session_id == ""


def test_goodbye_is_not_full_shutdown():
    runtime, _ = _runtime()
    _start_active(runtime)

    result = runtime.handle_text("goodbye Ares")

    assert result.command_category == RUNTIME_COMMAND_STANDBY
    assert runtime.session_manager.state != BRAIN_STOPPED


@pytest.mark.parametrize(
    "text",
    [
        "goodbye",
        "goodbye my friend",
        "I said goodbye to Ares yesterday",
        "tell me about standby",
        "what does shutdown mean",
        "do not shutdown Ares",
        "don't shutdown Ares",
        "Ares should not shut down",
        "never standby Ares",
        "calculate two plus two Ares",
        "Ares remember this",
        "What does RS mean",
        "RS",
        "Paris",
        "Harris",
        "tell me about Aries",
        "what is the Aries zodiac sign",
        "please goodbye Ares now",
    ],
)
def test_lifecycle_controls_reject_partial_or_extra_sentence_words(text):
    runtime, _ = _runtime()
    assert runtime.classify_command(text).command_category == RUNTIME_COMMAND_ORDINARY


def test_standby_alias_bypasses_core_route_and_clears_session_once():
    calls = []
    runtime, _ = _runtime(handler=lambda text: calls.append(text))
    session_id = _start_active(runtime)

    result = runtime.handle_text("Goodbye, Aris.")

    assert result.status == "standby_entered"
    assert result.normalized_input == "goodbye"
    assert result.response_text != "I cannot handle that request yet."
    assert result.data["core_service_bypassed"] is True
    assert result.data["session_id_before"] == session_id
    assert result.data["lifecycle_command"]["assistant_alias_removed"] == "aris"
    assert result.data["lifecycle_command"]["alias_position"] == "suffix"
    assert calls == []
    transitions = [
        (item.source_state, item.target_state)
        for item in runtime.session_manager.history()
    ]
    assert transitions.count((BRAIN_ACTIVE, BRAIN_RETURNING_TO_STANDBY)) == 1
    assert transitions.count((BRAIN_RETURNING_TO_STANDBY, BRAIN_STANDBY)) == 1


def test_shutdown_alias_bypasses_core_route_and_releases_runtime():
    calls = []
    runtime, _ = _runtime(handler=lambda text: calls.append(text))
    _start_active(runtime)

    result = runtime.handle_text("Shut down, Aries.")

    assert result.status == "stopped"
    assert result.normalized_input == "shutdown"
    assert result.data["core_service_bypassed"] is True
    assert calls == []
    assert runtime.session_manager.state == BRAIN_STOPPED
    assert runtime.session_manager.session_id == ""


def test_rs_shutdown_bypasses_core_route_and_exposes_lifecycle_match():
    calls = []
    runtime, _ = _runtime(handler=lambda text: calls.append(text))
    _start_active(runtime)

    result = runtime.handle_text("Shut down RS")

    lifecycle = result.data["lifecycle_command"]
    assert result.status == "stopped"
    assert result.normalized_input == "shutdown"
    assert lifecycle["normalized_transcript"] == "shutdown"
    assert lifecycle["canonicalized_transcript"] == "shutdown"
    assert lifecycle["matched_alias"] == "rs"
    assert lifecycle["alias_type"] == "acoustic_alias"
    assert lifecycle["action"] == "shutdown"
    assert lifecycle["negation_detected"] is False
    assert result.data["core_service_bypassed"] is True
    assert calls == []


@pytest.mark.parametrize(
    ("phrase", "routed_phrase"),
    [
        ("Do not shut down Ares", "Do not shut down"),
        ("Don't shutdown Ares", "Don't shutdown"),
        ("Ares should not shut down", "should not shut down"),
        ("Never goodbye Ares", "Never goodbye"),
        ("Don't go to sleep", "Don't go to sleep"),
        ("Do not go to standby Ares", "Do not go to standby"),
    ],
)
def test_negated_lifecycle_phrase_stays_active_and_reaches_ordinary_route(
    phrase,
    routed_phrase,
):
    calls = []
    runtime, _ = _runtime(handler=lambda text: calls.append(text) or "ordinary")
    session_id = _start_active(runtime)

    result = runtime.handle_text(phrase)

    assert result.command_category == RUNTIME_COMMAND_ORDINARY
    assert result.data["core_service_bypassed"] is False
    assert result.data["lifecycle_command"]["negation_detected"] is True
    assert result.data["lifecycle_command"]["action"] == "none"
    assert runtime.session_manager.state == BRAIN_ACTIVE
    assert runtime.session_manager.session_id == session_id
    assert calls == [routed_phrase]


def test_inactivity_before_exact_and_after_boundary_is_deterministic():
    inputs = QueuedRuntimeInputAdapter()
    runtime, clock = _runtime(inputs=inputs)
    _start_active(runtime)
    clock.advance(29.999)
    inputs.push(RuntimeInputResult.timeout())

    before = runtime.poll_once()
    clock.advance(0.001)
    inputs.push(RuntimeInputResult.timeout())
    exact = runtime.poll_once()

    assert before.status == "input_timeout"
    assert exact.status == "standby_entered"
    assert runtime.session_manager.state == BRAIN_STANDBY
    assert runtime.session_manager.session_id == ""


def test_attention_only_does_not_reset_active_inactivity_deadline():
    inputs = QueuedRuntimeInputAdapter()
    runtime, clock = _runtime(inputs=inputs)
    _start_active(runtime)
    clock.advance(30)

    attention = runtime.handle_text("Aris")
    inputs.push(RuntimeInputResult.timeout())
    expired = runtime.poll_once()

    assert attention.status == "attention_only"
    assert expired.status == "standby_entered"
    assert runtime.session_manager.state == BRAIN_STANDBY


def test_command_processing_and_response_time_do_not_reset_owner_inactivity():
    inputs = QueuedRuntimeInputAdapter()
    clock = FakeClock()

    def handler(_text):
        clock.advance(10)
        return "response"

    runtime, _ = _runtime(inputs=inputs, clock=clock, handler=handler)
    _start_active(runtime)
    clock.advance(5)

    completed = runtime.handle_text("owner command")
    clock.advance(20)
    inputs.push(RuntimeInputResult.timeout())
    expired = runtime.poll_once()

    assert completed.status == "command_completed"
    assert expired.status == "standby_entered"
    assert runtime.session_manager.state == BRAIN_STANDBY


def test_active_transcription_timeout_is_nonterminal_and_preserves_retryable_session():
    inputs = QueuedRuntimeInputAdapter()
    routed = []
    runtime, _ = _runtime(
        inputs=inputs,
        handler=lambda text: routed.append(text) or "unexpected",
    )
    session_id = _start_active(runtime)
    inputs.push(
        RuntimeInputResult(
            status="timeout",
            metadata={
                "transcription_failure_type": "transcription_timeout",
                "retryable": True,
                "runtime_terminal": False,
            },
        )
    )

    result = runtime.poll_once()

    assert result.status == "input_timeout"
    assert runtime.session_manager.state == BRAIN_ACTIVE
    assert runtime.session_manager.session_id == session_id
    assert routed == []


def test_inactivity_after_boundary_returns_to_standby():
    inputs = QueuedRuntimeInputAdapter()
    runtime, clock = _runtime(inputs=inputs)
    _start_active(runtime)
    clock.advance(31)
    inputs.push(RuntimeInputResult.timeout())

    result = runtime.poll_once()

    assert result.stop_reason == "inactivity_expired"
    assert runtime.session_manager.state == BRAIN_STANDBY


def test_input_item_at_timeout_boundary_wins_and_is_processed():
    inputs = QueuedRuntimeInputAdapter()
    runtime, clock = _runtime(inputs=inputs, handler=lambda text: "accepted")
    session_id = _start_active(runtime)
    clock.advance(30)
    inputs.push("owner command")

    result = runtime.poll_once()

    assert result.status == "command_completed"
    assert runtime.session_manager.state == BRAIN_ACTIVE
    assert runtime.session_manager.session_id == session_id


def test_new_activation_after_inactivity_creates_new_session_id():
    inputs = QueuedRuntimeInputAdapter()
    runtime, clock = _runtime(inputs=inputs)
    first = _start_active(runtime)
    clock.advance(30)
    inputs.push(RuntimeInputResult.timeout())
    runtime.poll_once()

    second = runtime.handle_text("Ares")

    assert second.success
    assert runtime.session_manager.session_id != first


def test_clock_rollback_does_not_expire_or_extend_from_a_false_past():
    inputs = QueuedRuntimeInputAdapter()
    runtime, clock = _runtime(inputs=inputs)
    _start_active(runtime)
    clock.advance(10)
    runtime.handle_text("owner command")
    clock.rollback(100)
    inputs.push(RuntimeInputResult.timeout())

    result = runtime.poll_once()

    assert result.status == "input_timeout"
    assert runtime.session_manager.state == BRAIN_ACTIVE


def test_adapter_timeout_in_standby_is_bounded_noop():
    inputs = QueuedRuntimeInputAdapter([RuntimeInputResult.timeout()])
    runtime, _ = _runtime(inputs=inputs)

    result = runtime.poll_once()

    assert result.status == "input_timeout"
    assert runtime.session_manager.state == BRAIN_STANDBY


def test_malformed_input_adapter_result_is_structured_failure():
    class Malformed:
        def wait_for_input(self, timeout_seconds):
            return {"status": "input", "text": "Ares"}

    runtime, _ = _runtime(inputs=Malformed())

    result = runtime.poll_once()

    assert result.status == "input_failed"
    assert result.error_code == "malformed_input_adapter_result"
    assert runtime.session_manager.state == BRAIN_STANDBY


@pytest.mark.parametrize(
    "input_result,expected_status",
    [(RuntimeInputResult.cancelled(), "cancelled"), (RuntimeInputResult.end(), "end_of_input")],
)
def test_cancellation_and_end_of_input_stop_cleanly(input_result, expected_status):
    runtime, _ = _runtime(inputs=QueuedRuntimeInputAdapter([input_result]))

    result = runtime.poll_once()

    assert result.status == expected_status
    assert runtime.session_manager.state == BRAIN_STOPPED
    assert runtime.session_manager.session_id == ""


def test_source_local_end_of_input_does_not_terminate_persistent_runtime():
    inputs = QueuedRuntimeInputAdapter(
        [
            "Ares",
            RuntimeInputResult(
                status="end_of_input",
                metadata={
                    "runtime_terminal": False,
                    "input_scope": "active_command",
                },
            ),
            "shutdown Ares",
        ]
    )
    runtime, _ = _runtime(inputs=inputs)

    result = runtime.run()

    assert result.success is True
    assert result.status == "stopped"
    assert result.stop_reason == "explicit_shutdown_command"
    assert result.iteration_count == 3
    assert runtime.session_manager.state == BRAIN_STOPPED


def test_input_adapter_exception_is_structured_and_runtime_remains_until_limit():
    class Broken:
        def wait_for_input(self, timeout_seconds):
            raise OSError("private input content must not leak")

    runtime, _ = _runtime(inputs=Broken())

    result = runtime.poll_once()

    assert result.status == "input_failed"
    assert runtime.session_manager.state == BRAIN_STANDBY
    assert runtime.session_manager.snapshot().consecutive_failure_count == 1


def test_repeated_input_failures_reach_manager_limit_and_shutdown():
    class Broken:
        def wait_for_input(self, timeout_seconds):
            return RuntimeInputResult.failed("injected_input_failure", "injected")

    runtime, _ = _runtime(inputs=Broken())

    first = runtime.poll_once()
    second = runtime.poll_once()
    third = runtime.poll_once()

    assert first.status == "input_failed"
    assert second.status == "input_failed"
    assert third.status == "maximum_failures_reached"
    assert runtime.session_manager.state == BRAIN_STOPPED


def test_activation_output_failure_returns_safely_to_standby():
    output = CollectingRuntimeOutputAdapter(fail_after=0)
    runtime, _ = _runtime(output=output)
    runtime.start()

    result = runtime.handle_text("Ares")

    assert result.status == "output_failed"
    assert runtime.session_manager.state == BRAIN_STANDBY
    assert runtime.session_manager.session_id == ""


def test_brain_response_output_failure_preserves_lifecycle_consistency():
    output = CollectingRuntimeOutputAdapter(fail_after=1)
    runtime, _ = _runtime(output=output, handler=lambda text: "response")
    _start_active(runtime)

    result = runtime.handle_text("command")

    assert result.status == "output_failed"
    assert runtime.session_manager.state == BRAIN_STANDBY
    assert runtime.session_manager.session_id == ""


def test_failure_driven_shutdown_is_never_reported_as_clean_runtime_completion():
    config = BrainRuntimeConfig(maximum_consecutive_failures=1)
    output = CollectingRuntimeOutputAdapter(fail_after=1)
    runtime, _ = _runtime(
        inputs=QueuedRuntimeInputAdapter(["Ares", "ordinary command"]),
        output=output,
        handler=lambda text: f"response:{text}",
        config=config,
    )

    result = runtime.run()

    assert result.success is False
    assert result.status == "output_failed"
    assert result.stop_reason == "output_failure_recovery"
    assert runtime.session_manager.state == BRAIN_STOPPED


def test_output_adapter_exception_is_structured():
    class BrokenOutput:
        def write(self, message):
            raise OSError("speaker unavailable")

    runtime, _ = _runtime(output=BrokenOutput())
    runtime.start()

    result = runtime.handle_text("Ares")

    assert result.status == "output_failed"
    assert result.error_code == "output_adapter_exception"


def test_malformed_output_adapter_result_is_structured():
    class BrokenOutput:
        def write(self, message):
            return object()

    runtime, _ = _runtime(output=BrokenOutput())
    runtime.start()

    result = runtime.handle_text("Ares")

    assert result.status == "output_failed"
    assert result.error_code == "malformed_output_adapter_result"


def test_core_service_exception_returns_to_standby_without_crashing_runtime():
    def fail(text):
        raise RuntimeError("raw private command")

    runtime, _ = _runtime(handler=fail)
    _start_active(runtime)

    result = runtime.handle_text("owner command")

    assert result.status == "command_failed"
    assert result.response_text == "I could not process that request."
    assert runtime.session_manager.state == BRAIN_STANDBY


def test_structured_core_failure_returns_to_standby():
    runtime, _ = _runtime(
        handler=lambda text: SimpleNamespace(
            success=False,
            text="",
            error_message="service unavailable",
        )
    )
    _start_active(runtime)

    result = runtime.handle_text("command")

    assert result.status == "command_failed"
    assert result.error_code == "core_service_failure"
    assert runtime.session_manager.state == BRAIN_STANDBY


def test_synchronous_command_timeout_discards_late_response_and_recovers():
    clock = FakeClock()

    def delayed(text):
        clock.advance(31)
        return "late response"

    runtime, _ = _runtime(handler=delayed, clock=clock)
    _start_active(runtime)

    result = runtime.handle_text("slow command")

    assert result.status == "command_failed"
    assert result.error_code == "command_processing_timeout"
    assert "late response" not in result.response_text
    assert runtime.session_manager.state == BRAIN_STANDBY


def test_runtime_request_rejects_wrong_runtime_and_oversized_input():
    runtime, _ = _runtime()
    runtime.start()

    wrong = runtime.handle_request(BrainRuntimeRequestV1(runtime_id="other", input_text="Ares"))
    oversized = runtime.handle_text("x" * 4097)

    assert wrong.error_code == "runtime_id_mismatch"
    assert oversized.error_code == "input_too_long"
    assert runtime.session_manager.state == BRAIN_STANDBY


def test_empty_input_is_rejected_without_state_change():
    runtime, _ = _runtime()
    runtime.start()

    result = runtime.handle_text("  ...  ")

    assert result.error_code == "empty_input"
    assert runtime.session_manager.state == BRAIN_STANDBY


def test_runtime_loop_processes_queue_until_explicit_shutdown():
    inputs = QueuedRuntimeInputAdapter(
        ["ignored", "Ares", "one", "two", "goodbye Ares", "Ares", "shutdown Ares"]
    )
    output = CollectingRuntimeOutputAdapter()
    runtime, _ = _runtime(inputs=inputs, output=output, handler=lambda text: f"answer:{text}")

    result = runtime.run()

    assert isinstance(result, BrainRuntimeLoopResultV1)
    assert result.success
    assert result.current_lifecycle_state == BRAIN_STOPPED
    assert result.command_count == 2
    assert result.activation_count == 2
    assert result.standby_return_count == 1
    assert output.texts == [
        "Yes Gabi.",
        "answer:one",
        "answer:two",
        "Yes Gabi.",
        "ARES is shutting down.",
    ]


def test_runtime_loop_is_bounded_when_maximum_iterations_is_supplied():
    inputs = QueuedRuntimeInputAdapter([RuntimeInputResult.timeout()] * 5)
    runtime, _ = _runtime(inputs=inputs)

    result = runtime.run(maximum_iterations=2)

    assert result.success is False
    assert result.status == "maximum_iterations_reached"
    assert result.iteration_count == 2
    assert result.stop_reason == "maximum_iterations_reached"
    assert runtime.session_manager.state == BRAIN_STOPPED


def test_shutdown_and_cleanup_are_idempotent():
    runtime, _ = _runtime()
    _start_active(runtime)

    first = runtime.shutdown()
    second = runtime.shutdown()

    assert first.status == "stopped"
    assert second.status == "already_stopped"
    assert runtime.session_manager.state == BRAIN_STOPPED
    assert runtime.session_manager.session_id == ""


def test_safe_snapshot_reads_during_serialized_transitions():
    runtime, _ = _runtime(handler=lambda text: "ok")
    _start_active(runtime)
    snapshots = []

    def read_snapshots():
        for _ in range(100):
            snapshots.append(runtime.snapshot())

    readers = [Thread(target=read_snapshots) for _ in range(4)]
    for thread in readers:
        thread.start()
    result = runtime.handle_text("one command")
    for thread in readers:
        thread.join(timeout=2)

    assert result.success
    assert all(not thread.is_alive() for thread in readers)
    assert len(snapshots) == 400
    assert all(snapshot.runtime_id == "brain-runtime-test" for snapshot in snapshots)


def test_two_commands_are_serialized_without_concurrent_handler_execution():
    active_calls = 0
    maximum_active = 0
    calls_lock = Lock()

    def handler(text):
        nonlocal active_calls, maximum_active
        with calls_lock:
            active_calls += 1
            maximum_active = max(maximum_active, active_calls)
        time.sleep(0.01)
        with calls_lock:
            active_calls -= 1
        return f"ok:{text}"

    runtime, _ = _runtime(handler=handler)
    _start_active(runtime)
    results = []
    threads = [Thread(target=lambda text=value: results.append(runtime.handle_text(text))) for value in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert maximum_active == 1
    assert len(results) == 2
    assert all(result.success for result in results)


def test_concurrent_poll_is_rejected_and_does_not_spawn_worker_threads():
    entered = ThreadEvent()
    release = ThreadEvent()

    class BlockingInput:
        def wait_for_input(self, timeout_seconds):
            entered.set()
            release.wait(timeout=1)
            return RuntimeInputResult.timeout()

    runtime, _ = _runtime(inputs=BlockingInput())
    first_results = []
    thread = Thread(target=lambda: first_results.append(runtime.poll_once()))
    thread.start()
    assert entered.wait(timeout=1)

    second = runtime.poll_once()
    release.set()
    thread.join(timeout=2)

    assert second.error_code == "concurrent_poll_rejected"
    assert not thread.is_alive()
    assert len(first_results) == 1


def test_shutdown_closes_and_releases_a_blocked_input_adapter():
    entered = ThreadEvent()
    released = ThreadEvent()

    class CancellableInput:
        def wait_for_input(self, timeout_seconds):
            entered.set()
            released.wait(timeout=2)
            return RuntimeInputResult.cancelled()

        def close(self):
            released.set()

    runtime, _ = _runtime(inputs=CancellableInput())
    results = []
    thread = Thread(target=lambda: results.append(runtime.poll_once()))
    thread.start()
    assert entered.wait(timeout=1)

    stopped = runtime.shutdown(reason="external_test_shutdown")
    thread.join(timeout=2)

    assert stopped.success
    assert not thread.is_alive()
    assert results[0].status == "cancelled"
    assert runtime.session_manager.state == BRAIN_STOPPED
    assert runtime.session_manager.session_id == ""


def test_runtime_emits_required_events_without_private_input_or_memory_values(tmp_path):
    runtime, _, _, _ = _real_runtime(tmp_path)
    runtime.start()
    runtime.handle_text("Ares")
    runtime.handle_text("Remember that my favorite color is ultraviolet-secret.")
    runtime.handle_text("goodbye Ares")
    runtime.handle_text("shutdown Ares")

    event_types = {event.type for event in runtime.events()}
    assert {
        EVENT_RUNTIME_STARTED,
        EVENT_RUNTIME_INPUT_RECEIVED,
        EVENT_ACTIVATION_REQUESTED,
        EVENT_ACTIVATION_ACCEPTED,
        EVENT_RUNTIME_COMMAND_STARTED,
        EVENT_RUNTIME_COMMAND_COMPLETED,
        EVENT_RUNTIME_STANDBY_REQUESTED,
        EVENT_RUNTIME_SHUTDOWN_REQUESTED,
        EVENT_RUNTIME_STOPPED,
    } <= event_types
    serialized = json.dumps([event.to_dict() for event in runtime.events()]).lower()
    assert "favorite color" not in serialized
    assert "ultraviolet-secret" not in serialized
    assert "transcript" not in serialized
    assert all(event.metadata["contains_private_content"] is False for event in runtime.events())


def test_active_attention_is_not_an_activation_event_and_inactivity_is_emitted():
    inputs = QueuedRuntimeInputAdapter()
    runtime, clock = _runtime(inputs=inputs)
    _start_active(runtime)
    runtime.handle_text("Ares")
    clock.advance(30)
    inputs.push(RuntimeInputResult.timeout())
    runtime.poll_once()

    event_types = [event.type for event in runtime.events()]
    assert event_types.count(EVENT_ACTIVATION_ACCEPTED) == 1
    assert EVENT_ACTIVATION_REJECTED not in event_types
    assert EVENT_RUNTIME_INACTIVITY_EXPIRED in event_types


def test_runtime_event_history_failure_does_not_break_runtime():
    class BrokenHistory:
        def add(self, event, decision):
            raise OSError("history unavailable")

    runtime, _ = _runtime(event_history_store=BrokenHistory())

    result = runtime.start()

    assert result.success
    assert runtime.event_history_failures()


def test_runtime_input_and_output_adapters_are_deterministic():
    queued = QueuedRuntimeInputAdapter(["Ares", RuntimeInputResult.timeout()])
    output = CollectingRuntimeOutputAdapter()

    assert queued.wait_for_input(0.1).text == "Ares"
    assert queued.wait_for_input(0.1).status == "timeout"
    assert queued.wait_for_input(0.1).status == "end_of_input"
    assert output.write(RuntimeOutputMessage(category="test", text="hello")).success
    assert output.texts == ["hello"]


def test_console_adapters_translate_eof_cancel_and_output():
    values = iter(("hello", EOFError(), KeyboardInterrupt()))

    def fake_input(prompt):
        value = next(values)
        if isinstance(value, BaseException):
            raise value
        return value

    input_adapter = ConsoleRuntimeInputAdapter(input_func=fake_input)
    printed = []
    output_adapter = ConsoleRuntimeOutputAdapter(output_func=printed.append)

    assert input_adapter.wait_for_input(1).text == "hello"
    assert input_adapter.wait_for_input(1).status == "end_of_input"
    assert input_adapter.wait_for_input(1).status == "cancelled"
    assert output_adapter.write(RuntimeOutputMessage(category="test", text="response")).success
    assert printed == ["response"]


def test_manual_runtime_verification_script_passes_and_is_concise():
    output = []

    code = manual_verify_brain_runtime.run_verification(output.append)

    assert code == 0
    assert output[-1] == "Brain Runtime verification passed."
    assert any("Result: 4" in line for line in output)
    assert any("30.000s" in line for line in output)
    assert not any("favorite color is blue" in line.lower() for line in output)


def test_console_runtime_parser_defaults_and_overrides():
    defaults = run_ares_brain_runtime_text.build_parser().parse_args([])
    custom = run_ares_brain_runtime_text.build_parser().parse_args(
        [
            "--inactivity-timeout",
            "45",
            "--max-consecutive-failures",
            "4",
            "--poll-interval",
            "0.5",
            "--command-timeout",
            "10",
            "--standby-response",
            "Standing by.",
        ]
    )

    assert defaults.inactivity_timeout == 30.0
    assert defaults.max_consecutive_failures == 3
    assert custom.inactivity_timeout == 45.0
    assert custom.max_consecutive_failures == 4
    assert custom.poll_interval == 0.5
    assert custom.standby_response == "Standing by."
    assert defaults.command == []


def test_console_runtime_invalid_configuration_returns_nonzero(capsys):
    code = run_ares_brain_runtime_text.run_text_runtime(["--inactivity-timeout", "0"])

    assert code == 2
    assert "Configuration error" in capsys.readouterr().out


def test_console_runtime_supports_a_bounded_deterministic_command_sequence(capsys):
    code = run_ares_brain_runtime_text.run_text_runtime(
        [
            "--command",
            "Ares",
            "--command",
            "calculate 2 plus 2",
            "--command",
            "goodbye Ares",
            "--command",
            "Ares",
            "--command",
            "shutdown Ares",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "Yes Gabi." in output
    assert "Result: 4" in output
    assert "ARES is shutting down." in output


def test_runtime_has_no_hardware_or_voice_ownership_and_voice_launcher_is_unchanged():
    runtime_source = Path("core/BrainRuntime.py").read_text(encoding="utf-8").lower()
    adapters_source = Path("core/BrainRuntimeAdapters.py").read_text(encoding="utf-8").lower()
    voice_launcher = Path("scripts/run_ares_voice.py").read_text(encoding="utf-8")

    for forbidden in ("arecord", "aplay", "whisper", "piper", "microphone", "speaker", "shell=true"):
        assert forbidden not in runtime_source
    assert "thread(" not in runtime_source
    assert "thread(" not in adapters_source
    assert "BrainRuntime" not in voice_launcher


def test_manager_clock_rollback_is_clamped_for_safe_activity_deadline():
    clock = FakeClock()
    manager = BrainSessionManager(clock=clock)
    manager.begin_boot()
    manager.begin_initialization()
    manager.enter_standby()
    manager.activate_session()
    entered = manager.snapshot().entered_at
    clock.rollback(50)

    activity = manager.record_activity()

    assert activity.last_activity_at == entered
    assert manager.inactivity_expired() is False
