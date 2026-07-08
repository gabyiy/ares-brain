from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

from core.PCService import PCService, WindowsPCService


@dataclass(frozen=True)
class CoreServiceResult:
    success: bool
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class CoreService:
    def __init__(
        self,
        services: Optional[Mapping[str, Any]] = None,
        pc_service: Optional[PCService] = None,
        register_default_pc: bool = True,
    ):
        self._services: Dict[str, Any] = {}
        for name, service in (services or {}).items():
            self.register_service(name, service)

        if pc_service is not None:
            self.register_service("pc", pc_service)
        elif register_default_pc and "pc" not in self._services:
            self.register_service("pc", WindowsPCService())

    def register_service(self, name: str, service: Any) -> Any:
        service_name = _normalize_service_name(name)
        if not service_name:
            raise ValueError("Service name is required")
        if service is None:
            raise ValueError("Service instance is required")
        self._services[service_name] = service
        return service

    def get_service(self, name: str) -> Optional[Any]:
        return self._services.get(_normalize_service_name(name))

    def list_services(self) -> List[Dict[str, str]]:
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
            if not hasattr(service, "get_capabilities") or not callable(service.get_capabilities):
                errors.append(
                    {
                        "service": name,
                        "error": "missing_get_capabilities",
                    }
                )
                services.append({**service_info, "success": False})
                continue

            result = service.get_capabilities()
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


def _normalize_service_name(name: str) -> str:
    return "_".join(part for part in str(name or "").strip().lower().split() if part)
