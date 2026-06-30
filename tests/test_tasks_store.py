import pytest

from events import EventBus
from memory import TasksStore


def test_tasks_store_adds_task_with_optional_due_and_persists(tmp_path):
    path = tmp_path / "tasks.json"
    store = TasksStore(path=path, event_bus=EventBus())

    task = store.add("buy milk", due="tomorrow")

    assert task.id.startswith("task-")
    assert task.text == "buy milk"
    assert task.created_at
    assert task.due == "tomorrow"
    assert task.completed is False

    reloaded = TasksStore(path=path, event_bus=EventBus())
    tasks = reloaded.list()

    assert len(tasks) == 1
    assert tasks[0].id == task.id
    assert tasks[0].text == "buy milk"
    assert tasks[0].due == "tomorrow"


def test_tasks_store_marks_done_deletes_and_clears_completed(tmp_path):
    store = TasksStore(path=tmp_path / "tasks.json", event_bus=EventBus())
    first = store.add("buy milk")
    second = store.add("call mom")

    completed = store.mark_done(first.id)
    missing = store.mark_done("missing")
    cleared = store.clear_completed()
    deleted = store.delete(second.id)

    assert completed.id == first.id
    assert completed.completed is True
    assert missing is None
    assert cleared == 1
    assert deleted.id == second.id
    assert store.list() == []


def test_tasks_store_rejects_empty_task_text(tmp_path):
    store = TasksStore(path=tmp_path / "tasks.json", event_bus=EventBus())

    with pytest.raises(ValueError, match="Task text is required"):
        store.add("   ")
