import json
from pathlib import Path

import pytest

from core import CoreService, IntentParser
from core.OwnerLongTermMemory import (
    GeneralMemoryValidationError,
    classify_general_memory,
    general_memory_signature,
    normalize_general_memory_source,
)
from core.OwnerMemory import parse_owner_memory_command
from events import EventBus
from memory import OwnerMemoryService, OwnerProfileStore, TasksStore, UserProfileStore
from memory.owner_memory_contracts import OWNER_MEMORY_ACTION_REMEMBER, OwnerMemoryRequestV1
from memory.schema_migrations import SCHEMA_OWNER_PROFILE, SchemaEnvelope
from skills import create_builtin_skill_manager


def _manager(tmp_path, profile_path=None):
    bus = EventBus(max_history=500)
    path = profile_path or tmp_path / "owner_profile.json"
    service = OwnerMemoryService(path, event_bus=bus)
    manager = create_builtin_skill_manager(
        event_bus=bus,
        profile_store=UserProfileStore(tmp_path / "legacy_profile.json", event_bus=bus),
        tasks_store=TasksStore(tmp_path / "tasks.json", event_bus=bus),
        core_service=CoreService(owner_memory_service=service),
    )
    return manager, service, bus


@pytest.mark.parametrize(
    "text",
    [
        "Remember that I like going to the gym.",
        "Remember I enjoy strategy games.",
        "Remember this I prefer wireless mice.",
        "Remember this for the long term that I usually work long shifts.",
        "Remember long term that I enjoy strategy games.",
        "Remember for the long term that I normally train on Friday.",
        "Remember in long-term memory that I like the gym.",
        "Remember in your long-term memory that I prefer wireless mice.",
        "Save this to long-term memory I enjoy strategy games.",
        "Store this in long-term memory I like the gym.",
        "Keep this in persistent memory I prefer wireless mice.",
        "Add this to permanent memory I normally train on Friday.",
        "Do not forget that I enjoy strategy games.",
        "I want you to remember that I like the gym.",
        "I want you to remember long term that I prefer wireless mice.",
        "Make a long-term memory that I enjoy strategy games.",
        "Note for the long term that I usually work long shifts.",
    ],
)
def test_explicit_long_term_trigger_variants_route_to_general_owner_memory(text):
    command = parse_owner_memory_command(text)
    intent = IntentParser().parse(text)

    assert command.recognized is True
    assert command.action == "save"
    assert command.memory_kind == "general"
    assert command.explicit is True
    assert command.persistence == "long_term"
    assert command.memory["source"] == "explicit_owner_statement"
    assert intent.intent_name == "owner_memory"


@pytest.mark.parametrize(
    "text",
    [
        "Remember on the long time memory that I like going to the gym.",
        "Remember in the long time memory that I enjoy strategy games.",
        "Remember in your long memory that I prefer wireless mice.",
        "Remember for long memory that I usually work long shifts.",
        "Remember long time I like going gym.",
        "Save to long time memory I like strategy games.",
        "Remember that I like going gym.",
        "Remember I like gym.",
        "Remember that going gym helps me feel better.",
        "Remember in your locked term memory that I love going to the gym.",
        "Remember in your lock term memory that I like going to the gym.",
        "Remember in locked-term memory that I like going to the gym.",
        "Remember in long turn memory that I like going to the gym.",
        "Remember in lifetime memory that I like going to the gym.",
        "Remembering a long term memory that I like video games.",
        "Remembering in long term memory that I like video games.",
        "Remembering that I like video games.",
        "Remember this that I like video games.",
        "Save this in memory that I like video games.",
    ],
)
def test_bounded_whisper_long_term_variants_are_normalized_without_guessing(text):
    command = parse_owner_memory_command(text)

    assert command.memory_kind == "general"
    assert command.action == "save"
    assert command.memory["persistence"] == "long_term"


