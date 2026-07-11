from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence


HEALTH_STATUS_HEALTHY = "healthy"
HEALTH_STATUS_DEGRADED = "degraded"
HEALTH_STATUS_UNAVAILABLE = "unavailable"
HEALTH_STATUS_FAILED = "failed"
HEALTH_STATUS_DISABLED = "disabled"
HEALTH_STATUS_UNKNOWN = "unknown"
HEALTH_STATUSES = {
    HEALTH_STATUS_HEALTHY,
    HEALTH_STATUS_DEGRADED,
    HEALTH_STATUS_UNAVAILABLE,
    HEALTH_STATUS_FAILED,
    HEALTH_STATUS_DISABLED,
    HEALTH_STATUS_UNKNOWN,
}

RETRY_SAFE = "retry_safe"
RETRY_UNSAFE = "retry_unsafe"
RETRY_UNKNOWN = "unknown"
RETRY_SAFETY_VALUES = {RETRY_SAFE, RETRY_UNSAFE, RETRY_UNKNOWN}

CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN = "open"
CIRCUIT_HALF_OPEN = "half_open"
CIRCUIT_STATES = {CIRCUIT_CLOSED, CIRCUIT_OPEN, CIRCUIT_HALF_OPEN}

HEALTH_ERROR_NO_HEALTHY_ADAPTER = "no_healthy_adapter"
HEALTH_ERROR_INCOMPATIBLE_INTERFACE_VERSION = "incompatible_interface_version"
HEALTH_ERROR_MISSING_CAPABILITY = "missing_capability"
HEALTH_ERROR_DISABLED_ADAPTER = "disabled_adapter"
HEALTH_ERROR_TIMEOUT = "health_check_timeout"
HEALTH_ERROR_EXCEPTION = "health_check_exception"
HEALTH_ERROR_CIRCUIT_OPEN = "circuit_open"
HEALTH_ERROR_FALLBACK_EXHAUSTED = "fallback_exhausted"
HEALTH_ERROR_RETRY_UNSAFE = "retry_unsafe_operation"
HEALTH_ERROR_MALFORMED_RESULT = "malformed_health_result"

HEALTH_EVENT_CHECK_FAILED = "health.check_failed"
HEALTH_EVENT_FALLBACK_SELECTED = "health.fallback_selected"
HEALTH_EVENT_ALL_CANDIDATES_UNAVAILABLE = "health.all_candidates_unavailable"
HEALTH_EVENT_CIRCUIT_OPENED = "health.circuit_opened"
HEALTH_EVENT_CIRCUIT_HALF_OPEN_PROBE = "health.circuit_half_open_probe"
HEALTH_EVENT_CIRCUIT_RECOVERED = "health.circuit_recovered"

DEFAULT_HEALTH_TIMEOUT_SECONDS = 1.0
DEFAULT_HEALTH_CACHE_TTL_SECONDS = 0.0
DEFAULT_MAX_FALLBACK_ATTEMPTS = 2
DEFAULT_CIRCUIT_FAILURE_THRESHOLD = 2
DEFAULT_CIRCUIT_COOLDOWN_SECONDS = 30.0

Clock = Callable[[], float]
HealthOperation = Callable[[Any], Any]


def utc_health_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class HealthResult:
    component_name: str
    component_type: str
    status: str
    healthy: bool
    available: bool
    degraded: bool
    checked_at: str = field(default_factory=utc_health_timestamp)
    latency_ms: Optional[float] = None
    error_code: str = ""
    message: str = ""
    capabilities: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        status = _normalize_health_status(self.status)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "component_name", _normalize_name(self.component_name))
        object.__setattr__(self, "component_type", _normalize_name(self.component_type) or "component")
        object.__setattr__(self, "healthy", bool(self.healthy))
        object.__setattr__(self, "available", bool(self.available))
        object.__setattr__(self, "degraded", bool(self.degraded))
        object.__setattr__(self, "checked_at", str(self.checked_at or utc_health_timestamp()))
        if self.latency_ms is not None:
            object.__setattr__(self, "latency_ms", round(max(0.0, float(self.latency_ms)), 3))
        object.__setattr__(self, "error_code", str(self.error_code or "").strip())
        object.__setattr__(self, "message", str(self.message or "").strip())
        object.__setattr__(self, "capabilities", _normalize_capabilities(self.capabilities))
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_name": self.component_name,
            "component_type": self.component_type,
            "status": self.status,
            "healthy": self.healthy,
            "available": self.available,
            "degraded": self.degraded,
            "checked_at": self.checked_at,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "message": self.message,
            "capabilities": list(self.capabilities),
            "metadata": _stable_data(self.metadata),
        }


class HealthCheckable:
    """Common health-check boundary for services, cities, and adapters."""

    def health_check(self) -> HealthResult:
        raise NotImplementedError


