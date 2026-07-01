from core import (
    ExecutionPipeline,
    MockMarketAdapter,
    MockWeatherAdapter,
    Plan,
    Planner,
    PlanStep,
    ToolAdapterRegistry,
    ToolRequest,
)
from events import EventBus
from skills import SkillContext


def test_tool_adapter_registry_registers_adapter():
    registry = ToolAdapterRegistry()
    adapter = registry.register(MockWeatherAdapter())

    assert adapter.name == "mock_weather"
    assert registry.all() == [adapter]
    assert registry.metadata()[0]["capabilities"] == ["weather.current", "weather.forecast"]


def test_tool_adapter_registry_looks_up_adapter():
    registry = ToolAdapterRegistry([MockWeatherAdapter()])

    adapter = registry.get("mock_weather")
    matches = registry.find_by_capability("weather.current")

    assert adapter.name == "mock_weather"
    assert matches == [adapter]


def test_tool_adapter_registry_handles_missing_adapter():
    registry = ToolAdapterRegistry()

    response = registry.execute(ToolRequest(adapter_name="missing", capability="weather.current"))

    assert response.success is False
    assert response.error_message == "Tool adapter is not available: missing"
    assert response.metadata["missing_adapter"] == "missing"


def test_mock_weather_adapter_returns_offline_response():
    registry = ToolAdapterRegistry([MockWeatherAdapter()])

    response = registry.execute(
        ToolRequest(
            adapter_name="mock_weather",
            capability="weather.current",
            parameters={"location": "Madrid"},
        )
    )

    assert response.success is True
    assert response.text == "Mock weather for Madrid: clear, 21 C."
    assert response.data == {
        "location": "Madrid",
        "condition": "clear",
        "temperature_c": 21,
        "source": "mock",
    }


def test_mock_market_adapter_returns_offline_response():
    registry = ToolAdapterRegistry([MockMarketAdapter()])

    response = registry.execute(
        ToolRequest(
            adapter_name="mock_market",
            capability="market.quote",
            parameters={"symbol": "ares"},
        )
    )

    assert response.success is True
    assert response.text == "Mock market quote for ARES: 100.00 USD."
    assert response.data == {
        "symbol": "ARES",
        "price": 100.0,
        "currency": "USD",
        "source": "mock",
    }


def test_mock_adapters_do_not_require_network_or_auth():
    weather = MockWeatherAdapter()
    market = MockMarketAdapter()

    assert weather.metadata()["requires_network"] is False
    assert weather.metadata()["requires_auth"] is False
    assert market.metadata()["requires_network"] is False
    assert market.metadata()["requires_auth"] is False


def test_planner_accepts_tool_adapter_registry_for_future_planning():
    registry = ToolAdapterRegistry([MockWeatherAdapter()])
    planner = Planner(tool_adapter_registry=registry)

    assert planner.tool_adapter_registry.get("mock_weather").supports("weather.current") is True


def test_execution_pipeline_calls_tool_adapter_safely():
    event_bus = EventBus(raise_handler_errors=True)
    registry = ToolAdapterRegistry([MockWeatherAdapter()])
    pipeline = ExecutionPipeline(
        skill_resolver=lambda name: None,
        event_bus=event_bus,
        tool_adapter_registry=registry,
    )
    plan = Plan(
        raw_text="adapter weather",
        intent_name="tool_adapter",
        steps=[
            PlanStep(
                order=1,
                target="tool_adapter",
                action="mock_weather",
                input_text="weather Madrid",
                intent_name="tool_adapter",
                entities={
                    "adapter_name": "mock_weather",
                    "capability": "weather.current",
                    "parameters": {"location": "Madrid"},
                },
            )
        ],
    )

    result = pipeline.execute(plan, SkillContext(event_bus=event_bus))
    step = result.step_results[0]

    assert result.success is True
    assert step.success is True
    assert step.target == "tool_adapter"
    assert step.returned_data["skill"] == "tool_adapter"
    assert step.returned_data["text"] == "Mock weather for Madrid: clear, 21 C."
    assert step.returned_data["data"]["source"] == "mock"
    assert step.returned_data["metadata"]["adapter_name"] == "mock_weather"
    assert event_bus.history("execution.step_completed")