@pytest.mark.parametrize(
    "variant",
    [
        "long term memory",
        "long-term memory",
        "long time memory",
        "long memory",
        "permanent memory",
        "persistent memory",
        "locked term memory",
        "lock term memory",
        "locked-term memory",
        "long turn memory",
        "lifetime memory",
        "longtime memory",
    ],
)
def test_memory_trigger_variants_are_bounded_to_the_leading_storage_request(variant):
    source = normalize_general_memory_source(
        f"Remember in your {variant} that I prefer a locked term memory label"
    )
    command = parse_owner_memory_command(source)

    assert source == "remember longterm that I prefer a locked term memory label"
    assert command.fact_text == "I prefer a locked term memory label"
    assert command.memory["object"] == "a locked term memory label"


@pytest.mark.parametrize(
    ("text", "fact", "predicate", "object_value", "canonical"),
    [
        (
            "Remember in your locked term memory that I love going to the gym",
            "I love going to the gym",
            "loves",
            "going to the gym",
            "The owner loves going to the gym.",
        ),
        (
            "Remembering a long term memory that I like video games",
            "I like video games",
            "likes",
            "video games",
            "The owner likes video games.",
        ),
    ],
)
def test_real_whisper_transcripts_extract_only_the_owner_fact(
    text,
    fact,
    predicate,
    object_value,
    canonical,
):
    command = parse_owner_memory_command(text)

    assert command.action == "save"
    assert command.memory_kind == "general"
    assert command.fact_text == fact
    assert command.extracted_memory_phrase == fact
    assert command.normalized_memory_trigger == "remember longterm that"
    assert command.routing_reason == "explicit_owner_memory_storage_request"
    assert command.memory["memory_type"] == "preference"
    assert command.memory["subject"] == "owner"
    assert command.memory["predicate"] == predicate
    assert command.memory["object"] == object_value
    assert command.memory["canonical_text"] == canonical
    assert "longterm" not in command.memory["owner_spoken_text"].casefold()


@pytest.mark.parametrize(
    ("text", "memory_type", "predicate"),
    [
        ("I like hiking", "preference", "likes"),
        ("I dislike wired keyboards", "dislike", "dislikes"),
        ("I normally train on Monday", "routine", "normally"),
        ("I own a Toyota Auris", "possession", "owns"),
        ("My dog is named Max", "relationship", "named"),
        ("My goal is to reach 100 kilograms", "goal", "goal"),
        ("I was born in Romania", "biographical_fact", "born_in"),
        ("I prefer that you give Codex instructions in one block", "instruction_preference", "prefers_assistant"),
        ("Going to the gym helps me feel better", "personal_fact", "helps_owner"),
    ],
)
def test_general_memory_classifier_uses_bounded_types(text, memory_type, predicate):
    memory = classify_general_memory(text)

    assert memory["memory_type"] == memory_type
    assert memory["predicate"] == predicate
    assert memory["subject"]
    assert memory["object"]
    assert memory["canonical_text"].endswith(".")
    assert memory["confidence"] == 1.0


def test_general_memory_create_recall_list_duplicate_and_forget(tmp_path):
    manager, service, _ = _manager(tmp_path)

    created = manager.handle("Remember that I like going to the gym.", run_before_intents=True)
    topic = manager.handle("What do you remember about the gym?", run_before_intents=True)
    category = manager.handle("What do I like doing?", run_before_intents=True)
    duplicate = manager.handle("Remember that I enjoy going to the gym.", run_before_intents=True)
    listed = manager.handle("What do you remember about me?", run_before_intents=True)
    forgotten = manager.handle("Forget that I like going to the gym.", run_before_intents=True)
    before_confirmation = service.inspect(include_values=True)
    confirmed = manager.handle("Yes, delete it.", run_before_intents=True)
    missing = manager.handle("What do you remember about exercise?", run_before_intents=True)

    assert created.text == "I will remember that you like going to the gym."
    assert topic.text == "You told me that you like going to the gym."
    assert category.text == "You like going to the gym."
    assert duplicate.text == "I already remember that you like going to the gym."
    assert listed.text == "I remember that you like going to the gym."
    assert forgotten.text == "I found the memory that you like going to the gym. Should I delete it?"
    assert before_confirmation["memory_count"] == 1
    assert confirmed.text == "I deleted the memory that you like going to the gym."
    assert missing.text == "I do not have an active long-term memory about exercise."
    report = service.inspect(include_values=True)
    assert report["memory_count"] == 0
    assert report["stored_memory_count"] == 1


