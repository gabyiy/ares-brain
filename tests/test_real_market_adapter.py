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


def test_real_market_uses_env_name_only_and_does_not_expose_raw_key(monkeypatch):
    env_name = "ARES_TEST_REAL_MARKET_API_KEY"
    raw_env_value = "test-market-env-value-used-only-in-memory"
    monkeypatch.setenv(env_name, raw_env_value)
    registry = ToolAdapterRegistry(
        [RealMarketAdapter()],
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
    assert response.success is False
    assert response.metadata["real_market_skeleton"] is True
    assert response.metadata["api_key_env_name"] == env_name
    assert response.metadata["timeout_seconds"] == 4.0
    assert response.data == {
        "symbol": "NVIDIA",
        "source": "real_market_skeleton",
    }
    assert raw_env_value not in serialized
    assert "network execution is not implemented" in response.text


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
