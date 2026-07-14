import json

import pytest

from core.OwnerMemory import (
    MAX_OWNER_FACT_KEY_SOURCE_LENGTH,
    MAX_OWNER_FACT_VALUE_LENGTH,
)
from events import EventBus
from memory import OwnerProfileStore
from memory.schema_migrations import SCHEMA_OWNER_PROFILE, SchemaEnvelope


def _payload(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_missing_profile_is_empty_without_creating_a_file(tmp_path):
    path = tmp_path / "memory" / "owner_profile.json"
    store = OwnerProfileStore(path, event_bus=EventBus())

    result = store.list_facts()

    assert result.success is True
    assert result.status == "listed"
    assert result.metadata == {
        "owner_id": "primary_owner",
        "fact_keys": [],
        "fact_count": 0,
    }
    assert not path.exists()


def test_save_creates_parent_and_versioned_utf8_profile_atomically(tmp_path):
    path = tmp_path / "nested" / "memory" / "owner_profile.json"
    store = OwnerProfileStore(
        path,
        event_bus=EventBus(),
        timestamp_factory=lambda: "2026-07-14T10:00:00Z",
    )

    result = store.save_fact("favorite color", "blå")
    payload = _payload(path)

    assert result.status == "created"
    assert payload["schema_name"] == SCHEMA_OWNER_PROFILE
    assert payload["schema_version"] == 1
    assert payload["data"] == {
        "owner_id": "primary_owner",
        "facts": {
            "favorite_color": {
                "updated_at": "2026-07-14T10:00:00Z",
                "value": "blå",
            }
        },
    }
    assert not path.with_suffix(".json.tmp").exists()


def test_recall_survives_new_store_instance(tmp_path):
    path = tmp_path / "owner_profile.json"
    OwnerProfileStore(path, event_bus=EventBus()).save_fact("favorite color", "blue")

    result = OwnerProfileStore(path, event_bus=EventBus()).recall_fact("favorite color")

    assert result.success is True
    assert result.status == "recalled"
    assert result.value == "blue"


def test_update_and_forget_are_persistent(tmp_path):
    path = tmp_path / "owner_profile.json"
    store = OwnerProfileStore(path, event_bus=EventBus())

    assert store.save_fact("favorite color", "blue").status == "created"
    assert store.save_fact("favorite color", "red").status == "updated"
    assert OwnerProfileStore(path, event_bus=EventBus()).recall_fact("favorite color").value == "red"
    assert store.forget_fact("favorite color").status == "forgotten"
    assert OwnerProfileStore(path, event_bus=EventBus()).recall_fact("favorite color").status == "missing"


def test_forget_missing_fact_is_safe_and_does_not_create_file(tmp_path):
    path = tmp_path / "owner_profile.json"

    result = OwnerProfileStore(path, event_bus=EventBus()).forget_fact("favorite color")

    assert result.success is True
    assert result.status == "missing"
    assert not path.exists()


@pytest.mark.parametrize(
    "source",
    ["favorite color", "favorite colour", "  Favorite   Colour!!!  "],
)
def test_favorite_colour_and_whitespace_normalize_to_one_key(tmp_path, source):
    store = OwnerProfileStore(tmp_path / "owner_profile.json", event_bus=EventBus())

    result = store.save_fact(source, " blue. ")

    assert result.normalized_key == "favorite_color"
    assert store.recall_fact("favorite color").value == "blue"


def test_fact_keys_are_serialized_in_deterministic_order(tmp_path):
    path = tmp_path / "owner_profile.json"
    store = OwnerProfileStore(path, event_bus=EventBus())
    store.save_fact("timezone", "Europe Madrid")
    store.save_fact("favorite color", "blue")

    facts = _payload(path)["data"]["facts"]

    assert list(facts) == ["favorite_color", "timezone"]


@pytest.mark.parametrize(
    ("key", "value", "error_code"),
    [
        ("", "blue", "empty_key"),
        ("favorite color", "", "empty_value"),
        ("favorite\ncolor", "blue", "control_character_rejected"),
        ("favorite color", "blue\nred", "control_character_rejected"),
        ("../password", "secret", "path_like_key_rejected"),
        ("x" * (MAX_OWNER_FACT_KEY_SOURCE_LENGTH + 1), "blue", "key_too_long"),
        ("favorite color", "x" * (MAX_OWNER_FACT_VALUE_LENGTH + 1), "value_too_long"),
    ],
)
def test_invalid_fact_input_is_rejected_without_writing(tmp_path, key, value, error_code):
    path = tmp_path / "owner_profile.json"

    result = OwnerProfileStore(path, event_bus=EventBus()).save_fact(key, value)

    assert result.success is False
    assert result.status == "rejected"
    assert result.error_code == error_code
    assert not path.exists()


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "passcode",
        "pin",
        "api key",
        "access token",
        "private key",
        "recovery phrase",
        "seed phrase",
    ],
)
def test_protected_key_is_rejected_and_value_never_enters_result_or_event(tmp_path, key):
    secret = "DO_NOT_EXPOSE_123"
    bus = EventBus()
    result = OwnerProfileStore(tmp_path / "owner_profile.json", event_bus=bus).save_fact(
        key,
        secret,
    )

    assert result.success is False
    assert result.error_code == "protected_key_rejected"
    assert secret not in str(result.to_dict())
    assert secret not in str([event.payload for event in bus.history()])


