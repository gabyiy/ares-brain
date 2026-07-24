from datetime import datetime, timedelta, timezone
import json
import socket
from threading import Event as ThreadEvent, Thread
import time

import pytest

from core import (
    EVENT_DECISION_ESCALATED,
    EVENT_DECISION_RECORDED,
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    CoreEventDecisionResult,
)
from core.EventBus import Event
from events import EventHistoryStore
from memory.schema_migrations import (
    LOCK_METADATA_SCHEMA,
    LOCK_METADATA_VERSION,
    StoreWriteLock,
    store_lock_path,
)


def _event(source="voice", type="voice.status", priority=PRIORITY_NORMAL):
    return Event(
        source=source,
        type=type,
        priority=priority,
        payload={"sample": True},
    )


def _result(decision=EVENT_DECISION_RECORDED, success=True):
    return CoreEventDecisionResult(
        success=success,
        decision=decision,
        text=f"{decision} event",
        data={"escalated": decision == EVENT_DECISION_ESCALATED},
        metadata={"safe": True},
    )


def test_event_history_store_empty_history_is_safe(tmp_path):
    store = EventHistoryStore(path=tmp_path / "events.json")

    assert store.list() == []
    assert store.recent() == []
    assert store.recent(source="voice") == []
    assert store.recent(type="voice.status") == []
    assert store.recent(priority=PRIORITY_HIGH) == []


def test_event_history_store_adds_event_decision_result(tmp_path):
    store = EventHistoryStore(path=tmp_path / "events.json")
    event = _event(source="voice", type="voice.status", priority=PRIORITY_LOW)
    result = _result(EVENT_DECISION_RECORDED)

    record = store.add(event, result)

    assert record.source == "voice"
    assert record.type == "voice.status"
    assert record.priority == PRIORITY_LOW
    assert record.decision == EVENT_DECISION_RECORDED
    assert record.event == event.to_dict()
    assert record.result["decision"] == EVENT_DECISION_RECORDED
    assert store.list() == [record]
    assert (tmp_path / "events.json").exists()


def test_event_history_store_queries_recent_events_by_source_type_and_priority(tmp_path):
    store = EventHistoryStore(path=tmp_path / "events.json")
    voice = store.add(_event(source="voice", type="voice.status", priority=PRIORITY_LOW), _result())
    pc = store.add(_event(source="pc", type="device.safety", priority=PRIORITY_HIGH), _result(EVENT_DECISION_ESCALATED))
    weather = store.add(_event(source="weather", type="weather.changed", priority=PRIORITY_NORMAL), _result())

    assert store.recent(source="pc") == [pc]
    assert store.recent(type="voice.status") == [voice]
    assert store.recent(priority=PRIORITY_HIGH) == [pc]
    assert store.recent(limit=2) == [weather, pc]


def test_event_history_store_limits_max_history_size_safely(tmp_path):
    store = EventHistoryStore(path=tmp_path / "events.json", max_records=2)
    first = store.add(_event(source="voice", type="first", priority=PRIORITY_LOW), _result())
    second = store.add(_event(source="pc", type="second", priority=PRIORITY_HIGH), _result(EVENT_DECISION_ESCALATED))
    third = store.add(_event(source="weather", type="third", priority=PRIORITY_CRITICAL), _result(EVENT_DECISION_ESCALATED))

    assert first not in store.list()
    assert store.list() == [second, third]
    assert store.recent() == [third, second]


def test_event_history_store_persists_after_reload(tmp_path):
    path = tmp_path / "events.json"
    store = EventHistoryStore(path=path)
    record = store.add(
        _event(source="calendar", type="calendar.changed", priority=PRIORITY_NORMAL),
        _result(EVENT_DECISION_RECORDED),
    )

    reloaded = EventHistoryStore(path=path)

    assert reloaded.list() == [record]
    assert reloaded.recent(source="calendar") == [record]


def test_event_history_store_rejects_invalid_priority(tmp_path):
    store = EventHistoryStore(path=tmp_path / "events.json")

    with pytest.raises(ValueError, match="Invalid event priority"):
        store.add(
            {"source": "voice", "type": "voice.status", "priority": "urgent"},
            {"decision": EVENT_DECISION_RECORDED},
        )


def test_event_history_store_zero_max_size_keeps_no_records(tmp_path):
    store = EventHistoryStore(path=tmp_path / "events.json", max_records=0)

    store.add(_event(), _result())

    assert store.list() == []