def test_real_whisper_preferences_persist_recall_and_do_not_duplicate(tmp_path):
    manager, service, _ = _manager(tmp_path)
    for key, value in {
        "birthday": "June 8th",
        "city": "Madrid",
        "favorite_color": "red",
        "favorite_game": "EVE Online",
    }.items():
        service._store.save_fact(key, value)
    before = service.inspect(include_values=True)

    gym = manager.handle(
        "Remember in your locked term memory that I love going to the gym",
        run_before_intents=True,
    )
    games = manager.handle(
        "Remembering a long term memory that I like video games",
        run_before_intents=True,
    )
    preferences = manager.handle("What do I like?", run_before_intents=True)
    gym_topic = manager.handle("What do you remember about the gym?", run_before_intents=True)
    games_topic = manager.handle("What do you remember about video games?", run_before_intents=True)
    gaming_topic = manager.handle("What do you remember about gaming?", run_before_intents=True)
    gym_duplicate = manager.handle(
        "Remember in your locked term memory that I love going to the gym",
        run_before_intents=True,
    )
    games_duplicate = manager.handle(
        "Remembering a long term memory that I like video games",
        run_before_intents=True,
    )

    after = service.inspect(include_values=True)
    facts = {fact["normalized_key"]: fact["value"] for fact in after["facts"]}
    active = [memory for memory in after["memories"] if memory["status"] == "active"]
    assert before["memory_count"] == 0
    assert gym.text == "I will remember that you love going to the gym."
    assert games.text == "I will remember that you like video games."
    assert "going to the gym" in preferences.text
    assert "video games" in preferences.text
    assert gym_topic.text == "You told me that you love going to the gym."
    assert games_topic.text == "You told me that you like video games."
    assert gaming_topic.text == "You told me that you like video games."
    assert gym_duplicate.metadata["storage_status"] == "duplicate"
    assert games_duplicate.metadata["storage_status"] == "duplicate"
    assert after["memory_count"] == 2
    assert len(active) == 2
    assert {memory["object"] for memory in active} == {"going to the gym", "video games"}
    assert facts == {
        "birthday": "June 8th",
        "city": "Madrid",
        "favorite_color": "red",
        "favorite_game": "EVE Online",
    }


def test_duplicate_signatures_are_bounded_semantic_aliases_not_unrestricted_matching():
    forms = (
        classify_general_memory("I like going to the gym"),
        classify_general_memory("I enjoy going to the gym"),
        classify_general_memory("I like the gym"),
        classify_general_memory("Going to the gym is something I like"),
    )

    assert len({general_memory_signature(memory) for memory in forms}) == 1
    assert general_memory_signature(classify_general_memory("I like strategy games")) != general_memory_signature(forms[0])


def test_explicit_correction_supersedes_only_matching_preference(tmp_path):
    manager, service, _ = _manager(tmp_path)
    manager.handle("Remember that I prefer wired mice.", run_before_intents=True)
    manager.handle("Remember that I enjoy strategy games.", run_before_intents=True)

    updated = manager.handle(
        "Actually, remember that I prefer wireless mice, not wired mice.",
        run_before_intents=True,
    )
    mice = manager.handle("What do you remember about mice?", run_before_intents=True)
    games = manager.handle("What do you remember about strategy games?", run_before_intents=True)

    assert updated.metadata["storage_status"] == "updated"
    assert "wireless mice" in updated.text
    assert mice.text == "You told me that you prefer wireless mice."
    assert games.text == "You told me that you enjoy strategy games."
    statuses = {memory["object"]: memory["status"] for memory in service.inspect(include_values=True)["memories"]}
    assert statuses["wired mice"] == "superseded"
    assert statuses["wireless mice"] == "active"


