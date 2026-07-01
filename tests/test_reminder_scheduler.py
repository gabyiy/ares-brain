from datetime import datetime, timedelta, timezone

from events import EventBus
from memory import ReminderScheduler, TasksStore, parse_due_text


def _now():
    return datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_tomorrow():
    due_at = parse_due_text("tomorrow", now=_now())

    assert due_at == datetime(2026, 7, 2, 23, 59, 59, tzinfo=timezone.utc)


def test_parse_in_10_minutes():
    due_at = parse_due_text("in 10 minutes", now=_now())

    assert due_at == datetime(2026, 7, 1, 12, 10, tzinfo=timezone.utc)


def test_parse_in_2_hours():
    due_at = parse_due_text("in 2 hours", now=_now())

    assert due_at == datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)


def test_parse_at_1800():
    due_at = parse_due_text("at 18:00", now=_now())

    assert due_at == datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)


def test_due_task_detection(tmp_path):
    store = TasksStore(path=tmp_path / "tasks.json", event_bus=EventBus())
    scheduler = ReminderScheduler(store)
    task = store.add("call mom", due="in 10 minutes")
    created_at = datetime.fromisoformat(task.created_at.replace("Z", "+00:00"))

    before_due = scheduler.due_tasks(created_at + timedelta(minutes=9))
    after_due = scheduler.due_tasks(created_at + timedelta(minutes=11))

    assert before_due == []
    assert [due_task.id for due_task in after_due] == [task.id]


def test_upcoming_task_ordering(tmp_path):
    store = TasksStore(path=tmp_path / "tasks.json", event_bus=EventBus())
    scheduler = ReminderScheduler(store)
    later = store.add("later task", due="in 2 hours")
    sooner = store.add("sooner task", due="in 10 minutes")
    now = datetime.fromisoformat(later.created_at.replace("Z", "+00:00"))

    upcoming = scheduler.upcoming_tasks(now, limit=2)

    assert [task.id for task in upcoming] == [sooner.id, later.id]


def test_invalid_due_text_handling(tmp_path):
    store = TasksStore(path=tmp_path / "tasks.json", event_bus=EventBus())
    scheduler = ReminderScheduler(store)
    store.add("unclear task", due="someday maybe")

    assert parse_due_text("someday maybe", now=_now()) is None
    assert scheduler.due_tasks(_now()) == []
    assert scheduler.upcoming_tasks(_now(), limit=3) == []
