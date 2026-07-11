from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from core.Contracts import (
    CONTRACT_LIFECYCLE_EXECUTION_REQUEST,
    CONTRACT_LIFECYCLE_EXECUTION_RESULT,
    CONTRACT_VERSION_V1,
    ContractCompatibilityResult,
    utc_contract_timestamp,
    validate_contract,
)


LIFECYCLE_UNLOADED = "UNLOADED"
LIFECYCLE_STARTING = "STARTING"
LIFECYCLE_READY = "READY"
LIFECYCLE_BUSY = "BUSY"
LIFECYCLE_DEGRADED = "DEGRADED"
LIFECYCLE_STOPPING = "STOPPING"
LIFECYCLE_STOPPED = "STOPPED"
LIFECYCLE_FAILED = "FAILED"
LIFECYCLE_STATES = {
    LIFECYCLE_UNLOADED,
    LIFECYCLE_STARTING,
    LIFECYCLE_READY,
    LIFECYCLE_BUSY,
    LIFECYCLE_DEGRADED,
    LIFECYCLE_STOPPING,
    LIFECYCLE_STOPPED,
    LIFECYCLE_FAILED,
}

_RECOVERY_REQUIRED_STATES = {LIFECYCLE_DEGRADED, LIFECYCLE_FAILED}
_ALLOWED_TRANSITIONS = {
    LIFECYCLE_UNLOADED: {LIFECYCLE_STARTING, LIFECYCLE_STOPPING},
    LIFECYCLE_STARTING: {LIFECYCLE_READY, LIFECYCLE_FAILED},
    LIFECYCLE_READY: {
        LIFECYCLE_BUSY,
        LIFECYCLE_DEGRADED,
        LIFECYCLE_STOPPING,
        LIFECYCLE_FAILED,
    },
    LIFECYCLE_BUSY: {LIFECYCLE_READY, LIFECYCLE_FAILED},
    LIFECYCLE_DEGRADED: {LIFECYCLE_STOPPING, LIFECYCLE_FAILED},
    LIFECYCLE_STOPPING: {LIFECYCLE_STOPPED, LIFECYCLE_FAILED},
    LIFECYCLE_STOPPED: {LIFECYCLE_STARTING, LIFECYCLE_STOPPING},
    LIFECYCLE_FAILED: {LIFECYCLE_STOPPING},
}

LifecycleOperation = Callable[[Any], Any]


@dataclass(frozen=True)
class LifecyclePolicy:
    health_failure_state: str = LIFECYCLE_DEGRADED
    stop_after_execute: bool = False
    inactivity_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        clean_state = _normalize_state(self.health_failure_state)
        if clean_state not in {LIFECYCLE_DEGRADED, LIFECYCLE_FAILED}:
            raise ValueError("health_failure_state must be DEGRADED or FAILED")
        object.__setattr__(self, "health_failure_state", clean_state)
        if self.inactivity_seconds is not None and self.inactivity_seconds < 0:
            raise ValueError("inactivity_seconds must be non-negative")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "health_failure_state": self.health_failure_state,
            "stop_after_execute": self.stop_after_execute,
            "inactivity_seconds": self.inactivity_seconds,
            "background_timer": "disabled",
        }


@dataclass(frozen=True)
class LifecycleRequest:
    module_name: str
    operation: str
    payload: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    correlation_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    contract_name: str = CONTRACT_LIFECYCLE_EXECUTION_REQUEST
    contract_version: str = CONTRACT_VERSION_V1
    created_at: str = field(default_factory=utc_contract_timestamp)

    def __post_init__(self) -> None:
        object.__setattr__(self, "module_name", _normalize_module_name(self.module_name))
        object.__setattr__(self, "operation", str(self.operation or "").strip())
        object.__setattr__(self, "payload", dict(self.payload or {}))
        object.__setattr__(self, "session_id", str(self.session_id or "").strip())
        object.__setattr__(self, "correlation_id", str(self.correlation_id or "").strip())
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        object.__setattr__(self, "contract_name", str(self.contract_name or "").strip())
        object.__setattr__(self, "contract_version", str(self.contract_version or "").strip())
        object.__setattr__(self, "created_at", str(self.created_at or "").strip() or utc_contract_timestamp())
        if not self.module_name:
            raise ValueError("Lifecycle request module_name is required")
        if not self.operation:
            raise ValueError("Lifecycle request operation is required")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "module_name": self.module_name,
            "operation": self.operation,
            "payload": _stable_data(self.payload),
            "metadata": _stable_data(self.metadata),
        }


