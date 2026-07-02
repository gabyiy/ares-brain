import io

from core import (
    ExecutionPipeline,
    IntentParser,
    MockWeatherAdapter,
    Plan,
    Planner,
    PlanStep,
    ToolAdapterRegistry,
    ToolChain,
)
from events import EventBus, get_global_bus
from interfaces import text_repl
import memory.v1 as memory_v1
from skills import SkillContext, SkillManager, SkillRegistry, ToolSelector
from skills.builtin.weather import WeatherSkill
import skills.manager as manager_module
import skills.selector as selector_module


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


def test_tool_chain_rejects_repeated_weather_step_without_execution():
    class FailingPipeline:
        def __init__(self):
            self.called = False

        def execute(self, plan, context=None):
            self.called = True
            raise AssertionError("ToolChain should reject repeated weather steps before execution")

    duplicate = PlanStep(
        order=1,
        target="weather",
        action="weather",
        input_text="weather in Madrid",
        intent_name="weather",
        entities={
            "action": "weather",
            "location": "Madrid",
            "period": "today",
            "adapter_name": "mock_weather",
            "capability": "weather.current",
        },
    )
    plan = Plan(
        raw_text="weather loop",
        intent_name="weather",
        steps=[
            duplicate,
            PlanStep(
                order=2,
                target=duplicate.target,
                action=duplicate.action,
                input_text=duplicate.input_text,
                intent_name=duplicate.intent_name,
                entities=dict(duplicate.entities),
            ),
        ],
    )
    pipeline = FailingPipeline()
    chain = ToolChain(execution_pipeline=pipeline)

    result = chain.execute(plan, SkillContext())

    assert result.success is False
    assert pipeline.called is False
    assert result.execution is None
    assert result.errors == ["Loop detected: step 1 and step 2 repeat weather.weather."]
    assert all(step.status == "rejected" for step in result.trace)


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
    assert execution["step_results"][0]["returned_data"]["metadata"]["adapter_name"] == "mock_weather"
    assert execution["step_results"][0]["returned_data"]["metadata"]["capability"] == "weather.current"


def test_text_repl_weather_uses_full_live_runtime_path(monkeypatch, tmp_path, capsys):
    calls = []
    target_text = "weather in Madrid"

    class RecordingIntentParser(IntentParser):
        def parse(self, text: str):
            if text == target_text:
                calls.append("intent_parser")
            return super().parse(text)

    class RecordingPlanner(Planner):
        def plan(self, intent):
            if intent.raw_text == target_text:
                calls.append("planner")
            return super().plan(intent)

    class RecordingExecutionPipeline(ExecutionPipeline):
        def execute(self, plan, context=None):
            if plan.raw_text == target_text:
                calls.append("execution_pipeline")
            return super().execute(plan, context)

    original_weather_handle = WeatherSkill.handle
    original_adapter_handle = MockWeatherAdapter.handle

    def recording_weather_handle(self, text, context):
        if "weather" in text.lower() and "madrid" in text.lower():
            calls.append("weather_skill")
        return original_weather_handle(self, text, context)

    def recording_adapter_handle(self, request):
        if request.adapter_name == "mock_weather" and request.parameters.get("location") == "Madrid":
            calls.append("mock_weather_adapter")
        return original_adapter_handle(self, request)

    monkeypatch.setattr(manager_module, "IntentParser", RecordingIntentParser)
    monkeypatch.setattr(selector_module, "Planner", RecordingPlanner)
    monkeypatch.setattr(manager_module, "ExecutionPipeline", RecordingExecutionPipeline)
    monkeypatch.setattr(WeatherSkill, "handle", recording_weather_handle)
    monkeypatch.setattr(MockWeatherAdapter, "handle", recording_adapter_handle)
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

    assert "Mock weather for Madrid: clear, 21 C." in output
    assert calls == [
        "intent_parser",
        "planner",
        "execution_pipeline",
        "weather_skill",
        "mock_weather_adapter",
    ]


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
