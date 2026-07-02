import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from core.AdapterConfig import ExternalAdapterConfig, SecretsGuard


@dataclass(frozen=True)
class ToolRequest:
    adapter_name: str
    capability: str
    query: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "capability": self.capability,
            "query": self.query,
            "parameters": dict(self.parameters),
            "raw_text": self.raw_text,
        }


@dataclass(frozen=True)
class ToolResponse:
    adapter_name: str
    capability: str
    success: bool
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "capability": self.capability,
            "success": self.success,
            "text": self.text,
            "data": dict(self.data),
            "error_message": self.error_message,
            "metadata": dict(self.metadata),
        }


class ToolAdapter(ABC):
    name = ""
    description = ""
    capabilities: Tuple[str, ...] = ()
    requires_network = False
    requires_auth = False
    supports_real_mode = False

    def metadata(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "requires_network": bool(self.requires_network),
            "requires_auth": bool(self.requires_auth),
            "supports_real_mode": bool(self.supports_real_mode),
        }

    def supports(self, capability: str) -> bool:
        return (capability or "").strip() in set(self.capabilities)

    def handle_configured(
        self,
        request: ToolRequest,
        config: Optional[ExternalAdapterConfig] = None,
    ) -> ToolResponse:
        return self.handle(request)

    @abstractmethod
    def handle(self, request: ToolRequest) -> ToolResponse:
        raise NotImplementedError


