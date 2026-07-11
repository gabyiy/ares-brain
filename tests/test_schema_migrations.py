import json
from pathlib import Path

import pytest

from core import CoreService
from events import EventBus, EventHistoryStore
from memory import NotesStore
from memory.schema_migrations import (
    DEFAULT_MIGRATION_REGISTRY,
    SCHEMA_NOTES,
    SCHEMA_TEST_FIXTURE,
    SCHEMA_VERSION_1,
    SCHEMA_VERSION_2,
    SchemaEnvelope,
    MigrationError,
    MigrationRegistry,
    inspect_store,
    load_store_envelope,
    record_migration_failure,
    save_store_data,
)


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _valid_note(note_id="note-1", text="calibrate sensors"):
    return {"id": note_id, "timestamp": "2026-01-01T00:00:00Z", "text": text}


def _test_registry(current_version=3, order=None):
    registry = MigrationRegistry()

    def validate_v1(data):
        if not isinstance(data, dict) or "items" not in data:
            raise MigrationError("v1 requires items")

    def validate_v2(data):
        if not isinstance(data, dict) or "entries" not in data:
            raise MigrationError("v2 requires entries")

    def validate_v3(data):
        if not isinstance(data, dict) or "records" not in data:
            raise MigrationError("v3 requires records")

    validators = {1: validate_v1, 2: validate_v2, 3: validate_v3}
    registry.register_schema(
        "test.schema",
        current_version,
        validators[current_version],
        validators=validators,
    )

    def migrate_1_to_2(envelope):
        if order is not None:
            order.append("1-2")
        return envelope.with_data({"entries": envelope.data["items"]}, 2)

    def migrate_2_to_3(envelope):
        if order is not None:
            order.append("2-3")
        return envelope.with_data({"records": envelope.data["entries"]}, 3)

    registry.register_migration("test.schema", 1, 2, migrate_1_to_2)
    if current_version >= 3:
        registry.register_migration("test.schema", 2, 3, migrate_2_to_3)
    return registry


def test_current_schema_loads_without_migration(tmp_path):
    path = tmp_path / "notes.json"
    save_store_data(path, SCHEMA_NOTES, [_valid_note()])

    result = load_store_envelope(path, SCHEMA_NOTES, [])

    assert result.status == "current"
    assert result.migration_needed is False
    assert result.changed is False
    assert result.envelope.data[0]["text"] == "calibrate sensors"


def test_known_legacy_format_imports_into_v1_with_backup(tmp_path):
    path = tmp_path / "notes.json"
    _write_json(path, [_valid_note()])

    result = load_store_envelope(path, SCHEMA_NOTES, [])
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert result.status == "imported"
    assert result.backup_path
    assert payload["schema_name"] == SCHEMA_NOTES
    assert payload["schema_version"] == SCHEMA_VERSION_1
    assert payload["data"][0]["text"] == "calibrate sensors"


def test_missing_schema_version_is_not_guessed_unless_legacy_matches(tmp_path):
    path = tmp_path / "notes.json"
    _write_json(path, {"schema_name": SCHEMA_NOTES, "data": []})

    with pytest.raises(MigrationError, match="Malformed schema envelope"):
        load_store_envelope(path, SCHEMA_NOTES, [])

    random_path = tmp_path / "random.json"
    _write_json(random_path, {"unexpected": True})
    with pytest.raises(MigrationError, match="Unrecognized legacy list format"):
        load_store_envelope(random_path, SCHEMA_NOTES, [])


def test_v1_to_v2_migration_works_for_test_fixture():
    envelope = SchemaEnvelope.create(
        SCHEMA_TEST_FIXTURE,
        SCHEMA_VERSION_1,
        {"items": ["alpha"]},
        metadata={"durable": "kept"},
    )

    result = DEFAULT_MIGRATION_REGISTRY.migrate(envelope)

    assert result.success is True
    assert result.target_version == SCHEMA_VERSION_2
    assert result.migration_path == [(1, 2)]
    assert result.envelope.data == {"entries": ["alpha"], "migrated_from": 1}
    assert result.envelope.metadata["durable"] == "kept"


