from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from core.CapabilityManifest import (
    CapabilityManifest,
    CapabilityManifestRegistry,
    ManifestPolicy,
    ManifestValidationResult,
    ProviderSelectionResult,
    build_pc_service_manifest,
    build_service_manifest,
    build_voice_city_manifest,
    register_default_voice_manifests,
)
from core.EventBus import (
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    Event,
)
from core.Contracts import (
    CONTRACT_CORE_EXECUTION_REQUEST,
    CONTRACT_CORE_EXECUTION_RESULT,
    CONTRACT_VERSION_V1,
    CoreExecutionRequestV1,
    ContractCompatibilityResult,
    utc_contract_timestamp,
    validate_contract,
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
    contract_name: str = CONTRACT_CORE_EXECUTION_RESULT
    contract_version: str = CONTRACT_VERSION_V1
    correlation_id: str = ""
    session_id: str = ""
    created_at: str = field(default_factory=utc_contract_timestamp)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "success": self.success,
            "text": self.text,
            "data": _stable_data(self.data),
            "error_message": self.error_message,
            "metadata": _stable_data(self.metadata),
        }


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
        manifest_registry: Optional[CapabilityManifestRegistry] = None,
        manifest_policy: Optional[ManifestPolicy] = None,
        register_default_pc: bool = True,
        register_default_voice: bool = True,
    ):
        self._services: Dict[str, Any] = {}
        self._service_metadata: Dict[str, Dict[str, Any]] = {}
        self._event_decisions: List[CoreEventDecisionResult] = []
        self._event_history_store = event_history_store
        self.lifecycle_manager = lifecycle_manager or ModuleLifecycleManager()
        self.manifest_registry = manifest_registry or CapabilityManifestRegistry(
            policy=manifest_policy,
        )
        if register_default_voice or voice_service is not None:
            register_default_voice_manifests(self.manifest_registry)
        for name, service in (services or {}).items():
            self.register_service(name, service)

        if pc_service is not None:
            self.register_service(
                PC_SERVICE_NAME,
                pc_service,
                capabilities=_default_pc_capabilities(),
                manifest=build_pc_service_manifest(_default_pc_capabilities()),
            )
        elif register_default_pc and PC_SERVICE_NAME not in self._services:
            self.register_service(
                PC_SERVICE_NAME,
                WindowsPCService(),
                capabilities=_default_pc_capabilities(),
                manifest=build_pc_service_manifest(_default_pc_capabilities()),
            )

        if voice_service is not None:
            self.register_service(
                VOICE_SERVICE_NAME,
                voice_service,
                capabilities=_default_voice_capabilities(),
                manifest=build_voice_city_manifest(_default_voice_capabilities()),
            )
        elif register_default_voice and VOICE_SERVICE_NAME not in self._services:
            self.register_service(
                VOICE_SERVICE_NAME,
                PlaceholderVoiceService(),
                capabilities=_default_voice_capabilities(),
                manifest=build_voice_city_manifest(_default_voice_capabilities()),
            )

    def register_service(
        self,
        name: str,
        service: Any,
        capabilities: Optional[List[str]] = None,
        city_status: str = CITY_STATE_IDLE,
        manifest: Optional[CapabilityManifest | Dict[str, Any]] = None,
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
        self._ensure_service_manifest(service_name, service, capabilities or [], manifest)
        self.lifecycle_manager.register_module(service_name, service, LifecyclePolicy())
        return service

    def get_service(self, name: str) -> Optional[Any]:
        """Return a registered service by normalized name, or None when absent."""
        return self._services.get(_normalize_service_name(name))

    def get_manifest(self, name: str) -> Optional[CapabilityManifest]:
        """Return a registered capability manifest by normalized name."""
        return self.manifest_registry.get_manifest(name)

    def list_manifests(self) -> List[Dict[str, Any]]:
        """Return registered capability manifests as deterministic dictionaries."""
        return [
            manifest.to_dict()
            for manifest in self.manifest_registry.list_manifests()
        ]

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
        contract_version: str = CONTRACT_VERSION_V1,
    ) -> CoreServiceResult:
        """Route one request to the first idle service registered for a capability."""
        clean_capability = _normalize_capability(capability)
        core_request = _core_execution_request(
            capability=clean_capability,
            payload=dict(request_payload or {}),
            session_id=session_id,
            correlation_id=correlation_id,
            contract_version=contract_version,
        )
        compatibility = validate_contract(
            core_request,
            expected_contract_name=CONTRACT_CORE_EXECUTION_REQUEST,
        )
        if not compatibility.success:
            self._store_contract_rejection(core_request, compatibility)
            return CoreServiceResult(
                success=False,
                text="Core execution request rejected by compatibility gate.",
                error_message=compatibility.error_message or compatibility.status,
                data={
                    "source": "core_service",
                    "status": "contract_rejected",
                    "capability": clean_capability,
                    "compatibility": compatibility.to_dict(),
                    "request": _contract_payload(core_request),
                    "city_statuses": self._city_statuses(),
                },
                correlation_id=str(_contract_payload(core_request).get("correlation_id") or ""),
                session_id=str(_contract_payload(core_request).get("session_id") or ""),
                metadata={"safe": True, "source": "core_service"},
            )
        if not clean_capability:
            return CoreServiceResult(
                success=False,
                text="Capability is required for routing.",
                error_message="missing_capability",
                data={"source": "core_service", "city_statuses": self._city_statuses()},
                correlation_id=correlation_id,
                session_id=session_id,
                metadata={"safe": True, "source": "core_service"},
            )

        service_name, selection = self._select_service_for_capability(clean_capability)
        if service_name is None and selection is None:
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
                correlation_id=correlation_id,
                session_id=session_id,
                metadata={"safe": True, "source": "core_service"},
            )
        if selection is not None and not selection.success:
            self._store_manifest_rejection(
                clean_capability,
                "",
                selection,
                correlation_id,
                session_id,
            )
            return self._manifest_activation_failure(
                clean_capability,
                "",
                selection,
                correlation_id,
                session_id,
            )

        service = self._services[service_name]
        before_status = self._city_status(service_name)
        manifest_gate = self.manifest_registry.validate_manifest_requirements(
            service_name,
            clean_capability,
            implementation=service,
        )
        if not manifest_gate.success:
            self._store_manifest_rejection(
                clean_capability,
                service_name,
                manifest_gate,
                correlation_id,
                session_id,
            )
            return self._manifest_activation_failure(
                clean_capability,
                service_name,
                manifest_gate,
                correlation_id,
                session_id,
            )
        lifecycle_request = LifecycleRequest(
            module_name=service_name,
            operation="route_by_capability",
            payload={
                "capability": clean_capability,
                **dict(core_request.payload if isinstance(core_request, CoreExecutionRequestV1) else request_payload or {}),
                "core_contract": _contract_payload(core_request),
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
                correlation_id=correlation_id,
                session_id=session_id,
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
                "provider_selection": selection.to_dict() if selection else {},
                "manifest_validation": manifest_gate.to_dict(),
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
            correlation_id=correlation_id,
            session_id=session_id,
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

    def _select_service_for_capability(
        self,
        capability: str,
    ) -> tuple[Optional[str], Optional[ProviderSelectionResult]]:
        candidates = [
            name
            for name in self._services
            if self._city_status(name) == CITY_STATE_IDLE
            and capability in self._service_capabilities(name)
        ]
        if not candidates:
            return None, None
        selection = self.manifest_registry.select_provider(
            capability,
            available_modules=candidates,
        )
        if not selection.success:
            return None, selection
        return selection.selected_provider, selection

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

    def _store_contract_rejection(
        self,
        request: Any,
        compatibility: ContractCompatibilityResult,
    ) -> None:
        payload = _contract_payload(request)
        event = Event(
            source="core_service",
            type="contract.compatibility_rejected",
            priority=PRIORITY_NORMAL,
            payload={
                "request": payload,
                "compatibility": compatibility.to_dict(),
            },
            correlation_id=str(payload.get("correlation_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            metadata={"safe": True, "source": "core_service"},
        )
        result = CoreEventDecisionResult(
            success=False,
            decision=EVENT_DECISION_FAILED,
            text="Core execution request rejected by compatibility gate.",
            error_message=compatibility.error_message or compatibility.status,
            data={
                "source": "core_service",
                "event": event.to_dict(),
                "compatibility": compatibility.to_dict(),
                "city_statuses": self._city_statuses(),
                "escalated": False,
            },
            metadata={"safe": True, "source": "core_service"},
        )
        self._store_event_history(event, result)

    def _store_manifest_rejection(
        self,
        capability: str,
        service_name: str,
        result: ManifestValidationResult | ProviderSelectionResult,
        correlation_id: str,
        session_id: str,
    ) -> None:
        payload = result.to_dict()
        event = Event(
            source="core_service",
            type="manifest.validation_failed",
            priority=PRIORITY_NORMAL,
            payload={
                "capability": capability,
                "service": service_name,
                "manifest_result": payload,
            },
            correlation_id=str(correlation_id or ""),
            session_id=str(session_id or ""),
            metadata={"safe": True, "source": "core_service"},
        )
        decision = CoreEventDecisionResult(
            success=False,
            decision=EVENT_DECISION_FAILED,
            text="Capability manifest validation failed before activation.",
            error_message=str(payload.get("error_message") or payload.get("status") or "manifest_validation_failed"),
            data={
                "source": "core_service",
                "event": event.to_dict(),
                "manifest_result": payload,
                "city_statuses": self._city_statuses(),
                "escalated": False,
            },
            metadata={"safe": True, "source": "core_service"},
        )
        self._store_event_history(event, decision)

    def _manifest_activation_failure(
        self,
        capability: str,
        service_name: str,
        result: ManifestValidationResult | ProviderSelectionResult,
        correlation_id: str,
        session_id: str,
    ) -> CoreServiceResult:
        payload = result.to_dict()
        return CoreServiceResult(
            success=False,
            text=f"Capability route failed manifest gate: {capability}",
            error_message=str(payload.get("error_message") or payload.get("status") or "manifest_validation_failed"),
            data={
                "source": "core_service",
                "capability": capability,
                "service": service_name,
                "status": "manifest_rejected",
                "manifest_result": payload,
                "city_statuses": self._city_statuses(),
            },
            correlation_id=correlation_id,
            session_id=session_id,
            metadata={"safe": True, "source": "core_service"},
        )

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
            correlation_id=str(getattr(lifecycle_result, "correlation_id", "") or ""),
            session_id=str(getattr(lifecycle_result, "session_id", "") or ""),
            metadata={"safe": True, "source": "core_service"},
        )

    def _ensure_service_manifest(
        self,
        service_name: str,
        service: Any,
        capabilities: List[str],
        manifest: Optional[CapabilityManifest | Dict[str, Any]],
    ) -> None:
        parsed_manifest = None
        if manifest is not None:
            parsed_manifest = (
                manifest
                if isinstance(manifest, CapabilityManifest)
                else CapabilityManifest.from_dict(manifest)
            )
        else:
            parsed_manifest = build_service_manifest(
                module_name=service_name,
                capabilities=capabilities,
                module_type="service",
                description=f"ARES registered service: {service_name}",
                metadata={
                    "source": "core_service",
                    "service_type": type(service).__name__,
                    "generated": True,
                },
            )

        existing = self.manifest_registry.get_manifest(service_name)
        if existing is None:
            self.manifest_registry.register_manifest(parsed_manifest)
            return
        if existing.to_dict() != parsed_manifest.to_dict():
            raise ValueError(f"Conflicting manifest for service: {service_name}")


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


def _core_execution_request(
    capability: str,
    payload: Dict[str, Any],
    session_id: str,
    correlation_id: str,
    contract_version: str,
) -> Any:
    try:
        return CoreExecutionRequestV1(
            capability=capability,
            payload=dict(payload or {}),
            session_id=session_id,
            correlation_id=correlation_id,
            contract_version=contract_version,
            metadata={"source": "core_service"},
        )
    except ValueError:
        return {
            "contract_name": CONTRACT_CORE_EXECUTION_REQUEST,
            "contract_version": str(contract_version or ""),
            "correlation_id": str(correlation_id or ""),
            "session_id": str(session_id or ""),
            "created_at": utc_contract_timestamp(),
            "metadata": {"source": "core_service"},
            "capability": capability,
            "payload": dict(payload or {}),
        }


def _contract_payload(contract: Any) -> Dict[str, Any]:
    if isinstance(contract, dict):
        return dict(contract)
    to_dict = getattr(contract, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            return dict(data)
    return {}


def _stable_data(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _stable_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_stable_data(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _stable_data(to_dict())
    return repr(value)


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
