from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import os
import select
import sys
from threading import RLock
import time
from typing import Any, Callable, Deque, Dict, Optional, Protocol, Sequence, runtime_checkable


RUNTIME_INPUT_ITEM = "input"
RUNTIME_INPUT_TIMEOUT = "timeout"
RUNTIME_INPUT_END = "end_of_input"
RUNTIME_INPUT_CANCELLED = "cancelled"
RUNTIME_INPUT_FAILED = "failed"
RUNTIME_INPUT_STATUSES = {
    RUNTIME_INPUT_ITEM,
    RUNTIME_INPUT_TIMEOUT,
    RUNTIME_INPUT_END,
    RUNTIME_INPUT_CANCELLED,
    RUNTIME_INPUT_FAILED,
}


@dataclass(frozen=True)
class RuntimeInputResult:
    status: str
    text: str = ""
    error_code: str = ""
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clean_status = str(self.status or "").strip().lower()
        if clean_status not in RUNTIME_INPUT_STATUSES:
            raise ValueError(f"Unsupported runtime input status: {clean_status or '<empty>'}")
        object.__setattr__(self, "status", clean_status)
        object.__setattr__(self, "text", str(self.text or ""))
        object.__setattr__(self, "error_code", str(self.error_code or "").strip())
        object.__setattr__(self, "error_message", str(self.error_message or "").strip()[:160])
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata))

    @classmethod
    def input(cls, text: str) -> "RuntimeInputResult":
        return cls(status=RUNTIME_INPUT_ITEM, text=text)

    @classmethod
    def timeout(cls) -> "RuntimeInputResult":
        return cls(status=RUNTIME_INPUT_TIMEOUT)

    @classmethod
    def end(cls) -> "RuntimeInputResult":
        return cls(status=RUNTIME_INPUT_END)

    @classmethod
    def cancelled(cls) -> "RuntimeInputResult":
        return cls(status=RUNTIME_INPUT_CANCELLED)

    @classmethod
    def failed(cls, error_code: str, error_message: str) -> "RuntimeInputResult":
        return cls(
            status=RUNTIME_INPUT_FAILED,
            error_code=error_code,
            error_message=error_message,
        )


@runtime_checkable
class RuntimeInputAdapter(Protocol):
    def wait_for_input(self, timeout_seconds: float) -> RuntimeInputResult:
        ...


@dataclass(frozen=True)
class RuntimeOutputMessage:
    category: str
    text: str
    correlation_id: str = ""
    session_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        category = str(self.category or "").strip().lower()
        if not category or len(category) > 48:
            raise ValueError("Runtime output category must be a bounded value")
        text = str(self.text or "").strip()
        if not text or len(text) > 4096:
            raise ValueError("Runtime output text must be between 1 and 4096 characters")
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "correlation_id", str(self.correlation_id or "").strip())
        object.__setattr__(self, "session_id", str(self.session_id or "").strip())
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata))


@dataclass(frozen=True)
class RuntimeOutputResult:
    success: bool
    status: str
    error_code: str = ""
    error_message: str = ""


@runtime_checkable
class RuntimeOutputAdapter(Protocol):
    def write(self, message: RuntimeOutputMessage) -> RuntimeOutputResult:
        ...


