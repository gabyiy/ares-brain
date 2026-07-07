import pytest

from core import (
    ExternalAdapterConfig,
    Intent,
    MockMarketAdapter,
    RealMarketAdapter,
    SecretValidationError,
    SecretsGuard,
    ToolAdapterRegistry,
    ToolRequest,
    load_adapter_configs,
)
from skills import SkillContext
from skills.builtin.market import MarketSkill


class FakeMarketResponse:
    def __init__(self, payload, status_code=200, json_error=None):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


def test_default_market_remains_mock_even_when_real_adapter_exists():
    registry = ToolAdapterRegistry(
        [MockMarketAdapter(), RealMarketAdapter()],
        configs={
            "real_market": ExternalAdapterConfig(
                name="real_market",
                enabled=True,
                mode="real",
                api_key_env_name="ARES_TEST_REAL_MARKET_API_KEY",
                base_url="https://example.invalid/market",
            )
        },
    )
    skill = MarketSkill()

    response = skill.handle(
        "stock nvidia",
        SkillContext(tool_adapter_registry=registry),
    )

    assert response.skill == "market"
    assert response.text == "Mock market quote for NVIDIA: 100.00 USD."
    assert response.metadata["adapter_name"] == "mock_market"
    assert response.metadata["data"]["source"] == "mock"


def test_real_market_adapter_can_be_instantiated():
    adapter = RealMarketAdapter()

    assert adapter.name == "real_market"
    assert adapter.supports("market.quote") is True
    assert adapter.supports("market.summary") is True
    assert adapter.requires_network is True
    assert adapter.requires_auth is True
    assert adapter.metadata()["supports_real_mode"] is True


def test_real_market_real_mode_without_env_key_fails_safely(monkeypatch):
    monkeypatch.delenv("ARES_TEST_REAL_MARKET_API_KEY", raising=False)
    registry = ToolAdapterRegistry(
        [RealMarketAdapter()],
        configs={
            "real_market": ExternalAdapterConfig(
                name="real_market",
                enabled=True,
                mode="real",
                api_key_env_name="ARES_TEST_REAL_MARKET_API_KEY",
                base_url="https://example.invalid/market",
            )
        },
    )

    response = registry.execute(
        ToolRequest(
            adapter_name="real_market",
            capability="market.quote",
            parameters={"symbol": "nvidia"},
        )
    )

    assert response.success is False
    assert response.error_message == (
        "Real mode for adapter real_market requires environment variable ARES_TEST_REAL_MARKET_API_KEY."
    )
    assert response.metadata["missing_env_key"] == "ARES_TEST_REAL_MARKET_API_KEY"


def test_real_market_uses_env_name_only_and_does_not_expose_raw_key_with_mocked_http(monkeypatch):
    env_name = "ARES_TEST_REAL_MARKET_API_KEY"
    raw_env_value = "test-market-env-value-used-only-in-memory"
    calls = []

    def fake_http_get(url, params, timeout):
        calls.append({"url": url, "params": dict(params), "timeout": timeout})
        return FakeMarketResponse(
            {
                "quote": {
                    "symbol": "NVIDIA",
                    "price": "123.45",
                    "currency": "USD",
                    "name": "NVIDIA Corporation",
                    "change": "1.23",
                    "changePercent": "0.99%",
                }
            }
        )

    monkeypatch.setenv(env_name, raw_env_value)
    registry = ToolAdapterRegistry(
        [RealMarketAdapter(http_get=fake_http_get)],
        configs={
            "real_market": ExternalAdapterConfig(
                name="real_market",
                enabled=True,
                mode="real",
                api_key_env_name=env_name,
                base_url="https://example.invalid/market",
                timeout_seconds=4,
            )
        },
    )

    response = registry.execute(
        ToolRequest(
            adapter_name="real_market",
            capability="market.quote",
            parameters={"symbol": "nvidia"},
        )
    )

    serialized = str(response.to_dict())
    assert response.success is True
    assert response.text == "Real market quote for NVIDIA: 123.45 USD."
    assert response.data == {
        "symbol": "NVIDIA",
        "price": 123.45,
        "currency": "USD",
        "capability": "market.quote",
        "source": "real_market",
        "name": "NVIDIA Corporation",
        "change": 1.23,
        "change_percent": 0.99,
    }
    assert response.metadata["api_key_env_name"] == env_name
    assert response.metadata["timeout_seconds"] == 4.0
    assert raw_env_value not in serialized
    assert calls == [
        {
            "url": "https://example.invalid/market",
            "params": {
                "symbol": "NVIDIA",
                "key": raw_env_value,
                "capability": "market.quote",
            },
            "timeout": 4.0,
        }
    ]


def test_real_market_http_timeout_returns_safe_error(monkeypatch):
    def timeout_http_get(url, params, timeout):
        raise TimeoutError("test timeout")

    monkeypatch.setenv("ARES_TEST_REAL_MARKET_API_KEY", "test-key")
    registry = ToolAdapterRegistry(
        [RealMarketAdapter(http_get=timeout_http_get)],
        configs={
            "real_market": ExternalAdapterConfig(
                name="real_market",
                enabled=True,
                mode="real",
                api_key_env_name="ARES_TEST_REAL_MARKET_API_KEY",
                base_url="https://example.invalid/market",
                timeout_seconds=2,
            )
        },
    )

    response = registry.execute(
        ToolRequest(
            adapter_name="real_market",
            capability="market.quote",
            parameters={"symbol": "nvidia"},
        )
    )

    assert response.success is False
    assert response.error_message == "Real market request timed out."
    assert response.metadata["reason"] == "timeout"
    assert response.metadata["timeout_seconds"] == 2.0