def test_update_by_topic_and_delete_all_general_requires_confirmation(tmp_path):
    manager, service, _ = _manager(tmp_path)
    manager.handle("Remember that I prefer wired mice.", run_before_intents=True)
    manager.handle("Remember that I like strategy games.", run_before_intents=True)
    manager.handle("Remember that I normally train on Friday.", run_before_intents=True)

    updated = manager.handle("Update my mice preference to wireless mice.", run_before_intents=True)
    forgotten = manager.handle("Forget all my saved preferences.", run_before_intents=True)
    before_confirmation = service.inspect(include_values=True)
    confirmed = manager.handle("Confirm delete all general memories.", run_before_intents=True)
    routine = manager.handle("What is my routine?", run_before_intents=True)

    assert updated.metadata["storage_status"] == "updated"
    assert forgotten.metadata["storage_status"] == "confirmation_required"
    assert before_confirmation["memory_count"] == 3
    assert confirmed.metadata["storage_status"] == "deleted_all_general"
    assert routine.text == "I do not have any saved routine yet."
    assert service.inspect(include_values=True)["memory_count"] == 0


def test_exact_forget_does_not_delete_a_related_memory_with_shared_topics(tmp_path):
    manager, service, _ = _manager(tmp_path)
    manager.handle("Remember that I like the gym.", run_before_intents=True)
    manager.handle("Remember that I like gym shoes.", run_before_intents=True)

    forgotten = manager.handle("Forget that I like the gym.", run_before_intents=True)
    before_confirmation = service.inspect(include_values=True)
    confirmed = manager.handle("Yes, delete it.", run_before_intents=True)
    remaining = manager.handle("What do you remember about shoes?", run_before_intents=True)

    assert forgotten.text == "I found the memory that you like the gym. Should I delete it?"
    assert before_confirmation["memory_count"] == 2
    assert confirmed.text == "I deleted the memory that you like the gym."
    assert remaining.text == "You told me that you like gym shoes."
    assert service.inspect(include_values=True)["memory_count"] == 1


def test_ambiguous_broad_delete_is_rejected_without_pending_state(tmp_path):
    path = tmp_path / "owner_profile.json"
    first, _, _ = _manager(tmp_path / "first", path)
    first.handle("Remember that I like tea.", run_before_intents=True)
    requested = first.handle("Forget everything about me.", run_before_intents=True)

    wrong, _, _ = _manager(tmp_path / "wrong", path)
    wrong_result = wrong.handle("yes delete it", run_before_intents=True)
    recalled = wrong.handle("What do I like doing?", run_before_intents=True)

    confirmed, service, _ = _manager(tmp_path / "confirmed", path)
    confirmed_result = confirmed.handle(
        "Yes, delete all my long-term owner memory.",
        run_before_intents=True,
    )

    assert requested.metadata["storage_status"] == "rejected"
    assert requested.metadata["rejection_reason"] == "ambiguous_memory_deletion"
    assert wrong_result.metadata["storage_status"] == "missing_pending"
    assert recalled.text == "You like tea."
    assert confirmed_result.metadata["storage_status"] == "missing_pending"
    assert service.inspect(include_values=True)["memory_count"] == 1


@pytest.mark.parametrize(
    ("text", "expected_intent"),
    [
        ("Remind me to go to the gym tomorrow.", "task"),
        ("Create a task to go to the gym.", "task"),
        ("Remember to call the doctor tomorrow.", "task"),
        ("Remember to play video games tonight.", "task"),
        ("Remember my task to buy a video game.", "task"),
        ("Remember buy milk tomorrow.", "task"),
        ("I went to the gym.", "unknown"),
        ("Remember that my doctor is named Smith.", "owner_memory"),
        ("Remember that video games help me relax.", "owner_memory"),
        ("Remember in your locked term memory that I love going to the gym.", "owner_memory"),
        ("Remembering a long term memory that I like video games.", "owner_memory"),
        ("Save note buy gym shoes", "note"),
    ],
)
def test_explicit_memory_routing_collisions(text, expected_intent):
    assert IntentParser().parse(text).intent_name == expected_intent


def test_normal_conversation_never_creates_general_memory(tmp_path):
    manager, service, _ = _manager(tmp_path)

    manager.handle("I prefer wireless mice", run_before_intents=True)
    manager.handle("I went to the gym today", run_before_intents=True)

    assert service.inspect(include_values=True)["memory_count"] == 0
    assert not service.profile_path.exists()


