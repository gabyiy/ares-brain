import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Optional

from memory.schema_migrations import (
    DEFAULT_STALE_LOCK_SECONDS,
    MigrationError,
    SCHEMA_EVENT_HISTORY,
    load_store_data,
    save_store_data,
)


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_EVENT_HISTORY_PATH = DATA_DIR / "event_history.json"
DEFAULT_MAX_HISTORY = 100
EVENT_PRIORITIES = {"low", "normal", "high", "critical"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_history_path_from_env() -> Optional[Path]:
    path = os.environ.get("ARES_EVENT_HISTORY_PATH", "").strip()
    return Path(path) if path else None


@dataclass(frozen=True)
class EventHistoryRecord:
    timestamp: str
    source: str
    type: str
    priority: str
    decision: str
    event: Dict[str, Any]
    result: Dict[str, Any]

    @classmethod
    def from_event_result(cls, event: Any, result: Any):
        event_data = _event_to_dict(event)
        result_data = _result_to_dict(result)
        source = _clean_required(event_data.get("source"), "Event source")
        event_type = _clean_required(event_data.get("type") or event_data.get("name"), "Event type")
        priority = _clean_priority(event_data.get("priority") or "normal")
        timestamp = str(event_data.get("timestamp") or _utc_now())
        decision = str(result_data.get("decision") or "recorded").strip().lower() or "recorded"

        normalized_event = dict(event_data)
        normalized_event["source"] = source
        normalized_event["type"] = event_type
        normalized_event["priority"] = priority
        normalized_event["timestamp"] = timestamp

        return cls(
            timestamp=timestamp,
            source=source,
            type=event_type,
            priority=priority,
            decision=decision,
            event=normalized_event,
            result=result_data,
        )

    @classmethod
    def from_dict(cls, entry: Dict[str, Any]):
        event_data = dict(entry.get("event") or {})
        result_data = dict(entry.get("result") or {})
        source = _clean_required(entry.get("source") or event_data.get("source"), "Event source")
        event_type = _clean_required(
            entry.get("type") or event_data.get("type") or event_data.get("name"),
            "Event type",
        )
        priority = _clean_priority(entry.get("priority") or event_data.get("priority") or "normal")
        timestamp = str(entry.get("timestamp") or event_data.get("timestamp") or _utc_now())
        decision = str(entry.get("decision") or result_data.get("decision") or "recorded").strip().lower() or "recorded"

        event_data["source"] = source
        event_data["type"] = event_type
        event_data["priority"] = priority
        event_data["timestamp"] = timestamp

        return cls(
            timestamp=timestamp,
            source=source,
            type=event_type,
            priority=priority,
            decision=decision,
            event=event_data,
            result=result_data,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "type": self.type,
            "priority": self.priority,
            "decision": self.decision,
            "event": dict(self.event),
            "result": dict(self.result),
        }


class EventHistoryStore:
    """Persistent local store for internal event decisions and results."""

    def __init__(
        self,
        path: Optional[Path] = None,
        max_records: int = DEFAULT_MAX_HISTORY,
        *,
        warning_callback: Optional[Callable[[str], None]] = None,
        recover_dead_owner_lock: bool = True,
        stale_lock_seconds: float = DEFAULT_STALE_LOCK_SECONDS,
        lock_process_alive: Optional[Callable[[int], Optional[bool]]] = None,
    ):
        self.path = Path(path) if path else (_event_history_path_from_env() or DEFAULT_EVENT_HISTORY_PATH)
        self.max_records = max(0, int(max_records))
        self._warning_callback = warning_callback or _default_warning
        if not isinstance(recover_dead_owner_lock, bool):
            raise ValueError("recover_dead_owner_lock must be a boolean")
        if (
            isinstance(stale_lock_seconds, bool)
            or not isinstance(stale_lock_seconds, (int, float))
            or not 1.0 <= float(stale_lock_seconds) <= 86400.0
        ):
            raise ValueError("stale_lock_seconds must be between 1 and 86400")
        self._recover_dead_owner_lock = recover_dead_owner_lock
        self._stale_lock_seconds = float(stale_lock_seconds)
        self._lock_process_alive = lock_process_alive
        self._lock = RLock()
        self._dropped_event_count = 0
        self._reported_failures: set[str] = set()

    @property
    def dropped_event_count(self) -> int:
        with self._lock:
            return self._dropped_event_count

    def add(self, event: Any, result: Any) -> Optional[EventHistoryRecord]:
        record = EventHistoryRecord.from_event_result(event, result)
        with self._lock:
            try:
                records = self._load()
                records.append(record)
                self._save(self._bounded(records))
            except (MigrationError, OSError) as error:
                self._record_dropped_event(error)
                return None
            return record

    def list(self) -> List[EventHistoryRecord]:
        with self._lock:
            return self._load()

    def recent(
        self,
        source: Optional[str] = None,
        type: Optional[str] = None,
        priority: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[EventHistoryRecord]:
        records = self.list()
        clean_source = _clean_optional(source)
        clean_type = _clean_optional(type)
        clean_priority = _clean_priority(priority) if priority is not None else None

        if clean_source:
            records = [record for record in records if record.source == clean_source]
        if clean_type:
            records = [record for record in records if record.type == clean_type]
        if clean_priority:
            records = [record for record in records if record.priority == clean_priority]

        records = list(reversed(records))
        if limit is not None:
            records = records[: max(0, int(limit))]
        return records

    def clear(self) -> None:
        with self._lock:
            self._save([])

    def _load(self) -> List[EventHistoryRecord]:
        data = load_store_data(self.path, SCHEMA_EVENT_HISTORY, [])

        records = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            try:
                records.append(EventHistoryRecord.from_dict(entry))
            except ValueError:
                continue
        return self._bounded(records)

    def _save(self, records: List[EventHistoryRecord]) -> None:
        payload = [record.to_dict() for record in self._bounded(records)]
        save_store_data(
            self.path,
            SCHEMA_EVENT_HISTORY,
            payload,
            recover_if_owner_dead=self._recover_dead_owner_lock,
            stale_lock_seconds=self._stale_lock_seconds,
            lock_owner_kind="event_history_append",
            lock_process_alive=self._lock_process_alive,
        )

    def _bounded(self, records: List[EventHistoryRecord]) -> List[EventHistoryRecord]:
        if self.max_records == 0:
            return []
        return list(records)[-self.max_records :]

    def _record_dropped_event(self, error: Exception) -> None:
        self._dropped_event_count += 1
        status = error.status if isinstance(error, MigrationError) else type(error).__name__
        reason = (
            "store locked"
            if isinstance(error, MigrationError) and error.status == "store_locked"
            else f"{status}: {str(error)[:160]}"
        )
        warning = f"WARNING: event history append skipped: {reason}: {self.path}"
        warning_key = f"{status}:{str(error)[:160]}"
        if warning_key in self._reported_failures:
            return
        self._reported_failures.add(warning_key)
        self._warning_callback(warning)


def _default_warning(message: str) -> None:
    print(message, file=sys.stderr)


def _event_to_dict(event: Any) -> Dict[str, Any]:
    if isinstance(event, dict):
        return dict(event)
    to_dict = getattr(event, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            return dict(data)
    return {
        "source": getattr(event, "source", ""),
        "type": getattr(event, "type", getattr(event, "name", "")),
        "priority": getattr(event, "priority", "normal"),
        "payload": dict(getattr(event, "payload", {}) or {}),
        "timestamp": getattr(event, "timestamp", _utc_now()),
    }


def _result_to_dict(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            return dict(data)
    return {
        "success": bool(getattr(result, "success", False)),
        "decision": str(getattr(result, "decision", "") or ""),
        "text": str(getattr(result, "text", "") or ""),
        "data": dict(getattr(result, "data", {}) or {}),
        "error_message": str(getattr(result, "error_message", "") or ""),
        "metadata": dict(getattr(result, "metadata", {}) or {}),
    }


def _clean_required(value: Any, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{label} is required")
    return clean


def _clean_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return str(value or "").strip() or None


def _clean_priority(priority: Any) -> str:
    clean = str(priority or "").strip().lower()
    if clean not in EVENT_PRIORITIES:
        raise ValueError(f"Invalid event priority: {priority}")
    return clean