def test_multi_step_migration_order_is_correct():
    order = []
    registry = _test_registry(current_version=3, order=order)
    envelope = SchemaEnvelope.create("test.schema", 1, {"items": ["a"]})

    result = registry.migrate(envelope)

    assert result.envelope.schema_version == 3
    assert result.envelope.data == {"records": ["a"]}
    assert order == ["1-2", "2-3"]


def test_unknown_future_version_is_rejected(tmp_path):
    path = tmp_path / "notes.json"
    _write_json(
        path,
        SchemaEnvelope.create(SCHEMA_NOTES, 99, [_valid_note()]).to_dict(),
    )

    with pytest.raises(MigrationError) as error:
        load_store_envelope(path, SCHEMA_NOTES, [])

    assert error.value.status == "future_schema_version"


def test_missing_migration_path_is_rejected():
    registry = MigrationRegistry()
    registry.register_schema("test.missing", 3, lambda data: None, validators={1: lambda data: None, 3: lambda data: None})
    envelope = SchemaEnvelope.create("test.missing", 1, {"anything": True})

    with pytest.raises(MigrationError) as error:
        registry.migrate(envelope)

    assert error.value.status == "missing_migration_path"


def test_duplicate_migration_registration_is_rejected():
    registry = _test_registry(current_version=2)

    with pytest.raises(ValueError, match="Duplicate migration edge"):
        registry.register_migration("test.schema", 1, 2, lambda envelope: envelope)


def test_cyclic_registration_is_rejected():
    registry = _test_registry(current_version=2)

    with pytest.raises(ValueError, match="creates a cycle"):
        registry.register_migration("test.schema", 2, 1, lambda envelope: envelope)


def test_dry_run_does_not_modify_source_or_create_backup(tmp_path):
    path = tmp_path / "notes.json"
    legacy = [_valid_note()]
    _write_json(path, legacy)

    result = load_store_envelope(path, SCHEMA_NOTES, [], dry_run=True)

    assert result.dry_run is True
    assert result.changed is False
    assert json.loads(path.read_text(encoding="utf-8")) == legacy
    assert not (tmp_path / ".migration_backups").exists()


def test_backup_created_before_write(tmp_path):
    path = tmp_path / "notes.json"
    legacy = [_valid_note()]
    _write_json(path, legacy)

    result = load_store_envelope(path, SCHEMA_NOTES, [])
    backup = tmp_path / ".migration_backups"

    assert result.backup_path
    assert backup.exists()
    backup_path = Path(result.backup_path)
    assert json.loads(backup_path.read_text(encoding="utf-8")) == legacy
    assert backup_path.with_suffix(backup_path.suffix + ".meta.json").exists()


def test_original_preserved_when_migration_fails(tmp_path):
    registry = MigrationRegistry()
    registry.register_schema("test.fail", 2, lambda data: None, validators={1: lambda data: None, 2: lambda data: None})

    def fail(envelope):
        raise MigrationError("step failed", schema_name="test.fail")

    registry.register_migration("test.fail", 1, 2, fail)
    envelope = SchemaEnvelope.create("test.fail", 1, {"value": 1})

    with pytest.raises(MigrationError, match="step failed"):
        registry.migrate(envelope)

    assert envelope.data == {"value": 1}


