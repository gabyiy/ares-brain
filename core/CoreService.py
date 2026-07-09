from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from core.PCService import PCService, WindowsPCService
from core.VoiceService import PlaceholderVoiceService, VoiceService

PC_SERVICE_NAME = "pc"
VOICE_SERVICE_NAME = "voice"
CapabilityMethod = Callable[[], Any]


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
        for name, service in (services or {}).items():
            self.register_service(name, service)

        if pc_service is not None:
            self.register_service(PC_SERVICE_NAME, pc_service)
        elif register_default_pc and PC_SERVICE_NAME not in self._services:
            self.register_service(PC_SERVICE_NAME, WindowsPCService())

        if voice_service is not None:
            self.register_service(VOICE_SERVICE_NAME, voice_service)
        elif register_default_voice and VOICE_SERVICE_NAME not in self._services:
            self.register_service(VOICE_SERVICE_NAME, PlaceholderVoiceService())

    def register_service(self, name: str, service: Any) -> Any:
        """Register a service by its normalized name and return the service instance."""
        service_name = _normalize_service_name(name)
        if not service_name:
            raise ValueError("Service name is required")
        if service is None:
            raise ValueError("Service instance is required")
        self._services[service_name] = service
        return service

    def get_service(self, name: str) -> Optional[Any]:
        """Return a registered service by normalized name, or None when absent."""
        return self._services.get(_normalize_service_name(name))

    def list_services(self) -> List[Dict[str, str]]:
        """Return stable metadata for registered services."""
        return [
            {
                "name": name,
                "type": type(service).__name__,
            }
            for name, service in self._services.items()
        ]

    def get_capabilities(self) -> CoreServiceResult:
        capabilities_by_service: Dict[str, Dict[str, Any]] = {}
        services: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []

        for name, service in self._services.items():
            service_info = {
                "name": name,
                "type": type(service).__name__,
            }
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
                "errors": errors,
            },
            error_message="" if not errors else "capability_discovery_errors",
            metadata={"safe": True, "source": "core_service"},
        )


def _service_capability_method(service: Any) -> Optional[CapabilityMethod]:
    get_capabilities = getattr(service, "get_capabilities", None)
    if callable(get_capabilities):
        return get_capabilities
    return None


def _normalize_service_name(name: str) -> str:
    return "_".join(part for part in str(name or "").strip().lower().split() if part)