@dataclass(frozen=True)
class HealthPolicyConfig:
    adapter_priority: List[str] = field(default_factory=list)
    allow_degraded: bool = False
    health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS
    health_cache_ttl_seconds: float = DEFAULT_HEALTH_CACHE_TTL_SECONDS
    max_fallback_attempts: int = DEFAULT_MAX_FALLBACK_ATTEMPTS
    circuit_failure_threshold: int = DEFAULT_CIRCUIT_FAILURE_THRESHOLD
    circuit_cooldown_seconds: float = DEFAULT_CIRCUIT_COOLDOWN_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_priority", _normalize_names(self.adapter_priority))
        object.__setattr__(self, "allow_degraded", bool(self.allow_degraded))
        object.__setattr__(
            self,
            "health_timeout_seconds",
            _bounded_float(self.health_timeout_seconds, 0.001, 60.0, "health_timeout_seconds"),
        )
        object.__setattr__(
            self,
            "health_cache_ttl_seconds",
            _bounded_float(self.health_cache_ttl_seconds, 0.0, 3600.0, "health_cache_ttl_seconds"),
        )
        object.__setattr__(
            self,
            "max_fallback_attempts",
            _bounded_int(self.max_fallback_attempts, 1, 10, "max_fallback_attempts"),
        )
        object.__setattr__(
            self,
            "circuit_failure_threshold",
            _bounded_int(self.circuit_failure_threshold, 1, 100, "circuit_failure_threshold"),
        )
        object.__setattr__(
            self,
            "circuit_cooldown_seconds",
            _bounded_float(self.circuit_cooldown_seconds, 0.0, 3600.0, "circuit_cooldown_seconds"),
        )

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]]):
        data = dict(payload or {})
        return cls(
            adapter_priority=list(data.get("adapter_priority") or []),
            allow_degraded=bool(data.get("allow_degraded", False)),
            health_timeout_seconds=float(
                data.get("health_timeout_seconds", DEFAULT_HEALTH_TIMEOUT_SECONDS)
            ),
            health_cache_ttl_seconds=float(
                data.get("health_cache_ttl_seconds", DEFAULT_HEALTH_CACHE_TTL_SECONDS)
            ),
            max_fallback_attempts=int(
                data.get("max_fallback_attempts", DEFAULT_MAX_FALLBACK_ATTEMPTS)
            ),
            circuit_failure_threshold=int(
                data.get("circuit_failure_threshold", DEFAULT_CIRCUIT_FAILURE_THRESHOLD)
            ),
            circuit_cooldown_seconds=float(
                data.get("circuit_cooldown_seconds", DEFAULT_CIRCUIT_COOLDOWN_SECONDS)
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter_priority": list(self.adapter_priority),
            "allow_degraded": self.allow_degraded,
            "health_timeout_seconds": self.health_timeout_seconds,
            "health_cache_ttl_seconds": self.health_cache_ttl_seconds,
            "max_fallback_attempts": self.max_fallback_attempts,
            "circuit_failure_threshold": self.circuit_failure_threshold,
            "circuit_cooldown_seconds": self.circuit_cooldown_seconds,
        }


@dataclass(frozen=True)
class AdapterCandidate:
    name: str
    adapter: Any
    component_type: str = "adapter"
    capabilities: List[str] = field(default_factory=list)
    interface_version: str = "v1"
    enabled: bool = True
    manifest: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clean_name = _normalize_name(self.name) or _normalize_name(
            getattr(self.adapter, "name", "") or getattr(self.adapter, "source", "")
        )
        if not clean_name:
            raise ValueError("Adapter candidate name is required")
        object.__setattr__(self, "name", clean_name)
        if self.adapter is None:
            raise ValueError("Adapter candidate instance is required")
        object.__setattr__(self, "component_type", _normalize_name(self.component_type) or "adapter")
        capabilities = self.capabilities or _candidate_capabilities(self.adapter, self.manifest)
        object.__setattr__(self, "capabilities", _normalize_capabilities(capabilities))
        object.__setattr__(self, "interface_version", str(self.interface_version or "").strip())
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata))

    def supports(self, capability: str) -> bool:
        return _normalize_capability(capability) in set(self.capabilities)

    def cache_key(self) -> str:
        return f"{self.name}:{self.interface_version}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "component_type": self.component_type,
            "capabilities": list(self.capabilities),
            "interface_version": self.interface_version,
            "enabled": self.enabled,
            "metadata": _stable_data(self.metadata),
        }


@dataclass(frozen=True)
class AdapterRejection:
    adapter_name: str
    reason: str
    message: str = ""
    health: Optional[HealthResult] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "reason": self.reason,
            "message": self.message,
            "health": self.health.to_dict() if self.health else None,
            "metadata": _stable_data(_safe_metadata(self.metadata)),
        }


@dataclass(frozen=True)
class AdapterSelectionResult:
    success: bool
    capability: str
    selected_adapter_name: str = ""
    selected_adapter: Any = None
    selected_health: Optional[HealthResult] = None
    rejections: List[AdapterRejection] = field(default_factory=list)
    attempts: int = 0
    status: str = ""
    error_code: str = ""
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "capability": self.capability,
            "selected_adapter_name": self.selected_adapter_name,
            "selected_health": self.selected_health.to_dict() if self.selected_health else None,
            "rejections": [rejection.to_dict() for rejection in self.rejections],
            "attempts": self.attempts,
            "status": self.status,
            "error_code": self.error_code,
            "message": self.message,
            "metadata": _stable_data(_safe_metadata(self.metadata)),
        }


