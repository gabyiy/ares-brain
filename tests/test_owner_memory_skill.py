import json

import pytest

from core import CoreService, IntentParser, Planner
from core.OwnerMemory import (
    OWNER_MEMORY_REJECT,
    owner_memory_uses_explicit_store,
    parse_owner_memory_command,
)
from events import EventBus
from memory import OwnerMemoryResultV1, OwnerProfileStore
from skills import create_builtin_skill_manager
from skills.builtin.owner_memory import OwnerMemorySkill


def _manager(tmp_path, bus=None):
    event_bus = bus or EventBus(max_history=500)
    store = OwnerProfileStore(tmp_path / "owner_profile.json", event_bus=event_bus)
    return create_builtin_skill_manager(
        event_bus=event_bus,
        owner_profile_store=store,
    ), store, event_bus


@pytest.mark.parametrize(
    ("text", "action", "key"),
    [
        ("remember that my favorite color is blue", "save", "favorite_color"),
        ("remember my favorite colour is blue.", "save", "favorite_color"),
        ("my favorite color is blue.", "save", "favorite_color"),
        ("update my favorite color to red.", "update", "favorite_color"),
        ("what is my favorite color?", "recall", "favorite_color"),
        ("what's my favorite colour", "recall", "favorite_color"),
        ("do you remember my favorite color?", "recall", "favorite_color"),
        ("forget my favorite color.", "forget", "favorite_color"),
        ("delete my favorite color.", "forget", "favorite_color"),
        (
            "remember that modified white color is blue.",
            "save",
            "favorite_color",
        ),
    ],
)
def test_explicit_owner_memory_intents_are_deterministic(text, action, key):
    intent = IntentParser().parse(text)

    assert intent.intent_name == "owner_memory"
    assert intent.extracted_entities["action"] == action
    assert intent.extracted_entities["normalized_key"] == key
    assert intent.raw_text == f"owner memory {action} {key}"


def test_owner_memory_parser_rejects_ambiguous_command_instead_of_guessing():
    command = parse_owner_memory_command(
        "remember that my favorite color is blue and delete files"
    )

    assert command.recognized is True
    assert command.action == OWNER_MEMORY_REJECT
    assert command.rejection_reason == "ambiguous_value_rejected"


@pytest.mark.parametrize(
    "text",
    [
        "remember to buy milk",
        "remember buy milk tomorrow",
        "remind me to buy milk",
        "remind me about the appointment",
    ],
)
def test_generic_task_reminders_remain_tasks(text):
    assert IntentParser().parse(text).intent_name == "task"


def test_legacy_declarative_profile_facts_are_not_claimed_by_owner_memory_skill():
    skill = OwnerMemorySkill()
    command = parse_owner_memory_command("My favorite tank is Leopard 2")

    assert command.recognized is True
    assert owner_memory_uses_explicit_store(command) is False
    assert skill.can_handle("My favorite tank is Leopard 2") is False
    assert skill.can_handle("My name is Gabi") is False
    assert skill.can_handle("My favorite color is blue") is True


def test_protected_key_intent_does_not_retain_supplied_value():
    secret = "OWNER_SECRET_123"

    intent = IntentParser().parse(f"remember that my API key is {secret}")
    serialized = json.dumps(intent.to_dict())

    assert intent.intent_name == "owner_memory"
    assert intent.extracted_entities["action"] == "reject"
    assert intent.extracted_entities["protected"] is True
    assert "value" not in intent.extracted_entities
    assert secret not in serialized
    assert secret not in intent.raw_text


def test_owner_memory_plan_executes_registered_skill_with_redacted_serialization():
    intent = IntentParser().parse("remember that my favorite color is blue")

    plan = Planner().plan(intent)

    assert plan.steps[0].target == "owner_memory"
    assert plan.steps[0].action == "save"
    assert plan.steps[0].entities["value"] == "blue"
    assert plan.steps[0].redact_operational_events is True
    assert plan.to_dict()["steps"][0]["entities"]["value"] == "[REDACTED]"
    assert "blue" not in json.dumps(plan.to_dict())


def test_required_owner_memory_interactions_use_planner_and_skill(tmp_path):
    manager, _, _ = _manager(tmp_path)
    steps = (
        (
            "Remember that my favorite color is blue.",
            "I will remember that your favorite color is blue.",
            "created",
        ),
        (
            "What is my favorite color?",
            "Your favorite color is blue.",
            "recalled",
        ),
        (
            "Remember that my favorite color is red.",
            "I updated your favorite color to red.",
            "updated",
        ),
        (
            "What's my favorite colour?",
            "Your favorite color is red.",
            "recalled",
        ),
        (
            "Forget my favorite color.",
            "I forgot your favorite color.",
            "forgotten",
        ),
        (
            "Do you remember my favorite color?",
            "I do not know your favorite color yet.",
            "missing",
        ),
    )

    for text, expected, storage_status in steps:
        response = manager.handle(text, run_before_intents=True)

        assert response.skill == "owner_memory"
        assert response.text == expected
        assert response.metadata["storage_status"] == storage_status
        assert manager.last_plan.steps[0].target == "owner_memory"
        assert manager.last_execution.step_results[0].returned_data["skill"] == "owner_memory"


def test_new_manager_instance_recalls_persisted_fact(tmp_path):
    first, _, _ = _manager(tmp_path)
    first.handle("remember that my favorite color is blue", run_before_intents=True)

    second, _, _ = _manager(tmp_path)
    response = second.handle("what is my favorite color", run_before_intents=True)

    assert response.text == "Your favorite color is blue."


