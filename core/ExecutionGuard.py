from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


EXECUTION_GUARD_STARTED = "started"
EXECUTION_GUARD_COMPLETED = "completed"
EXECUTION_GUARD_FAILED_BEFORE_EXECUTION = "failed_before_execution"
EXECUTION_GUARD_UNCERTAIN = "uncertain_execution_state"
EXECUTION_GUARD_DUPLICATE_COMPLETED = "duplicate_completed"
EXECUTION_GUARD_TOKEN_SCOPE_MISMATCH = "idempotency_token_scope_mismatch"
EXECUTION_GUARD_IN_PROGRESS = "execution_in_progress"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ExecutionGuardRecord:
    token: str
    action_key: str
    status: str
    created_at: str
    updated_at: str
    result: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "action_key": self.action_key,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": _stable_data(self.result),
            "error_message": self.error_message,
            "metadata": _stable_data(self.metadata),
        }


@dataclass(frozen=True)
class ExecutionGuardDecision:
    success: bool
    status: str
    token: str
    action_key: str
    text: str = ""
    error_message: str = ""
    record: Optional[ExecutionGuardRecord] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "token": self.token,
            "action_key": self.action_key,
            "text": self.text,
            "error_message": self.error_message,
            "record": self.record.to_dict() if self.record else None,
            "metadata": _stable_data(self.metadata),
        }


class ExecutionGuard:
    """Bounded local exactly-once guard for confirmed destructive actions."""

    def __init__(self, max_records: int = 100):
        self.max_records = max(1, int(max_records))
        self._records: Dict[str, ExecutionGuardRecord] = {}
        self._order: list[str] = []

    def begin(self, token: str, action_key: str) -> ExecutionGuardDecision:
        clean_token = _clean_token(token)
        clean_action_key = _clean_action_key(action_key)
        if not clean_token:
            return ExecutionGuardDecision(
                success=False,
                status="missing_idempotency_token",
                token="",
                action_key=clean_action_key,
                text="Confirmed action rejected because the idempotency token is missing.",
                error_message="missing_idempotency_token",
                metadata=_metadata(),
            )
        if not clean_action_key:
            return ExecutionGuardDecision(
                success=False,
                status="missing_action_key",
                token=clean_token,
                action_key="",
                text="Confirmed action rejected because the action scope is missing.",
                error_message="missing_action_key",
                metadata=_metadata(),
            )

        existing = self._records.get(clean_token)
        if existing is not None:
            if existing.action_key != clean_action_key:
                return ExecutionGuardDecision(
                    success=False,
                    status=EXECUTION_GUARD_TOKEN_SCOPE_MISMATCH,
                    token=clean_token,
                    action_key=clean_action_key,
                    text="Confirmed action rejected because the token scope does not match.",
                    error_message=EXECUTION_GUARD_TOKEN_SCOPE_MISMATCH,
                    record=existing,
                    metadata=_metadata(),
                )
            if existing.status == EXECUTION_GUARD_COMPLETED:
                return ExecutionGuardDecision(
                    success=True,
                    status=EXECUTION_GUARD_DUPLICATE_COMPLETED,
                    token=clean_token,
                    action_key=clean_action_key,
                    text="Confirmed action already completed; returning recorded result.",
                    record=existing,
                    metadata={**_metadata(), "duplicate": True},
                )
            if existing.status == EXECUTION_GUARD_STARTED:
                return ExecutionGuardDecision(
                    success=False,
                    status=EXECUTION_GUARD_IN_PROGRESS,
                    token=clean_token,
                    action_key=clean_action_key,
                    text="Confirmed action is already in progress.",
                    error_message=EXECUTION_GUARD_IN_PROGRESS,
                    record=existing,
                    metadata=_metadata(),
                )
            return ExecutionGuardDecision(
                success=False,
                status=existing.status,
                token=clean_token,
                action_key=clean_action_key,
                text="Confirmed action cannot be repeated safely.",
                error_message=existing.error_message or existing.status,
                record=existing,
                metadata=_metadata(),
            )

        now = _utc_now()
        record = ExecutionGuardRecord(
            token=clean_token,
            action_key=clean_action_key,
            status=EXECUTION_GUARD_STARTED,
            created_at=now,
            updated_at=now,
            metadata=_metadata(),
        )
        self._store(record)
        return ExecutionGuardDecision(
            success=True,
            status=EXECUTION_GUARD_STARTED,
            token=clean_token,
            action_key=clean_action_key,
            text="Confirmed action execution started.",
            record=record,
            metadata=_metadata(),
        )

    def complete(
        self,
        token: str,
        action_key: str,
        result: Dict[str, Any],
    ) -> ExecutionGuardRecord:
        return self._update(
            token,
            action_key,
            EXECUTION_GUARD_COMPLETED,
            result=result,
        )

    def fail_before_execution(
        self,
        token: str,
        action_key: str,
        result: Dict[str, Any],
        error_message: str,
    ) -> ExecutionGuardRecord:
        return self._update(
            token,
            action_key,
            EXECUTION_GUARD_FAILED_BEFORE_EXECUTION,
            result=result,
            error_message=error_message,
        )

    def mark_uncertain(
        self,
        token: str,
        action_key: str,
        error_message: str,
    ) -> ExecutionGuardRecord:
        return self._update(
            token,
            action_key,
            EXECUTION_GUARD_UNCERTAIN,
            error_message=error_message,
        )

    def get(self, token: str) -> Optional[ExecutionGuardRecord]:
        return self._records.get(_clean_token(token))

    def records(self) -> list[Dict[str, Any]]:
        return [
            self._records[token].to_dict()
            for token in self._order
            if token in self._records
        ]

    def _update(
        self,
        token: str,
        action_key: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error_message: str = "",
    ) -> ExecutionGuardRecord:
        clean_token = _clean_token(token)
        clean_action_key = _clean_action_key(action_key)
        existing = self._records.get(clean_token)
        created_at = existing.created_at if existing else _utc_now()
        record = ExecutionGuardRecord(
            token=clean_token,
            action_key=clean_action_key,
            status=status,
            created_at=created_at,
            updated_at=_utc_now(),
            result=dict(result or {}),
            error_message=str(error_message or ""),
            metadata=_metadata(),
        )
        self._store(record)
        return record

    def _store(self, record: ExecutionGuardRecord) -> None:
        if record.token not in self._records:
            self._order.append(record.token)
        self._records[record.token] = record
        while len(self._order) > self.max_records:
            oldest = self._order.pop(0)
            self._records.pop(oldest, None)


def _clean_token(value: Any) -> str:
    return str(value or "").strip()


def _clean_action_key(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _metadata() -> Dict[str, Any]:
    return {"safe": True, "source": "execution_guard"}


def _stable_data(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _stable_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stable_data(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _stable_data(to_dict())
    return repr(value)