@dataclass(frozen=True)
class FallbackExecutionResult:
    success: bool
    status: str
    capability: str
    selected_adapter_name: str = ""
    attempts: int = 0
    data: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    original_error: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "capability": self.capability,
            "selected_adapter_name": self.selected_adapter_name,
            "attempts": self.attempts,
            "data": _stable_data(self.data),
            "error_message": self.error_message,
            "original_error": self.original_error,
            "history": [_stable_data(item) for item in self.history],
            "metadata": _stable_data(_safe_metadata(self.metadata)),
        }


@dataclass
class _CircuitRecord:
    state: str = CIRCUIT_CLOSED
    failure_count: int = 0
    opened_at: Optional[float] = None
    last_error: str = ""


class CircuitBreaker:
    """Small local circuit breaker with deterministic clock injection."""

    def __init__(
        self,
        failure_threshold: int = DEFAULT_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_CIRCUIT_COOLDOWN_SECONDS,
        clock: Optional[Clock] = None,
    ):
        self.failure_threshold = _bounded_int(
            failure_threshold,
            1,
            100,
            "failure_threshold",
        )
        self.cooldown_seconds = _bounded_float(
            cooldown_seconds,
            0.0,
            3600.0,
            "cooldown_seconds",
        )
        self.clock = clock or _monotonic_clock
        self._records: Dict[str, _CircuitRecord] = {}

    def state(self, name: str) -> str:
        record = self._record(name)
        if record.state == CIRCUIT_OPEN and self._cooldown_elapsed(record):
            record.state = CIRCUIT_HALF_OPEN
        return record.state

    def allow_request(self, name: str) -> bool:
        return self.state(name) != CIRCUIT_OPEN

    def record_success(self, name: str) -> str:
        record = self._record(name)
        previous = self.state(name)
        record.state = CIRCUIT_CLOSED
        record.failure_count = 0
        record.opened_at = None
        record.last_error = ""
        return previous

    def record_failure(self, name: str, reason: str = "") -> str:
        record = self._record(name)
        current = self.state(name)
        record.failure_count += 1
        record.last_error = str(reason or "").strip()
        if current == CIRCUIT_HALF_OPEN or record.failure_count >= self.failure_threshold:
            record.state = CIRCUIT_OPEN
            record.opened_at = self.clock()
        return record.state

    def status(self, name: str) -> Dict[str, Any]:
        record = self._record(name)
        state = self.state(name)
        return {
            "adapter_name": _normalize_name(name),
            "state": state,
            "failure_count": record.failure_count,
            "opened_at": record.opened_at,
            "last_error": record.last_error,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
        }

    def _record(self, name: str) -> _CircuitRecord:
        clean_name = _normalize_name(name)
        if clean_name not in self._records:
            self._records[clean_name] = _CircuitRecord()
        return self._records[clean_name]

    def _cooldown_elapsed(self, record: _CircuitRecord) -> bool:
        if record.opened_at is None:
            return True
        return (self.clock() - record.opened_at) >= self.cooldown_seconds


class HealthCache:
    """Short-lived in-memory health result cache."""

    def __init__(self, ttl_seconds: float = DEFAULT_HEALTH_CACHE_TTL_SECONDS, clock: Optional[Clock] = None):
        self.ttl_seconds = _bounded_float(ttl_seconds, 0.0, 3600.0, "ttl_seconds")
        self.clock = clock or _monotonic_clock
        self._entries: Dict[str, tuple[HealthResult, float]] = {}

    def get(self, key: str) -> Optional[HealthResult]:
        if self.ttl_seconds <= 0:
            return None
        entry = self._entries.get(str(key or "").strip())
        if entry is None:
            return None
        result, checked_at = entry
        if (self.clock() - checked_at) > self.ttl_seconds:
            self._entries.pop(str(key or "").strip(), None)
            return None
        return result

    def set(self, key: str, result: HealthResult) -> None:
        if self.ttl_seconds <= 0:
            return
        self._entries[str(key or "").strip()] = (result, self.clock())

    def invalidate(self, key: str) -> None:
        self._entries.pop(str(key or "").strip(), None)

    def clear(self) -> None:
        self._entries.clear()


