from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from core.PCService import PCService, WindowsPCService
from core.VoiceService import PlaceholderVoiceService, VoiceService

PC_SERVICE_NAME = "pc"
VOICE_SERVICE_NAME = "voice"
CITY_STATE_IDLE = "idle"
CITY_STATE_ACTIVE = "active"
CITY_STATE_FAILED = "failed"
CITY_STATE_DISABLED = "disabled"
CITY_LIFECYCLE_STATES = {
    CITY_STATE_IDLE,
    CITY_STATE_ACTIVE,
    CITY_STATE_FAILED,
    CITY_STATE_DISABLED,
}
CapabilityMethod = Callable[[], Any]
RouteHandler = Callable[[Any], Any]


@dataclass(frozen=True)
class CoreServiceResult:
    success: bool
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class CoreService:
    """Registry and capability aggregator for local/external service boundaries."""

    def __init__(
        self,
        services: Optional[Mapping[str, Any]] = None,
        pc_service: Optional[PCService] = None,
        voice_service: Optional[VoiceService] = None,
        register_default_pc: bool = True,
        register_default_voice: bool = True,
    ):
        self._services: Dict[str, Any] = {}
        self._service_metadata: Dict[str, Dict[str, Any]] = {}
        for name, service in (services or {}).items():
            self.register_service(name, service)

        if pc_service is not None:
            self.register_service(
                PC_SERVICE_NAME,
                pc_service,
                capabilities=_default_pc_capabilities(),
            )
        elif register_default_pc and PC_SERVICE_NAME not in self._services:
            self.register_service(
                PC_SERVICE_NAME,
                WindowsPCService(),
                capabilities=_default_pc_capabilities(),
            )

        if voice_service is not None:
            self.register_service(
                VOICE_SERVICE_NAME,
                voice_service,
                capabilities=_default_voice_capabilities(),
            )
        elif register_default_voice and VOICE_SERVICE_NAME not in self._services:
            self.register_service(
                VOICE_SERVICE_NAME,
                PlaceholderVoiceService(),
                capabilities=_default_voice_capabilities(),
            )

    def register_service(
        self,
        name: str,
        service: Any,
        capabilities: Optional[List[str]] = None,
        city_status: str = CITY_STATE_IDLE,
    ) -> Any:
        """Register a service by its normalized name and return the service instance."""
        service_name = _normalize_service_name(name)
        if not service_name:
            raise ValueError("Service name is required")
        if service is None:
            raise ValueError("Service instance is required")
        clean_status = _normalize_city_status(city_status)
        self._services[service_name] = service
        self._service_metadata[service_name] = {
            "city_status": clean_status,
            "capabilities": _normalize_capabilities(capabilities or []),
        }
        return service

    def get_service(self, name: str) -> Optional[Any]:
        """Return a registered service by normalized name, or None when absent."""
        return self._services.get(_normalize_service_name(name))

    def list_services(self) -> List[Dict[str, Any]]:
        """Return stable metadata for registered services."""
        return [
            {
                "name": name,
                "type": type(service).__name__,
                "city_status": self._city_status(name),
                "capabilities": list(self._service_capabilities(name)),
            }
            for name, service in self._services.items()
        ]

    def get_service_status(self, name: str) -> Optional[str]:
        """Return the lifecycle state for a registered service."""
        service_name = _normalize_service_name(name)
        if service_name not in self._services:
            return None
        return self._city_status(service_name)

    def disable_service(self, name: str) -> bool:
        """Disable a registered city/service without removing it."""
        return self._set_city_status(name, CITY_STATE_DISABLED)

    def enable_service(self, name: str) -> bool:
        """Return a disabled or failed city/service to idle state."""
        return self._set_city_status(name, CITY_STATE_IDLE)

    def route_by_capability(self, capability: str, handler: RouteHandler) -> CoreServiceResult:
        """Route one request to the first idle service registered for a capability."""
        clean_capability = _normalize_capability(capability)
        if not clean_capability:
            return CoreServiceResult(
                success=False,
                text="Capability is required for routing.",
                error_message="missing_capability",
                data={"source": "core_service", "city_statuses": self._city_statuses()},
                metadata={"safe": True, "source": "core_service"},
            )

        service_name = self._first_service_for_capability(clean_capability)
        if not service_name:
            return CoreServiceResult(
                success=False,
                text=f"No idle city is registered for capability: {clean_capability}",
                error_message="capability_not_available",
                data={
                    "source": "core_service",
                    "capability": clean_capability,
                    "capability_registry": self._capability_registry(),
                    "city_statuses": self._city_statuses(),
                },
                metadata={"safe": True, "source": "core_service"},
            )

        service = self._services[service_name]
        before_status = self._city_status(service_name)
        self._set_city_status(service_name, CITY_STATE_ACTIVE)
        try:
            response = handler(service)
        except Exception as error:
            self._set_city_status(service_name, CITY_STATE_FAILED)
            return CoreServiceResult(
                success=False,
                text=f"Capability route failed safely: {clean_capability}",
                error_message=f"{type(error).__name__}: {error}",
                data={
                    "source": "core_service",
                    "capability": clean_capability,
                    "service": service_name,
                    "city_lifecycle": {
                        "before": before_status,
                        "during": CITY_STATE_ACTIVE,
                        "after": self._city_status(service_name),
                    },
                    "city_statuses": self._city_statuses(),
                },
                metadata={"safe": True, "source": "core_service"},
            )

        self._set_city_status(service_name, CITY_STATE_IDLE)
        return CoreServiceResult(
            success=True,
            text=f"Capability routed to city: {service_name}",
            data={
                "source": "core_service",
                "capability": clean_capability,
                "service": service_name,
                "response": _route_response_to_data(response),
                "city_lifecycle": {
                    "before": before_status,
                    "during": CITY_STATE_ACTIVE,
                    "after": self._city_status(service_name),
                },
                "city_statuses": self._city_statuses(),
            },
            metadata={"safe": True, "source": "core_service"},
        )

    def get_capabilities(self) -> CoreServiceResult:
        capabilities_by_service: Dict[str, Dict[str, Any]] = {}
        services: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []

        for name, service in self._services.items():
            service_info = {
                "name": name,
                "type": type(service).__name__,
                "city_status": self._city_status(name),
                "registered_capabilities": list(self._service_capabilities(name)),
            }
            if self._city_status(name) == CITY_STATE_DISABLED:
                services.append({**service_info, "success": False, "disabled": True})
                continue

            get_capabilities = _service_capability_method(service)
            if get_capabilities is None:
                errors.append(
                    {
                        "service": name,
                        "error": "missing_get_capabilities",
                    }
                )
                services.append({**service_info, "success": False})
                continue

            result = get_capabilities()
            data = dict(getattr(result, "data", {}) or {})
            success = bool(getattr(result, "success", False))
            capabilities_by_service[name] = data
            services.append(
                {
                    **service_info,
                    "success": success,
                    "capabilities": data,
                }
            )
            if not success:
                errors.append(
                    {
                        "service": name,
                        "error": str(getattr(result, "error_message", "") or "capability_failure"),
                    }
                )

        return CoreServiceResult(
            success=not errors,
            text="Core service capabilities discovered." if not errors else "Core service capability discovery completed with errors.",
            data={
                "source": "core_service",
                "services": services,
                "available_services": [service["name"] for service in self.list_services()],
                "capabilities_by_service": capabilities_by_service,
                "capability_registry": self._capability_registry(),
                "city_statuses": self._city_statuses(),
                "errors": errors,
            },
            error_message="" if not errors else "capability_discovery_errors",
            metadata={"safe": True, "source": "core_service"},
        )

    def _city_status(self, name: str) -> str:
        metadata = self._service_metadata.get(_normalize_service_name(name), {})
        return str(metadata.get("city_status") or CITY_STATE_IDLE)

    def _service_capabilities(self, name: str) -> List[str]:
        metadata = self._service_metadata.get(_normalize_service_name(name), {})
        return list(metadata.get("capabilities") or [])

    def _set_city_status(self, name: str, city_status: str) -> bool:
        service_name = _normalize_service_name(name)
        if service_name not in self._services:
            return False
        self._service_metadata.setdefault(service_name, {})
        self._service_metadata[service_name]["city_status"] = _normalize_city_status(city_status)
        return True

    def _city_statuses(self) -> Dict[str, str]:
        return {
            name: self._city_status(name)
            for name in self._services
        }

    def _capability_registry(self) -> Dict[str, Dict[str, Any]]:
        return {
            name: {
                "type": type(service).__name__,
                "city_status": self._city_status(name),
                "capabilities": list(self._service_capabilities(name)),
            }
            for name, service in self._services.items()
        }

    def _first_service_for_capability(self, capability: str) -> Optional[str]:
        for name in self._services:
            if self._city_status(name) != CITY_STATE_IDLE:
                continue
            if capability in self._service_capabilities(name):
                return name
        return None