@dataclass(frozen=True)
class LifecycleTransition:
    module_name: str
    from_state: str
    to_state: str
    operation: str
    timestamp: str
    reason: str = ""
    session_id: str = ""
    correlation_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_name": self.module_name,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "operation": self.operation,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True)
class LifecycleResult:
    success: bool
    status: str
    state: str
    text: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    request: Optional[LifecycleRequest] = None
    contract_name: str = CONTRACT_LIFECYCLE_EXECUTION_RESULT
    contract_version: str = CONTRACT_VERSION_V1
    correlation_id: str = ""
    session_id: str = ""
    created_at: str = field(default_factory=utc_contract_timestamp)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        correlation_id = self.correlation_id or (self.request.correlation_id if self.request else "")
        session_id = self.session_id or (self.request.session_id if self.request else "")
        return {
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "correlation_id": correlation_id,
            "session_id": session_id,
            "created_at": self.created_at,
            "success": self.success,
            "status": self.status,
            "state": self.state,
            "text": self.text,
            "error_message": self.error_message,
            "data": _stable_data(self.data),
            "request": self.request.to_dict() if self.request else None,
            "metadata": _stable_data(self.metadata),
        }


@dataclass(frozen=True)
class LifecycleStatus:
    module_name: str
    state: str
    healthy: bool
    reason: str = ""
    last_transition_at: str = ""
    last_started_at: str = ""
    last_health_check_at: str = ""
    last_executed_at: str = ""
    inactive_since: str = ""
    transition_count: int = 0
    policy: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_name": self.module_name,
            "state": self.state,
            "healthy": self.healthy,
            "reason": self.reason,
            "last_transition_at": self.last_transition_at,
            "last_started_at": self.last_started_at,
            "last_health_check_at": self.last_health_check_at,
            "last_executed_at": self.last_executed_at,
            "inactive_since": self.inactive_since,
            "transition_count": self.transition_count,
            "policy": _stable_data(self.policy),
            "metadata": _stable_data(self.metadata),
        }


class LifecycleModule:
    """Optional module-side lifecycle contract for cities and heavy adapters."""

    def start(self) -> Any:
        raise NotImplementedError

    def health_check(self) -> Any:
        raise NotImplementedError

    def execute(self, request: LifecycleRequest) -> Any:
        raise NotImplementedError

    def stop(self) -> Any:
        raise NotImplementedError


@dataclass
class _LifecycleRecord:
    module_name: str
    module: Any
    policy: LifecyclePolicy
    state: str = LIFECYCLE_UNLOADED
    reason: str = ""
    last_transition_at: str = ""
    last_started_at: str = ""
    last_health_check_at: str = ""
    last_executed_at: str = ""
    inactive_since: str = ""
    transitions: List[LifecycleTransition] = field(default_factory=list)