class ToolAdapterRegistry:
    def __init__(
        self,
        adapters: Optional[Iterable[ToolAdapter]] = None,
        configs: Optional[Mapping[str, ExternalAdapterConfig]] = None,
        secrets_guard: Optional[SecretsGuard] = None,
    ):
        self._adapters: Dict[str, ToolAdapter] = {}
        self._configs: Dict[str, ExternalAdapterConfig] = {}
        self.secrets_guard = secrets_guard or SecretsGuard()
        for name, config in (configs or {}).items():
            self.set_config(name, config)
        for adapter in adapters or ():
            self.register(adapter)

    def register(self, adapter: ToolAdapter) -> ToolAdapter:
        if not isinstance(adapter, ToolAdapter):
            raise TypeError("Registered adapter must implement ToolAdapter")

        name = (adapter.name or "").strip()
        if not name:
            raise ValueError("Tool adapter name is required")

        if name in self._adapters:
            raise ValueError(f"Tool adapter already registered: {name}")

        self._adapters[name] = adapter
        return adapter

    def unregister(self, name: str) -> Optional[ToolAdapter]:
        return self._adapters.pop((name or "").strip(), None)

    def set_config(self, name: str, config: ExternalAdapterConfig) -> ExternalAdapterConfig:
        if not isinstance(config, ExternalAdapterConfig):
            raise TypeError("Adapter config must be an ExternalAdapterConfig")

        clean_name = (name or config.name or "").strip()
        if not clean_name:
            raise ValueError("Adapter config name is required")
        if config.name != clean_name:
            config = ExternalAdapterConfig(
                name=clean_name,
                enabled=config.enabled,
                mode=config.mode,
                api_key_env_name=config.api_key_env_name,
                base_url=config.base_url,
                timeout_seconds=config.timeout_seconds,
            )

        self.secrets_guard.validate_adapter_config(config)
        self._configs[clean_name] = config
        return config

    def config_for(self, name: str) -> Optional[ExternalAdapterConfig]:
        return self._configs.get((name or "").strip())

    def get(self, name: str) -> Optional[ToolAdapter]:
        return self._adapters.get((name or "").strip())

    def all(self) -> List[ToolAdapter]:
        return list(self._adapters.values())

    def find_by_capability(self, capability: str) -> List[ToolAdapter]:
        clean_capability = (capability or "").strip()
        return [adapter for adapter in self.all() if adapter.supports(clean_capability)]

    def execute(self, request: ToolRequest) -> ToolResponse:
        adapter = self.get(request.adapter_name)
        if not adapter:
            return ToolResponse(
                adapter_name=request.adapter_name,
                capability=request.capability,
                success=False,
                text=f"Tool adapter is not available: {request.adapter_name}",
                error_message=f"Tool adapter is not available: {request.adapter_name}",
                metadata={"missing_adapter": request.adapter_name},
            )

        blocked_response = self._blocked_by_config(adapter, request)
        if blocked_response:
            return blocked_response

        if not adapter.supports(request.capability):
            return ToolResponse(
                adapter_name=adapter.name,
                capability=request.capability,
                success=False,
                text=f"Adapter {adapter.name} does not support capability: {request.capability}",
                error_message=f"Adapter {adapter.name} does not support capability: {request.capability}",
                metadata={"unsupported_capability": request.capability},
            )

        config = self.config_for(adapter.name)
        return adapter.handle_configured(request, config)

    def metadata(self) -> List[Dict[str, Any]]:
        metadata = []
        for adapter in self.all():
            adapter_metadata = adapter.metadata()
            config = self.config_for(adapter.name)
            if config:
                adapter_metadata["config"] = config.to_dict()
            metadata.append(adapter_metadata)
        return metadata

    def _blocked_by_config(self, adapter: ToolAdapter, request: ToolRequest) -> Optional[ToolResponse]:
        config = self.config_for(adapter.name)
        if not config:
            return None

        if not config.enabled:
            return ToolResponse(
                adapter_name=adapter.name,
                capability=request.capability,
                success=False,
                text=f"Tool adapter is disabled: {adapter.name}",
                error_message=f"Tool adapter is disabled: {adapter.name}",
                metadata={"disabled_adapter": adapter.name, "config": config.to_dict()},
            )

        if config.mode in {"mock", "local"}:
            return None

        env_name = config.api_key_env_name
        if not env_name or config.api_key_env_name_is_placeholder:
            return ToolResponse(
                adapter_name=adapter.name,
                capability=request.capability,
                success=False,
                text=f"Real mode for adapter {adapter.name} requires a configured environment variable name.",
                error_message=f"Real mode for adapter {adapter.name} requires a configured environment variable name.",
                metadata={"missing_api_key_env_name": True, "config": config.to_dict()},
            )

        if not os.environ.get(env_name):
            return ToolResponse(
                adapter_name=adapter.name,
                capability=request.capability,
                success=False,
                text=f"Real mode for adapter {adapter.name} requires environment variable {env_name}.",
                error_message=f"Real mode for adapter {adapter.name} requires environment variable {env_name}.",
                metadata={"missing_env_key": env_name, "config": config.to_dict()},
            )

        if not bool(getattr(adapter, "supports_real_mode", False)):
            return ToolResponse(
                adapter_name=adapter.name,
                capability=request.capability,
                success=False,
                text=f"Real mode is not implemented for adapter {adapter.name}.",
                error_message=f"Real mode is not implemented for adapter {adapter.name}.",
                metadata={"real_mode_not_implemented": True, "config": config.to_dict()},
            )

        return None


class MockWeatherAdapter(ToolAdapter):
    name = "mock_weather"
    description = "Offline mock weather adapter for adapter pipeline tests."
    capabilities = ("weather.current", "weather.forecast")
    requires_network = False
    requires_auth = False

    def handle(self, request: ToolRequest) -> ToolResponse:
        location = str(request.parameters.get("location") or request.query or "local").strip()
        data = {
            "location": location,
            "condition": "clear",
            "temperature_c": 21,
            "source": "mock",
        }
        return ToolResponse(
            adapter_name=self.name,
            capability=request.capability,
            success=True,
            text=f"Mock weather for {location}: clear, 21 C.",
            data=data,
            metadata={"mock": True},
        )


