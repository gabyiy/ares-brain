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
    assert store.recent() == []