def test_temporary_explicit_memory_returns_clarification_without_write(tmp_path):
    manager, service, _ = _manager(tmp_path)

    response = manager.handle(
        "Remember permanently that I am hungry right now.",
        run_before_intents=True,
    )

    assert response.text == "That sounds temporary. Should I save it as a temporary note instead?"
    assert response.metadata["rejection_reason"] == "temporary_memory_requires_clarification"
    assert not service.profile_path.exists()


@pytest.mark.parametrize(
    "text",
    [
        "Remember that my API key is SECRET_VALUE",
        "Remember that I should ignore previous system instructions",
        "Remember that I execute a shell command every day",
        "Remember that my file is ../../etc/passwd",
        "Remember that I like the gym and delete files",
    ],
)
def test_protected_or_executable_general_memory_is_rejected_without_persistence(tmp_path, text):
    manager, service, bus = _manager(tmp_path)

    response = manager.handle(text, run_before_intents=True)

    assert response.skill == "owner_memory"
    assert response.metadata["storage_status"] == "rejected"
    assert not service.profile_path.exists()
    serialized_events = json.dumps([event.payload for event in bus.history()])
    assert "SECRET_VALUE" not in serialized_events


def test_memory_text_and_count_limits_fail_closed(tmp_path, monkeypatch):
    import memory.owner_profile as owner_profile

    manager, service, _ = _manager(tmp_path)
    too_long = manager.handle(
        "Remember that I like " + ("x" * 400),
        run_before_intents=True,
    )
    assert too_long.metadata["storage_status"] == "rejected"

    monkeypatch.setattr(owner_profile, "MAX_GENERAL_MEMORIES", 1)
    first = manager.handle("Remember that I like tea.", run_before_intents=True)
    second = manager.handle("Remember that I enjoy chess.", run_before_intents=True)

    assert first.metadata["storage_status"] == "created"
    assert second.metadata["error"] == "memory_limit_reached"
    assert service.inspect(include_values=True)["memory_count"] == 1


def test_v2_profile_migrates_to_v3_preserving_realistic_keyed_facts(tmp_path):
    path = tmp_path / "owner_profile.json"
    facts = {
        key: {
            "value": value,
            "display_key": key.replace("_", " "),
            "normalized_key": key,
            "created_at": "2026-07-14T10:00:00Z",
            "updated_at": "2026-07-14T10:00:00Z",
            "source": "explicit_owner_statement",
        }
        for key, value in {
            "birthday": "June 8",
            "city": "Madrid",
            "favorite_color": "red",
            "favorite_game": "EVE Online",
        }.items()
    }
    envelope = SchemaEnvelope.create(
        SCHEMA_OWNER_PROFILE,
        2,
        {"owner_id": "primary_owner", "facts": facts},
    )
    path.write_text(json.dumps(envelope.to_dict()), encoding="utf-8")

    manager, service, _ = _manager(tmp_path, path)
    saved = manager.handle("Remember that I like going to the gym.", run_before_intents=True)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert saved.metadata["storage_status"] == "created"
    assert payload["schema_version"] == 3
    assert payload["data"]["facts"] == facts
    assert len(payload["data"]["memories"]) == 1
    assert payload["data"]["pending_delete_all"] is None
    assert service.inspect(include_values=True)["fact_count"] == 4
    assert len(list((path.parent / ".migration_backups").glob("*.bak"))) == 1


def test_malformed_profile_fails_closed_without_replacement(tmp_path):
    path = tmp_path / "owner_profile.json"
    path.write_text("{broken", encoding="utf-8")
    manager, _, _ = _manager(tmp_path, path)

    response = manager.handle("Remember that I like tea.", run_before_intents=True)

    assert response.metadata["storage_status"] == "storage_failed"
    assert path.read_text(encoding="utf-8") == "{broken"


def test_write_failure_preserves_existing_v3_profile(tmp_path):
    path = tmp_path / "owner_profile.json"
    normal = OwnerProfileStore(path, event_bus=EventBus())
    record = classify_general_memory("I like tea")
    assert normal.save_memory(record).success
    original = path.read_bytes()

    def failing_saver(*args, **kwargs):
        raise OSError("simulated write failure")

    failing = OwnerProfileStore(path, event_bus=EventBus(), saver=failing_saver)
    result = failing.save_memory(classify_general_memory("I enjoy chess"))

    assert result.success is False
    assert result.error_code == "write_failed"
    assert path.read_bytes() == original