class AdapterFallbackPolicy:
    """Centralized adapter health selection and bounded fallback execution."""

    def __init__(
        self,
        config: Optional[HealthPolicyConfig] = None,
        cache: Optional[HealthCache] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        event_history_store: Any = None,
        clock: Optional[Clock] = None,
    ):
        self.config = config or HealthPolicyConfig()
        self.clock = clock or _monotonic_clock
        self.cache = cache or HealthCache(
            ttl_seconds=self.config.health_cache_ttl_seconds,
            clock=self.clock,
        )
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=self.config.circuit_failure_threshold,
            cooldown_seconds=self.config.circuit_cooldown_seconds,
            clock=self.clock,
        )
        self.event_history_store = event_history_store

    def select(
        self,
        candidates: Sequence[AdapterCandidate | Any],
        capability: str,
        required_interface_version: str = "v1",
        allow_degraded: Optional[bool] = None,
        force_refresh: bool = False,
    ) -> AdapterSelectionResult:
        clean_capability = _normalize_capability(capability)
        candidates_list = self._ordered_candidates(candidates)
        rejections: List[AdapterRejection] = []
        degraded_allowed = self.config.allow_degraded if allow_degraded is None else bool(allow_degraded)

        for candidate in candidates_list:
            if not candidate.enabled:
                self.cache.invalidate(candidate.cache_key())
                rejections.append(
                    AdapterRejection(
                        adapter_name=candidate.name,
                        reason=HEALTH_ERROR_DISABLED_ADAPTER,
                        message="Adapter is disabled by policy.",
                    )
                )
                continue

            if clean_capability and not candidate.supports(clean_capability):
                rejections.append(
                    AdapterRejection(
                        adapter_name=candidate.name,
                        reason=HEALTH_ERROR_MISSING_CAPABILITY,
                        message=f"Adapter does not provide capability: {clean_capability}",
                        metadata={"required_capability": clean_capability},
                    )
                )
                continue

            if required_interface_version and candidate.interface_version != required_interface_version:
                rejections.append(
                    AdapterRejection(
                        adapter_name=candidate.name,
                        reason=HEALTH_ERROR_INCOMPATIBLE_INTERFACE_VERSION,
                        message="Adapter interface version is incompatible.",
                        metadata={
                            "required_interface_version": required_interface_version,
                            "candidate_interface_version": candidate.interface_version,
                        },
                    )
                )
                continue

            circuit_state = self.circuit_breaker.state(candidate.name)
            if circuit_state == CIRCUIT_OPEN:
                rejections.append(
                    AdapterRejection(
                        adapter_name=candidate.name,
                        reason=HEALTH_ERROR_CIRCUIT_OPEN,
                        message="Adapter circuit is open.",
                        metadata=self.circuit_breaker.status(candidate.name),
                    )
                )
                continue
            if circuit_state == CIRCUIT_HALF_OPEN:
                self._record_event(
                    HEALTH_EVENT_CIRCUIT_HALF_OPEN_PROBE,
                    {
                        "adapter_name": candidate.name,
                        "capability": clean_capability,
                        "circuit_state": CIRCUIT_HALF_OPEN,
                    },
                )

            health = self._candidate_health(candidate, force_refresh=force_refresh)
            if health.status in {HEALTH_STATUS_FAILED, HEALTH_STATUS_UNAVAILABLE, HEALTH_STATUS_UNKNOWN}:
                self._record_candidate_failure(candidate, health.error_code or health.status)
                rejections.append(
                    AdapterRejection(
                        adapter_name=candidate.name,
                        reason=health.error_code or health.status,
                        message=health.message or "Adapter health check failed.",
                        health=health,
                    )
                )
                continue

            if health.status == HEALTH_STATUS_DISABLED:
                self.cache.invalidate(candidate.cache_key())
                rejections.append(
                    AdapterRejection(
                        adapter_name=candidate.name,
                        reason=HEALTH_ERROR_DISABLED_ADAPTER,
                        message=health.message or "Adapter is disabled.",
                        health=health,
                    )
                )
                continue

            if health.degraded and not degraded_allowed:
                rejections.append(
                    AdapterRejection(
                        adapter_name=candidate.name,
                        reason=HEALTH_STATUS_DEGRADED,
                        message="Adapter is degraded and policy is strict.",
                        health=health,
                    )
                )
                continue

            previous_circuit_state = self.circuit_breaker.record_success(candidate.name)
            if previous_circuit_state == CIRCUIT_HALF_OPEN:
                self._record_event(
                    HEALTH_EVENT_CIRCUIT_RECOVERED,
                    {
                        "adapter_name": candidate.name,
                        "capability": clean_capability,
                        "circuit_state": CIRCUIT_CLOSED,
                    },
                )
            self._record_event(
                HEALTH_EVENT_FALLBACK_SELECTED,
                {
                    "adapter_name": candidate.name,
                    "capability": clean_capability,
                    "health_status": health.status,
                    "degraded": health.degraded,
                    "rejection_count": len(rejections),
                },
            )
            return AdapterSelectionResult(
                success=True,
                capability=clean_capability,
                selected_adapter_name=candidate.name,
                selected_adapter=candidate.adapter,
                selected_health=health,
                rejections=rejections,
                attempts=len(rejections) + 1,
                status="selected",
                message=f"Adapter selected for capability: {clean_capability}",
                metadata={"source": "adapter_fallback_policy"},
            )

        self._record_event(
            HEALTH_EVENT_ALL_CANDIDATES_UNAVAILABLE,
            {
                "capability": clean_capability,
                "candidate_count": len(candidates_list),
                "rejections": [rejection.to_dict() for rejection in rejections],
            },
        )
        return AdapterSelectionResult(
            success=False,
            capability=clean_capability,
            rejections=rejections,
            attempts=len(rejections),
            status="selection_failed",
            error_code=HEALTH_ERROR_NO_HEALTHY_ADAPTER,
            message=f"No healthy adapter is available for capability: {clean_capability}",
            metadata={"source": "adapter_fallback_policy"},
        )

    def execute(
        self,
        candidates: Sequence[AdapterCandidate | Any],
        capability: str,
        operation: HealthOperation,
        retry_safety: str = RETRY_UNKNOWN,
        required_interface_version: str = "v1",
        allow_degraded: Optional[bool] = None,
        force_refresh: bool = False,
    ) -> FallbackExecutionResult:
        clean_retry_safety = _normalize_retry_safety(retry_safety)
        clean_capability = _normalize_capability(capability)
        remaining = self._ordered_candidates(candidates)
        attempted_names: set[str] = set()
        history: List[Dict[str, Any]] = []
        original_error = ""

        while remaining and len(attempted_names) < self.config.max_fallback_attempts:
            selection = self.select(
                [candidate for candidate in remaining if candidate.name not in attempted_names],
                clean_capability,
                required_interface_version=required_interface_version,
                allow_degraded=allow_degraded,
                force_refresh=force_refresh,
            )
            history.append({"selection": selection.to_dict()})
            if not selection.success:
                return FallbackExecutionResult(
                    success=False,
                    status=selection.error_code or HEALTH_ERROR_NO_HEALTHY_ADAPTER,
                    capability=clean_capability,
                    attempts=len(attempted_names),
                    error_message=selection.message,
                    original_error=original_error,
                    history=history,
                    metadata={"source": "adapter_fallback_policy"},
                )

            adapter_name = selection.selected_adapter_name
            attempted_names.add(adapter_name)
            try:
                response = operation(selection.selected_adapter)
            except Exception as error:
                response = _operation_exception_result(error)

            response_data = _response_to_dict(response)
            success = _response_success(response)
            history[-1]["operation"] = response_data

            if success:
                self.circuit_breaker.record_success(adapter_name)
                return FallbackExecutionResult(
                    success=True,
                    status="executed",
                    capability=clean_capability,
                    selected_adapter_name=adapter_name,
                    attempts=len(attempted_names),
                    data=response_data,
                    original_error=original_error,
                    history=history,
                    metadata={"source": "adapter_fallback_policy"},
                )

            error_message = _response_error(response, "operation_failed")
            if not original_error:
                original_error = error_message
            new_state = self.circuit_breaker.record_failure(adapter_name, error_message)
            if new_state == CIRCUIT_OPEN:
                self._record_event(
                    HEALTH_EVENT_CIRCUIT_OPENED,
                    {
                        "adapter_name": adapter_name,
                        "capability": clean_capability,
                        "error_code": error_message,
                        "failure_count": self.circuit_breaker.status(adapter_name)["failure_count"],
                    },
                )

            if clean_retry_safety != RETRY_SAFE:
                return FallbackExecutionResult(
                    success=False,
                    status=HEALTH_ERROR_RETRY_UNSAFE,
                    capability=clean_capability,
                    selected_adapter_name=adapter_name,
                    attempts=len(attempted_names),
                    data=response_data,
                    error_message=HEALTH_ERROR_RETRY_UNSAFE,
                    original_error=original_error,
                    history=history,
                    metadata={"source": "adapter_fallback_policy"},
                )

        return FallbackExecutionResult(
            success=False,
            status=HEALTH_ERROR_FALLBACK_EXHAUSTED,
            capability=clean_capability,
            attempts=len(attempted_names),
            error_message=HEALTH_ERROR_FALLBACK_EXHAUSTED,
            original_error=original_error,
            history=history,
            metadata={"source": "adapter_fallback_policy"},
        )

    def _ordered_candidates(self, candidates: Sequence[AdapterCandidate | Any]) -> List[AdapterCandidate]:
        normalized = [
            candidate if isinstance(candidate, AdapterCandidate) else AdapterCandidate(
                name=getattr(candidate, "name", "") or getattr(candidate, "source", ""),
                adapter=candidate,
                capabilities=_candidate_capabilities(candidate, None),
                interface_version=str(getattr(candidate, "contract_version", "") or "v1"),
            )
            for candidate in candidates
        ]
        priority = {name: index for index, name in enumerate(self.config.adapter_priority)}
        return sorted(
            normalized,
            key=lambda candidate: (
                priority.get(candidate.name, len(priority)),
                candidate.name,
                candidate.interface_version,
            ),
        )

    def _candidate_health(
        self,
        candidate: AdapterCandidate,
        force_refresh: bool = False,
    ) -> HealthResult:
        cache_key = candidate.cache_key()
        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return HealthResult(
                    component_name=cached.component_name,
                    component_type=cached.component_type,
                    status=cached.status,
                    healthy=cached.healthy,
                    available=cached.available,
                    degraded=cached.degraded,
                    checked_at=cached.checked_at,
                    latency_ms=cached.latency_ms,
                    error_code=cached.error_code,
                    message=cached.message,
                    capabilities=cached.capabilities,
                    metadata={**cached.metadata, "cache_hit": True},
                )

        result = check_component_health(
            candidate.adapter,
            component_name=candidate.name,
            component_type=candidate.component_type,
            capabilities=candidate.capabilities,
            timeout_seconds=self.config.health_timeout_seconds,
            clock=self.clock,
        )
        self.cache.set(cache_key, result)
        return result

    def _record_candidate_failure(self, candidate: AdapterCandidate, reason: str) -> None:
        state = self.circuit_breaker.record_failure(candidate.name, reason)
        self._record_event(
            HEALTH_EVENT_CHECK_FAILED,
            {
                "adapter_name": candidate.name,
                "status": reason,
                "circuit_state": state,
            },
        )
        if state == CIRCUIT_OPEN:
            self._record_event(
                HEALTH_EVENT_CIRCUIT_OPENED,
                {
                    "adapter_name": candidate.name,
                    "error_code": reason,
                    "failure_count": self.circuit_breaker.status(candidate.name)["failure_count"],
                },
            )

    def _record_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.event_history_store is None:
            return
        safe_payload = _safe_metadata(payload)
        event = {
            "source": "health",
            "type": event_type,
            "priority": "normal",
            "payload": safe_payload,
            "timestamp": utc_health_timestamp(),
        }
        result = {
            "success": event_type != HEALTH_EVENT_ALL_CANDIDATES_UNAVAILABLE,
            "decision": "recorded",
            "text": "Health/fallback event recorded.",
            "data": {
                "source": "adapter_fallback_policy",
                "event_type": event_type,
                "payload": safe_payload,
            },
            "error_message": "",
            "metadata": {"safe": True, "source": "adapter_fallback_policy"},
        }
        self.event_history_store.add(event, result)


