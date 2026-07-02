import io

from core import (
    ExecutionPipeline,
    IntentParser,
    MockWeatherAdapter,
    Planner,
    ToolAdapterRegistry,
)
from events import EventBus, get_global_bus
from interfaces import text_repl
import memory.v1 as memory_v1
from skills import SkillContext, SkillManager, SkillRegistry, ToolSelector
from skills.builtin.weather import WeatherSkill


def test_weather_intent_parsing():
    parser = IntentParser()

    basic = parser.parse("weather")
    today = parser.parse("weather today")
    tomorrow = parser.parse("weather tomorrow")
    madrid = parser.parse("weather in Madrid")

    assert basic.intent_name == "weather"
    assert basic.extracted_entities["location"] == "local"
    assert basic.extracted_entities["period"] == "today"
    assert basic.extracted_entities["capability"] == "weather.current"
    assert today.intent_name == "weather"
    assert tomorrow.extracted_entities["period"] == "tomorrow"
    assert tomorrow.extracted_entities["capability"] == "weather.forecast"
    assert madrid.extracted_entities["location"] == "Madrid"


def test_weather_skill_calls_mock_weather_adapter():
    registry = ToolAdapterRegistry([MockWeatherAdapter()])
    skill = WeatherSkill()

    response = skill.handle(
        "weather in Madrid",
        SkillContext(tool_adapter_registry=registry),
    )

    assert response.skill == "weather"
    assert response.text == "Mock weather for Madrid: clear, 21 C."
    assert response.metadata["adapter_name"] == "mock_weather"
    assert response.metadata["capability"] == "weather.current"
    assert response.metadata["data"]["source"] == "mock"


def test_weather_skill_response_uses_structured_intent_entities():
    registry = ToolAdapterRegistry([MockWeatherAdapter()])
    intent = IntentParser().parse("weather tomorrow in Madrid")
    skill = WeatherSkill()

    response = skill.handle(
        intent.raw_text,
        SkillContext(tool_adapter_registry=registry, metadata={"intent": intent}),
    )

    assert response.text == "Mock weather for Madrid: clear, 21 C."
    assert response.metadata["period"] == "tomorrow"
    assert response.metadata["capability"] == "weather.forecast"
    assert response.metadata["location"] == "Madrid"


def test_planner_creates_weather_step():
    plan = Planner().plan(IntentParser().parse("weather in Madrid"))

    assert plan.errors == []
    assert len(plan.steps) == 1
    assert plan.steps[0].target == "weather"
    assert plan.steps[0].action == "weather"
    assert plan.steps[0].entities["adapter_name"] == "mock_weather"
    assert plan.steps[0].entities["location"] == "Madrid"


def test_tool_selector_routes_weather_skill():
    selector = ToolSelector()

    selection = selector.select(
        IntentParser().parse("weather tomorrow"),
        [WeatherSkill()],
        run_before_intents=True,
    )

    assert selection.skill.name == "weather"
    assert selection.reason == "structured intent match: weather"
    assert selection.plan.steps[0].target == "weather"


def test_execution_pipeline_executes_weather_step():
    event_bus = EventBus(raise_handler_errors=True)
    registry = SkillRegistry()
    registry.register(WeatherSkill())
    adapter_registry = ToolAdapterRegistry([MockWeatherAdapter()])
    pipeline = ExecutionPipeline(
        registry.get,
        event_bus=event_bus,
        tool_adapter_registry=adapter_registry,
    )
    plan = Planner().plan(IntentParser().parse("weather in Madrid"))

    result = pipeline.execute(
        plan,
        SkillContext(event_bus=event_bus, tool_adapter_registry=adapter_registry),
    )

    assert result.success is True
    assert result.step_results[0].target == "weather"
    assert result.step_results[0].returned_data["skill"] == "weather"
    assert result.step_results[0].returned_data["text"] == "Mock weather for Madrid: clear, 21 C."
    assert event_bus.history("execution.step_completed")


def test_text_repl_routes_weather_through_skill_path(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nweather in Madrid\nquit\n"))

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    detected = [
        event.payload
        for event in event_bus.history("skill.detected")
        if event.payload.get("skill") == "weather"
    ]
    execution = event_bus.history("execution.completed")[-1].payload

    assert "Mock weather for Madrid: clear, 21 C." in output
    assert detected
    assert detected[-1]["intent"] == "weather"
    assert execution["step_results"][0]["target"] == "weather"


def test_weather_skill_reports_missing_adapter_error():
    manager = SkillManager(
        event_bus=EventBus(raise_handler_errors=True),
        tool_adapter_registry=ToolAdapterRegistry(),
    )
    manager.register(WeatherSkill())

    response = manager.handle("weather in Madrid", run_before_intents=True)

    assert response.skill == "weather"
    assert "Tool adapter is not available: mock_weather" in response.text
    assert response.metadata["execution"]["success"] is False
    assert response.metadata["results"][0]["error_message"] == "Tool adapter is not available: mock_weather"
