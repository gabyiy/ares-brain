from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
import re
from threading import RLock
from typing import Any, Callable, Dict, List, Mapping, Optional
from uuid import uuid4

from core.Contracts import (
    CONTRACT_BRAIN_SESSION_TRANSITION_REQUEST,
    BrainSessionSnapshotV1,
    BrainSessionTransitionRequestV1,
    new_correlation_id,
    validate_contract,
)
from core.EventBus import PRIORITY_CRITICAL, PRIORITY_NORMAL, Event, EventBus
from memory.schema_migrations import MigrationError


BRAIN_BOOTING = "BOOTING"
BRAIN_INITIALIZING = "INITIALIZING"
BRAIN_STANDBY = "STANDBY"
BRAIN_ACTIVE = "ACTIVE"
BRAIN_PROCESSING = "PROCESSING"
BRAIN_RESPONDING = "RESPONDING"
BRAIN_RETURNING_TO_STANDBY = "RETURNING_TO_STANDBY"
BRAIN_SHUTTING_DOWN = "SHUTTING_DOWN"
BRAIN_STOPPED = "STOPPED"
BRAIN_ERROR = "ERROR"

BRAIN_SESSION_STATES = (
    BRAIN_BOOTING,
    BRAIN_INITIALIZING,
    BRAIN_STANDBY,
    BRAIN_ACTIVE,
    BRAIN_PROCESSING,
    BRAIN_RESPONDING,
    BRAIN_RETURNING_TO_STANDBY,
    BRAIN_SHUTTING_DOWN,
    BRAIN_STOPPED,
    BRAIN_ERROR,
)

DEFAULT_INACTIVITY_TIMEOUT_SECONDS = 30.0
MIN_INACTIVITY_TIMEOUT_SECONDS = 1.0
MAX_INACTIVITY_TIMEOUT_SECONDS = 3600.0
DEFAULT_MAXIMUM_CONSECUTIVE_FAILURES = 3
MIN_MAXIMUM_CONSECUTIVE_FAILURES = 1
MAX_MAXIMUM_CONSECUTIVE_FAILURES = 20
MAX_TRANSITION_HISTORY = 200
MAX_RECENT_SESSION_IDS = 256

EVENT_BRAIN_BOOT_STARTED = "brain_boot_started"
EVENT_BRAIN_INITIALIZATION_STARTED = "brain_initialization_started"
EVENT_BRAIN_STANDBY_ENTERED = "brain_standby_entered"
EVENT_BRAIN_SESSION_ACTIVATED = "brain_session_activated"
EVENT_BRAIN_PROCESSING_STARTED = "brain_processing_started"
EVENT_BRAIN_RESPONSE_STARTED = "brain_response_started"
EVENT_BRAIN_SESSION_ACTIVITY_RECORDED = "brain_session_activity_recorded"
EVENT_BRAIN_RETURNING_TO_STANDBY = "brain_returning_to_standby"
EVENT_BRAIN_SHUTDOWN_STARTED = "brain_shutdown_started"
EVENT_BRAIN_STOPPED = "brain_stopped"
EVENT_BRAIN_STATE_TRANSITION_REJECTED = "brain_state_transition_rejected"
EVENT_BRAIN_LIFECYCLE_ERROR = "brain_lifecycle_error"

_SAFE_REASON_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")
_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")

