from core import (
    ExternalAdapterConfig,
    Intent,
    MockWeatherAdapter,
    RealWeatherAdapter,
    SecretValidationError,
    SecretsGuard,
    ToolAdapterRegistry,
    ToolRequest,
    load_adapter_configs,
)
from skills import SkillContext
from skills.builtin.weather import WeatherSkill


def test_default_weather_remains_mock_even_when_real_adapter_exists():
    registry = ToolAdapterRegistry(
        [MockWeatherAdapter(), RealWeatherAdapter()],
        configs={
            "real_weather": ExternalAdapterConfig(
                name="real_weather",
                enabled=True,
                mode="real",
                api_key_env_name="ARES_TEST_REAL_WEATHER_API_KEY",
                base_url="https://example.invalid/weather",
            )
        },
    )
    skill = WeatherSkill()

    response = skill.handle(
        "weather in Madrid",
        SkillContext(tool_adapter_registry=registry),
    )

    assert response.skill == "weather"
    assert response.text == "Mock weather for Madrid: clear, 21 C."
    assert response.metadata["adapter_name"] == "mock_weather"
    assert response.metadata["data"]["source"] == "mock"


def test_real_weather_adapter_can_be_instantiated():
    adapter = RealWeatherAdapter()

    assert adapter.name == "real_weather"
    assert adapter.supports("weather.current") is True
    assert adapter.requires_network is True
    assert adapter.requires_auth is True
    assert adapter.metadata()["supports_real_mode"] is True


def test_real_weather_real_mode_without_env_key_fails_safely(monkeypatch):
    monkeypatch.delenv("ARES_TEST_REAL_WEATHER_API_KEY", raising=False)
    registry = ToolAdapterRegistry(
        [RealWeatherAdapter()],
        configs={
            "real_weather": ExternalAdapterConfig(
                name="real_weather",
                enabled=True,
                mode="real",
                api_key_env_name="ARES_TEST_REAL_WEATHER_API_KEY",
                base_url="https://example.invalid/weather",
            )
        },
    )

    response = registry.execute(
        ToolRequest(
            adapter_name="real_weather",
            capability="weather.current",
            parameters={"location": "Madrid"},
        )
    )

    assert response.success is False
    assert response.error_message == (
        "Real mode for adapter real_weather requires environment variable ARES_TEST_REAL_WEATHER_API_KEY."
    )
    assert response.metadata["missing_env_key"] == "ARES_TEST_REAL_WEATHER_API_KEY"


def test_real_weather_uses_env_name_only_and_does_not_expose_raw_key(monkeypatch):
    env_name = "ARES_TEST_REAL_WEATHER_API_KEY"
    raw_env_value = "test-env-value-used-only-in-memory"
    monkeypatch.setenv(env_name, raw_env_value)
    registry = ToolAdapterRegistry(
        [RealWeatherAdapter()],
        configs={
            "real_weather": ExternalAdapterConfig(
                name="real_weather",
                enabled=True,
                mode="real",
                api_key_env_name=env_name,
                base_url="https://example.invalid/weather",
                timeout_seconds=3,
            )
        },
    )

    response = registry.execute(
        ToolRequest(
            adapter_name="real_weather",
            capability="weather.forecast",
            parameters={"location": "Madrid", "period": "tomorrow"},
        )
    )

    serialized = str(response.to_dict())
    assert response.success is False
    assert response.metadata["real_weather_skeleton"] is True
    assert response.metadata["api_key_env_name"] == env_name
    assert raw_env_value not in serialized
    assert "network execution is not implemented" in response.text


def test_weather_skill_handles_real_adapter_failure_safely(monkeypatch):
    monkeypatch.delenv("ARES_TEST_REAL_WEATHER_API_KEY", raising=False)
    registry = ToolAdapterRegistry(
        [RealWeatherAdapter()],
        configs={
            "real_weather": ExternalAdapterConfig(
                name="real_weather",
                enabled=True,
                mode="real",
                api_key_env_name="ARES_TEST_REAL_WEATHER_API_KEY",
                base_url="https://example.invalid/weather",
            )
        },
    )
    intent = Intent(
        intent_name="weather",
        confidence=1.0,
        extracted_entities={
            "action": "weather",
            "location": "Madrid",
            "period": "today",
            "adapter_name": "real_weather",
            "capability": "weather.current",
        },
        raw_text="weather in Madrid",
    )

    response = WeatherSkill().handle(
        intent.raw_text,
        SkillContext(tool_adapter_registry=registry, metadata={"intent": intent}),
    )

    assert response.skill == "weather"
    assert response.metadata["adapter_name"] == "real_weather"
    assert response.metadata["error"] == (
        "Real mode for adapter real_weather requires environment variable ARES_TEST_REAL_WEATHER_API_KEY."
    )
    assert "requires environment variable ARES_TEST_REAL_WEATHER_API_KEY" in response.text


def test_real_weather_config_uses_env_name_not_raw_key():
    config = ExternalAdapterConfig(
        name="real_weather",
        enabled=True,
        mode="real",
        api_key_env_name="ARES_REAL_WEATHER_API_KEY",
        base_url="https://example.invalid/weather",
    )

    SecretsGuard().validate_adapter_config(config)

    assert config.api_key_env_name == "ARES_REAL_WEATHER_API_KEY"
    assert config.api_key_env_name_is_placeholder is False


def test_real_weather_rejects_raw_looking_key_in_config():
    raw_key = "sk-" + ("b" * 32)

    try:
        ExternalAdapterConfig(
            name="real_weather",
            enabled=True,
            mode="real",
            api_key_env_name=raw_key,
        )
    except SecretValidationError as error:
        assert "raw secret" in str(error).lower()
    else:
        raise AssertionError("Raw-looking API key must be rejected")


def test_secrets_guard_passes_example_real_weather_placeholder():
    configs = load_adapter_configs("config/adapters.example.json")

    real_weather = configs["real_weather"]

    assert real_weather.enabled is False
    assert real_weather.mode == "mock"
    assert real_weather.api_key_env_name_is_placeholder is True