def _service_capability_method(service: Any) -> Optional[CapabilityMethod]:
    get_capabilities = getattr(service, "get_capabilities", None)
    if callable(get_capabilities):
        return get_capabilities
    return None


def _normalize_service_name(name: str) -> str:
    return "_".join(part for part in str(name or "").strip().lower().split() if part)


def _normalize_city_status(city_status: str) -> str:
    clean_status = str(city_status or "").strip().lower()
    if clean_status not in CITY_LIFECYCLE_STATES:
        raise ValueError(f"Invalid city lifecycle state: {city_status}")
    return clean_status


def _normalize_capability(capability: str) -> str:
    return str(capability or "").strip().lower()


def _normalize_capabilities(capabilities: List[str]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for capability in capabilities:
        clean_capability = _normalize_capability(capability)
        if clean_capability and clean_capability not in seen:
            seen.add(clean_capability)
            normalized.append(clean_capability)
    return normalized


def _route_response_to_data(response: Any) -> Dict[str, Any]:
    if isinstance(response, CoreServiceResult):
        return response.data
    if isinstance(response, str):
        return {"text": response}
    data = getattr(response, "data", None)
    if isinstance(data, dict) and data:
        return dict(data)
    return {
        "success": getattr(response, "success", None),
        "text": getattr(response, "text", ""),
        "data": dict(data or {}),
        "error_message": getattr(response, "error_message", ""),
        "metadata": dict(getattr(response, "metadata", {}) or {}),
    }


def _default_pc_capabilities() -> List[str]:
    return [
        "pc.status",
        "pc.capabilities",
        "device.actions",
        "device.status",
        "device.apps",
        "device.open_app",
    ]


def _default_voice_capabilities() -> List[str]:
    return [
        "voice.status",
        "voice.capabilities",
        "voice.text_loop",
    ]
