from core import PlanStep
from core.Confirmation import requires_confirmation
from events import EventBus
from memory import GoalsStore, NotesStore, TasksStore
from skills import SkillManager
from skills.builtin.calculator import CalculatorSkill
from skills.builtin.calendar import CalendarSkill
from skills.builtin.goals import GoalsSkill
from skills.builtin.market import MarketSkill
from skills.builtin.notes import NotesSkill
from skills.builtin.tasks import TasksSkill
from skills.builtin.weather import WeatherSkill


def _manager(event_bus=None, notes_store=None, tasks_store=None, goals_store=None):
    manager = SkillManager(
        event_bus=event_bus or EventBus(raise_handler_errors=True),
        notes_store=notes_store,
        tasks_store=tasks_store,
        goals_store=goals_store,
    )
    manager.register(NotesSkill())
    manager.register(TasksSkill())
    manager.register(GoalsSkill())
    manager.register(CalculatorSkill())
    manager.register(WeatherSkill())
    manager.register(MarketSkill())
    manager.register(CalendarSkill())
    return manager


def test_delete_note_requires_confirmation(tmp_path):
    notes_store = NotesStore(path=tmp_path / "notes.json", event_bus=EventBus())
    note = notes_store.add("temporary note")
    manager = _manager(notes_store=notes_store)

    response = manager.handle(f"delete note {note.id}", run_before_intents=True)

    assert response.skill == "confirmation"
    assert "Confirmation required to delete note" in response.text
    assert manager.confirmation_manager.pending() is not None
    assert notes_store.list()[0].id == note.id


def test_delete_task_requires_confirmation(tmp_path):
    tasks_store = TasksStore(path=tmp_path / "tasks.json", event_bus=EventBus())
    task = tasks_store.add("temporary task")
    manager = _manager(tasks_store=tasks_store)

    response = manager.handle(f"delete task {task.id}", run_before_intents=True)

    assert response.skill == "confirmation"
    assert "Confirmation required to delete task" in response.text
    assert manager.confirmation_manager.pending() is not None
    assert tasks_store.list()[0].id == task.id


def test_delete_goal_requires_confirmation(tmp_path):
    goals_store = GoalsStore(path=tmp_path / "goals.json", event_bus=EventBus())
    goal = goals_store.add("Temporary goal")
    manager = _manager(goals_store=goals_store)

    response = manager.handle(f"delete goal {goal.id}", run_before_intents=True)

    assert response.skill == "confirmation"
    assert "Confirmation required to delete goal" in response.text
    assert manager.confirmation_manager.pending() is not None
    assert goals_store.get(goal.id) is not None


def test_confirm_executes_pending_action(tmp_path):
    notes_store = NotesStore(path=tmp_path / "notes.json", event_bus=EventBus())
    note = notes_store.add("delete after confirm")
    manager = _manager(notes_store=notes_store)

    manager.handle(f"delete note {note.id}", run_before_intents=True)
    response = manager.handle("yes")

    assert response.skill == "notes"
    assert response.text == f"Deleted note {note.id}."
    assert manager.confirmation_manager.pending() is None
    assert notes_store.list() == []


def test_cancel_does_not_execute_pending_action(tmp_path):
    tasks_store = TasksStore(path=tmp_path / "tasks.json", event_bus=EventBus())
    task = tasks_store.add("keep after cancel")
    manager = _manager(tasks_store=tasks_store)

    manager.handle(f"delete task {task.id}", run_before_intents=True)
    response = manager.handle("cancel")

    assert response.skill == "confirmation"
    assert response.text == f"Cancelled: delete task {task.id}."
    assert manager.confirmation_manager.pending() is None
    assert tasks_store.list()[0].id == task.id


def test_missing_pending_confirmation_fails_safely():
    manager = _manager()

    response = manager.handle("confirm")

    assert response.skill == "confirmation"
    assert response.text == "No pending confirmation to confirm or cancel."
    assert response.metadata["error"] == "missing_confirmation"


def test_weather_market_calendar_unaffected_by_confirmation():
    manager = _manager()

    weather = manager.handle("weather in Madrid", run_before_intents=True)
    market = manager.handle("stock nvidia", run_before_intents=True)
    calendar = manager.handle("calendar today", run_before_intents=True)

    assert weather.skill == "weather"
    assert market.skill == "market"
    assert calendar.skill == "calendar"
    assert "Confirmation required" not in weather.text
    assert "Confirmation required" not in market.text
    assert "Confirmation required" not in calendar.text
    assert manager.confirmation_manager.pending() is None


def test_multistep_plan_with_confirmation_pauses_safely(tmp_path):
    notes_store = NotesStore(path=tmp_path / "notes.json", event_bus=EventBus())
    note = notes_store.add("delete in multi-step")
    manager = _manager(notes_store=notes_store)

    response = manager.handle(
        f"weather today and delete note {note.id} and calculate 2+2",
        run_before_intents=True,
    )
    execution = response.metadata["execution"]

    assert response.skill == "planner"
    assert "Partial results:" in response.text
    assert "Mock weather for local" in response.text
    assert "Confirmation required to delete note" in response.text
    assert "Result: 4" not in response.text
    assert execution["stopped"] is True
    assert [step["target"] for step in execution["step_results"]] == ["weather", "notes"]
    assert execution["pending_confirmation"]["id"].startswith("confirm-")
    assert notes_store.list()[0].id == note.id


def test_clear_completed_and_goal_status_actions_require_confirmation(tmp_path):
    tasks_store = TasksStore(path=tmp_path / "tasks.json", event_bus=EventBus())
    goals_store = GoalsStore(path=tmp_path / "goals.json", event_bus=EventBus())
    task = tasks_store.add("completed task")
    tasks_store.mark_done(task.id)
    goal = goals_store.add("Important goal")
    manager = _manager(tasks_store=tasks_store, goals_store=goals_store)

    clear_response = manager.handle("clear completed tasks", run_before_intents=True)
    manager.handle("cancel")
    pause_response = manager.handle(f"pause goal {goal.id}", run_before_intents=True)
    manager.handle("cancel")
    complete_response = manager.handle(f"complete goal {goal.id}", run_before_intents=True)

    assert "Confirmation required to clear completed tasks" in clear_response.text
    assert "Confirmation required to pause goal" in pause_response.text
    assert "Confirmation required to complete goal" in complete_response.text
    assert tasks_store.list()[0].id == task.id
    assert goals_store.get(goal.id).status == "active"


def test_future_external_write_actions_require_confirmation():
    step = PlanStep(
        order=1,
        target="tool_adapter",
        action="write",
        input_text="write external data",
        intent_name="tool_adapter",
        entities={"capability": "external.write"},
    )

    assert requires_confirmation(step) is True