_LEGAL_TRANSITIONS = {
    BRAIN_STOPPED: {BRAIN_BOOTING},
    BRAIN_BOOTING: {BRAIN_INITIALIZING, BRAIN_SHUTTING_DOWN},
    BRAIN_INITIALIZING: {BRAIN_STANDBY, BRAIN_SHUTTING_DOWN},
    BRAIN_STANDBY: {BRAIN_ACTIVE, BRAIN_SHUTTING_DOWN},
    BRAIN_ACTIVE: {
        BRAIN_PROCESSING,
        BRAIN_RETURNING_TO_STANDBY,
        BRAIN_SHUTTING_DOWN,
    },
    BRAIN_PROCESSING: {
        BRAIN_RESPONDING,
        BRAIN_RETURNING_TO_STANDBY,
        BRAIN_SHUTTING_DOWN,
    },
    BRAIN_RESPONDING: {
        BRAIN_ACTIVE,
        BRAIN_RETURNING_TO_STANDBY,
        BRAIN_SHUTTING_DOWN,
    },
    BRAIN_RETURNING_TO_STANDBY: {BRAIN_STANDBY, BRAIN_SHUTTING_DOWN},
    BRAIN_ERROR: {BRAIN_RETURNING_TO_STANDBY, BRAIN_SHUTTING_DOWN},
    BRAIN_SHUTTING_DOWN: {BRAIN_STOPPED},
}

_ERROR_CAPABLE_STATES = {
    BRAIN_BOOTING,
    BRAIN_INITIALIZING,
    BRAIN_STANDBY,
    BRAIN_ACTIVE,
    BRAIN_PROCESSING,
    BRAIN_RESPONDING,
    BRAIN_RETURNING_TO_STANDBY,
}

_SESSION_ACTIVITY_STATES = {
    BRAIN_ACTIVE,
    BRAIN_PROCESSING,
    BRAIN_RESPONDING,
}

_TRANSITION_EVENTS = {
    (BRAIN_STOPPED, BRAIN_BOOTING): EVENT_BRAIN_BOOT_STARTED,
    (BRAIN_BOOTING, BRAIN_INITIALIZING): EVENT_BRAIN_INITIALIZATION_STARTED,
    (BRAIN_INITIALIZING, BRAIN_STANDBY): EVENT_BRAIN_STANDBY_ENTERED,
    (BRAIN_RETURNING_TO_STANDBY, BRAIN_STANDBY): EVENT_BRAIN_STANDBY_ENTERED,
    (BRAIN_STANDBY, BRAIN_ACTIVE): EVENT_BRAIN_SESSION_ACTIVATED,
    (BRAIN_ACTIVE, BRAIN_PROCESSING): EVENT_BRAIN_PROCESSING_STARTED,
    (BRAIN_PROCESSING, BRAIN_RESPONDING): EVENT_BRAIN_RESPONSE_STARTED,
    (BRAIN_RESPONDING, BRAIN_ACTIVE): EVENT_BRAIN_SESSION_ACTIVITY_RECORDED,
    (BRAIN_ACTIVE, BRAIN_RETURNING_TO_STANDBY): EVENT_BRAIN_RETURNING_TO_STANDBY,
    (BRAIN_PROCESSING, BRAIN_RETURNING_TO_STANDBY): EVENT_BRAIN_RETURNING_TO_STANDBY,
    (BRAIN_RESPONDING, BRAIN_RETURNING_TO_STANDBY): EVENT_BRAIN_RETURNING_TO_STANDBY,
    (BRAIN_ERROR, BRAIN_RETURNING_TO_STANDBY): EVENT_BRAIN_RETURNING_TO_STANDBY,
    (BRAIN_SHUTTING_DOWN, BRAIN_STOPPED): EVENT_BRAIN_STOPPED,
}


@dataclass(frozen=True)
class BrainSessionConfig:
    inactivity_timeout_seconds: float = DEFAULT_INACTIVITY_TIMEOUT_SECONDS
    maximum_consecutive_failures: int = DEFAULT_MAXIMUM_CONSECUTIVE_FAILURES

    def __post_init__(self) -> None:
        timeout = _validated_timeout(self.inactivity_timeout_seconds)
        maximum_failures = _validated_failure_limit(self.maximum_consecutive_failures)
        object.__setattr__(self, "inactivity_timeout_seconds", timeout)
        object.__setattr__(self, "maximum_consecutive_failures", maximum_failures)

    @classmethod
    def from_mapping(
        cls,
        value: Optional["BrainSessionConfig | Mapping[str, Any]"] = None,
    ) -> "BrainSessionConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("brain_session configuration must be a mapping")
        allowed = {"inactivity_timeout_seconds", "maximum_consecutive_failures"}
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValueError(f"Unknown brain_session configuration fields: {', '.join(unknown)}")
        return cls(**dict(value))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "inactivity_timeout_seconds": self.inactivity_timeout_seconds,
            "maximum_consecutive_failures": self.maximum_consecutive_failures,
        }


