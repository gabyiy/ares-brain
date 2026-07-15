from datetime import datetime, timedelta, timezone
import json

import pytest

from core import CoreService, IntentParser
from events import EventBus
from memory import OwnerMemoryService, PendingOwnerMemoryActionStore, TasksStore, UserProfileStore
from skills import create_builtin_skill_manager


def _manager(tmp_path, profile_path=None, *, service=None):
    bus = EventBus(max_history=500)
    profile = profile_path or tmp_path / "owner_profile.json"
    owner_service = service or OwnerMemoryService(profile, event_bus=bus)
    manager = create_builtin_skill_manager(
        event_bus=bus,
        profile_store=UserProfileStore(tmp_path / "legacy_profile.json", event_bus=bus),
        tasks_store=TasksStore(tmp_path / "tasks.json", event_bus=bus),
        core_service=CoreService(owner_memory_service=owner_service),
    )
    return manager, owner_service, bus


def _handle(manager, text):
    response = manager.handle(text, run_before_intents=True)
    assert response is not None
    return response


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("List my long-term memories.", "list"),
        ("List my saved facts.", "list"),
        ("How many memories do you have about me?", "count"),
        ("How many preferences have you saved?", "count"),
        ("What do you remember about the gym?", "inspect"),
        ("Forget that I like going to the gym.", "forget_specific"),
        ("Forget my gaming memory.", "forget_specific"),
        ("Forget everything about gaming.", "forget_topic"),
        ("Forget all my general long-term memories.", "forget_all_general"),
        ("Forget my favorite color.", "forget_keyed_fact"),
        ("Yes, delete it.", "confirm_delete"),
        ("Never mind.", "cancel_delete"),
    ],
)
def test_management_commands_route_to_owner_memory_before_tasks(text, action):
    intent = IntentParser().parse(text)

    assert intent.intent_name == "owner_memory"
    assert intent.extracted_entities["action"] == action


@pytest.mark.parametrize(
    "text",
    [
        "Remind me to delete the file tomorrow.",
        "Remember to remove the rubbish.",
        "Create a task to clear the desk.",
        "Remind me to call the gym.",
    ],
)
def test_task_deletion_language_does_not_become_owner_memory(text):
    assert IntentParser().parse(text).intent_name == "task"


def test_list_count_and_topic_inspection_are_bounded_read_only_operations(tmp_path):
    manager, service, _ = _manager(tmp_path)
    _handle(manager, "Remember that my favorite color is red.")
    _handle(manager, "Remember that I like going to the gym.")
    _handle(manager, "Remember that I enjoy video games.")
    before = service.profile_path.read_bytes()

    count = _handle(manager, "How many memories do you have about me?")
    listed = _handle(manager, "List my general long-term memories.")
    topic = _handle(manager, "What do you remember about the gym?")

    assert count.text == "I have one keyed fact and two active general memories about you."
    assert count.metadata["storage_status"] == "counted"
    assert "you like going to the gym" in listed.text
    assert "you enjoy video games" in listed.text
    assert topic.text == "You told me that you like going to the gym."
    assert service.profile_path.read_bytes() == before


def test_specific_deletion_requires_cross_process_confirmation_and_uses_exact_id(tmp_path):
    profile = tmp_path / "owner_profile.json"
    first, service, _ = _manager(tmp_path / "first", profile)
    _handle(first, "Remember that I like going to the gym.")
    _handle(first, "Remember that I like gym shoes.")

    requested = _handle(first, "Forget that I like going to the gym.")
    before_confirmation = service.inspect(include_values=True)

    second, reloaded, _ = _manager(tmp_path / "second", profile)
    confirmed = _handle(second, "Yes.")
    after_confirmation = reloaded.inspect(include_values=True)

    assert requested.text == "I found the memory that you like going to the gym. Should I delete it?"
    assert requested.metadata["storage_status"] == "confirmation_required"
    assert before_confirmation["memory_count"] == 2
    assert confirmed.text == "I deleted the memory that you like going to the gym."
    assert confirmed.metadata["storage_status"] == "deleted_specific"
    assert after_confirmation["memory_count"] == 1
    active = [memory for memory in after_confirmation["memories"] if memory["status"] == "active"]
    assert active[0]["object"] == "gym shoes"
    assert after_confirmation["pending_action_status"] == "missing"