def test_real_market_bad_api_response_returns_safe_error(monkeypatch):
    monkeypatch.setenv("ARES_TEST_REAL_MARKET_API_KEY", "test-key")
    registry = ToolAdapterRegistry(
        [RealMarketAdapter(http_get=lambda url, params, timeout: FakeMarketResponse({"unexpected": True}))],
        configs={
            "real_market": ExternalAdapterConfig(
                name="real_market",
                enabled=True,
                mode="real",
                api_key_env_name="ARES_TEST_REAL_MARKET_API_KEY",
                base_url="https://example.invalid/market",
            )
        },
    )

    response = registry.execute(
        ToolRequest(
            adapter_name="real_market",
            capability="market.quote",
            parameters={"symbol": "nvidia"},
        )
    )

    assert response.success is False
    assert response.error_message == "Real market response could not be normalized."
    assert response.metadata["reason"] == "invalid_response"


def test_real_market_http_status_error_returns_safe_error(monkeypatch):
    monkeypatch.setenv("ARES_TEST_REAL_MARKET_API_KEY", "test-key")
    registry = ToolAdapterRegistry(
        [RealMarketAdapter(http_get=lambda url, params, timeout: FakeMarketResponse({"error": "bad"}, status_code=503))],
        configs={
            "real_market": ExternalAdapterConfig(
                name="real_market",
                enabled=True,
                mode="real",
                api_key_env_name="ARES_TEST_REAL_MARKET_API_KEY",
                base_url="https://example.invalid/market",
            )
        },
    )

    response = registry.execute(
        ToolRequest(
            adapter_name="real_market",
            capability="market.quote",
            parameters={"symbol": "nvidia"},
        )
    )

    assert response.success is False
    assert response.error_message == "Real market request failed with HTTP status 503."
    assert response.metadata["reason"] == "http_status"
    assert response.metadata["http_status_code"] == 503


def test_real_market_normalizes_global_quote_payload(monkeypatch):
    monkeypatch.setenv("ARES_TEST_REAL_MARKET_API_KEY", "test-key")
    registry = ToolAdapterRegistry(
        [
            RealMarketAdapter(
                http_get=lambda url, params, timeout: FakeMarketResponse(
                    {
                        "Global Quote": {
                            "01. symbol": "AAPL",
                            "05. price": "199.0100",
                            "10. change percent": "1.5000%",
                        }
                    }
                )
            )
        ],
        configs={
            "real_market": ExternalAdapterConfig(
                name="real_market",
                enabled=True,
                mode="real",
                api_key_env_name="ARES_TEST_REAL_MARKET_API_KEY",
                base_url="https://example.invalid/market",
            )
        },
    )

    response = registry.execute(
        ToolRequest(
            adapter_name="real_market",
            capability="market.quote",
            parameters={"symbol": "apple"},
        )
    )

    assert response.success is True
    assert response.text == "Real market quote for AAPL: 199.01 USD."
    assert response.data == {
        "symbol": "AAPL",
        "price": 199.01,
        "currency": "USD",
        "capability": "market.quote",
        "source": "real_market",
        "change_percent": 1.5,
    }


def test_market_skill_handles_real_adapter_failure_safely(monkeypatch):
    monkeypatch.delenv("ARES_TEST_REAL_MARKET_API_KEY", raising=False)
    registry = ToolAdapterRegistry(
        [RealMarketAdapter()],
        configs={
            "real_market": ExternalAdapterConfig(
                name="real_market",
                enabled=True,
                mode="real",
                api_key_env_name="ARES_TEST_REAL_MARKET_API_KEY",
                base_url="https://example.invalid/market",
            )
        },
    )
    intent = Intent(
        intent_name="market",
        confidence=1.0,
        extracted_entities={
            "action": "quote",
            "symbol": "NVIDIA",
            "adapter_name": "real_market",
            "capability": "market.quote",
        },
        raw_text="stock nvidia",
    )

    response = MarketSkill().handle(
        intent.raw_text,
        SkillContext(tool_adapter_registry=registry, metadata={"intent": intent}),
    )

    assert response.skill == "market"
    assert response.metadata["adapter_name"] == "real_market"
    assert response.metadata["error"] == (
        "Real mode for adapter real_market requires environment variable ARES_TEST_REAL_MARKET_API_KEY."
    )
    assert "requires environment variable ARES_TEST_REAL_MARKET_API_KEY" in response.text


def test_real_market_config_uses_env_name_not_raw_key():
    config = ExternalAdapterConfig(
        name="real_market",
        enabled=True,
        mode="real",
        api_key_env_name="ARES_REAL_MARKET_API_KEY",
        base_url="https://example.invalid/market",
    )

    SecretsGuard().validate_adapter_config(config)

    assert config.api_key_env_name == "ARES_REAL_MARKET_API_KEY"
    assert config.api_key_env_name_is_placeholder is False


def test_real_market_rejects_raw_looking_key_in_config():
    raw_key = "sk-" + ("c" * 32)

    with pytest.raises(SecretValidationError):
        ExternalAdapterConfig(
            name="real_market",
            enabled=True,
            mode="real",
            api_key_env_name=raw_key,
        )


def test_secrets_guard_passes_example_real_market_placeholder():
    configs = load_adapter_configs("config/adapters.example.json")

    real_market = configs["real_market"]

    assert real_market.enabled is False
    assert real_market.mode == "mock"
    assert real_market.api_key_env_name_is_placeholder is True