def test_temporary_file_does_not_replace_original_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "notes.json"
    legacy = [_valid_note()]
    _write_json(path, legacy)

    import memory.schema_migrations as migrations

    def broken_atomic_write(store_path, envelope, registry):
        store_path.with_suffix(store_path.suffix + ".tmp").write_text("partial", encoding="utf-8")
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(migrations, "_atomic_write_envelope", broken_atomic_write)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        load_store_envelope(path, SCHEMA_NOTES, [])

    assert json.loads(path.read_text(encoding="utf-8")) == legacy
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_malformed_json_rejected_without_reset(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text("{ bad json", encoding="utf-8")

    with pytest.raises(MigrationError) as error:
        load_store_envelope(path, SCHEMA_NOTES, [])

    assert error.value.status == "invalid_json"
    assert path.read_text(encoding="utf-8") == "{ bad json"


def test_truncated_file_rejected_without_reset(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text("[", encoding="utf-8")

    with pytest.raises(MigrationError):
        load_store_envelope(path, SCHEMA_NOTES, [])

    assert path.read_text(encoding="utf-8") == "["


def test_wrong_schema_name_rejected(tmp_path):
    path = tmp_path / "notes.json"
    _write_json(path, SchemaEnvelope.create("other.schema", 1, []).to_dict())

    with pytest.raises(MigrationError) as error:
        load_store_envelope(path, SCHEMA_NOTES, [])

    assert error.value.status == "wrong_schema_name"


def test_post_migration_validation_failure_preserves_original():
    registry = MigrationRegistry()

    def validate_v1(data):
        if "items" not in data:
            raise MigrationError("invalid v1")

    def validate_v2(data):
        if "entries" not in data:
            raise MigrationError("invalid v2")

    registry.register_schema("test.invalid_post", 2, validate_v2, validators={1: validate_v1, 2: validate_v2})
    registry.register_migration("test.invalid_post", 1, 2, lambda envelope: envelope.with_data({"bad": True}, 2))
    envelope = SchemaEnvelope.create("test.invalid_post", 1, {"items": ["a"]})

    with pytest.raises(MigrationError, match="invalid v2"):
        registry.migrate(envelope)

    assert envelope.data == {"items": ["a"]}


def test_unknown_durable_metadata_survives_where_safe():
    envelope = SchemaEnvelope.create(
        SCHEMA_TEST_FIXTURE,
        SCHEMA_VERSION_1,
        {"items": ["a"]},
        metadata={"unknown_future": {"keep": True}},
    )

    result = DEFAULT_MIGRATION_REGISTRY.migrate(envelope)

    assert result.envelope.metadata["unknown_future"] == {"keep": True}


def test_deterministic_serialization():
    envelope = SchemaEnvelope.create(SCHEMA_NOTES, 1, [_valid_note()], metadata={"a": 1})

    first = envelope.to_dict()
    second = envelope.to_dict()

    assert first == second
    assert list(first) == [
        "schema_name",
        "schema_version",
        "created_at",
        "updated_at",
        "data",
        "metadata",
    ]


def test_store_remains_usable_after_successful_migration(tmp_path):
    path = tmp_path / "notes.json"
    _write_json(path, [_valid_note()])

    store = NotesStore(path=path, event_bus=EventBus())

    assert [note.text for note in store.list()] == ["calibrate sensors"]
    added = store.add("second note")
    assert added.text == "second note"
    assert json.loads(path.read_text(encoding="utf-8"))["schema_name"] == SCHEMA_NOTES


def test_unrelated_cities_do_not_activate_during_migration_failure(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text("{ bad json", encoding="utf-8")
    store = NotesStore(path=path, event_bus=EventBus())
    core = CoreService()
    before = {service["name"]: service["city_status"] for service in core.list_services()}

    with pytest.raises(MigrationError):
        store.list()

    after = {service["name"]: service["city_status"] for service in core.list_services()}
    assert after == before


def test_core_service_remains_usable_after_migration_failure(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text("{ bad json", encoding="utf-8")
    store = NotesStore(path=path, event_bus=EventBus())
    core = CoreService()

    with pytest.raises(MigrationError):
        store.list()

    assert core.get_capabilities().data["available_services"]


def test_event_history_records_migration_failure_where_configured(tmp_path):
    path = tmp_path / "notes.json"
    history = EventHistoryStore(path=tmp_path / "event_history.json")
    error = MigrationError("bad", schema_name=SCHEMA_NOTES, path=path, status="invalid_json")

    record = record_migration_failure(history, SCHEMA_NOTES, path, error)

    assert record.type == "storage.migration_failed"
    assert history.recent(type="storage.migration_failed")[0].result["data"]["status"] == "invalid_json"


def test_concurrent_migration_attempts_fail_safely(tmp_path):
    path = tmp_path / "notes.json"
    _write_json(path, [_valid_note()])
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.write_text("locked", encoding="utf-8")

    with pytest.raises(MigrationError) as error:
        load_store_envelope(path, SCHEMA_NOTES, [])

    assert error.value.status == "store_locked"
    assert json.loads(path.read_text(encoding="utf-8")) == [_valid_note()]


def test_inspection_report_does_not_expose_memory_contents(tmp_path):
    path = tmp_path / "notes.json"
    secret = "private owner memory"
    _write_json(path, [_valid_note(text=secret)])

    report = inspect_store(path, SCHEMA_NOTES)

    assert report.validation_state == "valid"
    assert secret not in str(report.to_dict())
    assert report.store_path == str(path)
