from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from core.EventBus import (
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    Event,
)
from core.ModuleLifecycle import (
    LIFECYCLE_DEGRADED,
    LIFECYCLE_FAILED,
    LifecyclePolicy,
    LifecycleRequest,
    ModuleLifecycleManager,
)
from core.PCService import PCService, WindowsPCService
from core.VoiceService import PlaceholderVoiceService, VoiceService
from events.EventHistoryStore import EventHistoryStore

PC_SERVICE_NAME = "pc"
VOICE_SERVICE_NAME = "voice"
EVENT_DECISION_IGNORED = "ignored"
EVENT_DECISION_RECORDED = "recorded"
EVENT_DECISION_ESCALATED = "escalated"
EVENT_DECISION_FAILED = "failed"
EVENT_DECISIONS = {
    EVENT_DECISION_IGNORED,
    EVENT_DECISION_RECORDED,
    EVENT_DECISION_ESCALATED,
    EVENT_DECISION_FAILED,
}
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


@dataclass(frozen=True)
class CoreEventDecisionResult:
    success: bool
    decision: str
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
        event_history_store: Optional[EventHistoryStore] = None,
        lifecycle_manager: Optional[ModuleLifecycleManager] = None,
        register_default_pc: bool = True,
        register_default_voice: bool = True,
    ):
        self._services: Dict[str, Any] = {}
        self._service_metadata: Dict[str, Dict[str, Any]] = {}
        self._event_decisions: List[CoreEventDecisionResult] = []
        self._event_history_store = event_history_store
        self.lifecycle_manager = lifecycle_manager or ModuleLifecycleManager()
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
        self.lifecycle_manager.register_module(service_name, service, LifecyclePolicy())
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

    def recover_service(self, name: str) -> CoreServiceResult:
        """Explicitly recover a failed or degraded module and return it to idle."""
        service_name = _normalize_service_name(name)
        if service_name not in self._services:
            return CoreServiceResult(
                success=False,
                text=f"Lifecycle recovery failed safely because service is missing: {name}",
                error_message="service_not_registered",
                data={"source": "core_service", "service": service_name},
                metadata={"safe": True, "source": "core_service"},
            )

        result = self.lifecycle_manager.recover(
            service_name,
            LifecycleRequest(module_name=service_name, operation="recover"),
        )
        if result.success:
            self._set_city_status(service_name, CITY_STATE_IDLE)
        return CoreServiceResult(
            success=result.success,
            text=result.text,
            error_message=result.error_message,
            data={
                "source": "core_service",
                "service": service_name,
                "module_lifecycle": result.to_dict(),
                "lifecycle_status": self.lifecycle_manager.status(service_name).to_dict(),
                "city_statuses": self._city_statuses(),
            },
            metadata={"safe": True, "source": "core_service"},
        )

    def get_lifecycle_status(self, name: Optional[str] = None) -> CoreServiceResult:
        """Return structured lifecycle state and health information."""
        if name is None:
            return CoreServiceResult(
                success=True,
                text="Lifecycle statuses discovered.",
                data={
                    "source": "core_service",
                    "modules": self.lifecycle_manager.statuses(),
                    "city_statuses": self._city_statuses(),
                },
                metadata={"safe": True, "source": "core_service"},
            )

        service_name = _normalize_service_name(name)
        if service_name not in self._services:
            return CoreServiceResult(
                success=False,
                text=f"Lifecycle status failed safely because service is missing: {name}",
                error_message="service_not_registered",
                data={"source": "core_service", "service": service_name},
                metadata={"safe": True, "source": "core_service"},
            )
        return CoreServiceResult(
            success=True,
            text=f"Lifecycle status discovered for service: {service_name}",
            data={
                "source": "core_service",
                "service": service_name,
                "lifecycle_status": self.lifecycle_manager.status(service_name).to_dict(),
                "city_status": self._city_status(service_name),
            },
            metadata={"safe": True, "source": "core_service"},
        )

    def get_lifecycle_history(self, name: str) -> CoreServiceResult:
        """Return structured lifecycle transition history for a service."""
        service_name = _normalize_service_name(name)
        if service_name not in self._services:
            return CoreServiceResult(
                success=False,
                text=f"Lifecycle history failed safely because service is missing: {name}",
                error_message="service_not_registered",
                data={"source": "core_service", "service": service_name},
                metadata={"safe": True, "source": "core_service"},
            )
        return CoreServiceResult(
            success=True,
            text=f"Lifecycle history discovered for service: {service_name}",
            data={
                "source": "core_service",
                "service": service_name,
                "history": [
                    transition.to_dict()
                    for transition in self.lifecycle_manager.history(service_name)
                ],
            },
            metadata={"safe": True, "source": "core_service"},
        )

    def route_by_capability(
        self,
        capability: str,
        handler: RouteHandler,
        session_id: str = "",
        correlation_id: str = "",
        request_payload: Optional[Dict[str, Any]] = None,
    ) -> CoreServiceResult:
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
        lifecycle_request = LifecycleRequest(
            module_name=service_name,
            operation="route_by_capability",
            payload={
                "capability": clean_capability,
                **dict(request_payload or {}),
            },
            session_id=session_id,
            correlation_id=correlation_id,
            metadata={"source": "core_service"},
        )
        start_result = self.lifecycle_manager.start(service_name, lifecycle_request)
        if not start_result.success:
            self._set_city_status(service_name, CITY_STATE_FAILED)
            return self._route_lifecycle_failure(
                clean_capability,
                service_name,
                before_status,
                start_result,
            )

        health_result = self.lifecycle_manager.health_check(service_name, lifecycle_request)
        if not health_result.success:
            self._set_city_status(service_name, CITY_STATE_FAILED)
            return self._route_lifecycle_failure(
                clean_capability,
                service_name,
                before_status,
                health_result,
            )

        self._set_city_status(service_name, CITY_STATE_ACTIVE)
        execute_result = self.lifecycle_manager.execute(
            service_name,
            lifecycle_request,
            lambda active_service: handler(active_service),
        )
        if not execute_result.success:
            self._set_city_status(service_name, CITY_STATE_FAILED)
            return CoreServiceResult(
                success=False,
                text=f"Capability route failed safely: {clean_capability}",
                error_message=execute_result.error_message or execute_result.status,
                data={
                    "source": "core_service",
                    "capability": clean_capability,
                    "service": service_name,
                    "module_lifecycle": {
                        "start": start_result.to_dict(),
                        "health_check": health_result.to_dict(),
                        "execute": execute_result.to_dict(),
                    },
                    "lifecycle_status": self.lifecycle_manager.status(service_name).to_dict(),
                    "city_lifecycle": {
                        "before": before_status,
                        "during": CITY_STATE_ACTIVE,
                        "after": self._city_status(service_name),
                    },
                    "city_statuses": self._city_statuses(),
                },
                metadata={"safe": True, "source": "core_service"},
            )

        response = execute_result.data.get("response")
        self._set_city_status(service_name, CITY_STATE_IDLE)
        return CoreServiceResult(
            success=True,
            text=f"Capability routed to city: {service_name}",
            data={
                "source": "core_service",
                "capability": clean_capability,
                "service": service_name,
                "response": _route_response_to_data(response),
                "module_lifecycle": {
                    "start": start_result.to_dict(),
                    "health_check": health_result.to_dict(),
                    "execute": execute_result.to_dict(),
                },
                "lifecycle_status": self.lifecycle_manager.status(service_name).to_dict(),
                "city_lifecycle": {
                    "before": before_status,
                    "during": CITY_STATE_ACTIVE,
                    "after": self._city_status(service_name),
                },
                "city_statuses": self._city_statuses(),
            },
            metadata={"safe": True, "source": "core_service"},
        )

    def handle_event(self, event: Event) -> CoreEventDecisionResult:
        """Decide whether a city event is ignored, recorded, or escalated."""
        if not isinstance(event, Event):
            return CoreEventDecisionResult(
                success=False,
                decision=EVENT_DECISION_IGNORED,
                text="Event ignored safely because it is not a core Event.",
                error_message="invalid_event",
                data={"source": "core_service", "city_statuses": self._city_statuses()},
                metadata={"safe": True, "source": "core_service"},
            )

        event_source = _normalize_service_name(event.source)
        if event_source not in self._services:
            result = self._ignored_event_result(
                event,
                "unknown_event_source",
                f"Event ignored safely because source is not registered: {event.source}",
            )
            self._store_event_history(event, result)
            return result

        city_status = self._city_status(event_source)
        if city_status == CITY_STATE_DISABLED:
            result = self._ignored_event_result(
                event,
                "disabled_event_source",
                f"Event ignored safely because source is disabled: {event_source}",
            )
            self._store_event_history(event, result)
            return result

        if event.priority in {PRIORITY_HIGH, PRIORITY_CRITICAL}:
            result = self._event_result(
                event,
                event_source,
                EVENT_DECISION_ESCALATED,
                "Event escalated by CoreService.",
                escalated=True,
            )
        elif event.priority in {PRIORITY_LOW, PRIORITY_NORMAL}:
            result = self._event_result(
                event,
                event_source,
                EVENT_DECISION_RECORDED,
                "Event recorded by CoreService.",
                escalated=False,
            )
        else:
            result = self._ignored_event_result(
                event,
                "invalid_event_priority",
                f"Event ignored safely because priority is invalid: {event.priority}",
            )
            self._store_event_history(event, result)
            return result

        self._event_decisions.append(result)
        self._store_event_history(event, result)
        return result

    def event_decisions(self, decision: Optional[str] = None) -> List[CoreEventDecisionResult]:
        """Return recorded CoreService event decisions, optionally filtered."""
        if decision is None:
            return list(self._event_decisions)
        clean_decision = _normalize_event_decision(decision)
        return [
            event_decision
            for event_decision in self._event_decisions
            if event_decision.decision == clean_decision
        ]

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

    def _ignored_event_result(
        self,
        event: Event,
        error_message: str,
        text: str,
    ) -> CoreEventDecisionResult:
        return CoreEventDecisionResult(
            success=False,
            decision=EVENT_DECISION_IGNORED,
            text=text,
            error_message=error_message,
            data={
                "source": "core_service",
                "event": event.to_dict(),
                "city_statuses": self._city_statuses(),
                "escalated": False,
            },
            metadata={"safe": True, "source": "core_service"},
        )

    def _event_result(
        self,
        event: Event,
        event_source: str,
        decision: str,
        text: str,
        escalated: bool,
    ) -> CoreEventDecisionResult:
        return CoreEventDecisionResult(
            success=True,
            decision=decision,
            text=text,
            data={
                "source": "core_service",
                "event": event.to_dict(),
                "event_source": event_source,
                "city_status": self._city_status(event_source),
                "city_statuses": self._city_statuses(),
                "escalated": escalated,
            },
            metadata={"safe": True, "source": "core_service"},
        )

    def _store_event_history(self, event: Event, result: CoreEventDecisionResult) -> None:
        if self._event_history_store is not None:
            self._event_history_store.add(event, result)

    def _route_lifecycle_failure(
        self,
        capability: str,
        service_name: str,
        before_status: str,
        lifecycle_result: Any,
    ) -> CoreServiceResult:
        lifecycle_state = str(getattr(lifecycle_result, "state", "") or "")
        return CoreServiceResult(
            success=False,
            text=f"Capability route failed lifecycle gate: {capability}",
            error_message=getattr(lifecycle_result, "error_message", "") or getattr(
                lifecycle_result,
                "status",
                "lifecycle_failure",
            ),
            data={
                "source": "core_service",
                "capability": capability,
                "service": service_name,
                "module_lifecycle": {"gate": lifecycle_result.to_dict()},
                "lifecycle_status": self.lifecycle_manager.status(service_name).to_dict(),
                "city_lifecycle": {
                    "before": before_status,
                    "during": LIFECYCLE_DEGRADED
                    if lifecycle_state == LIFECYCLE_DEGRADED
                    else LIFECYCLE_FAILED,
                    "after": self._city_status(service_name),
                },
                "city_statuses": self._city_statuses(),
            },
            metadata={"safe": True, "source": "core_service"},
        )


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


def _normalize_event_decision(decision: str) -> str:
    clean_decision = str(decision or "").strip().lower()
    if clean_decision not in EVENT_DECISIONS:
        raise ValueError(f"Invalid event decision: {decision}")
    return clean_decision


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
