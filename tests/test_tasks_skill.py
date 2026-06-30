from events import EventBus
from memory import TasksStore
from skills import SkillContext
from skills.builtin.tasks import TasksSkill


def _tasks_context(tmp_path):
    store = TasksStore(path=tmp_path / "tasks.json", event_bus=EventBus())
    return store, SkillContext(tasks_store=store)


def test_tasks_skill_adds_lists_marks_done_and_deletes_task(tmp_path):
    store, context = _tasks_context(tmp_path)
    skill = TasksSkill()

    saved = skill.handle("add task buy milk", context)
    task = store.list()[0]
    listed = skill.handle("list tasks", context)
    done = skill.handle(f"mark task {task.id} done", context)
    after_done = skill.handle("show tasks", context)
    deleted = skill.handle(f"delete task {task.id}", context)

    assert saved.text == f"Saved task {task.id}: buy milk"
    assert f"- {task.id} [open]: buy milk" in listed.text
    assert done.text == f"Marked task {task.id} done."
    assert f"- {task.id} [done]: buy milk" in after_done.text
    assert deleted.text == f"Deleted task {task.id}."
    assert store.list() == []


def test_tasks_skill_adds_reminder_with_due_text(tmp_path):
    store, context = _tasks_context(tmp_path)

    response = TasksSkill().handle("remind me to call mom due tomorrow", context)
    task = store.list()[0]

    assert response.text == f"Saved task {task.id}: call mom (due tomorrow)"
    assert task.text == "call mom"
    assert task.due == "tomorrow"
    assert task.completed is False


def test_tasks_skill_rejects_empty_task_text(tmp_path):
    store, context = _tasks_context(tmp_path)
    response = TasksSkill().handle("add task   ", context)

    assert response.text == "I need task text to save."
    assert response.metadata["error"] == "empty_task"
    assert store.list() == []


def test_tasks_skill_clears_completed_tasks_only(tmp_path):
    store, context = _tasks_context(tmp_path)
    skill = TasksSkill()
    first = store.add("buy milk")
    second = store.add("call mom")
    store.mark_done(first.id)

    response = skill.handle("clear completed tasks", context)

    assert response.text == "Cleared 1 completed tasks."
    assert [task.id for task in store.list()] == [second.id]


def test_tasks_skill_reports_missing_tasks_store():
    response = TasksSkill().handle("add task buy milk", SkillContext())

    assert response.text == "Task storage is not available."
    assert response.metadata["error"] == "missing_tasks_store"