class QueuedRuntimeInputAdapter:
    """Deterministic foreground input source with no worker or timer thread."""

    def __init__(
        self,
        items: Optional[Sequence[str | RuntimeInputResult]] = None,
        *,
        timeout_hook: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._items: Deque[str | RuntimeInputResult] = deque(items or ())
        self._timeout_hook = timeout_hook
        self._lock = RLock()
        self.wait_count = 0

    def push(self, item: str | RuntimeInputResult) -> None:
        with self._lock:
            self._items.append(item)

    def wait_for_input(self, timeout_seconds: float) -> RuntimeInputResult:
        with self._lock:
            self.wait_count += 1
            if not self._items:
                return RuntimeInputResult.end()
            item = self._items.popleft()
        result = RuntimeInputResult.input(item) if isinstance(item, str) else item
        if not isinstance(result, RuntimeInputResult):
            return result
        if result.status == RUNTIME_INPUT_TIMEOUT and self._timeout_hook is not None:
            self._timeout_hook(float(timeout_seconds))
        return result


class ConsoleRuntimeInputAdapter:
    """Bounded console polling adapter for explicit foreground text verification."""

    def __init__(
        self,
        *,
        input_func: Optional[Callable[[str], str]] = None,
        prompt: str = "ARES> ",
    ) -> None:
        self._input_func = input_func
        self.prompt = str(prompt)
        self._prompt_visible = False
        self._windows_buffer: list[str] = []

    def wait_for_input(self, timeout_seconds: float) -> RuntimeInputResult:
        if self._input_func is None:
            return (
                self._wait_windows(timeout_seconds)
                if os.name == "nt"
                else self._wait_posix(timeout_seconds)
            )
        try:
            return RuntimeInputResult.input(self._input_func(self.prompt))
        except EOFError:
            return RuntimeInputResult.end()
        except KeyboardInterrupt:
            return RuntimeInputResult.cancelled()
        except (OSError, RuntimeError) as error:
            return RuntimeInputResult.failed("console_input_failed", str(error))

    def _wait_posix(self, timeout_seconds: float) -> RuntimeInputResult:
        self._show_prompt()
        try:
            readable, _, _ = select.select([sys.stdin], [], [], max(0.0, float(timeout_seconds)))
            if not readable:
                return RuntimeInputResult.timeout()
            line = sys.stdin.readline()
        except (OSError, RuntimeError, ValueError) as error:
            return RuntimeInputResult.failed("console_input_failed", str(error))
        self._prompt_visible = False
        if line == "":
            return RuntimeInputResult.end()
        return RuntimeInputResult.input(line.rstrip("\r\n"))

    def _wait_windows(self, timeout_seconds: float) -> RuntimeInputResult:
        import msvcrt

        self._show_prompt()
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            if not msvcrt.kbhit():
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                continue
            character = msvcrt.getwch()
            if character == "\x03":
                self._windows_buffer.clear()
                self._prompt_visible = False
                sys.stdout.write("\n")
                sys.stdout.flush()
                return RuntimeInputResult.cancelled()
            if character == "\x1a":
                self._windows_buffer.clear()
                self._prompt_visible = False
                return RuntimeInputResult.end()
            if character in {"\r", "\n"}:
                text = "".join(self._windows_buffer)
                self._windows_buffer.clear()
                self._prompt_visible = False
                sys.stdout.write("\n")
                sys.stdout.flush()
                return RuntimeInputResult.input(text)
            if character == "\b":
                if self._windows_buffer:
                    self._windows_buffer.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if character in {"\x00", "\xe0"}:
                if msvcrt.kbhit():
                    msvcrt.getwch()
                continue
            if character.isprintable() and len(self._windows_buffer) < 4096:
                self._windows_buffer.append(character)
                sys.stdout.write(character)
                sys.stdout.flush()
        return RuntimeInputResult.timeout()

    def _show_prompt(self) -> None:
        if self._prompt_visible:
            return
        sys.stdout.write(self.prompt)
        sys.stdout.flush()
        self._prompt_visible = True


class CollectingRuntimeOutputAdapter:
    def __init__(self, *, fail_after: Optional[int] = None) -> None:
        self.messages: list[RuntimeOutputMessage] = []
        self.fail_after = fail_after
        self._lock = RLock()

    def write(self, message: RuntimeOutputMessage) -> RuntimeOutputResult:
        if not isinstance(message, RuntimeOutputMessage):
            return RuntimeOutputResult(False, "malformed_output", "malformed_output", "invalid message")
        with self._lock:
            if self.fail_after is not None and len(self.messages) >= self.fail_after:
                return RuntimeOutputResult(
                    False,
                    "output_failed",
                    "injected_output_failure",
                    "collecting output adapter injected failure",
                )
            self.messages.append(message)
        return RuntimeOutputResult(True, "written")

    @property
    def texts(self) -> list[str]:
        with self._lock:
            return [message.text for message in self.messages]


class ConsoleRuntimeOutputAdapter:
    def __init__(self, *, output_func: Callable[[str], None] = print) -> None:
        self._output_func = output_func

    def write(self, message: RuntimeOutputMessage) -> RuntimeOutputResult:
        if not isinstance(message, RuntimeOutputMessage):
            return RuntimeOutputResult(False, "malformed_output", "malformed_output", "invalid message")
        try:
            self._output_func(message.text)
        except (OSError, RuntimeError, ValueError) as error:
            return RuntimeOutputResult(False, "output_failed", "console_output_failed", str(error)[:160])
        return RuntimeOutputResult(True, "written")


def _safe_metadata(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"safe": True}
    for key, item in dict(value or {}).items():
        if isinstance(item, (str, int, float, bool)) and len(str(item)) <= 160:
            result[str(key)[:48]] = item
    return result