def test_cancellation_clears_pending_state_and_preserves_memory(tmp_path):
    manager, service, _ = _manager(tmp_path)
    _handle(manager, "Remember that I prefer wireless mice.")
    requested = _handle(manager, "Delete my preference for wireless mice.")
    cancelled = _handle(manager, "No, cancel.")

    assert requested.metadata["storage_status"] == "confirmation_required"
    assert cancelled.text == "Deletion cancelled. I kept the memory."
    assert cancelled.metadata["storage_status"] == "cancelled"
    assert service.inspect(include_values=True)["memory_count"] == 1
    assert service.inspect()["pending_action_status"] == "missing"


def test_expired_pending_action_cannot_delete_memory(tmp_path):
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    profile = tmp_path / "owner_profile.json"
    pending = tmp_path / "pending.json"
    pending_store = PendingOwnerMemoryActionStore(pending, clock=lambda: now[0], ttl_seconds=60)
    service = OwnerMemoryService(profile, pending_store=pending_store)
    manager, _, _ = _manager(tmp_path, profile, service=service)
    _handle(manager, "Remember that I like tea.")
    _handle(manager, "Forget that I like tea.")

    now[0] += timedelta(seconds=61)
    fresh_manager, _, _ = _manager(tmp_path / "fresh", profile, service=service)
    response = _handle(fresh_manager, "Yes, delete it.")

    assert response.text == "That deletion request expired. Please ask me again."
    assert response.metadata["storage_status"] == "expired"
    assert service.inspect(include_values=True)["memory_count"] == 1
    assert not pending.exists()


def test_corrupt_pending_state_fails_closed_without_profile_mutation(tmp_path):
    manager, service, _ = _manager(tmp_path)
    _handle(manager, "Remember that I like tea.")
    _handle(manager, "Forget that I like tea.")
    before = service.profile_path.read_bytes()
    service.pending_path.write_text("{corrupt", encoding="utf-8")

    response = _handle(manager, "Yes, delete it.")

    assert response.metadata["storage_status"] == "invalid_pending"
    assert response.text == "I could not verify that deletion request, so I did not delete anything."
    assert service.profile_path.read_bytes() == before
    assert not service.pending_path.exists()


def test_ambiguous_specific_match_refuses_then_topic_confirmation_deletes_only_general(tmp_path):
    manager, service, _ = _manager(tmp_path)
    _handle(manager, "Remember that my favorite game is EVE Online.")
    _handle(manager, "Remember that I like video games.")
    _handle(manager, "Remember that I enjoy strategy games.")

    ambiguous = _handle(manager, "Forget my gaming memories.")
    topic = _handle(manager, "Forget everything about gaming.")
    before = service.inspect(include_values=True)
    confirmed = _handle(manager, "Yes, delete them.")
    after = service.inspect(include_values=True)

    assert ambiguous.metadata["storage_status"] == "ambiguous"
    assert "I found two memories matching gaming" in ambiguous.text
    assert topic.metadata["storage_status"] == "confirmation_required"
    assert "Your keyed favorite-game fact will remain" in topic.text
    assert before["memory_count"] == 2
    assert confirmed.text == "I deleted two general memories about gaming."
    assert after["memory_count"] == 0
    assert {fact["normalized_key"] for fact in after["facts"]} == {"favorite_game"}