def check_component_health(
    component: Any,
    component_name: str = "",
    component_type: str = "component",
    capabilities: Optional[Iterable[str]] = None,
    timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
    clock: Optional[Clock] = None,
) -> HealthResult:
    clean_name = _normalize_name(component_name) or _normalize_name(
        getattr(component, "name", "") or getattr(component, "source", "")
    )
    clean_type = _normalize_name(component_type) or "component"
    clean_capabilities = _normalize_capabilities(capabilities or _candidate_capabilities(component, None))
    active_clock = clock or _monotonic_clock
    started_at = active_clock()
    try:
        raw_result = _call_health(component)
    except TimeoutError as error:
        return _health_failure(
            clean_name,
            clean_type,
            clean_capabilities,
            HEALTH_ERROR_TIMEOUT,
            f"Health check timed out: {error}",
            _elapsed_ms(active_clock, started_at),
        )
    except Exception as error:
        return _health_failure(
            clean_name,
            clean_type,
            clean_capabilities,
            HEALTH_ERROR_EXCEPTION,
            f"{type(error).__name__}: {error}",
            _elapsed_ms(active_clock, started_at),
        )

    latency_ms = _elapsed_ms(active_clock, started_at)
    if latency_ms > (float(timeout_seconds) * 1000.0):
        return _health_failure(
            clean_name,
            clean_type,
            clean_capabilities,
            HEALTH_ERROR_TIMEOUT,
            "Health check exceeded configured timeout.",
            latency_ms,
        )
    return normalize_health_result(
        raw_result,
        component_name=clean_name,
        component_type=clean_type,
        capabilities=clean_capabilities,
        latency_ms=latency_ms,
    )


