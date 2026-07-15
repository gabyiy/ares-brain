from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from memory.schema_migrations import StoreWriteLock


PENDING_OWNER_MEMORY_SCHEMA = "ares.pending_owner_memory_action"
PENDING_OWNER_MEMORY_SCHEMA_VERSION = 1
PENDING_OWNER_MEMORY_TTL_SECONDS = 60
PENDING_OWNER_MEMORY_ENV = "ARES_PENDING_OWNER_MEMORY_ACTION_PATH"

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PENDING_OWNER_MEMORY_ACTION_PATH = (
    BASE_DIR / "data" / "runtime" / "pending_owner_memory_action.json"
).resolve()

PENDING_OPERATION_FORGET_SPECIFIC = "forget_specific"
PENDING_OPERATION_FORGET_TOPIC = "forget_topic"
PENDING_OPERATION_FORGET_ALL_GENERAL = "forget_all_general"
PENDING_OPERATION_FORGET_KEYED_FACT = "forget_keyed_fact"
PENDING_OPERATIONS = {
    PENDING_OPERATION_FORGET_SPECIFIC,
    PENDING_OPERATION_FORGET_TOPIC,
    PENDING_OPERATION_FORGET_ALL_GENERAL,
    PENDING_OPERATION_FORGET_KEYED_FACT,
}

PENDING_TARGET_GENERAL_MEMORY = "general_memory"
PENDING_TARGET_GENERAL_MEMORIES = "general_memories"
PENDING_TARGET_KEYED_FACT = "keyed_fact"
PENDING_TARGET_KINDS = {
    PENDING_TARGET_GENERAL_MEMORY,
    PENDING_TARGET_GENERAL_MEMORIES,
    PENDING_TARGET_KEYED_FACT,
}

MAX_PENDING_TARGET_IDS = 100
MAX_PENDING_SUMMARY_LENGTH = 360
MAX_PENDING_REQUEST_LENGTH = 320
MAX_PENDING_TOPIC_LENGTH = 64
_MEMORY_ID_PATTERN = re.compile(r"^mem-[a-f0-9]{16}$")
_ACTION_ID_PATTERN = re.compile(r"^pending-[a-f0-9]{32}$")
_KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")


Clock = Callable[[], datetime]
Replace = Callable[[str, str], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PendingOwnerMemoryAction:
    action_id: str
    owner_id: str
    operation: str
    target_kind: str
    target_ids: Tuple[str, ...] = ()
    target_key: str = ""
    target_revision: str = ""
    topic: str = ""
    candidate_count: int = 0
    summary: str = ""
    created_at: str = ""
    expires_at: str = ""
    status: str = "pending"
    normalized_request: str = ""

    def to_dict(self, *, include_targets: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "action_id": self.action_id,
            "owner_id": self.owner_id,
            "operation": self.operation,
            "target_kind": self.target_kind,
            "target_key": self.target_key,
            "target_revision": self.target_revision,
            "topic": self.topic,
            "candidate_count": self.candidate_count,
            "summary": self.summary,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "normalized_request": self.normalized_request,
        }
        if include_targets:
            payload["target_ids"] = list(self.target_ids)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PendingOwnerMemoryAction":
        if not isinstance(payload, Mapping):
            raise ValueError("Pending owner-memory action data must be an object")
        required = {
            "action_id",
            "owner_id",
            "operation",
            "target_kind",
            "target_ids",
            "target_key",
            "target_revision",
            "topic",
            "candidate_count",
            "summary",
            "created_at",
            "expires_at",
            "status",
            "normalized_request",
        }
        if set(payload) != required:
            raise ValueError("Pending owner-memory action fields are malformed")
        target_ids = payload.get("target_ids")
        if not isinstance(target_ids, list) or any(not isinstance(item, str) for item in target_ids):
            raise ValueError("Pending owner-memory target ids must be a string list")
        action = cls(
            action_id=str(payload.get("action_id") or ""),
            owner_id=str(payload.get("owner_id") or ""),
            operation=str(payload.get("operation") or ""),
            target_kind=str(payload.get("target_kind") or ""),
            target_ids=tuple(target_ids),
            target_key=str(payload.get("target_key") or ""),
            target_revision=str(payload.get("target_revision") or ""),
            topic=str(payload.get("topic") or ""),
            candidate_count=payload.get("candidate_count"),
            summary=str(payload.get("summary") or ""),
            created_at=str(payload.get("created_at") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            status=str(payload.get("status") or ""),
            normalized_request=str(payload.get("normalized_request") or ""),
        )
        validate_pending_owner_memory_action(action)
        return action


@dataclass(frozen=True)
class PendingOwnerMemoryStateResult:
    success: bool
    status: str
    action: Optional[PendingOwnerMemoryAction] = None
    error_code: str = ""
    error_message: str = ""
    path: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_targets: bool = False) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "path": self.path,
            "action": self.action.to_dict(include_targets=include_targets) if self.action else None,
            "metadata": dict(self.metadata),
        }


