import io

from core import (
    ExecutionPipeline,
    IntentParser,
    MockMarketAdapter,
    Planner,
    ToolAdapterRegistry,
)
from events import EventBus, get_global_bus
from interfaces import text_repl
import memory.v1 as memory_v1
from skills import SkillContext, SkillManager, SkillRegistry, ToolSelector
from skills.builtin.market import MarketSkill


def test_market_intent_parsing():
    parser = IntentParser()

    stock_prefix = parser.parse("stock nvidia")
    stock_suffix = parser.parse("nvidia stock")
    apple = parser.parse("apple stock")
    market_price = parser.parse("market price for tesla")

    assert stock_prefix.intent_name == "market"
    assert stock_prefix.extracted_entities["symbol"] == "NVIDIA"
    assert stock_prefix.extracted_entities["adapter_name"] == "mock_market"
    assert stock_prefix.extracted_entities["capability"] == "market.quote"
    assert stock_suffix.extracted_entities["symbol"] == "NVIDIA"
    assert apple.extracted_entities["symbol"] == "APPLE"
    assert market_price.extracted_entities["symbol"] == "TESLA"


def test_market_skill_calls_mock_market_adapter():
    registry = ToolAdapterRegistry([MockMarketAdapter()])
    skill = MarketSkill()

    response = skill.handle(
        "stock nvidia",
        SkillContext(tool_adapter_registry=registry),
    )

    assert response.skill == "market"
    assert response.text == "Mock market quote for NVIDIA: 100.00 USD."
    assert response.metadata["adapter_name"] == "mock_market"
    assert response.metadata["capability"] == "market.quote"
    assert response.metadata["data"]["source"] == "mock"


def test_market_skill_response_uses_structured_intent_entities():
    registry = ToolAdapterRegistry([MockMarketAdapter()])
    intent = IntentParser().parse("market price for tesla")
    skill = MarketSkill()

    response = skill.handle(
        intent.raw_text,
        SkillContext(tool_adapter_registry=registry, metadata={"intent": intent}),
    )

    assert response.text == "Mock market quote for TESLA: 100.00 USD."
    assert response.metadata["symbol"] == "TESLA"
    assert response.metadata["capability"] == "market.quote"


def test_planner_creates_market_step():
    plan = Planner().plan(IntentParser().parse("apple stock"))

    assert plan.errors == []
    assert len(plan.steps) == 1
    assert plan.steps[0].target == "market"
    assert plan.steps[0].action == "quote"
    assert plan.steps[0].input_text == "stock APPLE"
    assert plan.steps[0].entities["adapter_name"] == "mock_market"
    assert plan.steps[0].entities["symbol"] == "APPLE"


def test_tool_selector_routes_market_skill():
    selector = ToolSelector()

    selection = selector.select(
        IntentParser().parse("market price for tesla"),
        [MarketSkill()],
        run_before_intents=True,
    )

    assert selection.skill.name == "market"
    assert selection.reason == "structured intent match: market"
    assert selection.plan.steps[0].target == "market"


def test_execution_pipeline_executes_market_step():
    event_bus = EventBus(raise_handler_errors=True)
    registry = SkillRegistry()
    registry.register(MarketSkill())
    adapter_registry = ToolAdapterRegistry([MockMarketAdapter()])
    pipeline = ExecutionPipeline(
        registry.get,
        event_bus=event_bus,
        tool_adapter_registry=adapter_registry,
    )
    plan = Planner().plan(IntentParser().parse("stock nvidia"))

    result = pipeline.execute(
        plan,
        SkillContext(event_bus=event_bus, tool_adapter_registry=adapter_registry),
    )

    assert result.success is True
    assert result.step_results[0].target == "market"
    assert result.step_results[0].returned_data["skill"] == "market"
    assert result.step_results[0].returned_data["text"] == "Mock market quote for NVIDIA: 100.00 USD."
    assert event_bus.history("execution.step_completed")


def test_text_repl_routes_market_through_skill_path(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nstock nvidia\nquit\n"))

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    detected = [
        event.payload
        for event in event_bus.history("skill.detected")
        if event.payload.get("skill") == "market"
    ]
    execution = event_bus.history("execution.completed")[-1].payload

    assert "Mock market quote for NVIDIA: 100.00 USD." in output
    assert detected
    assert detected[-1]["intent"] == "market"
    assert execution["step_results"][0]["target"] == "market"
    assert execution["step_results"][0]["returned_data"]["metadata"]["adapter_name"] == "mock_market"
    assert execution["step_results"][0]["returned_data"]["metadata"]["capability"] == "market.quote"


def test_market_skill_reports_missing_adapter_error():
    manager = SkillManager(
        event_bus=EventBus(raise_handler_errors=True),
        tool_adapter_registry=ToolAdapterRegistry(),
    )
    manager.register(MarketSkill())

    response = manager.handle("stock nvidia", run_before_intents=True)

    assert response.skill == "market"
    assert "Tool adapter is not available: mock_market" in response.text
    assert response.metadata["execution"]["success"] is False
    assert response.metadata["results"][0]["error_message"] == "Tool adapter is not available: mock_market"