def normalize_health_result(
    raw_result: Any,
    component_name: str,
    component_type: str = "component",
    capabilities: Optional[Iterable[str]] = None,
    latency_ms: Optional[float] = None,
) -> HealthResult:
    clean_name = _normalize_name(component_name)
    clean_type = _normalize_name(component_type) or "component"
    clean_capabilities = _normalize_capabilities(capabilities or [])

    if isinstance(raw_result, HealthResult):
        return HealthResult(
            component_name=raw_result.component_name or clean_name,
            component_type=raw_result.component_type or clean_type,
            status=raw_result.status,
            healthy=raw_result.healthy,
            available=raw_result.available,
            degraded=raw_result.degraded,
            checked_at=raw_result.checked_at,
            latency_ms=latency_ms if latency_ms is not None else raw_result.latency_ms,
            error_code=raw_result.error_code,
            message=raw_result.message,
            capabilities=raw_result.capabilities or clean_capabilities,
            metadata=raw_result.metadata,
        )

    if raw_result is None or isinstance(raw_result, (str, int, float, bool)):
        return _health_failure(
            clean_name,
            clean_type,
            clean_capabilities,
            HEALTH_ERROR_MALFORMED_RESULT,
            "Health check returned malformed result.",
            latency_ms,
        )

    result_data = _result_to_mapping(raw_result)
    if result_data is None:
        return _health_failure(
            clean_name,
            clean_type,
            clean_capabilities,
            HEALTH_ERROR_MALFORMED_RESULT,
            "Health check returned malformed result.",
            latency_ms,
        )

    data = dict(result_data.get("data") or {})
    metadata = dict(result_data.get("metadata") or {})
    status_hint = str(result_data.get("status") or data.get("status") or "").strip().lower()
    success = bool(result_data.get("success", True))
    available_hint = data.get("available")
    if available_hint is None:
        available_hint = metadata.get("available")

    status = _status_from_raw(success, status_hint, available_hint)
    healthy = status == HEALTH_STATUS_HEALTHY
    degraded = status == HEALTH_STATUS_DEGRADED
    available = status in {HEALTH_STATUS_HEALTHY, HEALTH_STATUS_DEGRADED}
    if status == HEALTH_STATUS_DISABLED:
        available = False
    if status == HEALTH_STATUS_UNKNOWN:
        available = False

    error_code = str(
        result_data.get("error_message")
        or data.get("error_code")
        or metadata.get("error_code")
        or ""
    ).strip()
    message = str(
        result_data.get("text")
        or data.get("message")
        or metadata.get("message")
        or status
    ).strip()
    return HealthResult(
        component_name=clean_name,
        component_type=clean_type,
        status=status,
        healthy=healthy,
        available=available,
        degraded=degraded,
        latency_ms=latency_ms,
        error_code=error_code if not healthy else "",
        message=message,
        capabilities=clean_capabilities,
        metadata={
            **metadata,
            "raw_status": status_hint,
            "raw_success": success,
        },
    )