def test_delete_all_general_requires_exact_confirmation_and_preserves_all_keyed_facts(tmp_path):
    manager, service, _ = _manager(tmp_path)
    _handle(manager, "Remember that my birthday is June 8.")
    _handle(manager, "Remember that my favorite color is red.")
    _handle(manager, "Remember that I like tea.")
    _handle(manager, "Remember that I enjoy books.")

    requested = _handle(manager, "Forget all my general long-term memories.")
    before = service.inspect(include_values=True)
    unrelated = _handle(manager, "What is my birthday?")
    confirmed = _handle(manager, "Confirm delete all general memories.")
    after = service.inspect(include_values=True)

    assert requested.metadata["storage_status"] == "confirmation_required"
    assert before["memory_count"] == 2
    assert unrelated.text == "Your birthday is June 8."
    assert confirmed.metadata["storage_status"] == "deleted_all_general"
    assert after["memory_count"] == 0
    assert {fact["normalized_key"] for fact in after["facts"]} == {"birthday", "favorite_color"}


def test_keyed_fact_delete_requires_confirmation_and_does_not_touch_general_memory(tmp_path):
    profile = tmp_path / "owner_profile.json"
    first, service, _ = _manager(tmp_path / "first", profile)
    _handle(first, "Remember that my favorite color is red.")
    _handle(first, "Remember that I like red cars.")

    requested = _handle(first, "Forget my favorite color.")
    second, reloaded, _ = _manager(tmp_path / "second", profile)
    confirmed = _handle(second, "Proceed.")
    report = reloaded.inspect(include_values=True)

    assert requested.text == "Your saved favorite color is red. Should I delete that fact?"
    assert confirmed.text == "I deleted your favorite-color fact."
    assert confirmed.metadata["storage_status"] == "deleted_keyed_fact"
    assert report["fact_count"] == 0
    assert report["memory_count"] == 1
    assert report["memories"][0]["object"] == "red cars"


def test_keyed_fact_changed_after_prompt_requires_a_new_deletion_request(tmp_path):
    manager, service, _ = _manager(tmp_path)
    _handle(manager, "Remember that my favorite color is red.")
    _handle(manager, "Forget my favorite color.")
    updated = _handle(manager, "Update my favorite color to blue.")
    confirmed = _handle(manager, "Yes, delete it.")
    recalled = _handle(manager, "What is my favorite color?")

    assert updated.metadata["storage_status"] == "updated"
    assert confirmed.metadata["storage_status"] == "target_changed"
    assert confirmed.text == "That saved fact changed after the deletion request. Please ask me again before deleting it."
    assert recalled.text == "Your favorite color is blue."
    assert service.inspect()["pending_action_status"] == "missing"


def test_pending_state_contains_bounded_normalized_request_not_raw_transcript(tmp_path):
    manager, service, _ = _manager(tmp_path)
    _handle(manager, "Remember that I like going to the gym.")
    _handle(manager, "  Forget that I like going to the gym!!!  ")

    pending = json.loads(service.pending_path.read_text(encoding="utf-8"))["data"]
    profile = service.profile_path.read_text(encoding="utf-8")

    assert pending["normalized_request"] == "forget that I like going to the gym"
    assert "!!!" not in pending["normalized_request"]
    assert "Forget that I like going to the gym" not in profile


def test_incorrectly_transcribed_saved_memory_is_never_auto_corrected_or_deleted(tmp_path):
    manager, service, _ = _manager(tmp_path)
    created = _handle(manager, "Remember that I like going on works.")
    requested = _handle(manager, "Forget that I like going on works.")
    cancelled = _handle(manager, "Never mind.")
    retained = service.inspect(include_values=True)
    requested_again = _handle(manager, "Forget that I like going on works.")
    confirmed = _handle(manager, "Yes, delete it.")

    assert created.text == "I will remember that you like going on works."
    assert requested.metadata["storage_status"] == "confirmation_required"
    assert cancelled.metadata["storage_status"] == "cancelled"
    assert retained["memory_count"] == 1
    assert retained["memories"][0]["object"] == "going on works"
    assert requested_again.metadata["storage_status"] == "confirmation_required"
    assert confirmed.text == "I deleted the memory that you like going on works."
    assert service.inspect(include_values=True)["memory_count"] == 0