class ModuleLifecycleManager:
    """Strict lifecycle gate for CoreService-managed modules."""

    def __init__(self):
        self._records: Dict[str, _LifecycleRecord] = {}
        self._last_timestamp: Optional[datetime] = None

    def register_module(
        self,
        name: str,
        module: Any,
        policy: Optional[LifecyclePolicy] = None,
    ) -> LifecycleStatus:
        module_name = _normalize_module_name(name)
        if not module_name:
            raise ValueError("Lifecycle module name is required")
        if module is None:
            raise ValueError("Lifecycle module instance is required")
        self._records[module_name] = _LifecycleRecord(
            module_name=module_name,
            module=module,
            policy=policy or LifecyclePolicy(),
        )
        return self.status(module_name)

    def start(
        self,
        name: str,
        request: Optional[LifecycleRequest] = None,
    ) -> LifecycleResult:
        record = self._record(name)
        lifecycle_request = _request_for(record.module_name, "start", request)
        compatibility = _validate_lifecycle_request(lifecycle_request)
        if not compatibility.success:
            return self._contract_rejected_result(record, lifecycle_request, compatibility)

        if record.state == LIFECYCLE_READY:
            return self._result(
                True,
                "already_ready",
                record,
                "Module is already READY.",
                request=lifecycle_request,
            )
        if record.state in _RECOVERY_REQUIRED_STATES:
            return self._result(
                False,
                "recovery_required",
                record,
                "Module requires explicit recovery before start.",
                error_message="recovery_required",
                request=lifecycle_request,
            )
        if record.state in {LIFECYCLE_STARTING, LIFECYCLE_BUSY, LIFECYCLE_STOPPING}:
            return self._illegal_transition_result(record, LIFECYCLE_STARTING, lifecycle_request)

        starting = self.transition(
            record.module_name,
            LIFECYCLE_STARTING,
            lifecycle_request,
            reason="start_requested",
        )
        if not starting.success:
            return starting

        try:
            external = _call_optional(record.module, "start")
        except Exception as error:
            return self._fail(record, lifecycle_request, "startup_exception", error)

        if not _external_success(external):
            reason = _external_error(external, "startup_failed")
            self._transition(record, LIFECYCLE_FAILED, lifecycle_request, reason=reason)
            return self._result(
                False,
                "startup_failed",
                record,
                "Module startup failed safely.",
                error_message=reason,
                request=lifecycle_request,
                data={"external_result": _stable_data(external)},
            )

        self._transition(record, LIFECYCLE_READY, lifecycle_request, reason="start_success")
        record.last_started_at = record.last_transition_at
        record.inactive_since = record.last_transition_at
        return self._result(
            True,
            "started",
            record,
            "Module started and is READY.",
            request=lifecycle_request,
            data={"external_result": _stable_data(external)},
        )

    def health_check(
        self,
        name: str,
        request: Optional[LifecycleRequest] = None,
    ) -> LifecycleResult:
        record = self._record(name)
        lifecycle_request = _request_for(record.module_name, "health_check", request)
        compatibility = _validate_lifecycle_request(lifecycle_request)
        if not compatibility.success:
            return self._contract_rejected_result(record, lifecycle_request, compatibility)
        if record.state != LIFECYCLE_READY:
            return self._result(
                False,
                "health_check_rejected_not_ready",
                record,
                "Module health check requires READY state.",
                error_message="module_not_ready",
                request=lifecycle_request,
            )

        try:
            external = _call_health(record.module)
        except Exception as error:
            return self._health_failure(record, lifecycle_request, f"{type(error).__name__}: {error}")

        record.last_health_check_at = self._timestamp()
        if not _external_success(external):
            return self._health_failure(
                record,
                lifecycle_request,
                _external_error(external, "health_check_failed"),
                external_result=external,
            )

        return self._result(
            True,
            "healthy",
            record,
            "Module health check passed.",
            request=lifecycle_request,
            data={
                "checked_at": record.last_health_check_at,
                "external_result": _stable_data(external),
            },
        )

    def execute(
        self,
        name: str,
        request: Optional[LifecycleRequest] = None,
        operation: Optional[LifecycleOperation] = None,
    ) -> LifecycleResult:
        record = self._record(name)
        lifecycle_request = _request_for(record.module_name, "execute", request)
        compatibility = _validate_lifecycle_request(lifecycle_request)
        if not compatibility.success:
            return self._contract_rejected_result(record, lifecycle_request, compatibility)
        if record.state != LIFECYCLE_READY:
            return self._result(
                False,
                "execution_rejected_not_ready",
                record,
                "Module execution requires READY state.",
                error_message="module_not_ready",
                request=lifecycle_request,
            )

        busy = self.transition(
            record.module_name,
            LIFECYCLE_BUSY,
            lifecycle_request,
            reason="execution_started",
        )
        if not busy.success:
            return busy

        try:
            response = operation(record.module) if operation else record.module.execute(lifecycle_request)
        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
            self._transition(record, LIFECYCLE_FAILED, lifecycle_request, reason=reason)
            return self._result(
                False,
                "execution_failed",
                record,
                "Module execution failed safely.",
                error_message=reason,
                request=lifecycle_request,
            )

        self._transition(record, LIFECYCLE_READY, lifecycle_request, reason="execution_completed")
        record.last_executed_at = record.last_transition_at
        record.inactive_since = record.last_transition_at
        return self._result(
            True,
            "executed",
            record,
            "Module execution completed.",
            request=lifecycle_request,
            data={"response": response},
        )

    def stop(
        self,
        name: str,
        request: Optional[LifecycleRequest] = None,
    ) -> LifecycleResult:
        record = self._record(name)
        lifecycle_request = _request_for(record.module_name, "stop", request)
        compatibility = _validate_lifecycle_request(lifecycle_request)
        if not compatibility.success:
            return self._contract_rejected_result(record, lifecycle_request, compatibility)
        if record.state == LIFECYCLE_STOPPED:
            return self._result(
                True,
                "already_stopped",
                record,
                "Module is already STOPPED.",
                request=lifecycle_request,
            )
        if record.state in {LIFECYCLE_STARTING, LIFECYCLE_BUSY, LIFECYCLE_STOPPING}:
            return self._illegal_transition_result(record, LIFECYCLE_STOPPING, lifecycle_request)

        stopping = self.transition(
            record.module_name,
            LIFECYCLE_STOPPING,
            lifecycle_request,
            reason="stop_requested",
        )
        if not stopping.success:
            return stopping

        try:
            external = _call_optional(record.module, "stop")
        except Exception as error:
            return self._fail(record, lifecycle_request, "stop_exception", error)

        if not _external_success(external):
            reason = _external_error(external, "stop_failed")
            self._transition(record, LIFECYCLE_FAILED, lifecycle_request, reason=reason)
            return self._result(
                False,
                "stop_failed",
                record,
                "Module stop failed safely.",
                error_message=reason,
                request=lifecycle_request,
                data={"external_result": _stable_data(external)},
            )

        self._transition(record, LIFECYCLE_STOPPED, lifecycle_request, reason="stop_success")
        return self._result(
            True,
            "stopped",
            record,
            "Module stopped.",
            request=lifecycle_request,
            data={"external_result": _stable_data(external)},
        )

    def recover(
        self,
        name: str,
        request: Optional[LifecycleRequest] = None,
    ) -> LifecycleResult:
        record = self._record(name)
        lifecycle_request = _request_for(record.module_name, "recover", request)
        compatibility = _validate_lifecycle_request(lifecycle_request)
        if not compatibility.success:
            return self._contract_rejected_result(record, lifecycle_request, compatibility)
        if record.state not in _RECOVERY_REQUIRED_STATES:
            return self._result(
                True,
                "recovery_not_required",
                record,
                "Module does not require recovery.",
                request=lifecycle_request,
            )

        stopped = self.stop(record.module_name, lifecycle_request)
        if not stopped.success:
            return stopped
        started = self.start(record.module_name, lifecycle_request)
        if not started.success:
            return started
        health = self.health_check(record.module_name, lifecycle_request)
        if not health.success:
            return health
        return self._result(
            True,
            "recovered",
            record,
            "Module recovered and is READY.",
            request=lifecycle_request,
        )

    def transition(
        self,
        name: str,
        to_state: str,
        request: Optional[LifecycleRequest] = None,
        reason: str = "",
    ) -> LifecycleResult:
        record = self._record(name)
        target_state = _normalize_state(to_state)
        lifecycle_request = _request_for(record.module_name, "transition", request)
        compatibility = _validate_lifecycle_request(lifecycle_request)
        if not compatibility.success:
            return self._contract_rejected_result(record, lifecycle_request, compatibility)
        if target_state not in _ALLOWED_TRANSITIONS[record.state]:
            return self._illegal_transition_result(record, target_state, lifecycle_request)

        self._transition(record, target_state, lifecycle_request, reason=reason)
        return self._result(
            True,
            "transitioned",
            record,
            f"Module transitioned to {target_state}.",
            request=lifecycle_request,
        )

    def status(self, name: str) -> LifecycleStatus:
        record = self._record(name)
        return LifecycleStatus(
            module_name=record.module_name,
            state=record.state,
            healthy=record.state == LIFECYCLE_READY,
            reason=record.reason,
            last_transition_at=record.last_transition_at,
            last_started_at=record.last_started_at,
            last_health_check_at=record.last_health_check_at,
            last_executed_at=record.last_executed_at,
            inactive_since=record.inactive_since,
            transition_count=len(record.transitions),
            policy=record.policy.to_dict(),
            metadata={
                "source": "module_lifecycle_manager",
                "module_type": type(record.module).__name__,
            },
        )

    def statuses(self) -> Dict[str, Dict[str, Any]]:
        return {
            name: self.status(name).to_dict()
            for name in self._records
        }

    def history(self, name: str) -> List[LifecycleTransition]:
        return list(self._record(name).transitions)

    def _record(self, name: str) -> _LifecycleRecord:
        module_name = _normalize_module_name(name)
        if module_name not in self._records:
            raise KeyError(f"Unknown lifecycle module: {name}")
        return self._records[module_name]

    def _transition(
        self,
        record: _LifecycleRecord,
        to_state: str,
        request: LifecycleRequest,
        reason: str = "",
    ) -> LifecycleTransition:
        from_state = record.state
        timestamp = self._timestamp()
        transition = LifecycleTransition(
            module_name=record.module_name,
            from_state=from_state,
            to_state=to_state,
            operation=request.operation,
            timestamp=timestamp,
            reason=reason,
            session_id=request.session_id,
            correlation_id=request.correlation_id,
        )
        record.state = to_state
        record.reason = reason if to_state in {LIFECYCLE_DEGRADED, LIFECYCLE_FAILED} else ""
        record.last_transition_at = timestamp
        record.transitions.append(transition)
        return transition

    def _timestamp(self) -> str:
        now = datetime.now(timezone.utc)
        if self._last_timestamp is not None and now <= self._last_timestamp:
            now = self._last_timestamp + timedelta(microseconds=1)
        self._last_timestamp = now
        return now.isoformat().replace("+00:00", "Z")

    def _health_failure(
        self,
        record: _LifecycleRecord,
        request: LifecycleRequest,
        reason: str,
        external_result: Any = None,
    ) -> LifecycleResult:
        target_state = record.policy.health_failure_state
        self._transition(record, target_state, request, reason=reason)
        return self._result(
            False,
            "health_check_failed",
            record,
            f"Module health check failed and state is {target_state}.",
            error_message=reason,
            request=request,
            data={
                "external_result": _stable_data(external_result),
                "health_failure_state": target_state,
            },
        )

    def _fail(
        self,
        record: _LifecycleRecord,
        request: LifecycleRequest,
        status: str,
        error: Exception,
    ) -> LifecycleResult:
        reason = f"{type(error).__name__}: {error}"
        self._transition(record, LIFECYCLE_FAILED, request, reason=reason)
        return self._result(
            False,
            status,
            record,
            "Module lifecycle operation failed safely.",
            error_message=reason,
            request=request,
        )

    def _illegal_transition_result(
        self,
        record: _LifecycleRecord,
        to_state: str,
        request: LifecycleRequest,
    ) -> LifecycleResult:
        return self._result(
            False,
            "illegal_transition",
            record,
            f"Illegal lifecycle transition {record.state} -> {to_state}.",
            error_message=f"illegal_transition:{record.state}->{to_state}",
            request=request,
            data={"from_state": record.state, "to_state": to_state},
        )

    def _contract_rejected_result(
        self,
        record: _LifecycleRecord,
        request: LifecycleRequest,
        compatibility: ContractCompatibilityResult,
    ) -> LifecycleResult:
        return self._result(
            False,
            "contract_rejected",
            record,
            "Lifecycle request rejected by compatibility gate.",
            error_message=compatibility.error_message or compatibility.status,
            request=request,
            data={"compatibility": compatibility.to_dict()},
        )

    def _result(
        self,
        success: bool,
        status: str,
        record: _LifecycleRecord,
        text: str,
        error_message: str = "",
        data: Optional[Dict[str, Any]] = None,
        request: Optional[LifecycleRequest] = None,
    ) -> LifecycleResult:
        return LifecycleResult(
            success=success,
            status=status,
            state=record.state,
            text=text,
            error_message=error_message,
            data=dict(data or {}),
            request=request,
            correlation_id=request.correlation_id if request else "",
            session_id=request.session_id if request else "",
            metadata={
                "safe": True,
                "source": "module_lifecycle_manager",
                "module_name": record.module_name,
            },
        )


