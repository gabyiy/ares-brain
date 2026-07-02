from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


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

    def metadata(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "requires_network": bool(self.requires_network),
            "requires_auth": bool(self.requires_auth),
        }

    def supports(self, capability: str) -> bool:
        return (capability or "").strip() in set(self.capabilities)

    @abstractmethod
    def handle(self, request: ToolRequest) -> ToolResponse:
        raise NotImplementedError


class ToolAdapterRegistry:
    def __init__(self, adapters: Optional[Iterable[ToolAdapter]] = None):
        self._adapters: Dict[str, ToolAdapter] = {}
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

        if not adapter.supports(request.capability):
            return ToolResponse(
                adapter_name=adapter.name,
                capability=request.capability,
                success=False,
                text=f"Adapter {adapter.name} does not support capability: {request.capability}",
                error_message=f"Adapter {adapter.name} does not support capability: {request.capability}",
                metadata={"unsupported_capability": request.capability},
            )

        return adapter.handle(request)

    def metadata(self) -> List[Dict[str, Any]]:
        return [adapter.metadata() for adapter in self.all()]


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
