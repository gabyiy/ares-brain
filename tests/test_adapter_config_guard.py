import pytest

from core import (
    ExecutionPipeline,
    ExternalAdapterConfig,
    MockCalendarAdapter,
    MockMarketAdapter,
    MockWeatherAdapter,
    Plan,
    PlanStep,
    SecretValidationError,
    SecretsGuard,
    ToolAdapterRegistry,
    ToolRequest,
    load_adapter_configs,
)
from events import EventBus
from skills import SkillContext


def test_mock_mode_config_preserves_mock_adapter_behavior():
    registry = ToolAdapterRegistry(
        [MockWeatherAdapter()],
        configs={
            "mock_weather": ExternalAdapterConfig(
                name="mock_weather",
                enabled=True,
                mode="mock",
                api_key_env_name="YOUR_WEATHER_API_KEY_ENV_VAR_PLACEHOLDER",
                base_url="https://example.invalid/weather",
                timeout_seconds=5,
            )
        },
    )

    response = registry.execute(
        ToolRequest(
            adapter_name="mock_weather",
            capability="weather.current",
            parameters={"location": "Madrid"},
        )
    )

    assert response.success is True
    assert response.text == "Mock weather for Madrid: clear, 21 C."
    assert registry.metadata()[0]["config"]["mode"] == "mock"


def test_real_mode_without_env_key_fails_safely(monkeypatch):
    class GuardedWeatherAdapter(MockWeatherAdapter):
        def handle(self, request):
            raise AssertionError("Real mode without an env key must not call the adapter")

    monkeypatch.delenv("ARES_TEST_WEATHER_API_KEY", raising=False)
    registry = ToolAdapterRegistry(
        [GuardedWeatherAdapter()],
        configs={
            "mock_weather": ExternalAdapterConfig(
                name="mock_weather",
                mode="real",
                api_key_env_name="ARES_TEST_WEATHER_API_KEY",
                base_url="https://example.invalid/weather",
            )
        },
    )

    response = registry.execute(
        ToolRequest(
            adapter_name="mock_weather",
            capability="weather.current",
            parameters={"location": "Madrid"},
        )
    )

    assert response.success is False
    assert response.error_message == (
        "Real mode for adapter mock_weather requires environment variable ARES_TEST_WEATHER_API_KEY."
    )
    assert response.metadata["missing_env_key"] == "ARES_TEST_WEATHER_API_KEY"


def test_fake_placeholder_is_accepted_only_as_placeholder():
    config = ExternalAdapterConfig(
        name="mock_weather",
        mode="real",
        api_key_env_name="YOUR_WEATHER_API_KEY_ENV_VAR_PLACEHOLDER",
    )

    SecretsGuard().validate_adapter_config(config)

    assert config.api_key_env_name_is_placeholder is True
    assert config.to_dict()["api_key_env_name_placeholder"] is True


def test_raw_looking_secret_is_rejected():
    raw_secret = "sk-" + ("a" * 32)

    with pytest.raises(SecretValidationError):
        ExternalAdapterConfig(
            name="mock_weather",
            mode="real",
            api_key_env_name=raw_secret,
        )

    with pytest.raises(SecretValidationError):
        SecretsGuard().validate_config_payload(
            {"adapters": {"mock_weather": {"api_key": raw_secret}}},
            path="test-config",
        )


def test_example_adapter_config_uses_only_safe_placeholders():
    configs = load_adapter_configs("config/adapters.example.json")

    assert sorted(configs) == ["mock_calendar", "mock_market", "mock_weather", "real_market", "real_weather"]
    assert configs["mock_weather"].mode == "mock"
    assert configs["real_weather"].enabled is False
    assert configs["real_weather"].mode == "mock"
    assert configs["real_market"].enabled is False
    assert configs["real_market"].mode == "mock"
    assert configs["mock_market"].api_key_env_name_is_placeholder is True
    assert configs["mock_calendar"].timeout_seconds == 5.0


def test_weather_market_calendar_mock_configs_do_not_require_network_or_auth():
    registry = ToolAdapterRegistry(
        [MockWeatherAdapter(), MockMarketAdapter(), MockCalendarAdapter()],
        configs=load_adapter_configs("config/adapters.example.json"),
    )

    weather = registry.execute(
        ToolRequest(
            adapter_name="mock_weather",
            capability="weather.current",
            parameters={"location": "Madrid"},
        )
    )
    market = registry.execute(
        ToolRequest(
            adapter_name="mock_market",
            capability="market.quote",
            parameters={"symbol": "nvidia"},
        )
    )
    calendar = registry.execute(
        ToolRequest(
            adapter_name="mock_calendar",
            capability="calendar.events",
            parameters={"period": "today"},
        )
    )

    assert weather.success is True
    assert market.success is True
    assert calendar.success is True
    assert all(metadata["requires_network"] is False for metadata in registry.metadata())
    assert all(metadata["requires_auth"] is False for metadata in registry.metadata())


def test_confirmation_layer_unaffected_by_adapter_config():
    event_bus = EventBus(raise_handler_errors=True)
    registry = ToolAdapterRegistry(
        [MockWeatherAdapter()],
        configs=load_adapter_configs("config/adapters.example.json"),
    )
    pipeline = ExecutionPipeline(
        skill_resolver=lambda name: None,
        event_bus=event_bus,
        tool_adapter_registry=registry,
    )
    plan = Plan(
        raw_text="delete note note-1",
        intent_name="note",
        steps=[
            PlanStep(
                order=1,
                target="notes",
                action="delete",
                input_text="delete note note-1",
                intent_name="note",
                entities={"action": "delete", "note_id": "note-1"},
            )
        ],
    )

    result = pipeline.execute(plan, SkillContext(event_bus=event_bus, tool_adapter_registry=registry))

    assert result.success is False
    assert result.stopped is True
    assert result.error_message == "Confirmation required."
    assert result.pending_confirmation["step"]["target"] == "notes"
    assert result.pending_confirmation["step"]["action"] == "delete"
    assert event_bus.history("confirmation.requested")