def test_missing_store_fails_safely_through_execution_pipeline():
    class UnavailableOwnerMemory:
        def execute(self, request):
            return OwnerMemoryResultV1(
                False,
                "storage_failed",
                request.action,
                error_code="owner_memory_unavailable",
                error_message="Owner memory is unavailable.",
            )

        def inspect(self, include_values=False):
            return {"validation_state": "invalid"}

    manager = create_builtin_skill_manager(
        event_bus=EventBus(),
        core_service=CoreService(owner_memory_service=UnavailableOwnerMemory()),
    )

    response = manager.handle(
        "remember that my favorite color is blue",
        run_before_intents=True,
    )

    assert response.skill == "owner_memory"
    assert response.text == "I could not update owner memory safely."
    assert manager.last_execution.success is False
    assert manager.last_execution.step_results[0].error_message == "owner_memory_unavailable"


def test_protected_key_returns_safe_response_and_redacts_all_events(tmp_path):
    secret = "DO_NOT_LOG_THIS_SECRET"
    bus = EventBus(max_history=500)
    manager, _, _ = _manager(tmp_path, bus)

    response = manager.handle(
        f"remember that my private key is {secret}",
        run_before_intents=True,
    )
    event_payloads = [event.payload for event in bus.history()]

    assert response.skill == "owner_memory"
    assert response.metadata["protected_key_rejected"] is True
    assert response.metadata["redact_transcript"] is True
    assert secret not in response.text
    assert secret not in json.dumps(event_payloads)
    detected = bus.history("skill.detected")[-1]
    assert "value" not in detected.payload["entities"]
    assert detected.payload["value_redacted"] is True


def test_ordinary_fact_value_and_spoken_response_are_absent_from_operational_events(tmp_path):
    bus = EventBus(max_history=500)
    manager, _, _ = _manager(tmp_path, bus)

    response = manager.handle(
        "remember that my favorite color is ultramarine",
        run_before_intents=True,
    )
    serialized_events = json.dumps([event.payload for event in bus.history()])

    assert "ultramarine" in response.text
    assert "ultramarine" not in serialized_events
    assert "I will remember" not in serialized_events
    response_event = bus.history("skill.response_generated")[-1]
    assert response_event.payload["response"] == "[REDACTED]"
    assert response_event.payload["storage_status"] == "created"


def test_owner_memory_skill_manifest_is_registered_with_explicit_capabilities(tmp_path):
    manager, _, _ = _manager(tmp_path)

    skill = manager.registry.get("owner_memory")
    manifest = manager.registry.manifest_registry.get_manifest("owner_memory")

    assert type(skill) is OwnerMemorySkill
    assert manifest.enabled_by_default is True
    assert set(manifest.capabilities) == {
        "memory.owner_fact.save",
        "memory.owner_fact.update",
        "memory.owner_fact.recall",
        "memory.owner_fact.forget",
        "memory.owner_fact.list",
        "memory.owner.general.remember",
        "memory.owner.general.recall",
        "memory.owner.general.forget",
        "memory.owner.general.list",
    }


def test_owner_memory_response_exposes_bounded_operation_diagnostics(tmp_path):
    manager, store, _ = _manager(tmp_path)

    response = manager.handle(
        "remember that my favorite color is blue",
        run_before_intents=True,
    )

    diagnostics = response.metadata["owner_memory_diagnostics"]
    assert diagnostics == {
        "action": "save",
        "normalized_key": "favorite_color",
        "extracted_value": "blue",
        "parser_rule": "owner_memory_explicit_v1",
        "memory_kind": "fact",
        "memory_type": "",
        "persistence": "long_term",
        "profile_path": str(store.path),
        "file_existed_before": False,
        "operation_result": "created",
        "rejection_reason": "",
        "extracted_memory_phrase": "",
        "fact_text_length": 0,
        "memory_id": "",
    }


def test_birthday_recall_routes_to_central_owner_memory():
    intent = IntentParser().parse("what is my birthday")

    assert intent.intent_name == "owner_memory"
    assert intent.extracted_entities["normalized_key"] == "birthday"


def test_calculator_and_unknown_command_regressions_remain_safe(tmp_path):
    manager, _, _ = _manager(tmp_path)

    calculator = manager.handle("calculate 2 + 2", run_before_intents=True)
    unknown = manager.handle("explain the philosophy of blue", run_before_intents=True)

    assert calculator.skill == "calculator"
    assert calculator.text == "Result: 4"
    assert unknown is None


def test_text_repl_does_not_duplicate_owner_command_into_general_memory(capsys):
    from interfaces.text_repl import print_and_record

    class GeneralMemory:
        def __init__(self):
            self.calls = []

        def remember(self, **kwargs):
            self.calls.append(kwargs)

    class LegacyProfile:
        def __init__(self):
            self.calls = []

        def learn_from_text(self, text):
            self.calls.append(text)

    memory = GeneralMemory()
    profile = LegacyProfile()

    print_and_record(
        memory,
        profile,
        "remember that my favorite color is blue",
        "I will remember that your favorite color is blue.",
    )

    assert "ARES:" in capsys.readouterr().out
    assert memory.calls == []
    assert profile.calls == []


def test_legacy_text_router_redacts_owner_memory_input_and_response_events(tmp_path):
    from core.intent_router import IntentRouter

    secret = "ROUTER_SECRET_321"
    bus = EventBus(max_history=500)
    manager, _, _ = _manager(tmp_path, bus)
    router = IntentRouter(event_bus=bus, skill_manager=manager)

    response = router.handle(f"remember that my API key is {secret}")
    serialized = json.dumps([event.payload for event in bus.history()])

    assert "cannot store or recall protected information" in response
    assert secret not in serialized
    assert any(
        event.payload.get("value_redacted") is True
        for event in bus.history("user_message_received")
    )
