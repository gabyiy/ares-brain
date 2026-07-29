from __future__ import annotations

from contextlib import contextmanager
import signal
from threading import RLock, current_thread, main_thread
from typing import Callable, Iterator, Optional


class ForegroundTerminationRequested(BaseException):
    """Raised on SIGINT/SIGTERM after bounded child cancellation is requested."""

    def __init__(self, signum: int, reason: str = "foreground_signal") -> None:
        self.signum = int(signum)
        self.reason = str(reason or "foreground_signal")[:120]
        super().__init__(f"{self.reason}:{self.signum}")


class ForegroundSignalCoordinator:
    """One idempotent cancellation owner for a foreground ARES process.

    Signal handlers only request cancellation from registered adapters and then
    unwind the main thread. Adapter cleanup remains responsible for bounded
    TERM/KILL/reap behavior and may safely be called again by final cleanup.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._callbacks: list[Callable[[str], object]] = []
        self._requested = False
        self._signum = 0
        self._reason = ""

    @property
    def cancellation_requested(self) -> bool:
        with self._lock:
            return self._requested

    @property
    def signum(self) -> int:
        with self._lock:
            return self._signum

    def register(self, callback: Callable[[str], object]) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("foreground cancellation callback must be callable")
        with self._lock:
            self._callbacks.append(callback)

        def unregister() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

        return unregister

    def request(self, *, signum: int = 0, reason: str = "foreground_cancelled") -> None:
        clean_reason = str(reason or "foreground_cancelled")[:120]
        with self._lock:
            first_request = not self._requested
            self._requested = True
            if signum:
                self._signum = int(signum)
            self._reason = clean_reason
            callbacks = tuple(reversed(self._callbacks))
        if not first_request:
            return
        for callback in callbacks:
            try:
                callback(clean_reason)
            except (OSError, RuntimeError, TimeoutError, TypeError, ValueError):
                # Top-level final cleanup runs again after the signal unwinds.
                continue

    def raise_if_requested(self) -> None:
        with self._lock:
            requested = self._requested
            signum = self._signum
            reason = self._reason
        if requested:
            raise ForegroundTerminationRequested(signum, reason)

    @contextmanager
    def signal_scope(self) -> Iterator["ForegroundSignalCoordinator"]:
        if current_thread() is not main_thread():
            yield self
            return
        supported = tuple(
            value
            for value in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None))
            if value is not None
        )
        previous: dict[int, object] = {}

        def handler(signum: int, _frame: Optional[object]) -> None:
            self.request(signum=int(signum), reason=f"signal_{int(signum)}")
            raise ForegroundTerminationRequested(int(signum), f"signal_{int(signum)}")

        try:
            for signum in supported:
                previous[int(signum)] = signal.getsignal(signum)
                signal.signal(signum, handler)
            yield self
        finally:
            for signum, old_handler in previous.items():
                signal.signal(signum, old_handler)