def health_from_lifecycle(
    component_name: str,
    component_type: str,
    lifecycle_status: Any,
    city_status: str = "",
    capabilities: Optional[Iterable[str]] = None,
) -> HealthResult:
    data = lifecycle_status.to_dict() if hasattr(lifecycle_status, "to_dict") else dict(lifecycle_status or {})
    state = str(data.get("state") or "").upper()
    city = str(city_status or "").strip().lower()
    if city == "disabled":
        status = HEALTH_STATUS_DISABLED
    elif state == "READY":
        status = HEALTH_STATUS_HEALTHY
    elif state in {"DEGRADED"}:
        status = HEALTH_STATUS_DEGRADED
    elif state in {"FAILED"}:
        status = HEALTH_STATUS_FAILED
    elif state in {"UNLOADED", "STOPPED", ""}:
        status = HEALTH_STATUS_UNKNOWN
    else:
        status = HEALTH_STATUS_UNKNOWN
    return HealthResult(
        component_name=component_name,
        component_type=component_type,
        status=status,
        healthy=status == HEALTH_STATUS_HEALTHY,
        available=status in {HEALTH_STATUS_HEALTHY, HEALTH_STATUS_DEGRADED, HEALTH_STATUS_UNKNOWN},
        degraded=status == HEALTH_STATUS_DEGRADED,
        error_code=str(data.get("reason") or ""),
        message=f"Lifecycle state: {state or 'unknown'}; city status: {city or 'unknown'}.",
        capabilities=list(capabilities or []),
        metadata={
            "source": "core_service",
            "active_probe": False,
            "lifecycle": data,
            "city_status": city,
        },
    )


def _call_health(component: Any) -> Any:
    health_check = getattr(component, "health_check", None)
    if callable(health_check):
        return health_check()
    get_status = getattr(component, "get_status", None)
    if callable(get_status):
        return get_status()
    status = getattr(component, "status", None)
    if callable(status):
        return status()
    return _health_failure(
        _normalize_name(getattr(component, "name", "") or getattr(component, "source", "")),
        "component",
        [],
        HEALTH_STATUS_UNKNOWN,
        "No health or status method is available.",
        None,
    )


