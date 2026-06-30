from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Dict, List, Optional


EventHandler = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    name: str
    payload: Dict[str, Any] = field(default_factory=dict)
    source: str = "ares"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    def __post_init__(self):
        object.__setattr__(self, "payload", dict(self.payload or {}))


class EventBus:
    """
    In-process event bus for ARES modules.

    Subscribers can listen to a specific event name or "*" for every event.
    Handler failures are isolated by default so one listener cannot stop the
    rest of the system. Set raise_handler_errors=True for tests/debugging.
    """

    def __init__(self, max_history: int = 100, raise_handler_errors: bool = False):
        self.max_history = max(0, int(max_history))
        self.raise_handler_errors = bool(raise_handler_errors)
        self._subscribers: Dict[str, List[EventHandler]] = {}
        self._history: List[Event] = []
        self._lock = RLock()

    def subscribe(self, event_name: str, handler: EventHandler):
        event_name = self._clean_event_name(event_name)
        if not callable(handler):
            raise TypeError("Event handler must be callable")

        with self._lock:
            self._subscribers.setdefault(event_name, []).append(handler)

        def unsubscribe():
            self.unsubscribe(event_name, handler)

        return unsubscribe

    def unsubscribe(self, event_name: str, handler: EventHandler):
        event_name = self._clean_event_name(event_name)
        with self._lock:
            handlers = self._subscribers.get(event_name, [])
            if handler in handlers:
                handlers.remove(handler)
            if not handlers:
                self._subscribers.pop(event_name, None)

    def publish(self, name: str, payload: Optional[Dict[str, Any]] = None, source: str = "ares"):
        event = Event(name=self._clean_event_name(name), payload=payload or {}, source=source)

        with self._lock:
            if self.max_history:
                self._history.append(event)
                self._history = self._history[-self.max_history :]

            handlers = list(self._subscribers.get(event.name, []))
            handlers.extend(self._subscribers.get("*", []))

        for handler in handlers:
            try:
                handler(event)
            except Exception:
                if self.raise_handler_errors:
                    raise

        return event

    def history(self, event_name: Optional[str] = None, limit: Optional[int] = None):
        with self._lock:
            events = list(self._history)

        if event_name:
            clean_name = self._clean_event_name(event_name)
            events = [event for event in events if event.name == clean_name]

        if limit is not None:
            events = events[-max(0, int(limit)) :]

        return events

    def clear_history(self):
        with self._lock:
            self._history.clear()

    @staticmethod
    def _clean_event_name(name: str) -> str:
        clean = (name or "").strip()
        if not clean:
            raise ValueError("Event name is required")
        return clean


_GLOBAL_BUS = EventBus()


def get_global_bus() -> EventBus:
    return _GLOBAL_BUS
