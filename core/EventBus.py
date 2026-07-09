from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Dict, List, Optional


PRIORITY_LOW = "low"
PRIORITY_NORMAL = "normal"
PRIORITY_HIGH = "high"
PRIORITY_CRITICAL = "critical"
EVENT_PRIORITIES = (
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PRIORITY_HIGH,
    PRIORITY_CRITICAL,
)
_PRIORITY_RANK = {
    PRIORITY_LOW: 0,
    PRIORITY_NORMAL: 1,
    PRIORITY_HIGH: 2,
    PRIORITY_CRITICAL: 3,
}

EventHandler = Callable[["Event"], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Event:
    source: str
    type: str
    priority: str = PRIORITY_NORMAL
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_utc_now)

    def __post_init__(self):
        object.__setattr__(self, "source", _clean_required(self.source, "Event source"))
        object.__setattr__(self, "type", _clean_required(self.type, "Event type"))
        object.__setattr__(self, "priority", _clean_priority(self.priority))
        object.__setattr__(self, "payload", dict(self.payload or {}))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "type": self.type,
            "priority": self.priority,
            "payload": dict(self.payload),
            "timestamp": self.timestamp,
        }


class EventBus:
    """Internal future city event bus skeleton with no background listener."""

    def __init__(self, max_history: int = 100, raise_handler_errors: bool = False):
        self.max_history = max(0, int(max_history))
        self.raise_handler_errors = bool(raise_handler_errors)
        self._subscribers: Dict[str, List[EventHandler]] = {}
        self._history: List[Event] = []
        self._lock = RLock()

    def subscribe(self, event_type: str, handler: EventHandler):
        clean_type = _clean_required(event_type, "Event type")
        if not callable(handler):
            raise TypeError("Event handler must be callable")

        with self._lock:
            self._subscribers.setdefault(clean_type, []).append(handler)

        def unsubscribe():
            self.unsubscribe(clean_type, handler)

        return unsubscribe

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        clean_type = _clean_required(event_type, "Event type")
        with self._lock:
            handlers = self._subscribers.get(clean_type, [])
            if handler in handlers:
                handlers.remove(handler)
            if not handlers:
                self._subscribers.pop(clean_type, None)

    def publish(
        self,
        source: str,
        type: str,
        payload: Optional[Dict[str, Any]] = None,
        priority: str = PRIORITY_NORMAL,
    ) -> Event:
        event = Event(
            source=source,
            type=type,
            priority=priority,
            payload=payload or {},
        )

        with self._lock:
            if self.max_history:
                self._history.append(event)
                self._history = self._ordered_history()[-self.max_history :]
            handlers = list(self._subscribers.get(event.type, []))

        for handler in handlers:
            try:
                handler(event)
            except Exception:
                if self.raise_handler_errors:
                    raise

        return event

    def history(self, event_type: Optional[str] = None, limit: Optional[int] = None) -> List[Event]:
        with self._lock:
            events = self._ordered_history()

        if event_type:
            clean_type = _clean_required(event_type, "Event type")
            events = [event for event in events if event.type == clean_type]

        if limit is not None:
            events = events[-max(0, int(limit)) :]

        return events

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def _ordered_history(self) -> List[Event]:
        indexed_events = list(enumerate(self._history))
        indexed_events.sort(
            key=lambda item: (-_PRIORITY_RANK[item[1].priority], item[0])
        )
        return [event for _, event in indexed_events]


def _clean_required(value: str, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{label} is required")
    return clean


def _clean_priority(priority: str) -> str:
    clean = str(priority or "").strip().lower()
    if clean not in _PRIORITY_RANK:
        raise ValueError(f"Invalid event priority: {priority}")
    return clean