class PendingOwnerMemoryActionStore:
    """Atomic, expiring runtime state for cross-process owner-memory confirmation."""

    def __init__(
        self,
        path: Optional[Path | str] = None,
        *,
        owner_id: str = "primary_owner",
        ttl_seconds: int = PENDING_OWNER_MEMORY_TTL_SECONDS,
        clock: Clock = _utc_now,
        replace_func: Replace = os.replace,
    ):
        self.path = resolve_pending_owner_memory_path(path)
        self.owner_id = str(owner_id or "primary_owner")
        self.ttl_seconds = max(1, min(300, int(ttl_seconds)))
        self._clock = clock
        self._replace = replace_func

    def create(
        self,
        *,
        operation: str,
        target_kind: str,
        target_ids: Iterable[str] = (),
        target_key: str = "",
        target_revision: str = "",
        topic: str = "",
        candidate_count: int,
        summary: str,
        normalized_request: str,
    ) -> PendingOwnerMemoryStateResult:
        now = self._now()
        replaced_previous = self.path.exists()
        action = PendingOwnerMemoryAction(
            action_id=f"pending-{uuid.uuid4().hex}",
            owner_id=self.owner_id,
            operation=str(operation or ""),
            target_kind=str(target_kind or ""),
            target_ids=tuple(dict.fromkeys(str(item or "") for item in target_ids)),
            target_key=str(target_key or ""),
            target_revision=str(target_revision or ""),
            topic=_clean_optional_text(topic, MAX_PENDING_TOPIC_LENGTH),
            candidate_count=int(candidate_count),
            summary=_clean_text(summary, MAX_PENDING_SUMMARY_LENGTH),
            created_at=_format_utc(now),
            expires_at=_format_utc(now + timedelta(seconds=self.ttl_seconds)),
            status="pending",
            normalized_request=_clean_text(normalized_request, MAX_PENDING_REQUEST_LENGTH),
        )
        try:
            validate_pending_owner_memory_action(action)
            envelope = self._envelope(action)
            with StoreWriteLock(self.path):
                self._atomic_write(envelope)
        except Exception as error:
            return self._failure("write_failed", "Pending owner-memory action could not be stored.", error)
        return PendingOwnerMemoryStateResult(
            True,
            "created",
            action=action,
            path=str(self.path),
            metadata={"expires_at": action.expires_at, "replaced_previous": replaced_previous},
        )

    def read(self, *, cleanup_invalid: bool = False, cleanup_expired: bool = False) -> PendingOwnerMemoryStateResult:
        if not self.path.exists():
            return PendingOwnerMemoryStateResult(True, "missing", path=str(self.path))
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            action = self._validate_envelope(payload)
        except Exception as error:
            if cleanup_invalid:
                self._remove_safely()
            return self._failure("invalid_pending_state", "Pending owner-memory action is invalid.", error, status="invalid")
        if self._now() >= _parse_utc(action.expires_at):
            if cleanup_expired:
                self._remove_safely()
            return PendingOwnerMemoryStateResult(
                True,
                "expired",
                action=action,
                path=str(self.path),
                metadata={"expired_at": action.expires_at},
            )
        return PendingOwnerMemoryStateResult(True, "active", action=action, path=str(self.path))

    def clear(self) -> PendingOwnerMemoryStateResult:
        existed = self.path.exists()
        try:
            with StoreWriteLock(self.path):
                self.path.unlink(missing_ok=True)
                self._temp_path().unlink(missing_ok=True)
        except Exception as error:
            return self._failure("clear_failed", "Pending owner-memory action could not be cleared.", error)
        return PendingOwnerMemoryStateResult(
            True,
            "cleared" if existed else "missing",
            path=str(self.path),
        )

    def inspection_report(self) -> Dict[str, Any]:
        result = self.read()
        return {
            "pending_state_path": str(self.path),
            "exists": self.path.exists(),
            "schema_name": PENDING_OWNER_MEMORY_SCHEMA,
            "schema_version": PENDING_OWNER_MEMORY_SCHEMA_VERSION,
            "validation_state": "valid" if result.status in {"active", "expired"} else "missing" if result.status == "missing" else "invalid",
            "status": result.status,
            "pending_action": result.action.to_dict(include_targets=True) if result.action else None,
            "error_code": result.error_code,
            "error_message": result.error_message,
        }

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise ValueError("Pending owner-memory clock must return datetime")
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    def _envelope(self, action: PendingOwnerMemoryAction) -> Dict[str, Any]:
        return {
            "schema_name": PENDING_OWNER_MEMORY_SCHEMA,
            "schema_version": PENDING_OWNER_MEMORY_SCHEMA_VERSION,
            "created_at": action.created_at,
            "updated_at": action.created_at,
            "data": action.to_dict(include_targets=True),
            "metadata": {
                "owner_id": self.owner_id,
                "purpose": "transient_owner_memory_confirmation",
            },
        }

    def _validate_envelope(self, payload: Any) -> PendingOwnerMemoryAction:
        if not isinstance(payload, Mapping):
            raise ValueError("Pending owner-memory state must be an object")
        required = {"schema_name", "schema_version", "created_at", "updated_at", "data", "metadata"}
        if set(payload) != required:
            raise ValueError("Pending owner-memory envelope fields are malformed")
        if payload.get("schema_name") != PENDING_OWNER_MEMORY_SCHEMA:
            raise ValueError("Wrong pending owner-memory schema")
        if payload.get("schema_version") != PENDING_OWNER_MEMORY_SCHEMA_VERSION:
            raise ValueError("Unsupported pending owner-memory schema version")
        metadata = payload.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("purpose") != "transient_owner_memory_confirmation":
            raise ValueError("Pending owner-memory metadata is invalid")
        action = PendingOwnerMemoryAction.from_dict(payload.get("data"))
        if action.owner_id != self.owner_id or metadata.get("owner_id") != self.owner_id:
            raise ValueError("Pending owner-memory owner is invalid")
        if payload.get("created_at") != action.created_at or payload.get("updated_at") != action.created_at:
            raise ValueError("Pending owner-memory timestamps are inconsistent")
        return action

    def _atomic_write(self, envelope: Dict[str, Any]) -> None:
        self._validate_envelope(envelope)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._temp_path()
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(envelope, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            self._replace(str(temp_path), str(self.path))
            self._validate_envelope(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def _remove_safely(self) -> None:
        try:
            with StoreWriteLock(self.path):
                self.path.unlink(missing_ok=True)
                self._temp_path().unlink(missing_ok=True)
        except Exception:
            return

    def _temp_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".tmp")

    def _failure(
        self,
        code: str,
        message: str,
        error: Exception,
        *,
        status: str = "failed",
    ) -> PendingOwnerMemoryStateResult:
        return PendingOwnerMemoryStateResult(
            False,
            status,
            error_code=code,
            error_message=message,
            path=str(self.path),
            metadata={"error_type": type(error).__name__},
        )


def validate_pending_owner_memory_action(action: PendingOwnerMemoryAction) -> None:
    if not _ACTION_ID_PATTERN.fullmatch(action.action_id):
        raise ValueError("Pending owner-memory action id is invalid")
    if action.owner_id != "primary_owner":
        raise ValueError("Pending owner-memory owner id is invalid")
    if action.operation not in PENDING_OPERATIONS or action.target_kind not in PENDING_TARGET_KINDS:
        raise ValueError("Pending owner-memory operation is invalid")
    if not isinstance(action.candidate_count, int) or isinstance(action.candidate_count, bool):
        raise ValueError("Pending owner-memory candidate count is invalid")
    if action.candidate_count < 1 or action.candidate_count > MAX_PENDING_TARGET_IDS:
        raise ValueError("Pending owner-memory candidate count is out of range")
    if len(action.target_ids) > MAX_PENDING_TARGET_IDS or len(set(action.target_ids)) != len(action.target_ids):
        raise ValueError("Pending owner-memory target ids are invalid")
    if any(not _MEMORY_ID_PATTERN.fullmatch(item) for item in action.target_ids):
        raise ValueError("Pending owner-memory target id is invalid")
    if action.target_kind == PENDING_TARGET_KEYED_FACT:
        if (
            action.target_ids
            or not _KEY_PATTERN.fullmatch(action.target_key)
            or not re.fullmatch(r"[a-f0-9]{64}", action.target_revision)
        ):
            raise ValueError("Pending keyed-fact target is invalid")
    elif action.target_key or action.target_revision or len(action.target_ids) != action.candidate_count:
        raise ValueError("Pending general-memory targets are invalid")
    if action.target_kind == PENDING_TARGET_GENERAL_MEMORY and len(action.target_ids) != 1:
        raise ValueError("Specific pending deletion must contain one target")
    if action.operation == PENDING_OPERATION_FORGET_KEYED_FACT and action.target_kind != PENDING_TARGET_KEYED_FACT:
        raise ValueError("Pending keyed-fact operation target is invalid")
    if action.operation != PENDING_OPERATION_FORGET_KEYED_FACT and action.target_kind == PENDING_TARGET_KEYED_FACT:
        raise ValueError("Pending general-memory operation target is invalid")
    _validate_clean_text(action.summary, MAX_PENDING_SUMMARY_LENGTH, "summary")
    _validate_clean_text(action.normalized_request, MAX_PENDING_REQUEST_LENGTH, "request")
    if action.topic:
        _validate_clean_text(action.topic, MAX_PENDING_TOPIC_LENGTH, "topic")
    if action.status != "pending":
        raise ValueError("Pending owner-memory status is invalid")
    created_at = _parse_utc(action.created_at)
    expires_at = _parse_utc(action.expires_at)
    if expires_at <= created_at or (expires_at - created_at).total_seconds() > 300:
        raise ValueError("Pending owner-memory expiry is invalid")


def resolve_pending_owner_memory_path(path: Optional[Path | str] = None) -> Path:
    configured = str(path or "").strip() or os.getenv(PENDING_OWNER_MEMORY_ENV, "").strip()
    candidate = Path(configured).expanduser() if configured else DEFAULT_PENDING_OWNER_MEMORY_ACTION_PATH
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    return candidate.resolve()


def _clean_text(value: str, maximum: int) -> str:
    source = unicodedata.normalize("NFKC", str(value or ""))
    clean = " ".join(source.strip().split())
    _validate_clean_text(clean, maximum, "text")
    return clean


def _clean_optional_text(value: str, maximum: int) -> str:
    source = unicodedata.normalize("NFKC", str(value or ""))
    clean = " ".join(source.strip().split())
    if not clean:
        return ""
    _validate_clean_text(clean, maximum, "text")
    return clean


def _validate_clean_text(value: str, maximum: int, name: str) -> None:
    if not value or len(value) > maximum:
        raise ValueError(f"Pending owner-memory {name} is invalid")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"Pending owner-memory {name} contains control characters")
