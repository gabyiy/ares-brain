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


class FakeWeatherResponse:
    def __init__(self, payload, status_code=200, json_error=None):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


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
    calls = []

    def fake_http_get(url, params, timeout):
        calls.append({"url": url, "params": dict(params), "timeout": timeout})
        return FakeWeatherResponse(
            {
                "location": {"name": "Madrid"},
                "current": {
                    "temp_c": 21.4,
                    "condition": {"text": "Sunny"},
                },
            }
        )

    monkeypatch.setenv(env_name, raw_env_value)
    registry = ToolAdapterRegistry(
        [RealWeatherAdapter(http_get=fake_http_get)],
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
    assert response.success is True
    assert response.text == "Real weather for Madrid: Sunny, 21.4 C."
    assert response.data == {
        "location": "Madrid",
        "condition": "Sunny",
        "temperature_c": 21.4,
        "period": "tomorrow",
        "capability": "weather.forecast",
        "source": "real_weather",
    }
    assert response.metadata["api_key_env_name"] == env_name
    assert raw_env_value not in serialized
    assert calls == [
        {
            "url": "https://example.invalid/weather",
            "params": {
                "q": "Madrid",
                "key": raw_env_value,
                "units": "metric",
                "period": "tomorrow",
                "capability": "weather.forecast",
            },
            "timeout": 3.0,
        }
    ]


def test_real_weather_http_timeout_returns_safe_error(monkeypatch):
    def timeout_http_get(url, params, timeout):
        raise TimeoutError("test timeout")

    monkeypatch.setenv("ARES_TEST_REAL_WEATHER_API_KEY", "test-key")
    registry = ToolAdapterRegistry(
        [RealWeatherAdapter(http_get=timeout_http_get)],
        configs={
            "real_weather": ExternalAdapterConfig(
                name="real_weather",
                enabled=True,
                mode="real",
                api_key_env_name="ARES_TEST_REAL_WEATHER_API_KEY",
                base_url="https://example.invalid/weather",
                timeout_seconds=2,
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
    assert response.error_message == "Real weather request timed out."
    assert response.metadata["reason"] == "timeout"
    assert response.metadata["timeout_seconds"] == 2.0


def test_real_weather_bad_api_response_returns_safe_error(monkeypatch):
    monkeypatch.setenv("ARES_TEST_REAL_WEATHER_API_KEY", "test-key")
    registry = ToolAdapterRegistry(
        [RealWeatherAdapter(http_get=lambda url, params, timeout: FakeWeatherResponse({"unexpected": True}))],
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
    assert response.error_message == "Real weather response could not be normalized."
    assert response.metadata["reason"] == "invalid_response"


def test_real_weather_http_status_error_returns_safe_error(monkeypatch):
    monkeypatch.setenv("ARES_TEST_REAL_WEATHER_API_KEY", "test-key")
    registry = ToolAdapterRegistry(
        [RealWeatherAdapter(http_get=lambda url, params, timeout: FakeWeatherResponse({"error": "bad"}, status_code=503))],
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
    assert response.error_message == "Real weather request failed with HTTP status 503."
    assert response.metadata["reason"] == "http_status"
    assert response.metadata["http_status_code"] == 503


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