def _request_for(
    module_name: str,
    operation: str,
    request: Optional[LifecycleRequest],
) -> LifecycleRequest:
    if request is not None:
        return LifecycleRequest(
            module_name=module_name,
            operation=operation,
            payload=request.payload,
            session_id=request.session_id,
            correlation_id=request.correlation_id,
            metadata=request.metadata,
            contract_name=request.contract_name,
            contract_version=request.contract_version,
            created_at=request.created_at,
        )
    return LifecycleRequest(module_name=module_name, operation=operation)


def _validate_lifecycle_request(request: LifecycleRequest) -> ContractCompatibilityResult:
    return validate_contract(
        request,
        expected_contract_name=CONTRACT_LIFECYCLE_EXECUTION_REQUEST,
    )


def _call_optional(module: Any, method_name: str) -> Any:
    method = getattr(module, method_name, None)
    if callable(method):
        return method()
    return {"success": True, "status": f"{method_name}_not_required"}


def _call_health(module: Any) -> Any:
    health_check = getattr(module, "health_check", None)
    if callable(health_check):
        return health_check()
    get_status = getattr(module, "get_status", None)
    if callable(get_status):
        return get_status()
    status = getattr(module, "status", None)
    if callable(status):
        return status()
    return {"success": True, "status": "health_check_not_required"}


def _external_success(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        return bool(result.get("success", True))
    if hasattr(result, "success"):
        return bool(getattr(result, "success"))
    return True


def _external_error(result: Any, fallback: str) -> str:
    if result is None:
        return fallback
    if isinstance(result, dict):
        return str(result.get("error_message") or result.get("status") or fallback)
    return str(getattr(result, "error_message", "") or getattr(result, "status", "") or fallback)


def _normalize_module_name(name: str) -> str:
    return "_".join(part for part in str(name or "").strip().lower().split() if part)


def _normalize_state(state: str) -> str:
    clean = str(state or "").strip().upper()
    if clean not in LIFECYCLE_STATES:
        raise ValueError(f"Invalid lifecycle state: {state}")
    return clean


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
