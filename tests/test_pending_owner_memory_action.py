from datetime import datetime, timedelta, timezone
import json

from memory import (
    PENDING_OWNER_MEMORY_SCHEMA,
    PENDING_OWNER_MEMORY_SCHEMA_VERSION,
    PendingOwnerMemoryActionStore,
)
from memory.pending_owner_memory import (
    PENDING_OPERATION_FORGET_KEYED_FACT,
    PENDING_OPERATION_FORGET_SPECIFIC,
    PENDING_TARGET_GENERAL_MEMORY,
    PENDING_TARGET_KEYED_FACT,
)


def test_pending_action_round_trip_uses_versioned_atomic_json(tmp_path):
    path = tmp_path / "runtime" / "pending.json"
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = PendingOwnerMemoryActionStore(path, clock=lambda: now)

    created = store.create(
        operation=PENDING_OPERATION_FORGET_SPECIFIC,
        target_kind=PENDING_TARGET_GENERAL_MEMORY,
        target_ids=("mem-0123456789abcdef",),
        candidate_count=1,
        topic="gym",
        summary="you like going to the gym",
        normalized_request="forget memory about gym",
    )
    loaded = store.read()
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert created.success is True
    assert loaded.status == "active"
    assert loaded.action == created.action
    assert payload["schema_name"] == PENDING_OWNER_MEMORY_SCHEMA
    assert payload["schema_version"] == PENDING_OWNER_MEMORY_SCHEMA_VERSION
    assert payload["data"]["target_ids"] == ["mem-0123456789abcdef"]
    assert not path.with_suffix(".json.tmp").exists()


def test_keyed_fact_pending_action_does_not_require_a_topic(tmp_path):
    store = PendingOwnerMemoryActionStore(tmp_path / "pending.json")

    result = store.create(
        operation=PENDING_OPERATION_FORGET_KEYED_FACT,
        target_kind=PENDING_TARGET_KEYED_FACT,
        target_key="favorite_color",
        target_revision="a" * 64,
        candidate_count=1,
        summary="favorite color is red",
        normalized_request="forget my favorite color",
    )

    assert result.success is True
    assert result.action is not None
    assert result.action.topic == ""
    assert result.action.target_ids == ()


def test_pending_action_expires_with_injected_clock_and_is_removed_on_use(tmp_path):
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    path = tmp_path / "pending.json"
    store = PendingOwnerMemoryActionStore(path, clock=lambda: now[0], ttl_seconds=60)
    store.create(
        operation=PENDING_OPERATION_FORGET_SPECIFIC,
        target_kind=PENDING_TARGET_GENERAL_MEMORY,
        target_ids=("mem-0123456789abcdef",),
        candidate_count=1,
        topic="gym",
        summary="gym preference",
        normalized_request="forget gym preference",
    )

    now[0] += timedelta(seconds=61)
    expired = store.read(cleanup_expired=True)

    assert expired.status == "expired"
    assert expired.action is not None
    assert not path.exists()


def test_corrupt_pending_action_is_reported_without_mutating_on_inspection(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text("{corrupt", encoding="utf-8")
    store = PendingOwnerMemoryActionStore(path)

    inspected = store.inspection_report()

    assert inspected["validation_state"] == "invalid"
    assert inspected["error_code"] == "invalid_pending_state"
    assert path.read_text(encoding="utf-8") == "{corrupt"


def test_failed_pending_replace_preserves_previous_valid_state_and_cleans_temp(tmp_path):
    path = tmp_path / "pending.json"
    store = PendingOwnerMemoryActionStore(path)
    original = store.create(
        operation=PENDING_OPERATION_FORGET_SPECIFIC,
        target_kind=PENDING_TARGET_GENERAL_MEMORY,
        target_ids=("mem-0123456789abcdef",),
        candidate_count=1,
        topic="gym",
        summary="gym preference",
        normalized_request="forget gym preference",
    )
    before = path.read_bytes()

    def fail_replace(_source: str, _target: str) -> None:
        raise OSError("injected replace failure")

    failing = PendingOwnerMemoryActionStore(path, replace_func=fail_replace)
    result = failing.create(
        operation=PENDING_OPERATION_FORGET_KEYED_FACT,
        target_kind=PENDING_TARGET_KEYED_FACT,
        target_key="favorite_color",
        target_revision="a" * 64,
        candidate_count=1,
        summary="favorite color is red",
        normalized_request="forget favorite color",
    )

    assert original.success is True
    assert result.success is False
    assert result.error_code == "write_failed"
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.tmp").exists()


def test_new_destructive_request_replaces_only_the_previous_pending_action(tmp_path):
    path = tmp_path / "pending.json"
    store = PendingOwnerMemoryActionStore(path)
    first = store.create(
        operation=PENDING_OPERATION_FORGET_SPECIFIC,
        target_kind=PENDING_TARGET_GENERAL_MEMORY,
        target_ids=("mem-0123456789abcdef",),
        candidate_count=1,
        topic="gym",
        summary="gym preference",
        normalized_request="forget gym preference",
    )
    second = store.create(
        operation=PENDING_OPERATION_FORGET_KEYED_FACT,
        target_kind=PENDING_TARGET_KEYED_FACT,
        target_key="birthday",
        target_revision="b" * 64,
        candidate_count=1,
        summary="birthday is June 8",
        normalized_request="forget my birthday",
    )

    assert first.action is not None and second.action is not None
    assert first.action.action_id != second.action.action_id
    assert second.metadata["replaced_previous"] is True
    assert store.read().action == second.action
