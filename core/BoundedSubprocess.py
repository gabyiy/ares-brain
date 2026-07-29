from __future__ import annotations

from dataclasses import dataclass, field
import errno
import os
import shutil
import signal
import subprocess
import tempfile
from threading import RLock
import time
from typing import Any, Callable, Optional, Sequence


@dataclass(frozen=True)
class BoundedProcessResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class BoundedProcessRunner:
    """Serialized process-group runner with TERM/KILL/reap cancellation."""

    def __init__(
        self,
        *,
        process_factory: Callable[..., Any] = subprocess.Popen,
        termination_grace_seconds: float = 1.0,
        hard_cleanup_deadline_seconds: float = 3.0,
        poll_interval_seconds: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        grace = float(termination_grace_seconds)
        cleanup = float(hard_cleanup_deadline_seconds)
        interval = float(poll_interval_seconds)
        if not 0.05 <= grace <= 10.0:
            raise ValueError("termination_grace_seconds must be between 0.05 and 10")
        if not grace <= cleanup <= 30.0:
            raise ValueError("hard_cleanup_deadline_seconds must be between grace and 30")
        if not 0.005 <= interval <= 0.5:
            raise ValueError("poll_interval_seconds must be between 0.005 and 0.5")
        self.process_factory = process_factory
        self.termination_grace_seconds = grace
        self.hard_cleanup_deadline_seconds = cleanup
        self.poll_interval_seconds = interval
        self.clock = clock
        self.sleeper = sleeper
        self._run_lock = RLock()
        self._active_lock = RLock()
        self._cleanup_lock = RLock()
        self._active_process: Any = None
        self._active_pgid = 0
        self._active_group_owned = False
        self._cancel_reason = ""

    def which(self, executable: str) -> Optional[str]:
        return shutil.which(executable)

    @property
    def active_pid(self) -> int:
        with self._active_lock:
            return int(getattr(self._active_process, "pid", 0) or 0)

    @property
    def active_pgid(self) -> int:
        with self._active_lock:
            return self._active_pgid

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        input_text: str = "",
    ) -> BoundedProcessResult:
        with self._run_lock:
            return self._run(args, timeout_seconds=timeout_seconds, input_text=input_text)

    def cancel_current(self, reason: str = "cancelled") -> bool:
        with self._active_lock:
            process = self._active_process
            pgid = self._active_pgid
            group_owned = self._active_group_owned
            if process is None:
                return False
            self._cancel_reason = str(reason or "cancelled")[:120]
        self._terminate(process, pgid, group_owned=group_owned)
        return True

    def _run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        input_text: str,
    ) -> BoundedProcessResult:
        safe_args = tuple(str(arg) for arg in args)
        timeout = float(timeout_seconds)
        if not 0 < timeout <= 900:
            raise ValueError("timeout_seconds must be > 0 and <= 900")
        stdout_file = tempfile.TemporaryFile(mode="w+b")
        stderr_file = tempfile.TemporaryFile(mode="w+b")
        stdin_file: Any = None
        if input_text:
            stdin_file = tempfile.TemporaryFile(mode="w+b")
            stdin_file.write(str(input_text).encode("utf-8"))
            stdin_file.flush()
            stdin_file.seek(0)
        process: Any = None
        pgid = 0
        group_owned = False
        timed_out = False
        error_message = ""
        cleanup: dict[str, Any] = {}
        started = self.clock()
        try:
            kwargs: dict[str, Any] = {
                "stdin": stdin_file if stdin_file is not None else subprocess.DEVNULL,
                "stdout": stdout_file,
                "stderr": stderr_file,
                "shell": False,
            }
            if os.name == "posix":
                kwargs["start_new_session"] = True
            process = self.process_factory(list(safe_args), **kwargs)
            group_owned = bool(
                os.name == "posix" and self.process_factory is subprocess.Popen
            )
            pgid = self._resolve_pgid(process, group_owned=group_owned)
            with self._active_lock:
                self._active_process = process
                self._active_pgid = pgid
                self._active_group_owned = group_owned
                self._cancel_reason = ""
            try:
                self._wait(process, timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                error_message = "process_timeout"
                cleanup = self._terminate(process, pgid, group_owned=group_owned)
            except BaseException:
                self._terminate(process, pgid, group_owned=group_owned)
                raise
            else:
                if self._group_alive(process, pgid, group_owned=group_owned):
                    cleanup = self._terminate(
                        process,
                        pgid,
                        group_owned=group_owned,
                    )
                else:
                    cleanup = {
                        "terminated": False,
                        "killed": False,
                        "reaped": self._reaped(process),
                        "cleanup_completed": self._reaped(process),
                    }
        except FileNotFoundError:
            error_message = "process_not_found"
        except OSError as error:
            if process is not None:
                cleanup = self._terminate(process, pgid, group_owned=group_owned)
            error_message = f"process_os_error:{error.__class__.__name__}"
        except BaseException:
            if process is not None:
                self._terminate(process, pgid, group_owned=group_owned)
            raise
        finally:
            with self._active_lock:
                cancel_reason = self._cancel_reason
                if self._active_process is process:
                    self._active_process = None
                    self._active_pgid = 0
                    self._active_group_owned = False
                    self._cancel_reason = ""
            stdout = self._read_capture(stdout_file)
            stderr = self._read_capture(stderr_file)
            stdout_file.close()
            stderr_file.close()
            if stdin_file is not None:
                stdin_file.close()
        if cancel_reason and not timed_out:
            error_message = "process_cancelled"
        returncode_value = getattr(process, "returncode", None) if process is not None else None
        return BoundedProcessResult(
            args=safe_args,
            returncode=int(returncode_value) if returncode_value is not None else -1,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            error_message=error_message,
            metadata={
                "pid": int(getattr(process, "pid", 0) or 0),
                "pgid": pgid,
                "start_new_session": os.name == "posix",
                "process_group_owned": group_owned,
                "elapsed_seconds": round(max(0.0, self.clock() - started), 6),
                "cancel_reason": cancel_reason,
                "output_handles_closed": True,
                "input_handle_closed": bool(
                    stdin_file is None or getattr(stdin_file, "closed", False)
                ),
                **cleanup,
            },
        )

    def _wait(self, process: Any, timeout_seconds: float) -> None:
        deadline = self.clock() + float(timeout_seconds)
        while True:
            value = process.poll()
            if value is not None:
                return
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(getattr(process, "args", "process"), timeout_seconds)
            self.sleeper(min(self.poll_interval_seconds, remaining))

    def _terminate(
        self,
        process: Any,
        pgid: int,
        *,
        group_owned: bool,
    ) -> dict[str, Any]:
        with self._cleanup_lock:
            terminated = self._signal(
                process,
                pgid,
                getattr(signal, "SIGTERM", 15),
                group_owned=group_owned,
            )
            self._wait_until(
                process,
                pgid,
                self.termination_grace_seconds,
                group_owned=group_owned,
            )
            killed = False
            if self._alive(process) or self._group_alive(
                process,
                pgid,
                group_owned=group_owned,
            ):
                killed = self._signal(
                    process,
                    pgid,
                    getattr(signal, "SIGKILL", 9),
                    group_owned=group_owned,
                )
            self._wait_until(
                process,
                pgid,
                self.hard_cleanup_deadline_seconds,
                group_owned=group_owned,
            )
            reaped = self._reaped(process)
            return {
                "terminated": terminated,
                "killed": killed,
                "reaped": reaped,
                "cleanup_completed": bool(
                    reaped
                    and not self._alive(process)
                    and not self._group_alive(
                        process,
                        pgid,
                        group_owned=group_owned,
                    )
                ),
            }

    def _wait_until(
        self,
        process: Any,
        pgid: int,
        timeout_seconds: float,
        *,
        group_owned: bool,
    ) -> None:
        deadline = self.clock() + max(0.0, float(timeout_seconds))
        while (
            self._alive(process)
            or self._group_alive(process, pgid, group_owned=group_owned)
        ) and self.clock() < deadline:
            try:
                process.poll()
            except (OSError, RuntimeError):
                break
            self.sleeper(min(self.poll_interval_seconds, max(0.0, deadline - self.clock())))

    def _signal(
        self,
        process: Any,
        pgid: int,
        signum: int,
        *,
        group_owned: bool,
    ) -> bool:
        if os.name == "posix" and group_owned and pgid > 0:
            try:
                os.killpg(pgid, int(signum))
                return True
            except ProcessLookupError:
                return False
            except OSError as error:
                if getattr(error, "errno", None) == errno.ESRCH:
                    return False
        method = process.terminate if int(signum) == int(getattr(signal, "SIGTERM", 15)) else process.kill
        try:
            method()
            return True
        except (OSError, RuntimeError):
            return False

    @staticmethod
    def _resolve_pgid(process: Any, *, group_owned: bool) -> int:
        pid = int(getattr(process, "pid", 0) or 0)
        if os.name != "posix" or not group_owned or pid <= 0:
            return pid
        try:
            return int(os.getpgid(pid))
        except OSError:
            return pid

    @staticmethod
    def _alive(process: Any) -> bool:
        try:
            return process.poll() is None
        except (OSError, RuntimeError):
            return False

    def _group_alive(self, process: Any, pgid: int, *, group_owned: bool) -> bool:
        if os.name != "posix" or not group_owned or pgid <= 0:
            return False
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError as error:
            return getattr(error, "errno", None) != errno.ESRCH

    @staticmethod
    def _reaped(process: Any) -> bool:
        try:
            return process.poll() is not None
        except (OSError, RuntimeError):
            return False

    @staticmethod
    def _read_capture(handle: Any) -> str:
        try:
            handle.flush()
            handle.seek(0)
            return handle.read().decode("utf-8", errors="replace")
        except (OSError, ValueError):
            return ""
