from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from memory.schema_migrations import MigrationError


CPU_WEIGHT_TINY = "tiny"
CPU_WEIGHT_LOW = "low"
CPU_WEIGHT_NORMAL = "normal"
CPU_WEIGHT_HIGH = "high"
CPU_WEIGHT_EXTREME = "extreme"
CPU_WEIGHTS = {
    CPU_WEIGHT_TINY,
    CPU_WEIGHT_LOW,
    CPU_WEIGHT_NORMAL,
    CPU_WEIGHT_HIGH,
    CPU_WEIGHT_EXTREME,
}

STARTUP_COST_INSTANT = "instant"
STARTUP_COST_LIGHT = "light"
STARTUP_COST_MEDIUM = "medium"
STARTUP_COST_HEAVY = "heavy"
STARTUP_COSTS = {
    STARTUP_COST_INSTANT,
    STARTUP_COST_LIGHT,
    STARTUP_COST_MEDIUM,
    STARTUP_COST_HEAVY,
}

TASK_PRIORITY_BACKGROUND = "background"
TASK_PRIORITY_LOW = "low"
TASK_PRIORITY_NORMAL = "normal"
TASK_PRIORITY_HIGH = "high"
TASK_PRIORITY_CRITICAL = "critical"
TASK_PRIORITIES = {
    TASK_PRIORITY_BACKGROUND,
    TASK_PRIORITY_LOW,
    TASK_PRIORITY_NORMAL,
    TASK_PRIORITY_HIGH,
    TASK_PRIORITY_CRITICAL,
}
TASK_PRIORITY_RANK = {
    TASK_PRIORITY_BACKGROUND: 0,
    TASK_PRIORITY_LOW: 1,
    TASK_PRIORITY_NORMAL: 2,
    TASK_PRIORITY_HIGH: 3,
    TASK_PRIORITY_CRITICAL: 4,
}

RESOURCE_PROFILE_TEST = "test"
RESOURCE_PROFILE_RASPBERRY_PI_5 = "raspberry_pi_5"
RESOURCE_PROFILE_DESKTOP = "desktop"
RESOURCE_PROFILE_FUTURE_ORIN = "future_orin"
RESOURCE_PROFILES = {
    RESOURCE_PROFILE_TEST,
    RESOURCE_PROFILE_RASPBERRY_PI_5,
    RESOURCE_PROFILE_DESKTOP,
    RESOURCE_PROFILE_FUTURE_ORIN,
}

RESOURCE_ERROR_RAM_BUDGET_EXCEEDED = "ram_budget_exceeded"
RESOURCE_ERROR_HEAVY_MODULE_LIMIT = "heavy_module_limit_exceeded"
RESOURCE_ERROR_GLOBAL_TASK_LIMIT = "global_task_limit_exceeded"
RESOURCE_ERROR_MODULE_TASK_LIMIT = "module_task_limit_exceeded"
RESOURCE_ERROR_NETWORK_REQUIRED_DENIED = "network_required_module_denied"
RESOURCE_ERROR_HARDWARE_ACCELERATION_DENIED = "hardware_acceleration_unavailable"
RESOURCE_ERROR_INVALID_MANIFEST = "invalid_resource_manifest"
RESOURCE_ERROR_RESERVATION_EXISTS = "reservation_already_exists"
RESOURCE_ERROR_RESERVATION_MISSING = "reservation_missing"
RESOURCE_ERROR_MODULE_ACTIVE = "module_still_active_during_release"
RESOURCE_ERROR_NO_EVICTION_CANDIDATE = "no_safe_eviction_candidate"
RESOURCE_ERROR_CANCELLATION_UNSUPPORTED = "cancellation_unsupported"
RESOURCE_ERROR_ACTIVATION_DENIED = "activation_denied_by_resource_policy"

RESOURCE_EVENT_RESERVATION_CREATED = "resource.reservation_created"
RESOURCE_EVENT_RESERVATION_RELEASED = "resource.reservation_released"
RESOURCE_EVENT_ACTIVATION_DENIED = "resource.activation_denied"
RESOURCE_EVENT_HEAVY_LIMIT_REACHED = "resource.heavy_module_limit_reached"
RESOURCE_EVENT_IDLE_UNLOADED = "resource.idle_module_unloaded"
RESOURCE_EVENT_EVICTION_PERFORMED = "resource.eviction_performed"
RESOURCE_EVENT_EVICTION_REFUSED = "resource.eviction_refused"
RESOURCE_EVENT_TASK_SLOT_ACQUIRED = "resource.task_slot_acquired"
RESOURCE_EVENT_TASK_SLOT_RELEASED = "resource.task_slot_released"
RESOURCE_EVENT_TASK_CANCELLED = "resource.task_cancelled"
RESOURCE_EVENT_MAINTENANCE_COMPLETED = "resource.maintenance_completed"

Clock = Callable[[], float]


