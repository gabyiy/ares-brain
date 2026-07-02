import io

from core import (
    ExecutionPipeline,
    IntentParser,
    MockCalendarAdapter,
    Planner,
    ToolAdapterRegistry,
)
from events import EventBus, get_global_bus
from interfaces import text_repl
import memory.v1 as memory_v1
from skills import SkillContext, SkillManager, SkillRegistry, ToolSelector
from skills.builtin.calendar import CalendarSkill


def test_calendar_intent_parsing():
    parser = IntentParser()

    today_calendar = parser.parse("what is on my calendar today")
    tomorrow_calendar = parser.parse("calendar tomorrow")
    schedule_today = parser.parse("schedule today")
    anything_tomorrow = parser.parse("do I have anything tomorrow")

    assert today_calendar.intent_name == "calendar"
    assert today_calendar.extracted_entities["period"] == "today"
    assert today_calendar.extracted_entities["adapter_name"] == "mock_calendar"
    assert today_calendar.extracted_entities["capability"] == "calendar.events"
    assert tomorrow_calendar.extracted_entities["period"] == "tomorrow"
    assert schedule_today.intent_name == "calendar"
    assert anything_tomorrow.intent_name == "calendar"
    assert anything_tomorrow.extracted_entities["period"] == "tomorrow"


def test_calendar_skill_calls_mock_calendar_adapter():
    registry = ToolAdapterRegistry([MockCalendarAdapter()])
    skill = CalendarSkill()

    response = skill.handle(
        "what is on my calendar today",
        SkillContext(tool_adapter_registry=registry),
    )

    assert response.skill == "calendar"
    assert response.text == "Mock calendar for today: ARES systems check at 09:00."
    assert response.metadata["adapter_name"] == "mock_calendar"
    assert response.metadata["capability"] == "calendar.events"
    assert response.metadata["data"]["source"] == "mock"


def test_calendar_skill_response_uses_structured_intent_entities():
    registry = ToolAdapterRegistry([MockCalendarAdapter()])
    intent = IntentParser().parse("do I have anything tomorrow")
    skill = CalendarSkill()

    response = skill.handle(
        intent.raw_text,
        SkillContext(tool_adapter_registry=registry, metadata={"intent": intent}),
    )

    assert response.text == "Mock calendar for tomorrow: no events."
    assert response.metadata["period"] == "tomorrow"
    assert response.metadata["capability"] == "calendar.events"


def test_planner_creates_calendar_step():
    plan = Planner().plan(IntentParser().parse("calendar tomorrow"))

    assert plan.errors == []
    assert len(plan.steps) == 1
    assert plan.steps[0].target == "calendar"
    assert plan.steps[0].action == "list"
    assert plan.steps[0].input_text == "calendar tomorrow"
    assert plan.steps[0].entities["adapter_name"] == "mock_calendar"
    assert plan.steps[0].entities["period"] == "tomorrow"


def test_tool_selector_routes_calendar_skill():
    selector = ToolSelector()

    selection = selector.select(
        IntentParser().parse("schedule today"),
        [CalendarSkill()],
        run_before_intents=True,
    )

    assert selection.skill.name == "calendar"
    assert selection.reason == "structured intent match: calendar"
    assert selection.plan.steps[0].target == "calendar"


def test_execution_pipeline_executes_calendar_step():
    event_bus = EventBus(raise_handler_errors=True)
    registry = SkillRegistry()
    registry.register(CalendarSkill())
    adapter_registry = ToolAdapterRegistry([MockCalendarAdapter()])
    pipeline = ExecutionPipeline(
        registry.get,
        event_bus=event_bus,
        tool_adapter_registry=adapter_registry,
    )
    plan = Planner().plan(IntentParser().parse("what is on my calendar today"))

    result = pipeline.execute(
        plan,
        SkillContext(event_bus=event_bus, tool_adapter_registry=adapter_registry),
    )

    assert result.success is True
    assert result.step_results[0].target == "calendar"
    assert result.step_results[0].returned_data["skill"] == "calendar"
    assert result.step_results[0].returned_data["text"] == "Mock calendar for today: ARES systems check at 09:00."
    assert event_bus.history("execution.step_completed")


def test_text_repl_routes_calendar_through_skill_path(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nwhat is on my calendar today\nquit\n"))

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    detected = [
        event.payload
        for event in event_bus.history("skill.detected")
        if event.payload.get("skill") == "calendar"
    ]
    execution = event_bus.history("execution.completed")[-1].payload

    assert "Mock calendar for today: ARES systems check at 09:00." in output
    assert detected
    assert detected[-1]["intent"] == "calendar"
    assert execution["step_results"][0]["target"] == "calendar"
    assert execution["step_results"][0]["returned_data"]["metadata"]["adapter_name"] == "mock_calendar"
    assert execution["step_results"][0]["returned_data"]["metadata"]["capability"] == "calendar.events"


def test_calendar_skill_reports_missing_adapter_error():
    manager = SkillManager(
        event_bus=EventBus(raise_handler_errors=True),
        tool_adapter_registry=ToolAdapterRegistry(),
    )
    manager.register(CalendarSkill())

    response = manager.handle("calendar tomorrow", run_before_intents=True)

    assert response.skill == "calendar"
    assert "Tool adapter is not available: mock_calendar" in response.text
    assert response.metadata["execution"]["success"] is False
    assert response.metadata["results"][0]["error_message"] == "Tool adapter is not available: mock_calendar"
