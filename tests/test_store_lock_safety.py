from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from memory.owner_profile import OwnerProfileStore
from memory.schema_migrations import (
    LOCK_METADATA_SCHEMA,
    LOCK_METADATA_VERSION,
    MigrationError,
    StoreWriteLock,
    inspect_store_lock,
    recover_store_lock,
    store_lock_path,
)
from scripts import inspect_store_lock as inspect_store_lock_script


FIXED_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _write_owned_lock(path, *, pid=43210, age_seconds=120, hostname=None):
    lock_path = store_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "schema_name": LOCK_METADATA_SCHEMA,
                "schema_version": LOCK_METADATA_VERSION,
                "pid": pid,
                "hostname": hostname or socket.gethostname(),
                "owner_token": "dead-owner-token",
                "owner_kind": "test_store_write",
                "created_at": (FIXED_NOW - timedelta(seconds=age_seconds))
                .isoformat()
                .replace("+00:00", "Z"),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return lock_path


def test_store_lock_metadata_identifies_live_owner_and_releases_in_finally(tmp_path):
    target = tmp_path / "events.json"
    with StoreWriteLock(
        target,
        owner_kind="event_history_write",
        now=lambda: FIXED_NOW,
    ):
        report = inspect_store_lock(
            target,
            now=lambda: FIXED_NOW + timedelta(seconds=2),
            process_alive=lambda pid: True,
        )
        assert report.metadata_valid is True
        assert report.owner_pid > 0
        assert report.owner_hostname == socket.gethostname()
        assert report.owner_kind == "event_history_write"
        assert report.owner_token
        assert report.owner_process_state == "alive"
        assert report.safe_recovery_possible is False
    assert not store_lock_path(target).exists()


def test_stale_dead_local_owner_lock_can_be_recovered_atomically(tmp_path):
    target = tmp_path / "events.json"
    _write_owned_lock(target)

    report = inspect_store_lock(
        target,
        stale_after_seconds=30,
        now=lambda: FIXED_NOW,
        process_alive=lambda pid: False,
    )
    recovery = recover_store_lock(
        target,
        stale_after_seconds=30,
        now=lambda: FIXED_NOW,
        process_alive=lambda pid: False,
    )

    assert report.owner_process_state == "dead"
    assert report.expired is True
    assert report.safe_recovery_possible is True
    assert recovery.success is True
    assert recovery.recovered is True
    assert recovery.status == "recovered_dead_owner_lock"
    assert not store_lock_path(target).exists()


def test_live_owner_lock_cannot_be_stolen(tmp_path):
    target = tmp_path / "events.json"
    _write_owned_lock(target)

    recovery = recover_store_lock(
        target,
        stale_after_seconds=30,
        now=lambda: FIXED_NOW,
        process_alive=lambda pid: True,
    )

    assert recovery.success is False
    assert recovery.recovered is False
    assert recovery.inspection.owner_process_state == "alive"
    assert store_lock_path(target).exists()


def test_malformed_lock_metadata_is_visible_and_not_deleted(tmp_path):
    target = tmp_path / "events.json"
    lock_path = store_lock_path(target)
    lock_path.write_text("not valid lock metadata", encoding="utf-8")

    report = inspect_store_lock(target, now=lambda: FIXED_NOW)
    recovery = recover_store_lock(target, now=lambda: FIXED_NOW)

    assert report.metadata_valid is False
    assert report.metadata_format == "malformed"
    assert "lock_metadata_malformed" in report.error_message
    assert recovery.success is False
    assert recovery.recovered is False
    assert lock_path.exists()
    with pytest.raises(MigrationError) as error:
        with StoreWriteLock(target):
            pass
    assert error.value.status == "store_locked"


def test_keyboard_interrupt_releases_owned_store_lock(tmp_path):
    target = tmp_path / "events.json"
    with pytest.raises(KeyboardInterrupt):
        with StoreWriteLock(target):
            raise KeyboardInterrupt
    assert not store_lock_path(target).exists()


def test_owner_memory_lock_failure_remains_strict_and_does_not_mutate_profile(tmp_path):
    profile = tmp_path / "owner_profile.json"
    store = OwnerProfileStore(path=profile)
    with StoreWriteLock(store._transaction_path):
        result = store.save_fact("favorite color", "blue")

    assert result.success is False
    assert result.status not in {"created", "updated"}
    assert not profile.exists()


def test_lock_inspection_script_reports_live_runtime_owner_without_recovery(tmp_path):
    target = tmp_path / "ares_standby_voice.runtime"
    output = []
    with StoreWriteLock(target, owner_kind="ares_standby_voice_runtime"):
        code = inspect_store_lock_script.run([str(target)], output_func=output.append)
        recovery_code = inspect_store_lock_script.run(
            [str(target), "--recover-if-owner-dead"],
            output_func=output.append,
        )

    text = "\n".join(output)
    assert code == 0
    assert recovery_code == 1
    assert "Owner process: alive" in text
    assert "Safe recovery possible: no" in text
    assert "Recovered: no" in text