class RealWeatherAdapter(ToolAdapter):
    name = "real_weather"
    description = "Real-weather-capable adapter skeleton. Network execution is intentionally not implemented yet."
    capabilities = ("weather.current", "weather.forecast")
    requires_network = True
    requires_auth = True
    supports_real_mode = True

    def handle(self, request: ToolRequest) -> ToolResponse:
        return ToolResponse(
            adapter_name=self.name,
            capability=request.capability,
            success=False,
            text="Real weather adapter requires explicit real-mode adapter config.",
            error_message="Real weather adapter requires explicit real-mode adapter config.",
            metadata={"missing_adapter_config": True},
        )

    def handle_configured(
        self,
        request: ToolRequest,
        config: Optional[ExternalAdapterConfig] = None,
    ) -> ToolResponse:
        if not config:
            return self.handle(request)

        if config.mode != "real":
            return ToolResponse(
                adapter_name=self.name,
                capability=request.capability,
                success=False,
                text="Real weather adapter is available only when adapter config mode is real.",
                error_message="Real weather adapter is available only when adapter config mode is real.",
                metadata={"invalid_mode": config.mode, "config": config.to_dict()},
            )

        env_name = config.api_key_env_name
        if not env_name or config.api_key_env_name_is_placeholder:
            return ToolResponse(
                adapter_name=self.name,
                capability=request.capability,
                success=False,
                text="Real weather adapter requires an API key environment variable name.",
                error_message="Real weather adapter requires an API key environment variable name.",
                metadata={"missing_api_key_env_name": True, "config": config.to_dict()},
            )

        api_key = os.environ.get(env_name)
        if not api_key:
            return ToolResponse(
                adapter_name=self.name,
                capability=request.capability,
                success=False,
                text=f"Real weather adapter requires environment variable {env_name}.",
                error_message=f"Real weather adapter requires environment variable {env_name}.",
                metadata={"missing_env_key": env_name, "config": config.to_dict()},
            )

        location = str(request.parameters.get("location") or request.query or "local").strip()
        period = str(request.parameters.get("period") or "today").strip().lower()
        return ToolResponse(
            adapter_name=self.name,
            capability=request.capability,
            success=False,
            text="Real weather adapter is configured, but network execution is not implemented yet.",
            error_message="Real weather adapter network execution is not implemented.",
            data={
                "location": location,
                "period": period,
                "source": "real_weather_skeleton",
            },
            metadata={
                "real_weather_skeleton": True,
                "api_key_env_name": env_name,
                "base_url": config.base_url,
                "timeout_seconds": config.timeout_seconds,
            },
        )


class MockMarketAdapter(ToolAdapter):
    name = "mock_market"
    description = "Offline mock market adapter for adapter pipeline tests."
    capabilities = ("market.quote", "market.summary")
    requires_network = False
    requires_auth = False

    def handle(self, request: ToolRequest) -> ToolResponse:
        symbol = str(request.parameters.get("symbol") or request.query or "ARES").strip().upper()
        data = {
            "symbol": symbol,
            "price": 100.0,
            "currency": "USD",
            "source": "mock",
        }
        return ToolResponse(
            adapter_name=self.name,
            capability=request.capability,
            success=True,
            text=f"Mock market quote for {symbol}: 100.00 USD.",
            data=data,
            metadata={"mock": True},
        )


class MockCalendarAdapter(ToolAdapter):
    name = "mock_calendar"
    description = "Offline mock calendar adapter for adapter pipeline tests."
    capabilities = ("calendar.events",)
    requires_network = False
    requires_auth = False

    def handle(self, request: ToolRequest) -> ToolResponse:
        period = str(request.parameters.get("period") or request.query or "today").strip().lower()
        events = _mock_calendar_events(period)
        data = {
            "period": period,
            "events": events,
            "source": "mock",
        }
        if events:
            event_text = "; ".join(f"{event['title']} at {event['time']}" for event in events)
            text = f"Mock calendar for {period}: {event_text}."
        else:
            text = f"Mock calendar for {period}: no events."

        return ToolResponse(
            adapter_name=self.name,
            capability=request.capability,
            success=True,
            text=text,
            data=data,
            metadata={"mock": True},
        )


def _mock_calendar_events(period: str) -> List[Dict[str, str]]:
    if period == "today":
        return [{"time": "09:00", "title": "ARES systems check"}]
    if period == "tomorrow":
        return []
    return []
