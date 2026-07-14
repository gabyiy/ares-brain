import json

import pytest

from events import EventBus
from memory import (
    OwnerMemoryRequestV1,
    OwnerMemoryService,
    OwnerProfileStore,
)
from memory.owner_memory_contracts import OWNER_MEMORY_ACTION_REMEMBER
from memory.schema_migrations import SCHEMA_OWNER_PROFILE, SchemaEnvelope


def test_v1_profile_migrates_sequentially_to_v3_without_losing_favorite_color(tmp_path):
    path = tmp_path / "owner_profile.json"
    v1 = SchemaEnvelope.create(
        SCHEMA_OWNER_PROFILE,
        1,
        {
            "owner_id": "primary_owner",
            "facts": {
                "favorite_color": {
                    "value": "blue",
                    "updated_at": "2026-07-01T10:00:00Z",
                }
            },
        },
    )
    path.write_text(json.dumps(v1.to_dict()), encoding="utf-8")

    result = OwnerProfileStore(path, event_bus=EventBus()).recall_fact("favorite color")
    migrated = json.loads(path.read_text(encoding="utf-8"))

    assert result.value == "blue"
    assert migrated["schema_version"] == 3
    assert migrated["data"]["facts"]["favorite_color"] == {
        "value": "blue",
        "display_key": "favorite color",
        "normalized_key": "favorite_color",
        "created_at": "2026-07-01T10:00:00Z",
        "updated_at": "2026-07-01T10:00:00Z",
        "source": "explicit_owner_statement",
    }
    assert migrated["data"]["memories"] == []
    assert migrated["data"]["pending_delete_all"] is None
    assert len(list((path.parent / ".migration_backups").glob("*.bak"))) == 1


def test_backup_retention_keeps_one_last_known_good_copy(tmp_path):
    path = tmp_path / "owner_profile.json"
    store = OwnerProfileStore(path, event_bus=EventBus())

    store.save_fact("favorite color", "blue")
    store.save_fact("favorite color", "red")
    store.save_fact("favorite color", "green")

    backups = list((path.parent / ".migration_backups").glob("*.bak"))
    assert len(backups) == 1
    assert store.recall_fact("favorite color").value == "green"
    assert json.loads(backups[0].read_text(encoding="utf-8"))["schema_name"] == SCHEMA_OWNER_PROFILE


def test_fact_count_limit_fails_closed(tmp_path, monkeypatch):
    import memory.owner_profile as owner_profile

    monkeypatch.setattr(owner_profile, "MAX_OWNER_FACTS", 2)
    store = OwnerProfileStore(tmp_path / "owner_profile.json", event_bus=EventBus())
    assert store.save_fact("first fact", "one").success
    assert store.save_fact("second fact", "two").success

    rejected = store.save_fact("third fact", "three")

    assert rejected.error_code == "fact_limit_reached"
    assert store.list_facts().metadata["fact_count"] == 2


def test_profile_size_limit_preserves_previous_file(tmp_path, monkeypatch):
    import memory.owner_profile as owner_profile

    path = tmp_path / "owner_profile.json"
    store = OwnerProfileStore(path, event_bus=EventBus())
    assert store.save_fact("first fact", "one").success
    original = path.read_text(encoding="utf-8")
    monkeypatch.setattr(owner_profile, "MAX_OWNER_PROFILE_BYTES", 10)

    rejected = store.save_fact("second fact", "two")

    assert rejected.error_code == "profile_size_limit_reached"
    assert path.read_text(encoding="utf-8") == original


def test_versioned_service_contract_preserves_correlation_and_metadata(tmp_path):
    service = OwnerMemoryService(tmp_path / "owner_profile.json", event_bus=EventBus())
    request = OwnerMemoryRequestV1(
        action=OWNER_MEMORY_ACTION_REMEMBER,
        normalized_key="birthday",
        display_key="birthday",
        value="June 8",
        correlation_id="corr-1",
        session_id="session-1",
        metadata={"transport": "text"},
    )

    result = service.execute(OwnerMemoryRequestV1.from_dict(request.to_dict()))

    assert result.success is True
    assert result.correlation_id == "corr-1"
    assert result.session_id == "session-1"
    assert result.contract_version == "v1"
    assert service.execute({"contract_name": "wrong", "contract_version": "v1", "action": "remember"}).error_code == "invalid_owner_memory_contract"


def test_list_supports_only_simple_bounded_values(tmp_path):
    store = OwnerProfileStore(tmp_path / "owner_profile.json", event_bus=EventBus())
    assert store.save_fact("lucky numbers", [8, 42]).success

    listed = store.list_facts(include_values=True)

    assert listed.facts[0]["value"] == [8, 42]


def test_concurrent_transaction_lock_rejects_mutation_without_overwrite(tmp_path):
    from memory.schema_migrations import StoreWriteLock

    path = tmp_path / "owner_profile.json"
    store = OwnerProfileStore(path, event_bus=EventBus())
    transaction_path = path.with_name(f"{path.name}.owner_transaction")
    with StoreWriteLock(transaction_path):
        result = store.save_fact("birthday", "June 8")

    assert result.success is False
    assert result.error_code == "store_locked"
    assert not path.exists()