@dataclass(frozen=True)
class ResourceDeclaration:
    """Declared logical resource estimate for a loadable ARES module."""

    estimated_ram_mb: int = 0
    estimated_cpu_weight: str = CPU_WEIGHT_TINY
    startup_cost: str = STARTUP_COST_INSTANT
    shutdown_cost: str = STARTUP_COST_INSTANT
    heavy_module: bool = False
    persistent_module: bool = False
    inactivity_timeout_seconds: Optional[float] = None
    maximum_concurrent_tasks: int = 1
    task_priority: str = TASK_PRIORITY_NORMAL
    network_required: bool = False
    hardware_acceleration_required: bool = False
    safe_to_stop: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "estimated_ram_mb",
            _bounded_int(self.estimated_ram_mb, 0, 1_048_576, "estimated_ram_mb"),
        )
        object.__setattr__(
            self,
            "estimated_cpu_weight",
            _enum_value(self.estimated_cpu_weight, CPU_WEIGHTS, "estimated_cpu_weight"),
        )
        object.__setattr__(
            self,
            "startup_cost",
            _enum_value(self.startup_cost, STARTUP_COSTS, "startup_cost"),
        )
        object.__setattr__(
            self,
            "shutdown_cost",
            _enum_value(self.shutdown_cost, STARTUP_COSTS, "shutdown_cost"),
        )
        object.__setattr__(self, "heavy_module", bool(self.heavy_module))
        object.__setattr__(self, "persistent_module", bool(self.persistent_module))
        if self.inactivity_timeout_seconds is not None:
            object.__setattr__(
                self,
                "inactivity_timeout_seconds",
                _bounded_float(
                    self.inactivity_timeout_seconds,
                    0.0,
                    86_400.0,
                    "inactivity_timeout_seconds",
                ),
            )
        object.__setattr__(
            self,
            "maximum_concurrent_tasks",
            _bounded_int(
                self.maximum_concurrent_tasks,
                1,
                1_000,
                "maximum_concurrent_tasks",
            ),
        )
        object.__setattr__(
            self,
            "task_priority",
            _enum_value(self.task_priority, TASK_PRIORITIES, "task_priority"),
        )
        object.__setattr__(self, "network_required", bool(self.network_required))
        object.__setattr__(
            self,
            "hardware_acceleration_required",
            bool(self.hardware_acceleration_required),
        )
        object.__setattr__(self, "safe_to_stop", bool(self.safe_to_stop))

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]]):
        data = dict(payload or {})
        unknown = sorted(set(data) - set(cls().to_dict()))
        if unknown:
            raise ValueError(f"Unknown resource declaration fields: {', '.join(unknown)}")
        return cls(
            estimated_ram_mb=int(data.get("estimated_ram_mb", 0)),
            estimated_cpu_weight=str(data.get("estimated_cpu_weight", CPU_WEIGHT_TINY)),
            startup_cost=str(data.get("startup_cost", STARTUP_COST_INSTANT)),
            shutdown_cost=str(data.get("shutdown_cost", STARTUP_COST_INSTANT)),
            heavy_module=bool(data.get("heavy_module", False)),
            persistent_module=bool(data.get("persistent_module", False)),
            inactivity_timeout_seconds=data.get("inactivity_timeout_seconds"),
            maximum_concurrent_tasks=int(data.get("maximum_concurrent_tasks", 1)),
            task_priority=str(data.get("task_priority", TASK_PRIORITY_NORMAL)),
            network_required=bool(data.get("network_required", False)),
            hardware_acceleration_required=bool(
                data.get("hardware_acceleration_required", False)
            ),
            safe_to_stop=bool(data.get("safe_to_stop", True)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "estimated_ram_mb": self.estimated_ram_mb,
            "estimated_cpu_weight": self.estimated_cpu_weight,
            "startup_cost": self.startup_cost,
            "shutdown_cost": self.shutdown_cost,
            "heavy_module": self.heavy_module,
            "persistent_module": self.persistent_module,
            "inactivity_timeout_seconds": self.inactivity_timeout_seconds,
            "maximum_concurrent_tasks": self.maximum_concurrent_tasks,
            "task_priority": self.task_priority,
            "network_required": self.network_required,
            "hardware_acceleration_required": self.hardware_acceleration_required,
            "safe_to_stop": self.safe_to_stop,
        }


@dataclass(frozen=True)
class ResourcePolicy:
    maximum_estimated_loaded_ram_mb: int = 512
    maximum_heavy_modules_loaded: int = 1
    maximum_concurrent_tasks: int = 4
    default_inactivity_timeout_seconds: float = 300.0
    per_priority_task_limits: Dict[str, int] = field(default_factory=dict)
    allow_network_required_modules: bool = False
    allow_hardware_accelerated_modules: bool = False
    platform_profile: str = RESOURCE_PROFILE_TEST
    eviction_enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "maximum_estimated_loaded_ram_mb",
            _bounded_int(
                self.maximum_estimated_loaded_ram_mb,
                0,
                1_048_576,
                "maximum_estimated_loaded_ram_mb",
            ),
        )
        object.__setattr__(
            self,
            "maximum_heavy_modules_loaded",
            _bounded_int(
                self.maximum_heavy_modules_loaded,
                0,
                1_000,
                "maximum_heavy_modules_loaded",
            ),
        )
        object.__setattr__(
            self,
            "maximum_concurrent_tasks",
            _bounded_int(
                self.maximum_concurrent_tasks,
                1,
                10_000,
                "maximum_concurrent_tasks",
            ),
        )
        object.__setattr__(
            self,
            "default_inactivity_timeout_seconds",
            _bounded_float(
                self.default_inactivity_timeout_seconds,
                0.0,
                86_400.0,
                "default_inactivity_timeout_seconds",
            ),
        )
        object.__setattr__(
            self,
            "per_priority_task_limits",
            _normalize_priority_limits(self.per_priority_task_limits),
        )
        object.__setattr__(
            self,
            "platform_profile",
            _enum_value(self.platform_profile, RESOURCE_PROFILES, "platform_profile"),
        )
        object.__setattr__(
            self,
            "allow_network_required_modules",
            bool(self.allow_network_required_modules),
        )
        object.__setattr__(
            self,
            "allow_hardware_accelerated_modules",
            bool(self.allow_hardware_accelerated_modules),
        )
        object.__setattr__(self, "eviction_enabled", bool(self.eviction_enabled))

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]]):
        data = dict(payload or {})
        return cls(
            maximum_estimated_loaded_ram_mb=int(
                data.get("maximum_estimated_loaded_ram_mb", 512)
            ),
            maximum_heavy_modules_loaded=int(
                data.get("maximum_heavy_modules_loaded", 1)
            ),
            maximum_concurrent_tasks=int(data.get("maximum_concurrent_tasks", 4)),
            default_inactivity_timeout_seconds=float(
                data.get("default_inactivity_timeout_seconds", 300.0)
            ),
            per_priority_task_limits=dict(data.get("per_priority_task_limits") or {}),
            allow_network_required_modules=bool(
                data.get("allow_network_required_modules", False)
            ),
            allow_hardware_accelerated_modules=bool(
                data.get("allow_hardware_accelerated_modules", False)
            ),
            platform_profile=str(data.get("platform_profile", RESOURCE_PROFILE_TEST)),
            eviction_enabled=bool(data.get("eviction_enabled", False)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "maximum_estimated_loaded_ram_mb": self.maximum_estimated_loaded_ram_mb,
            "maximum_heavy_modules_loaded": self.maximum_heavy_modules_loaded,
            "maximum_concurrent_tasks": self.maximum_concurrent_tasks,
            "default_inactivity_timeout_seconds": self.default_inactivity_timeout_seconds,
            "per_priority_task_limits": dict(sorted(self.per_priority_task_limits.items())),
            "allow_network_required_modules": self.allow_network_required_modules,
            "allow_hardware_accelerated_modules": self.allow_hardware_accelerated_modules,
            "platform_profile": self.platform_profile,
            "eviction_enabled": self.eviction_enabled,
        }


@dataclass(frozen=True)
class ResourceDecision:
    success: bool
    status: str
    module_name: str = ""
    text: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "module_name": self.module_name,
            "text": self.text,
            "error_message": self.error_message,
            "data": _stable_data(self.data),
            "metadata": _stable_data(self.metadata),
        }