@dataclass(frozen=True)
class BrainStateTransitionRecord:
    source_state: str
    target_state: str
    reason: str
    timestamp: str
    correlation_id: str
    session_id: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "source_state": self.source_state,
            "target_state": self.target_state,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
        }


class BrainSessionManager:
    """Central deterministic Brain lifecycle controller with no runtime loop."""

    def __init__(
        self,
        config: Optional[BrainSessionConfig | Mapping[str, Any]] = None,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        session_id_factory: Optional[Callable[[], str]] = None,
        event_bus: Optional[EventBus] = None,
        event_history_store: Any = None,
    ) -> None:
        self.config = BrainSessionConfig.from_mapping(config)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._session_id_factory = session_id_factory or (
            lambda: f"brain-session-{uuid4()}"
        )
        self._event_bus = event_bus or EventBus(max_history=MAX_TRANSITION_HISTORY)
        self._event_history_store = event_history_store
        self._lock = RLock()
        self._last_clock_at: Optional[datetime] = None
        now = self._now()
        self._state = BRAIN_STOPPED
        self._previous_state = ""
        self._entered_at = now
        self._last_activity_at: Optional[datetime] = None
        self._inactivity_deadline_at: Optional[datetime] = None
        self._session_id = ""
        self._last_correlation_id = new_correlation_id("brain-state")
        self._transition_reason = "manager_created"
        self._consecutive_failure_count = 0
        self._history: List[BrainStateTransitionRecord] = []
        self._event_history_failures: List[Dict[str, str]] = []
        self._recent_session_ids: deque[str] = deque(maxlen=MAX_RECENT_SESSION_IDS)

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    def snapshot(self) -> BrainSessionSnapshotV1:
        with self._lock:
            return self._snapshot_locked()

    def current_session_info(self) -> BrainSessionSnapshotV1:
        return self.snapshot()

    def history(self) -> List[BrainStateTransitionRecord]:
        with self._lock:
            return list(self._history)

    def events(self, event_type: Optional[str] = None) -> List[Event]:
        return self._event_bus.history(event_type=event_type)

    def event_history_failures(self) -> List[Dict[str, str]]:
        with self._lock:
            return [dict(item) for item in self._event_history_failures]

    def begin_boot(self, *, correlation_id: str = "", reason: str = "boot_requested") -> BrainSessionSnapshotV1:
        return self._request(BRAIN_BOOTING, correlation_id, reason)

    def begin_initialization(
        self,
        *,
        correlation_id: str = "",
        reason: str = "initialization_requested",
    ) -> BrainSessionSnapshotV1:
        return self._request(BRAIN_INITIALIZING, correlation_id, reason)

    def enter_standby(
        self,
        *,
        correlation_id: str = "",
        reason: str = "standby_ready",
    ) -> BrainSessionSnapshotV1:
        return self._request(BRAIN_STANDBY, correlation_id, reason)

    def activate_session(
        self,
        *,
        correlation_id: str = "",
        reason: str = "owner_session_requested",
    ) -> BrainSessionSnapshotV1:
        return self._request(BRAIN_ACTIVE, correlation_id, reason)

    def begin_processing(
        self,
        *,
        correlation_id: str = "",
        reason: str = "request_processing_started",
    ) -> BrainSessionSnapshotV1:
        return self._request(BRAIN_PROCESSING, correlation_id, reason)

    def begin_responding(
        self,
        *,
        correlation_id: str = "",
        reason: str = "response_started",
    ) -> BrainSessionSnapshotV1:
        return self._request(BRAIN_RESPONDING, correlation_id, reason)

    def finish_response(
        self,
        *,
        correlation_id: str = "",
        reason: str = "response_completed",
    ) -> BrainSessionSnapshotV1:
        return self._request(BRAIN_ACTIVE, correlation_id, reason)

    def request_return_to_standby(
        self,
        *,
        correlation_id: str = "",
        reason: str = "return_to_standby_requested",
    ) -> BrainSessionSnapshotV1:
        return self._request(BRAIN_RETURNING_TO_STANDBY, correlation_id, reason)

    def complete_return_to_standby(
        self,
        *,
        correlation_id: str = "",
        reason: str = "standby_restored",
    ) -> BrainSessionSnapshotV1:
        return self._request(BRAIN_STANDBY, correlation_id, reason)

    def begin_shutdown(
        self,
        *,
        correlation_id: str = "",
        reason: str = "shutdown_requested",
    ) -> BrainSessionSnapshotV1:
        return self._request(BRAIN_SHUTTING_DOWN, correlation_id, reason)

    def mark_stopped(
        self,
        *,
        correlation_id: str = "",
        reason: str = "shutdown_completed",
    ) -> BrainSessionSnapshotV1:
        return self._request(BRAIN_STOPPED, correlation_id, reason)

    def transition(self, request: BrainSessionTransitionRequestV1 | Dict[str, Any]) -> BrainSessionSnapshotV1:
        try:
            normalized = (
                request
                if isinstance(request, BrainSessionTransitionRequestV1)
                else BrainSessionTransitionRequestV1.from_dict(request)
            )
        except (TypeError, ValueError) as error:
            with self._lock:
                return self._reject_locked(
                    requested_state="",
                    correlation_id=new_correlation_id("brain-transition"),
                    reason="invalid_transition_request",
                    error_code="invalid_transition_contract",
                    error_message=str(error)[:160],
                )
        compatibility = validate_contract(
            normalized,
            expected_contract_name=CONTRACT_BRAIN_SESSION_TRANSITION_REQUEST,
        )
        if not compatibility.success:
            with self._lock:
                return self._reject_locked(
                    requested_state=normalized.requested_state,
                    correlation_id=_safe_correlation_id(normalized.correlation_id),
                    reason="invalid_transition_request",
                    error_code="incompatible_transition_contract",
                    error_message=compatibility.error_message[:160],
                )
        with self._lock:
            return self._transition_locked(
                normalized.requested_state,
                _safe_correlation_id(normalized.correlation_id),
                normalized.reason,
                recovery_safe=normalized.recovery_safe,
            )

    def record_activity(
        self,
        *,
        correlation_id: str = "",
        reason: str = "session_activity",
    ) -> BrainSessionSnapshotV1:
        correlation = _safe_correlation_id(correlation_id)
        with self._lock:
            clean_reason = _safe_reason(reason)
            if not clean_reason:
                return self._reject_locked(
                    requested_state=self._state,
                    correlation_id=correlation,
                    reason="invalid_activity_reason",
                    error_code="invalid_transition_reason",
                    error_message="activity reason must be a bounded machine-safe code",
                )
            if self._state not in _SESSION_ACTIVITY_STATES or not self._session_id:
                return self._reject_locked(
                    requested_state=self._state,
                    correlation_id=correlation,
                    reason=clean_reason,
                    error_code="activity_not_allowed",
                    error_message=f"activity cannot be recorded while state is {self._state}",
                )
            now = self._now()
            self._last_activity_at = now
            self._inactivity_deadline_at = now + timedelta(
                seconds=self.config.inactivity_timeout_seconds
            )
            self._consecutive_failure_count = 0
            self._last_correlation_id = correlation
            self._publish_locked(
                EVENT_BRAIN_SESSION_ACTIVITY_RECORDED,
                correlation,
                self._session_id,
                {
                    "state": self._state,
                    "reason": clean_reason,
                    "activity_at": _timestamp(now),
                    "inactivity_timeout_seconds": self.config.inactivity_timeout_seconds,
                },
            )
            return self._snapshot_locked(
                success=True,
                status="activity_recorded",
                requested_state=self._state,
                source_state=self._state,
                correlation_id=correlation,
                metadata={"activity_reason": clean_reason},
            )

    def inactivity_expired(self, at_time: Optional[datetime] = None) -> bool:
        with self._lock:
            if (
                self._state not in _SESSION_ACTIVITY_STATES
                or not self._session_id
                or self._inactivity_deadline_at is None
            ):
                return False
            now = self._normalize_datetime(at_time) if at_time is not None else self._now()
            return now >= self._inactivity_deadline_at

    def report_failure(
        self,
        *,
        correlation_id: str = "",
        reason: str = "lifecycle_failure",
        unrecoverable: bool = False,
    ) -> BrainSessionSnapshotV1:
        correlation = _safe_correlation_id(correlation_id)
        with self._lock:
            clean_reason = _safe_reason(reason)
            if not clean_reason:
                return self._reject_locked(
                    requested_state=BRAIN_ERROR,
                    correlation_id=correlation,
                    reason="invalid_failure_reason",
                    error_code="invalid_transition_reason",
                    error_message="failure reason must be a bounded machine-safe code",
                )
            if self._state not in _ERROR_CAPABLE_STATES:
                return self._reject_locked(
                    requested_state=BRAIN_ERROR,
                    correlation_id=correlation,
                    reason=clean_reason,
                    error_code="failure_report_not_allowed",
                    error_message=f"failure cannot be reported while state is {self._state}",
                )
            self._consecutive_failure_count = min(
                self._consecutive_failure_count + 1,
                self.config.maximum_consecutive_failures,
            )
            should_enter_error = (
                bool(unrecoverable)
                or self._consecutive_failure_count
                >= self.config.maximum_consecutive_failures
            )
            if should_enter_error:
                transition_result = self._transition_locked(
                    BRAIN_ERROR,
                    correlation,
                    clean_reason,
                    allow_error=True,
                )
                return self._snapshot_locked(
                    success=False,
                    status="lifecycle_error",
                    requested_state=BRAIN_ERROR,
                    source_state=transition_result.source_state,
                    correlation_id=correlation,
                    error_code=(
                        "unrecoverable_lifecycle_failure"
                        if unrecoverable
                        else "maximum_consecutive_failures_reached"
                    ),
                    error_message="brain lifecycle entered ERROR",
                )
            self._publish_locked(
                EVENT_BRAIN_LIFECYCLE_ERROR,
                correlation,
                self._session_id,
                {
                    "state": self._state,
                    "reason": clean_reason,
                    "unrecoverable": False,
                    "consecutive_failure_count": self._consecutive_failure_count,
                    "maximum_consecutive_failures": self.config.maximum_consecutive_failures,
                    "reported_at": _timestamp(self._now()),
                },
                priority=PRIORITY_CRITICAL,
            )
            return self._snapshot_locked(
                success=False,
                status="failure_recorded",
                requested_state=self._state,
                source_state=self._state,
                correlation_id=correlation,
                error_code="recoverable_lifecycle_failure",
                error_message="recoverable lifecycle failure recorded",
                metadata={"failure_reason": clean_reason},
            )

    def recover_to_standby(
        self,
        *,
        recovery_safe: bool,
        correlation_id: str = "",
        reason: str = "explicit_safe_recovery",
    ) -> BrainSessionSnapshotV1:
        correlation = _safe_correlation_id(correlation_id)
        with self._lock:
            if not recovery_safe:
                return self._reject_locked(
                    requested_state=BRAIN_RETURNING_TO_STANDBY,
                    correlation_id=correlation,
                    reason="unsafe_recovery_rejected",
                    error_code="recovery_not_confirmed_safe",
                    error_message="ERROR recovery requires recovery_safe=True",
                )
            returning = self._transition_locked(
                BRAIN_RETURNING_TO_STANDBY,
                correlation,
                reason,
                recovery_safe=True,
            )
            if not returning.success:
                return returning
            return self._transition_locked(
                BRAIN_STANDBY,
                correlation,
                "recovery_completed",
            )

    def _request(
        self,
        requested_state: str,
        correlation_id: str,
        reason: str,
    ) -> BrainSessionSnapshotV1:
        request = BrainSessionTransitionRequestV1(
            requested_state=requested_state,
            reason=reason,
            correlation_id=_safe_correlation_id(correlation_id),
        )
        return self.transition(request)

    def _transition_locked(
        self,
        requested_state: str,
        correlation_id: str,
        reason: str,
        *,
        recovery_safe: bool = False,
        allow_error: bool = False,
    ) -> BrainSessionSnapshotV1:
        target = str(requested_state or "").strip().upper()
        clean_reason = _safe_reason(reason)
        source = self._state
        if target not in BRAIN_SESSION_STATES:
            return self._reject_locked(
                target,
                correlation_id,
                clean_reason or "invalid_target_state",
                "unknown_brain_state",
                f"unknown brain state: {target or '<empty>'}",
            )
        if not clean_reason:
            return self._reject_locked(
                target,
                correlation_id,
                "invalid_transition_reason",
                "invalid_transition_reason",
                "transition reason must be a bounded machine-safe code",
            )
        if target == BRAIN_ERROR and not allow_error:
            return self._reject_locked(
                target,
                correlation_id,
                clean_reason,
                "error_transition_requires_report_failure",
                "ERROR may only be entered through report_failure()",
            )
        if source == BRAIN_ERROR and target == BRAIN_RETURNING_TO_STANDBY and not recovery_safe:
            return self._reject_locked(
                target,
                correlation_id,
                clean_reason,
                "recovery_not_confirmed_safe",
                "ERROR recovery requires explicit safe recovery",
            )
        legal_targets = set(_LEGAL_TRANSITIONS.get(source, set()))
        if allow_error and source in _ERROR_CAPABLE_STATES:
            legal_targets.add(BRAIN_ERROR)
        if target not in legal_targets:
            return self._reject_locked(
                target,
                correlation_id,
                clean_reason,
                "illegal_state_transition",
                f"transition {source} -> {target} is not allowed",
            )

        now = self._now()
        event_session_id = self._session_id
        if source == BRAIN_STANDBY and target == BRAIN_ACTIVE:
            try:
                self._session_id = self._new_unique_session_id_locked()
            except (RuntimeError, ValueError) as error:
                return self._reject_locked(
                    target,
                    correlation_id,
                    clean_reason,
                    "session_id_creation_failed",
                    str(error)[:160],
                )
            event_session_id = self._session_id
        # Only a new owner session starts an inactivity window here. Internal
        # lifecycle progress (PROCESSING -> RESPONDING -> ACTIVE) reflects ARES
        # work and playback, not new owner activity, so it must not extend that
        # window. BrainRuntime records genuine command speech explicitly before
        # beginning command processing.
        if source == BRAIN_STANDBY and target == BRAIN_ACTIVE:
            self._last_activity_at = now
            self._inactivity_deadline_at = now + timedelta(
                seconds=self.config.inactivity_timeout_seconds
            )
        if target == BRAIN_STANDBY:
            event_session_id = self._session_id
            self._session_id = ""
            self._inactivity_deadline_at = None
            self._consecutive_failure_count = 0
        elif target == BRAIN_STOPPED:
            event_session_id = self._session_id
            self._session_id = ""
            self._inactivity_deadline_at = None
            self._consecutive_failure_count = 0
        elif source == BRAIN_RESPONDING and target == BRAIN_ACTIVE:
            self._consecutive_failure_count = 0

        self._previous_state = source
        self._state = target
        self._entered_at = now
        self._transition_reason = clean_reason
        self._last_correlation_id = correlation_id
        self._history.append(
            BrainStateTransitionRecord(
                source_state=source,
                target_state=target,
                reason=clean_reason,
                timestamp=_timestamp(now),
                correlation_id=correlation_id,
                session_id=event_session_id,
            )
        )
        self._history = self._history[-MAX_TRANSITION_HISTORY:]

        event_type = self._event_type_for_transition(source, target)
        self._publish_locked(
            event_type,
            correlation_id,
            event_session_id,
            {
                "source_state": source,
                "target_state": target,
                "reason": clean_reason,
                "transitioned_at": _timestamp(now),
                "consecutive_failure_count": self._consecutive_failure_count,
                "inactivity_timeout_seconds": self.config.inactivity_timeout_seconds,
            },
            priority=(PRIORITY_CRITICAL if target == BRAIN_ERROR else PRIORITY_NORMAL),
        )
        return self._snapshot_locked(
            success=True,
            status="transitioned",
            requested_state=target,
            source_state=source,
            correlation_id=correlation_id,
        )

    def _reject_locked(
        self,
        requested_state: str,
        correlation_id: str,
        reason: str,
        error_code: str,
        error_message: str,
    ) -> BrainSessionSnapshotV1:
        safe_reason = _safe_reason(reason) or "transition_rejected"
        self._publish_locked(
            EVENT_BRAIN_STATE_TRANSITION_REJECTED,
            correlation_id,
            self._session_id,
            {
                "source_state": self._state,
                "requested_state": _safe_state_label(requested_state),
                "reason": safe_reason,
                "rejection_reason": error_code,
            },
            priority=PRIORITY_NORMAL,
        )
        return self._snapshot_locked(
            success=False,
            status="transition_rejected",
            requested_state=_safe_state_label(requested_state),
            source_state=self._state,
            correlation_id=correlation_id,
            error_code=error_code,
            error_message=error_message,
            metadata={"rejection_reason": error_code},
        )

    def _snapshot_locked(
        self,
        *,
        success: bool = True,
        status: str = "current",
        requested_state: str = "",
        source_state: Optional[str] = None,
        correlation_id: str = "",
        error_code: str = "",
        error_message: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> BrainSessionSnapshotV1:
        safe_metadata: Dict[str, Any] = {
            "safe": True,
            "source": "brain_session_manager",
        }
        for key, value in (metadata or {}).items():
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 96:
                safe_metadata[str(key)[:48]] = value
        return BrainSessionSnapshotV1(
            success=success,
            status=status,
            current_state=self._state,
            previous_state=self._previous_state,
            source_state=source_state if source_state is not None else self._state,
            requested_state=requested_state,
            session_id=self._session_id,
            correlation_id=correlation_id or self._last_correlation_id,
            created_at=_timestamp(self._now()),
            entered_at=_timestamp(self._entered_at),
            last_activity_at=(
                _timestamp(self._last_activity_at) if self._last_activity_at else ""
            ),
            inactivity_timeout_seconds=self.config.inactivity_timeout_seconds,
            inactivity_deadline_at=(
                _timestamp(self._inactivity_deadline_at)
                if self._inactivity_deadline_at
                else ""
            ),
            inactivity_expired=self._inactivity_expired_locked(),
            consecutive_failure_count=self._consecutive_failure_count,
            maximum_consecutive_failures=self.config.maximum_consecutive_failures,
            transition_reason=self._transition_reason,
            error_code=error_code,
            error_message=error_message,
            metadata=safe_metadata,
        )

    def _inactivity_expired_locked(self) -> bool:
        return bool(
            self._state in _SESSION_ACTIVITY_STATES
            and self._session_id
            and self._inactivity_deadline_at is not None
            and self._now() >= self._inactivity_deadline_at
        )

    def _event_type_for_transition(self, source: str, target: str) -> str:
        if target == BRAIN_ERROR:
            return EVENT_BRAIN_LIFECYCLE_ERROR
        if target == BRAIN_SHUTTING_DOWN:
            return EVENT_BRAIN_SHUTDOWN_STARTED
        return _TRANSITION_EVENTS[(source, target)]

    def _publish_locked(
        self,
        event_type: str,
        correlation_id: str,
        session_id: str,
        payload: Dict[str, Any],
        *,
        priority: str = PRIORITY_NORMAL,
    ) -> None:
        event = self._event_bus.publish(
            source="brain_session_manager",
            type=event_type,
            payload=dict(payload),
            priority=priority,
            correlation_id=correlation_id,
            session_id=session_id,
            metadata={"safe": True, "contains_private_content": False},
        )
        if self._event_history_store is None:
            return
        try:
            self._event_history_store.add(
                event,
                {
                    "success": True,
                    "decision": "recorded",
                    "status": "brain_lifecycle_event_recorded",
                    "metadata": {"safe": True},
                },
            )
        except (MigrationError, OSError) as error:
            self._event_history_failures.append(
                {
                    "event_type": event_type,
                    "error_type": type(error).__name__,
                    "error_message": str(error)[:160],
                }
            )
            self._event_history_failures = self._event_history_failures[-20:]

    def _new_unique_session_id_locked(self) -> str:
        for _ in range(8):
            candidate = str(self._session_id_factory() or "").strip()
            if not _SAFE_IDENTIFIER_PATTERN.fullmatch(candidate):
                raise ValueError("session_id_factory returned an invalid identifier")
            if candidate not in self._recent_session_ids:
                self._recent_session_ids.append(candidate)
                return candidate
        raise RuntimeError("session_id_factory did not produce a unique identifier")

    def _now(self) -> datetime:
        current = self._normalize_datetime(self._clock())
        if self._last_clock_at is not None and current < self._last_clock_at:
            return self._last_clock_at
        self._last_clock_at = current
        return current

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise ValueError("brain session clock must return datetime")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _validated_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("inactivity_timeout_seconds must be a finite number")
    timeout = float(value)
    if not isfinite(timeout):
        raise ValueError("inactivity_timeout_seconds must be finite")
    if not MIN_INACTIVITY_TIMEOUT_SECONDS <= timeout <= MAX_INACTIVITY_TIMEOUT_SECONDS:
        raise ValueError(
            "inactivity_timeout_seconds must be between "
            f"{MIN_INACTIVITY_TIMEOUT_SECONDS:g} and {MAX_INACTIVITY_TIMEOUT_SECONDS:g}"
        )
    return timeout


def _validated_failure_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("maximum_consecutive_failures must be an integer")
    if not MIN_MAXIMUM_CONSECUTIVE_FAILURES <= value <= MAX_MAXIMUM_CONSECUTIVE_FAILURES:
        raise ValueError(
            "maximum_consecutive_failures must be between "
            f"{MIN_MAXIMUM_CONSECUTIVE_FAILURES} and {MAX_MAXIMUM_CONSECUTIVE_FAILURES}"
        )
    return value


def _safe_reason(value: Any) -> str:
    clean = str(value or "").strip().lower()
    return clean if _SAFE_REASON_PATTERN.fullmatch(clean) else ""


def _safe_correlation_id(value: Any) -> str:
    clean = str(value or "").strip()
    if clean and _SAFE_IDENTIFIER_PATTERN.fullmatch(clean):
        return clean
    return new_correlation_id("brain-transition")


def _safe_state_label(value: Any) -> str:
    clean = str(value or "").strip().upper()
    if clean in BRAIN_SESSION_STATES:
        return clean
    return "INVALID"


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