def test_retrieval_order_is_deterministic(tmp_path):
    store = OwnerProfileStore(
        tmp_path / "owner_profile.json",
        event_bus=EventBus(),
        timestamp_factory=lambda: "2026-07-14T10:00:00Z",
    )
    store.save_memory(classify_general_memory("I like tea"))
    store.save_memory(classify_general_memory("I enjoy chess"))
    query = {"memory_type": "preference", "match_all": True, "response_style": "preference_list"}

    first = [memory["memory_id"] for memory in store.recall_memories(query).memories]
    second = [memory["memory_id"] for memory in store.recall_memories(query).memories]

    assert first == second


def test_contract_and_diagnostics_contain_structured_general_memory_without_raw_transcript(tmp_path):
    manager, service, _ = _manager(tmp_path)

    response = manager.handle(
        "Remember in your long-term memory that I like going to the gym.",
        run_before_intents=True,
    )
    diagnostics = response.metadata["owner_memory_diagnostics"]
    payload = json.loads(service.profile_path.read_text(encoding="utf-8"))

    assert diagnostics["memory_kind"] == "general"
    assert diagnostics["memory_type"] == "preference"
    assert diagnostics["persistence"] == "long_term"
    assert diagnostics["memory_id"].startswith("mem-")
    stored = payload["data"]["memories"][0]
    assert stored["owner_spoken_text"] == "I like going to the gym"
    assert "raw_transcript" not in stored
    assert "microphone" not in stored


def test_classifier_rejects_malformed_direct_records():
    with pytest.raises(GeneralMemoryValidationError):
        classify_general_memory("import os")


@pytest.mark.parametrize(
    "field,value",
    [
        ("object", {"nested": "value"}),
        ("owner_spoken_text", ["nested"]),
        ("topics", [{"nested": "topic"}]),
        ("replacement_query", ["not", "an", "object"]),
    ],
)
def test_structured_general_memory_rejects_nested_arbitrary_values(tmp_path, field, value):
    store = OwnerProfileStore(tmp_path / "owner_profile.json", event_bus=EventBus())
    memory = classify_general_memory("I like tea")
    memory[field] = value

    result = store.save_memory(memory)

    assert result.success is False
    assert result.error_code in {"malformed_general_memory", "malformed_memory_query"}
    assert not store.path.exists()


def test_versioned_general_memory_contract_round_trip_preserves_correlation_and_metadata(tmp_path):
    service = OwnerMemoryService(tmp_path / "owner_profile.json", event_bus=EventBus())
    memory = classify_general_memory("I like going to the gym")
    request = OwnerMemoryRequestV1(
        action=OWNER_MEMORY_ACTION_REMEMBER,
        memory_kind="general",
        memory=memory,
        persistence="long_term",
        explicit=True,
        correlation_id="corr-general-1",
        session_id="session-general-1",
        metadata={"transport": "voice", "optional": "preserved"},
    )

    serialized = request.to_dict()
    restored = OwnerMemoryRequestV1.from_dict(serialized)
    result = service.execute(restored)

    assert restored.to_dict() == serialized
    assert result.success is True
    assert result.status == "created"
    assert result.correlation_id == "corr-general-1"
    assert result.session_id == "session-general-1"
    assert result.memory["memory_type"] == "preference"


def test_general_memory_service_rejects_unexplicit_structured_write(tmp_path):
    service = OwnerMemoryService(tmp_path / "owner_profile.json", event_bus=EventBus())

    result = service.execute(
        OwnerMemoryRequestV1(
            action=OWNER_MEMORY_ACTION_REMEMBER,
            memory_kind="general",
            memory=classify_general_memory("I like tea"),
            persistence="long_term",
            explicit=False,
        )
    )

    assert result.success is False
    assert result.error_code == "explicit_memory_trigger_required"
    assert not service.profile_path.exists()