@dataclass
class ResourceReservation:
    module_name: str
    resources: ResourceDeclaration
    reserved_at: float
    last_activity_at: float
    active_tasks: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_name": self.module_name,
            "estimated_ram_mb": self.resources.estimated_ram_mb,
            "estimated_cpu_weight": self.resources.estimated_cpu_weight,
            "startup_cost": self.resources.startup_cost,
            "shutdown_cost": self.resources.shutdown_cost,
            "heavy_module": self.resources.heavy_module,
            "persistent_module": self.resources.persistent_module,
            "inactivity_timeout_seconds": self.resources.inactivity_timeout_seconds,
            "maximum_concurrent_tasks": self.resources.maximum_concurrent_tasks,
            "task_priority": self.resources.task_priority,
            "network_required": self.resources.network_required,
            "hardware_acceleration_required": self.resources.hardware_acceleration_required,
            "safe_to_stop": self.resources.safe_to_stop,
            "reserved_at": self.reserved_at,
            "last_activity_at": self.last_activity_at,
            "active_task_count": len(self.active_tasks),
            "active_tasks": dict(sorted(self.active_tasks.items())),
        }


@dataclass
class CancellationToken:
    task_id: str
    supports_cancellation: bool = True
    requested: bool = False
    reason: str = ""

    def cancel(self, reason: str = "") -> ResourceDecision:
        if not self.supports_cancellation:
            return ResourceDecision(
                success=False,
                status=RESOURCE_ERROR_CANCELLATION_UNSUPPORTED,
                text="Cancellation is unsupported for this task.",
                error_message=RESOURCE_ERROR_CANCELLATION_UNSUPPORTED,
                data={"task_id": self.task_id},
                metadata=_metadata("resource_manager"),
            )
        self.requested = True
        self.reason = str(reason or "cancelled").strip()
        return ResourceDecision(
            success=True,
            status="cancelled",
            text="Task cancellation requested cooperatively.",
            data={"task_id": self.task_id, "reason": self.reason},
            metadata=_metadata("resource_manager"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "supports_cancellation": self.supports_cancellation,
            "requested": self.requested,
            "reason": self.reason,
        }


class ResourceManager:
    """Central logical resource reservation manager for CoreService."""

    def __init__(
        self,
        policy: Optional[ResourcePolicy] = None,
        clock: Optional[Clock] = None,
        event_history_store: Any = None,
        metrics_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        self.policy = policy or resource_policy_for_profile(RESOURCE_PROFILE_TEST)
        self.clock = clock or time.monotonic
        self.event_history_store = event_history_store
        self.metrics_provider = metrics_provider
        self._reservations: Dict[str, ResourceReservation] = {}
        self._created_at = self.clock()
        self._event_history_failures: List[Dict[str, Any]] = []

    def can_activate(self, manifest: Any) -> ResourceDecision:
        module_name, resources = _manifest_identity(manifest)
        if not module_name:
            return self._decision(
                False,
                RESOURCE_ERROR_INVALID_MANIFEST,
                module_name,
                "Resource activation requires a module name.",
                RESOURCE_ERROR_INVALID_MANIFEST,
            )
        if module_name in self._reservations:
            return self._decision(
                True,
                "already_reserved",
                module_name,
                "Module already has a resource reservation.",
                data={"reservation": self._reservations[module_name].to_dict()},
            )
        policy_failure = self._policy_rejection(module_name, resources)
        if policy_failure is not None:
            return policy_failure
        projected = self._projected_usage(resources)
        if projected["declared_reserved_ram_mb"] > self.policy.maximum_estimated_loaded_ram_mb:
            return self._decision(
                False,
                RESOURCE_ERROR_RAM_BUDGET_EXCEEDED,
                module_name,
                "Activation denied because declared RAM budget would be exceeded.",
                RESOURCE_ERROR_RAM_BUDGET_EXCEEDED,
                data={
                    "limit_mb": self.policy.maximum_estimated_loaded_ram_mb,
                    "projected_mb": projected["declared_reserved_ram_mb"],
                    "requested_mb": resources.estimated_ram_mb,
                    "current_usage": self.current_usage(),
                },
            )
        if projected["active_heavy_module_count"] > self.policy.maximum_heavy_modules_loaded:
            return self._decision(
                False,
                RESOURCE_ERROR_HEAVY_MODULE_LIMIT,
                module_name,
                "Activation denied because the heavy-module limit would be exceeded.",
                RESOURCE_ERROR_HEAVY_MODULE_LIMIT,
                data={
                    "limit": self.policy.maximum_heavy_modules_loaded,
                    "projected": projected["active_heavy_module_count"],
                    "current_usage": self.current_usage(),
                },
            )
        return self._decision(
            True,
            "activation_allowed",
            module_name,
            "Resource activation is allowed.",
            data={"projected_usage": projected, "resources": resources.to_dict()},
        )

    def reserve(self, manifest: Any) -> ResourceDecision:
        module_name, resources = _manifest_identity(manifest)
        decision = self.can_activate(manifest)
        if not decision.success:
            self._record_event(
                RESOURCE_EVENT_HEAVY_LIMIT_REACHED
                if decision.status == RESOURCE_ERROR_HEAVY_MODULE_LIMIT
                else RESOURCE_EVENT_ACTIVATION_DENIED,
                {
                    "module_name": module_name,
                    "status": decision.status,
                    "error_message": decision.error_message,
                    "resources": resources.to_dict(),
                },
            )
            return decision
        if decision.status == "already_reserved":
            return decision
        now = self.clock()
        reservation = ResourceReservation(
            module_name=module_name,
            resources=resources,
            reserved_at=now,
            last_activity_at=now,
        )
        self._reservations[module_name] = reservation
        self._record_event(
            RESOURCE_EVENT_RESERVATION_CREATED,
            {
                "module_name": module_name,
                "estimated_ram_mb": resources.estimated_ram_mb,
                "heavy_module": resources.heavy_module,
            },
        )
        return self._decision(
            True,
            "reserved",
            module_name,
            "Resource reservation created.",
            data={
                "reservation": reservation.to_dict(),
                "current_usage": self.current_usage(),
            },
        )

    def release(self, module_name: str, force: bool = False) -> ResourceDecision:
        clean_name = _normalize_module_name(module_name)
        reservation = self._reservations.get(clean_name)
        if reservation is None:
            return self._decision(
                False,
                RESOURCE_ERROR_RESERVATION_MISSING,
                clean_name,
                "Resource reservation is missing.",
                RESOURCE_ERROR_RESERVATION_MISSING,
            )
        if reservation.active_tasks and not force:
            return self._decision(
                False,
                RESOURCE_ERROR_MODULE_ACTIVE,
                clean_name,
                "Resource reservation cannot be released while tasks are active.",
                RESOURCE_ERROR_MODULE_ACTIVE,
                data={"active_tasks": dict(reservation.active_tasks)},
            )
        removed = self._reservations.pop(clean_name)
        self._record_event(
            RESOURCE_EVENT_RESERVATION_RELEASED,
            {
                "module_name": clean_name,
                "estimated_ram_mb": removed.resources.estimated_ram_mb,
                "heavy_module": removed.resources.heavy_module,
            },
        )
        return self._decision(
            True,
            "released",
            clean_name,
            "Resource reservation released.",
            data={"released": removed.to_dict(), "current_usage": self.current_usage()},
        )

    def acquire_task(
        self,
        manifest_or_name: Any,
        task_id: str = "",
        priority: str = "",
    ) -> ResourceDecision:
        module_name = _module_name(manifest_or_name)
        reservation = self._reservations.get(module_name)
        if reservation is None:
            return self._decision(
                False,
                RESOURCE_ERROR_RESERVATION_MISSING,
                module_name,
                "Task slot requires an active resource reservation.",
                RESOURCE_ERROR_RESERVATION_MISSING,
            )
        clean_priority = _task_priority(priority or reservation.resources.task_priority)
        if self._active_task_count() >= self.policy.maximum_concurrent_tasks:
            return self._decision(
                False,
                RESOURCE_ERROR_GLOBAL_TASK_LIMIT,
                module_name,
                "Global task capacity is exhausted.",
                RESOURCE_ERROR_GLOBAL_TASK_LIMIT,
                data={"current_usage": self.current_usage()},
            )
        priority_limit = self.policy.per_priority_task_limits.get(clean_priority)
        if priority_limit is not None and self._priority_task_count(clean_priority) >= priority_limit:
            return self._decision(
                False,
                RESOURCE_ERROR_GLOBAL_TASK_LIMIT,
                module_name,
                "Task capacity for this priority is exhausted.",
                RESOURCE_ERROR_GLOBAL_TASK_LIMIT,
                data={"priority": clean_priority, "priority_limit": priority_limit},
            )
        if len(reservation.active_tasks) >= reservation.resources.maximum_concurrent_tasks:
            return self._decision(
                False,
                RESOURCE_ERROR_MODULE_TASK_LIMIT,
                module_name,
                "Module task capacity is exhausted.",
                RESOURCE_ERROR_MODULE_TASK_LIMIT,
                data={
                    "limit": reservation.resources.maximum_concurrent_tasks,
                    "active_task_count": len(reservation.active_tasks),
                },
            )
        clean_task_id = str(task_id or f"{module_name}:{len(reservation.active_tasks) + 1}").strip()
        reservation.active_tasks[clean_task_id] = clean_priority
        reservation.last_activity_at = self.clock()
        self._record_event(
            RESOURCE_EVENT_TASK_SLOT_ACQUIRED,
            {
                "module_name": module_name,
                "task_id": clean_task_id,
                "task_priority": clean_priority,
            },
        )
        return self._decision(
            True,
            "task_slot_acquired",
            module_name,
            "Task slot acquired.",
            data={
                "task_id": clean_task_id,
                "task_priority": clean_priority,
                "reservation": reservation.to_dict(),
            },
        )

    def release_task(self, module_name: str, task_id: str) -> ResourceDecision:
        clean_name = _normalize_module_name(module_name)
        clean_task = str(task_id or "").strip()
        reservation = self._reservations.get(clean_name)
        if reservation is None or clean_task not in reservation.active_tasks:
            return self._decision(
                False,
                RESOURCE_ERROR_RESERVATION_MISSING,
                clean_name,
                "Task slot is missing.",
                RESOURCE_ERROR_RESERVATION_MISSING,
                data={"task_id": clean_task},
            )
        priority = reservation.active_tasks.pop(clean_task)
        reservation.last_activity_at = self.clock()
        self._record_event(
            RESOURCE_EVENT_TASK_SLOT_RELEASED,
            {
                "module_name": clean_name,
                "task_id": clean_task,
                "task_priority": priority,
            },
        )
        return self._decision(
            True,
            "task_slot_released",
            clean_name,
            "Task slot released.",
            data={"task_id": clean_task, "task_priority": priority},
        )

    def cancel_task(
        self,
        module_name: str,
        token: CancellationToken,
        reason: str = "",
    ) -> ResourceDecision:
        decision = token.cancel(reason)
        if not decision.success:
            return decision
        release = self.release_task(module_name, token.task_id)
        self._record_event(
            RESOURCE_EVENT_TASK_CANCELLED,
            {
                "module_name": _normalize_module_name(module_name),
                "task_id": token.task_id,
                "reason": token.reason,
                "released": release.success,
            },
        )
        return self._decision(
            release.success,
            "task_cancelled" if release.success else release.status,
            _normalize_module_name(module_name),
            "Task cancelled cooperatively." if release.success else release.text,
            release.error_message,
            data={"token": token.to_dict(), "release": release.to_dict()},
        )

    def record_activity(self, module_name: str) -> ResourceDecision:
        clean_name = _normalize_module_name(module_name)
        reservation = self._reservations.get(clean_name)
        if reservation is None:
            return self._decision(
                False,
                RESOURCE_ERROR_RESERVATION_MISSING,
                clean_name,
                "Resource reservation is missing.",
                RESOURCE_ERROR_RESERVATION_MISSING,
            )
        reservation.last_activity_at = self.clock()
        return self._decision(
            True,
            "activity_recorded",
            clean_name,
            "Resource activity recorded.",
            data={"reservation": reservation.to_dict()},
        )

    def find_inactive_modules(self) -> List[Dict[str, Any]]:
        now = self.clock()
        inactive: List[Dict[str, Any]] = []
        for reservation in self._reservations.values():
            timeout = self._timeout_for(reservation)
            idle_seconds = max(0.0, now - reservation.last_activity_at)
            if reservation.resources.persistent_module:
                continue
            if reservation.active_tasks:
                continue
            if idle_seconds >= timeout:
                inactive.append(
                    {
                        **reservation.to_dict(),
                        "idle_seconds": round(idle_seconds, 3),
                        "timeout_seconds": timeout,
                    }
                )
        return sorted(inactive, key=lambda item: item["module_name"])

    def select_eviction_candidate(
        self,
        incoming_manifest: Any,
        priority: str = "",
    ) -> ResourceDecision:
        incoming_priority = _task_priority(
            priority or _resources_from_manifest(incoming_manifest).task_priority
        )
        now = self.clock()
        candidates = []
        for reservation in self._reservations.values():
            if reservation.resources.persistent_module:
                continue
            if reservation.active_tasks:
                continue
            if not reservation.resources.safe_to_stop:
                continue
            if reservation.resources.task_priority == TASK_PRIORITY_CRITICAL:
                continue
            if TASK_PRIORITY_RANK[reservation.resources.task_priority] >= TASK_PRIORITY_RANK[incoming_priority]:
                continue
            idle_seconds = max(0.0, now - reservation.last_activity_at)
            candidates.append(
                {
                    "module_name": reservation.module_name,
                    "idle_seconds": idle_seconds,
                    "task_priority": reservation.resources.task_priority,
                    "estimated_ram_mb": reservation.resources.estimated_ram_mb,
                    "reservation": reservation.to_dict(),
                }
            )
        if not candidates:
            self._record_event(
                RESOURCE_EVENT_EVICTION_REFUSED,
                {
                    "incoming_module": _module_name(incoming_manifest),
                    "incoming_priority": incoming_priority,
                    "status": RESOURCE_ERROR_NO_EVICTION_CANDIDATE,
                },
            )
            return self._decision(
                False,
                RESOURCE_ERROR_NO_EVICTION_CANDIDATE,
                _module_name(incoming_manifest),
                "No safe eviction candidate is available.",
                RESOURCE_ERROR_NO_EVICTION_CANDIDATE,
            )
        candidates.sort(
            key=lambda item: (
                -float(item["idle_seconds"]),
                TASK_PRIORITY_RANK[str(item["task_priority"])],
                -int(item["estimated_ram_mb"]),
                str(item["module_name"]),
            )
        )
        selected = candidates[0]
        return self._decision(
            True,
            "eviction_candidate_selected",
            str(selected["module_name"]),
            "Safe eviction candidate selected.",
            data={
                "candidate": selected,
                "incoming_module": _module_name(incoming_manifest),
                "incoming_priority": incoming_priority,
            },
        )

    def current_usage(self) -> Dict[str, Any]:
        reservations = list(self._reservations.values())
        return {
            "declared_reserved_ram_mb": sum(
                reservation.resources.estimated_ram_mb for reservation in reservations
            ),
            "active_module_count": len(reservations),
            "active_heavy_module_count": sum(
                1 for reservation in reservations if reservation.resources.heavy_module
            ),
            "active_task_count": self._active_task_count(),
            "loaded_city_count": len(reservations),
            "reservation_names": sorted(self._reservations),
            "policy": self.policy.to_dict(),
        }

    def list_reservations(self) -> List[Dict[str, Any]]:
        return [
            self._reservations[name].to_dict()
            for name in sorted(self._reservations)
        ]

    def list_loaded_modules(self) -> List[str]:
        return sorted(self._reservations)

    def module_status(self, module_name: str) -> ResourceDecision:
        clean_name = _normalize_module_name(module_name)
        reservation = self._reservations.get(clean_name)
        if reservation is None:
            return self._decision(
                False,
                RESOURCE_ERROR_RESERVATION_MISSING,
                clean_name,
                "Module has no resource reservation.",
                RESOURCE_ERROR_RESERVATION_MISSING,
            )
        return self._decision(
            True,
            "reserved",
            clean_name,
            "Module resource status discovered.",
            data={"reservation": reservation.to_dict()},
        )

    def observed_process_metrics(self) -> Dict[str, Any]:
        if self.metrics_provider is not None:
            data = self.metrics_provider()
            if not isinstance(data, dict):
                raise ValueError("metrics_provider must return a dictionary")
            return _stable_data(data)
        rss = _process_rss_bytes()
        return {
            "source": "resource_manager",
            "process_uptime_seconds": round(max(0.0, self.clock() - self._created_at), 3),
            "process_cpu_time_seconds": round(time.process_time(), 6),
            "process_rss_bytes": rss,
            "process_rss_status": "available" if rss is not None else "unavailable",
            "active_module_count": len(self._reservations),
            "active_heavy_module_count": self.current_usage()["active_heavy_module_count"],
            "active_task_count": self._active_task_count(),
            "declared_reserved_ram_mb": self.current_usage()["declared_reserved_ram_mb"],
            "loaded_city_count": len(self._reservations),
            "note": "Process metrics are process-level observations, not per-module exact measurements.",
        }

    def event_history_failures(self) -> List[Dict[str, Any]]:
        return list(self._event_history_failures)

    def record_maintenance_completed(self, data: Dict[str, Any]) -> None:
        self._record_event(RESOURCE_EVENT_MAINTENANCE_COMPLETED, data)

    def record_idle_unloaded(self, module_name: str) -> None:
        self._record_event(
            RESOURCE_EVENT_IDLE_UNLOADED,
            {"module_name": _normalize_module_name(module_name)},
        )

    def record_eviction_performed(self, module_name: str, incoming_module: str) -> None:
        self._record_event(
            RESOURCE_EVENT_EVICTION_PERFORMED,
            {
                "module_name": _normalize_module_name(module_name),
                "incoming_module": _normalize_module_name(incoming_module),
            },
        )

    def _policy_rejection(
        self,
        module_name: str,
        resources: ResourceDeclaration,
    ) -> Optional[ResourceDecision]:
        if resources.network_required and not self.policy.allow_network_required_modules:
            return self._decision(
                False,
                RESOURCE_ERROR_NETWORK_REQUIRED_DENIED,
                module_name,
                "Activation denied because network-required modules are disabled.",
                RESOURCE_ERROR_NETWORK_REQUIRED_DENIED,
                data={"resources": resources.to_dict(), "policy": self.policy.to_dict()},
            )
        if (
            resources.hardware_acceleration_required
            and not self.policy.allow_hardware_accelerated_modules
        ):
            return self._decision(
                False,
                RESOURCE_ERROR_HARDWARE_ACCELERATION_DENIED,
                module_name,
                "Activation denied because hardware acceleration is unavailable by policy.",
                RESOURCE_ERROR_HARDWARE_ACCELERATION_DENIED,
                data={"resources": resources.to_dict(), "policy": self.policy.to_dict()},
            )
        return None

    def _projected_usage(self, resources: ResourceDeclaration) -> Dict[str, Any]:
        usage = self.current_usage()
        return {
            **usage,
            "declared_reserved_ram_mb": usage["declared_reserved_ram_mb"]
            + resources.estimated_ram_mb,
            "active_heavy_module_count": usage["active_heavy_module_count"]
            + (1 if resources.heavy_module else 0),
        }

    def _active_task_count(self) -> int:
        return sum(len(reservation.active_tasks) for reservation in self._reservations.values())

    def _priority_task_count(self, priority: str) -> int:
        return sum(
            1
            for reservation in self._reservations.values()
            for task_priority in reservation.active_tasks.values()
            if task_priority == priority
        )

    def _timeout_for(self, reservation: ResourceReservation) -> float:
        if reservation.resources.inactivity_timeout_seconds is not None:
            return reservation.resources.inactivity_timeout_seconds
        return self.policy.default_inactivity_timeout_seconds

    def _decision(
        self,
        success: bool,
        status: str,
        module_name: str,
        text: str,
        error_message: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> ResourceDecision:
        return ResourceDecision(
            success=success,
            status=status,
            module_name=_normalize_module_name(module_name),
            text=text,
            error_message=error_message,
            data=dict(data or {}),
            metadata=_metadata("resource_manager"),
        )

    def _record_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.event_history_store is None:
            return
        safe_payload = _safe_payload(payload)
        event = {
            "source": "resource_manager",
            "type": event_type,
            "priority": "normal",
            "timestamp": _event_timestamp(),
            "payload": safe_payload,
        }
        result = {
            "success": True,
            "decision": "recorded",
            "text": "Resource event recorded.",
            "data": {
                "source": "resource_manager",
                "event_type": event_type,
                "payload": safe_payload,
            },
            "error_message": "",
            "metadata": _metadata("resource_manager"),
        }
        try:
            self.event_history_store.add(event, result)
        except (MigrationError, OSError) as error:
            self._event_history_failures.append(
                {
                    "source": "resource_manager",
                    "event_type": event_type,
                    "error_type": type(error).__name__,
                    "error_message": str(error)[:200],
                }
            )
            self._event_history_failures = self._event_history_failures[-20:]


def resource_policy_for_profile(profile: str) -> ResourcePolicy:
    clean_profile = _enum_value(profile, RESOURCE_PROFILES, "platform_profile")
    profiles = {
        RESOURCE_PROFILE_TEST: ResourcePolicy(
            maximum_estimated_loaded_ram_mb=256,
            maximum_heavy_modules_loaded=1,
            maximum_concurrent_tasks=4,
            default_inactivity_timeout_seconds=60.0,
            platform_profile=RESOURCE_PROFILE_TEST,
        ),
        RESOURCE_PROFILE_RASPBERRY_PI_5: ResourcePolicy(
            maximum_estimated_loaded_ram_mb=512,
            maximum_heavy_modules_loaded=1,
            maximum_concurrent_tasks=2,
            default_inactivity_timeout_seconds=120.0,
            platform_profile=RESOURCE_PROFILE_RASPBERRY_PI_5,
        ),
        RESOURCE_PROFILE_DESKTOP: ResourcePolicy(
            maximum_estimated_loaded_ram_mb=4_096,
            maximum_heavy_modules_loaded=3,
            maximum_concurrent_tasks=16,
            default_inactivity_timeout_seconds=600.0,
            platform_profile=RESOURCE_PROFILE_DESKTOP,
        ),
        RESOURCE_PROFILE_FUTURE_ORIN: ResourcePolicy(
            maximum_estimated_loaded_ram_mb=2_048,
            maximum_heavy_modules_loaded=2,
            maximum_concurrent_tasks=8,
            default_inactivity_timeout_seconds=300.0,
            platform_profile=RESOURCE_PROFILE_FUTURE_ORIN,
        ),
    }
    return profiles[clean_profile]


def _manifest_identity(manifest: Any) -> tuple[str, ResourceDeclaration]:
    module_name = _module_name(manifest)
    resources = _resources_from_manifest(manifest)
    return module_name, resources


def _module_name(manifest_or_name: Any) -> str:
    if isinstance(manifest_or_name, str):
        return _normalize_module_name(manifest_or_name)
    if isinstance(manifest_or_name, dict):
        return _normalize_module_name(str(manifest_or_name.get("module_name") or ""))
    return _normalize_module_name(str(getattr(manifest_or_name, "module_name", "") or ""))


def _resources_from_manifest(manifest: Any) -> ResourceDeclaration:
    raw_resources = None
    if isinstance(manifest, dict):
        raw_resources = manifest.get("resources")
    else:
        raw_resources = getattr(manifest, "resources", None)
    if isinstance(raw_resources, ResourceDeclaration):
        return raw_resources
    if isinstance(raw_resources, dict) or raw_resources is None:
        return ResourceDeclaration.from_dict(raw_resources)
    raise ValueError("Manifest resources must be a ResourceDeclaration or dictionary")


def _task_priority(priority: str) -> str:
    return _enum_value(priority, TASK_PRIORITIES, "task_priority")


def _normalize_module_name(name: str) -> str:
    return "_".join(part for part in str(name or "").strip().lower().split() if part)


def _enum_value(value: Any, allowed: set[str], label: str) -> str:
    clean = str(value or "").strip().lower()
    if clean not in allowed:
        raise ValueError(f"Invalid {label}: {value}")
    return clean


def _normalize_priority_limits(payload: Dict[str, Any]) -> Dict[str, int]:
    normalized: Dict[str, int] = {}
    for priority, limit in dict(payload or {}).items():
        clean_priority = _task_priority(str(priority))
        normalized[clean_priority] = _bounded_int(
            int(limit),
            0,
            10_000,
            f"per_priority_task_limits.{clean_priority}",
        )
    return normalized


def _bounded_int(value: int, minimum: int, maximum: int, label: str) -> int:
    number = int(value)
    if number < minimum or number > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return number


def _bounded_float(value: float, minimum: float, maximum: float, label: str) -> float:
    number = float(value)
    if number < minimum or number > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return number


def _process_rss_bytes() -> Optional[int]:
    if os.name == "posix":
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        return int(usage.ru_maxrss) * 1024
    return None


def _event_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_payload(payload: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
    if depth > 3:
        return {}
    safe: Dict[str, Any] = {}
    for key, value in dict(payload or {}).items():
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
        return _safe_payload(value, depth)
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth + 1) for item in list(value)[:20]]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _safe_payload(value.to_dict(), depth)
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


def _metadata(source: str) -> Dict[str, Any]:
    return {"safe": True, "source": source}