def test_event_history_lock_failure_warns_counts_drop_and_does_not_raise(tmp_path):
    path = tmp_path / "events.json"
    warnings = []
    store = EventHistoryStore(path=path, warning_callback=warnings.append)
    event = {
        "source": "brain_runtime",
        "type": "brain_activation_accepted",
        "priority": "normal",
        "payload": {"safe": True},
    }
    result = {"success": True, "decision": "recorded"}

    with StoreWriteLock(path):
        first = store.add(event, result)
        second = store.add(event, result)

    assert first is None and second is None
    assert store.dropped_event_count == 2
    assert len(warnings) == 1
    assert warnings[0].startswith("WARNING: event history append skipped: store locked:")
    assert str(path) in warnings[0]


@pytest.mark.parametrize(
    "value",
    [True, "0.05", float("-inf"), float("inf"), 0, 0.0009, 5.001],
)
def test_event_history_append_lock_timeout_is_strictly_validated(tmp_path, value):
    with pytest.raises(ValueError, match="append_lock_timeout_seconds"):
        EventHistoryStore(
            path=tmp_path / "events.json",
            append_lock_timeout_seconds=value,
        )


def test_contended_append_returns_within_bound_and_preserves_live_store_lock(tmp_path):
    path = tmp_path / "events.json"
    warnings = []
    store = EventHistoryStore(
        path=path,
        warning_callback=warnings.append,
        append_lock_timeout_seconds=0.01,
    )
    owner_ready = ThreadEvent()
    owner_release = ThreadEvent()

    def hold_internal_append_lock():
        with store._lock:
            owner_ready.set()
            owner_release.wait(2.0)

    owner = Thread(target=hold_internal_append_lock)
    owner.start()
    assert owner_ready.wait(1.0)

    try:
        with StoreWriteLock(path):
            started = time.monotonic()
            first = store.add(_event(), _result())
            second = store.add(_event(), _result())
            elapsed = time.monotonic() - started

            assert store_lock_path(path).exists()
    finally:
        owner_release.set()
        owner.join(timeout=1.0)

    assert not owner.is_alive()
    assert first is None and second is None
    assert elapsed < 0.5
    assert store.dropped_event_count == 2
    assert len(warnings) == 1
    assert "append_lock_timeout" in warnings[0]


def test_event_history_unexpected_programming_error_still_surfaces(tmp_path, monkeypatch):
    store = EventHistoryStore(path=tmp_path / "events.json", warning_callback=lambda _: None)
    monkeypatch.setattr(store, "_save", lambda records: (_ for _ in ()).throw(RuntimeError("bug")))

    with pytest.raises(RuntimeError, match="bug"):
        store.add(
            {"source": "test", "type": "unexpected", "priority": "normal"},
            {"success": True, "decision": "recorded"},
        )
    assert store.recent() == []


def test_event_history_recovers_only_expired_dead_owner_lock(tmp_path):
    path = tmp_path / "events.json"
    lock_path = store_lock_path(path)
    lock_path.write_text(
        json.dumps(
            {
                "schema_name": LOCK_METADATA_SCHEMA,
                "schema_version": LOCK_METADATA_VERSION,
                "pid": 987654,
                "hostname": socket.gethostname(),
                "owner_token": "expired-dead-event-owner",
                "owner_kind": "event_history_append",
                "created_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=120)
                ).isoformat().replace("+00:00", "Z"),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    warnings = []
    store = EventHistoryStore(
        path=path,
        warning_callback=warnings.append,
        stale_lock_seconds=30,
        lock_process_alive=lambda pid: False,
    )

    record = store.add(_event(), _result())

    assert record is not None
    assert warnings == []
    assert store.dropped_event_count == 0
    assert not lock_path.exists()
    assert len(store.list()) == 1


def test_event_history_never_steals_live_owner_lock(tmp_path):
    path = tmp_path / "events.json"
    warnings = []
    store = EventHistoryStore(
        path=path,
        warning_callback=warnings.append,
        stale_lock_seconds=1,
        lock_process_alive=lambda pid: True,
    )

    with StoreWriteLock(path):
        record = store.add(_event(), _result())
        assert store_lock_path(path).exists()

    assert record is None
    assert store.dropped_event_count == 1
    assert len(warnings) == 1