def _result_to_mapping(raw_result: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw_result, dict):
        return dict(raw_result)
    to_dict = getattr(raw_result, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            return dict(data)
    if hasattr(raw_result, "success") or hasattr(raw_result, "status"):
        return {
            "success": getattr(raw_result, "success", True),
            "status": getattr(raw_result, "status", ""),
            "text": getattr(raw_result, "text", ""),
            "error_message": getattr(raw_result, "error_message", ""),
            "data": dict(getattr(raw_result, "data", {}) or {}),
            "metadata": dict(getattr(raw_result, "metadata", {}) or {}),
        }
    return None


def _status_from_raw(success: bool, status_hint: str, available_hint: Any) -> str:
    if str(status_hint).lower() in HEALTH_STATUSES:
        return str(status_hint).lower()
    if available_hint is not None and not bool(available_hint):
        return HEALTH_STATUS_UNAVAILABLE
    if status_hint in {"disabled", "placeholder"} and not success:
        return HEALTH_STATUS_DISABLED
    if status_hint in {"unavailable", "not_available"}:
        return HEALTH_STATUS_UNAVAILABLE
    if status_hint in {"failed", "failure", "error", "start_failed", "read_failed", "stop_failed"}:
        return HEALTH_STATUS_FAILED
    if status_hint in {"degraded", "partial"}:
        return HEALTH_STATUS_DEGRADED
    if success:
        return HEALTH_STATUS_HEALTHY
    return HEALTH_STATUS_FAILED


def _health_failure(
    component_name: str,
    component_type: str,
    capabilities: Iterable[str],
    error_code: str,
    message: str,
    latency_ms: Optional[float],
) -> HealthResult:
    return HealthResult(
        component_name=component_name,
        component_type=component_type,
        status=HEALTH_STATUS_FAILED
        if error_code not in {HEALTH_STATUS_UNKNOWN, HEALTH_ERROR_CIRCUIT_OPEN}
        else HEALTH_STATUS_UNKNOWN,
        healthy=False,
        available=False,
        degraded=False,
        latency_ms=latency_ms,
        error_code=error_code,
        message=message,
        capabilities=list(capabilities),
        metadata={"safe": True, "source": "health"},
    )


def _candidate_capabilities(adapter: Any, manifest: Any) -> List[str]:
    manifest_capabilities = list(getattr(manifest, "capabilities", []) or [])
    adapter_capabilities = list(getattr(adapter, "capabilities", []) or [])
    return _normalize_capabilities(manifest_capabilities + adapter_capabilities)


def _response_success(response: Any) -> bool:
    if response is None:
        return True
    if isinstance(response, bool):
        return response
    if isinstance(response, dict):
        return bool(response.get("success", False))
    if hasattr(response, "success"):
        return bool(getattr(response, "success"))
    return True


def _response_error(response: Any, fallback: str) -> str:
    if response is None:
        return fallback
    if isinstance(response, dict):
        return str(response.get("error_message") or response.get("status") or fallback)
    return str(getattr(response, "error_message", "") or getattr(response, "status", "") or fallback)


def _response_to_dict(response: Any) -> Dict[str, Any]:
    if isinstance(response, dict):
        return _stable_data(response)
    to_dict = getattr(response, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            return _stable_data(data)
    return {
        "success": _response_success(response),
        "text": str(getattr(response, "text", "") or ""),
        "error_message": str(getattr(response, "error_message", "") or ""),
        "status": str(getattr(response, "status", "") or ""),
    }


def _operation_exception_result(error: Exception) -> Dict[str, Any]:
    return {
        "success": False,
        "status": "operation_exception",
        "error_message": f"{type(error).__name__}: {error}",
        "metadata": {"safe": True, "source": "adapter_fallback_policy"},
    }


def _normalize_health_status(status: str) -> str:
    clean_status = str(status or "").strip().lower()
    if clean_status not in HEALTH_STATUSES:
        raise ValueError(f"Invalid health status: {status}")
    return clean_status


def _normalize_retry_safety(value: str) -> str:
    clean_value = str(value or "").strip().lower()
    if clean_value not in RETRY_SAFETY_VALUES:
        raise ValueError(f"Invalid retry safety value: {value}")
    return clean_value


def _normalize_capability(capability: str) -> str:
    return str(capability or "").strip().lower()


def _normalize_capabilities(capabilities: Iterable[str]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for capability in capabilities:
        clean = _normalize_capability(capability)
        if clean and clean not in seen:
            seen.add(clean)
            normalized.append(clean)
    return normalized


def _normalize_name(name: Any) -> str:
    return "_".join(part for part in str(name or "").strip().lower().split() if part)


def _normalize_names(names: Iterable[str]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for name in names:
        clean = _normalize_name(name)
        if clean and clean not in seen:
            seen.add(clean)
            normalized.append(clean)
    return normalized


def _bounded_float(value: float, minimum: float, maximum: float, label: str) -> float:
    number = float(value)
    if number < minimum or number > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return number


def _bounded_int(value: int, minimum: int, maximum: int, label: str) -> int:
    number = int(value)
    if number < minimum or number > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return number


def _elapsed_ms(clock: Clock, started_at: float) -> float:
    return round(max(0.0, (clock() - started_at) * 1000.0), 3)


def _monotonic_clock() -> float:
    import time

    return time.monotonic()


def _safe_metadata(payload: Optional[Dict[str, Any]], depth: int = 0) -> Dict[str, Any]:
    if not isinstance(payload, dict) or depth > 3:
        return {}
    safe: Dict[str, Any] = {}
    for key, value in payload.items():
        clean_key = str(key or "").strip()
        if not clean_key or _sensitive_key(clean_key):
            continue
        safe[clean_key] = _safe_value(value, depth + 1)
    return safe


def _safe_value(value: Any, depth: int) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, dict):
        return _safe_metadata(value, depth)
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth + 1) for item in list(value)[:20]]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _safe_metadata(value.to_dict(), depth)
    return repr(value)[:200]


def _sensitive_key(key: str) -> bool:
    lowered = key.lower()
    sensitive_parts = (
        "api_key",
        "secret",
        "token",
        "password",
        "transcript",
        "accepted_text",
        "input_text",
        "raw_text",
        "text",
    )
    return any(part in lowered for part in sensitive_parts)


def _stable_data(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _stable_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stable_data(item) for item in value]
    if isinstance(value, set):
        return [_stable_data(item) for item in sorted(value, key=repr)]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _stable_data(to_dict())
    return repr(value)