def test_malformed_json_fails_closed_and_preserves_original(tmp_path):
    path = tmp_path / "owner_profile.json"
    path.write_text("{ malformed", encoding="utf-8")

    result = OwnerProfileStore(path, event_bus=EventBus()).recall_fact("favorite color")

    assert result.success is False
    assert result.status == "storage_failed"
    assert result.error_code == "invalid_json"
    assert path.read_text(encoding="utf-8") == "{ malformed"


def test_unsupported_future_schema_fails_closed_and_preserves_file(tmp_path):
    path = tmp_path / "owner_profile.json"
    future = SchemaEnvelope.create(
        SCHEMA_OWNER_PROFILE,
        99,
        {"owner_id": "primary_owner", "facts": {}},
    ).to_dict()
    path.write_text(json.dumps(future), encoding="utf-8")
    original = path.read_text(encoding="utf-8")

    result = OwnerProfileStore(path, event_bus=EventBus()).recall_fact("favorite color")

    assert result.success is False
    assert result.error_code == "future_schema_version"
    assert path.read_text(encoding="utf-8") == original


def test_unknown_durable_fields_are_rejected_without_reset(tmp_path):
    path = tmp_path / "owner_profile.json"
    payload = SchemaEnvelope.create(
        SCHEMA_OWNER_PROFILE,
        1,
        {"owner_id": "primary_owner", "facts": {}, "unknown": "keep evidence"},
    ).to_dict()
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = OwnerProfileStore(path, event_bus=EventBus()).list_facts()

    assert result.success is False
    assert result.error_code == "invalid_store_data"
    assert "unknown" in path.read_text(encoding="utf-8")


def test_injected_read_failure_returns_structured_failure(tmp_path):
    def fail_read(*args, **kwargs):
        raise OSError("read denied")

    result = OwnerProfileStore(
        tmp_path / "owner_profile.json",
        event_bus=EventBus(),
        loader=fail_read,
    ).recall_fact("favorite color")

    assert result.success is False
    assert result.error_code == "read_failed"
    assert "read denied" not in result.error_message


def test_injected_write_failure_returns_structured_failure_without_file(tmp_path):
    def fail_write(*args, **kwargs):
        raise OSError("disk denied")

    path = tmp_path / "owner_profile.json"
    result = OwnerProfileStore(
        path,
        event_bus=EventBus(),
        saver=fail_write,
    ).save_fact("favorite color", "blue")

    assert result.success is False
    assert result.error_code == "write_failed"
    assert not path.exists()


def test_atomic_write_failure_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    import memory.schema_migrations as migrations

    path = tmp_path / "owner_profile.json"
    store = OwnerProfileStore(path, event_bus=EventBus())
    assert store.save_fact("favorite color", "blue").success is True
    original = path.read_text(encoding="utf-8")

    def fail_atomic(store_path, envelope, registry):
        store_path.with_suffix(store_path.suffix + ".tmp").write_text(
            "partial",
            encoding="utf-8",
        )
        raise OSError("replacement failed")

    monkeypatch.setattr(migrations, "_atomic_write_envelope", fail_atomic)

    result = store.save_fact("favorite color", "red")

    assert result.success is False
    assert path.read_text(encoding="utf-8") == original
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_list_facts_hides_values_by_default(tmp_path):
    secret_like_value = "ordinary owner fact"
    store = OwnerProfileStore(tmp_path / "owner_profile.json", event_bus=EventBus())
    store.save_fact("favorite color", secret_like_value)

    result = store.list_facts()

    assert result.metadata["fact_keys"] == ["favorite_color"]
    assert secret_like_value not in str(result.to_dict())


def test_profile_contains_fact_only_not_transcript_or_audio(tmp_path):
    path = tmp_path / "owner_profile.json"
    store = OwnerProfileStore(path, event_bus=EventBus())
    store.save_fact("favorite color", "blue")
    serialized = path.read_text(encoding="utf-8")

    assert "favorite_color" in serialized
    assert "blue" in serialized
    assert "remember that my" not in serialized
    assert ".wav" not in serialized
    assert "transcript" not in serialized
