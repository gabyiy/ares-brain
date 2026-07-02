import io

from core import ExecutionPipeline, IntentParser, MockCalendarAdapter, MultiStepPlan, Plan, Planner, ToolAdapterRegistry
from events import EventBus, get_global_bus
from interfaces import text_repl
from memory import GoalsStore, TasksStore
import memory.v1 as memory_v1
from skills import SkillContext, SkillRegistry
from skills.builtin.calculator import CalculatorSkill
from skills.builtin.calendar import CalendarSkill
from skills.builtin.goals import GoalsSkill
from skills.builtin.tasks import TasksSkill


def _plan(text: str) -> Plan:
    return Planner().plan(IntentParser().parse(text))


def _registry(*skills):
    registry = SkillRegistry()
    for skill in skills:
        registry.register(skill)
    return registry


def test_single_step_plan_compatibility():
    plan = _plan("calendar tomorrow")

    assert isinstance(plan, Plan)
    assert not isinstance(plan, MultiStepPlan)
    assert plan.is_multi_step() is False
    assert len(plan.steps) == 1
    assert plan.steps[0].target == "calendar"


def test_multistep_weather_and_reminder_plan():
    plan = _plan("What's the weather tomorrow and remind me to go to the gym.")

    assert isinstance(plan, MultiStepPlan)
    assert plan.is_multi_step() is True
    assert plan.errors == []
    assert [step.order for step in plan.steps] == [1, 2]
    assert [step.target for step in plan.steps] == ["weather", "tasks"]
    assert plan.steps[0].input_text == "weather tomorrow"
    assert plan.steps[1].input_text == "add task go to the gym"


def test_multistep_goals_and_calendar_plan():
    plan = _plan("Show my goals and today's calendar.")

    assert isinstance(plan, MultiStepPlan)
    assert plan.errors == []
    assert [step.target for step in plan.steps] == ["goals", "calendar"]
    assert [step.action for step in plan.steps] == ["list", "list"]
    assert plan.steps[0].input_text == "list goals"
    assert plan.steps[1].input_text == "calendar today"


def test_three_step_plan_preserves_planner_ordering():
    plan = _plan("stock nvidia and schedule today and calculate 2+2")

    assert isinstance(plan, MultiStepPlan)
    assert [step.order for step in plan.steps] == [1, 2, 3]
    assert [step.target for step in plan.steps] == ["market", "calendar", "calculator"]


def test_execution_pipeline_runs_three_step_plan_in_order():
    event_bus = EventBus(raise_handler_errors=True)
    registry = _registry(CalendarSkill(), CalculatorSkill())
    adapter_registry = ToolAdapterRegistry([MockCalendarAdapter()])
    pipeline = ExecutionPipeline(
        registry.get,
        event_bus=event_bus,
        tool_adapter_registry=adapter_registry,
    )
    plan = _plan("schedule today and calculate 2+2 and calculate 3+3")

    result = pipeline.execute(
        plan,
        SkillContext(event_bus=event_bus, tool_adapter_registry=adapter_registry),
    )

    assert result.success is True
    assert [step.order for step in result.step_results] == [1, 2, 3]
    assert [step.target for step in result.step_results] == ["calendar", "calculator", "calculator"]
    assert [step.returned_data["text"] for step in result.step_results] == [
        "Mock calendar for today: ARES systems check at 09:00.",
        "Result: 4",
        "Result: 6",
    ]


def test_execution_pipeline_reports_partial_success_and_continues():
    event_bus = EventBus(raise_handler_errors=True)
    registry = _registry(CalendarSkill(), CalculatorSkill())
    adapter_registry = ToolAdapterRegistry([MockCalendarAdapter()])
    pipeline = ExecutionPipeline(
        registry.get,
        event_bus=event_bus,
        tool_adapter_registry=adapter_registry,
    )
    plan = _plan("calculate 2 + spam and calendar today")

    result = pipeline.execute(
        plan,
        SkillContext(event_bus=event_bus, tool_adapter_registry=adapter_registry),
    )

    assert result.success is False
    assert result.stopped is False
    assert [step.success for step in result.step_results] == [False, True]
    assert result.step_results[0].error_message == "only numbers and arithmetic operators are allowed"
    assert result.step_results[1].returned_data["text"] == "Mock calendar for today: ARES systems check at 09:00."
    assert "Partial results:" in result.format_response_text()
    assert "Mock calendar for today" in result.format_response_text()


def test_repl_executes_weather_and_reminder_multistep_request(monkeypatch, tmp_path, capsys):
    tasks_path = tmp_path / "tasks.json"
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tasks_path))
    monkeypatch.setenv("ARES_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("hello\nWhat's the weather tomorrow and remind me to go to the gym.\nquit\n"),
    )

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    tasks = TasksStore(path=tasks_path, event_bus=event_bus).list()
    execution = event_bus.history("execution.completed")[-1].payload

    assert "Plan results:" in output
    assert "Mock weather for local: clear, 21 C." in output
    assert "Saved task" in output
    assert tasks[0].text == "go to the gym"
    assert [step["target"] for step in execution["step_results"]] == ["weather", "tasks"]


def test_repl_executes_goals_and_calendar_multistep_request(monkeypatch, tmp_path, capsys):
    goals_path = tmp_path / "goals.json"
    event_bus = get_global_bus()
    event_bus.clear_history()
    GoalsStore(path=goals_path, event_bus=event_bus).add("Build ARES")

    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(goals_path))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("hello\nShow my goals and today's calendar.\nquit\n"),
    )

    text_repl.main()

    output = capsys.readouterr().out
    execution = event_bus.history("execution.completed")[-1].payload

    assert "Plan results:" in output
    assert "Your goals:" in output
    assert "Build ARES" in output
    assert "Mock calendar for today: ARES systems check at 09:00." in output
    assert [step["target"] for step in execution["step_results"]] == ["goals", "calendar"]
