import io

from core import ExecutionPipeline, IntentParser, MultiStepPlan, Planner
import memory.v1 as memory_v1
from events import EventBus, get_global_bus
from interfaces import text_repl
from memory import GoalsStore, NotesStore, TasksStore, UserProfileStore
from skills import SkillContext, SkillRegistry
from skills.builtin.calculator import CalculatorSkill


def _plan(text: str, **stores):
    return Planner(**stores).plan(IntentParser().parse(text))


def _registry(*skills):
    registry = SkillRegistry()
    for skill in skills:
        registry.register(skill)
    return registry


def test_planner_uses_goal_context_for_reminder(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    goals_store = GoalsStore(path=tmp_path / "goals.json", event_bus=event_bus)
    goal = goals_store.add("Build ARES", priority="high")

    plan = _plan("remind me about my main goal tomorrow", goals_store=goals_store)

    assert plan.errors == []
    assert len(plan.steps) == 1
    assert plan.steps[0].target == "tasks"
    assert plan.steps[0].input_text == "add task Review goal: Build ARES due tomorrow"
    assert plan.steps[0].entities["context_source"] == "goals"
    assert plan.steps[0].entities["context_goal_id"] == goal.id


def test_planner_uses_profile_context_for_favorite_reminder(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    profile_store = UserProfileStore(path=tmp_path / "profile.json", event_bus=event_bus)
    profile_store.set_fact("favorite_tank", "Leopard 2", "favorite tank", "My favorite tank is Leopard 2")

    plan = _plan("remind me about my favorite tank tomorrow", profile_store=profile_store)

    assert plan.errors == []
    assert plan.steps[0].target == "tasks"
    assert plan.steps[0].input_text == "add task Review favorite tank: Leopard 2 due tomorrow"
    assert plan.steps[0].entities["context_source"] == "profile"
    assert plan.steps[0].entities["context_value"] == "Leopard 2"


def test_planner_uses_notes_context_for_topic_search(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    notes_store = NotesStore(path=tmp_path / "notes.json", event_bus=event_bus)
    notes_store.add("gym program: legs tomorrow")

    plan = _plan("notes about gym", notes_store=notes_store)

    assert plan.errors == []
    assert plan.steps[0].target == "notes"
    assert plan.steps[0].action == "search"
    assert plan.steps[0].input_text == "search notes gym"
    assert plan.steps[0].entities["context_count"] == 1


def test_planner_uses_task_context_for_next_goal_step(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    goals_store = GoalsStore(path=tmp_path / "goals.json", event_bus=event_bus)
    tasks_store = TasksStore(path=tmp_path / "tasks.json", event_bus=event_bus)
    goals_store.add("Build ARES")
    task = tasks_store.add("write ARES roadmap", due="tomorrow")

    plan = _plan(
        "what should I do next for my goals?",
        goals_store=goals_store,
        tasks_store=tasks_store,
    )

    assert plan.errors == []
    assert plan.steps[0].target == "planner_context"
    assert plan.steps[0].entities["reason"] == "related_task"
    assert task.id in plan.steps[0].entities["text"]
    assert "write ARES roadmap" in plan.steps[0].entities["text"]


def test_missing_context_returns_safe_response():
    plan = _plan("what should I do next for my goals?")

    assert plan.errors == []
    assert plan.steps[0].target == "planner_context"
    assert plan.steps[0].entities["reason"] == "missing_goals_store"
    assert plan.steps[0].entities["text"] == "I do not have goal context available yet."


def test_multistep_context_plan_uses_goals_and_notes(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    goals_store = GoalsStore(path=tmp_path / "goals.json", event_bus=event_bus)
    notes_store = NotesStore(path=tmp_path / "notes.json", event_bus=event_bus)
    goals_store.add("Run faster")
    notes_store.add("gym routine: intervals")

    plan = _plan(
        "show my goals and notes about gym",
        goals_store=goals_store,
        notes_store=notes_store,
    )

    assert isinstance(plan, MultiStepPlan)
    assert plan.errors == []
    assert [step.target for step in plan.steps] == ["goals", "notes"]
    assert plan.steps[0].input_text == "list goals"
    assert plan.steps[1].input_text == "search notes gym"
    assert plan.steps[1].entities["context_count"] == 1


def test_execution_pipeline_keeps_partial_failure_recovery_with_context(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    goals_store = GoalsStore(path=tmp_path / "goals.json", event_bus=event_bus)
    goals_store.add("Build ARES")
    registry = _registry(CalculatorSkill())
    pipeline = ExecutionPipeline(registry.get, event_bus=event_bus)
    plan = _plan("calculate 2 + spam and what should I do next for my goals", goals_store=goals_store)

    result = pipeline.execute(plan, SkillContext(event_bus=event_bus, goals_store=goals_store))

    assert result.success is False
    assert result.stopped is False
    assert [step.target for step in result.step_results] == ["calculator", "planner_context"]
    assert [step.success for step in result.step_results] == [False, True]
    assert "Partial results:" in result.format_response_text()
    assert "Build ARES" in result.format_response_text()


def test_repl_executes_context_aware_goal_next_request(monkeypatch, tmp_path, capsys):
    event_bus = get_global_bus()
    event_bus.clear_history()
    goals_path = tmp_path / "goals.json"
    tasks_path = tmp_path / "tasks.json"
    GoalsStore(path=goals_path, event_bus=event_bus).add("Build ARES")
    task = TasksStore(path=tasks_path, event_bus=event_bus).add("write ARES roadmap", due="tomorrow")

    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tasks_path))
    monkeypatch.setenv("ARES_GOALS_PATH", str(goals_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nwhat should I do next for my goals?\nquit\n"))

    text_repl.main()

    output = capsys.readouterr().out
    execution = event_bus.history("execution.completed")[-1].payload

    assert "Next for your goal \"Build ARES\"" in output
    assert task.id in output
    assert "write ARES roadmap due tomorrow" in output
    assert execution["step_results"][0]["target"] == "planner_context"
    assert execution["step_results"][0]["success"] is True
