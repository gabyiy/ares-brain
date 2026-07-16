from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from memory.schema_migrations import (  # noqa: E402
    DEFAULT_STALE_LOCK_SECONDS,
    StoreLockInspection,
    inspect_store_lock,
    recover_store_lock,
)


DEFAULT_RUNTIME_TARGET = REPO_ROOT / "data" / "runtime" / "ares_standby_voice.runtime"
DEFAULT_EVENT_HISTORY_TARGET = REPO_ROOT / "data" / "event_history.json"
_ARES_PROCESS_MARKERS = (
    "scripts/run_ares_standby_voice.py",
    "scripts\\run_ares_standby_voice.py",
    "scripts/manual_verify_standby_wake_hardware.py",
    "scripts\\manual_verify_standby_wake_hardware.py",
)


@dataclass(frozen=True)
class AresProcessInfo:
    pid: int
    command_line: str
    start_time: str
    production_runtime: bool


@dataclass(frozen=True)
class AresRuntimeStateReport:
    processes: tuple[AresProcessInfo, ...]
    runtime_lock: StoreLockInspection
    event_history_lock: StoreLockInspection
    live_runtime_conflict: bool
    recovery_messages: tuple[str, ...] = ()


def inspect_ares_runtime_state(
    *,
    runtime_target: Path = DEFAULT_RUNTIME_TARGET,
    event_history_target: Path = DEFAULT_EVENT_HISTORY_TARGET,
    stale_after_seconds: float = DEFAULT_STALE_LOCK_SECONDS,
    recover_if_owner_dead: bool = False,
    process_provider: Optional[Callable[[], Sequence[AresProcessInfo]]] = None,
    lock_process_alive: Optional[Callable[[int], Optional[bool]]] = None,
    lock_now: Optional[Callable[[], datetime]] = None,
    hostname: Optional[str] = None,
) -> AresRuntimeStateReport:
    processes = tuple(
        (process_provider or (lambda: list_ares_python_processes()))()
    )
    runtime_lock = inspect_store_lock(
        Path(runtime_target),
        stale_after_seconds=stale_after_seconds,
        process_alive=lock_process_alive,
        now=lock_now,
        hostname=hostname,
    )
    event_lock = inspect_store_lock(
        Path(event_history_target),
        stale_after_seconds=stale_after_seconds,
        process_alive=lock_process_alive,
        now=lock_now,
        hostname=hostname,
    )
    recovery_messages: list[str] = []
    if recover_if_owner_dead:
        for label, target, inspection in (
            ("runtime", Path(runtime_target), runtime_lock),
            ("event_history", Path(event_history_target), event_lock),
        ):
            if not inspection.safe_recovery_possible:
                continue
            recovery = recover_store_lock(
                target,
                stale_after_seconds=stale_after_seconds,
                process_alive=lock_process_alive,
                now=lock_now,
                hostname=hostname,
            )
            recovery_messages.append(f"{label}:{recovery.status}")
        runtime_lock = inspect_store_lock(
            Path(runtime_target),
            stale_after_seconds=stale_after_seconds,
            process_alive=lock_process_alive,
            now=lock_now,
            hostname=hostname,
        )
        event_lock = inspect_store_lock(
            Path(event_history_target),
            stale_after_seconds=stale_after_seconds,
            process_alive=lock_process_alive,
            now=lock_now,
            hostname=hostname,
        )
    # Every process marker above identifies a foreground program that can own
    # the same microphone, including another bounded hardware verifier.
    live_process = bool(processes)
    live_runtime_lock = bool(
        runtime_lock.lock_exists
        and runtime_lock.owner_process_state == "alive"
    )
    return AresRuntimeStateReport(
        processes=processes,
        runtime_lock=runtime_lock,
        event_history_lock=event_lock,
        live_runtime_conflict=live_process or live_runtime_lock,
        recovery_messages=tuple(recovery_messages),
    )


def list_ares_python_processes(
    *,
    proc_root: Path = Path("/proc"),
    current_pid: Optional[int] = None,
) -> tuple[AresProcessInfo, ...]:
    """Read Linux procfs without executing a shell or consuming audio."""

    root = Path(proc_root)
    if not root.is_dir():
        return ()
    own_pid = os.getpid() if current_pid is None else int(current_pid)
    matches: list[AresProcessInfo] = []
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == own_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        command = " ".join(
            part.decode("utf-8", errors="replace")
            for part in raw.split(b"\x00")
            if part
        ).strip()
        folded = command.casefold()
        if "python" not in folded or not any(
            marker.casefold() in folded for marker in _ARES_PROCESS_MARKERS
        ):
            continue
        production = any(
            marker.casefold() in folded
            for marker in _ARES_PROCESS_MARKERS[:2]
        )
        try:
            started = datetime.fromtimestamp(
                entry.stat().st_ctime,
                timezone.utc,
            ).isoformat().replace("+00:00", "Z")
        except (FileNotFoundError, OSError, ValueError):
            started = "unknown"
        matches.append(
            AresProcessInfo(
                pid=pid,
                command_line=command[:1024],
                start_time=started,
                production_runtime=production,
            )
        )
    return tuple(sorted(matches, key=lambda item: item.pid))


def render_runtime_state_report(
    report: AresRuntimeStateReport,
    output_func: Callable[[str], None] = print,
) -> None:
    output_func("ARES process and lock preflight:")
    if report.processes:
        for process in report.processes:
            output_func(
                f"  PID {process.pid}; start={process.start_time}; "
                f"production={'yes' if process.production_runtime else 'no'}; "
                f"command={process.command_line}"
            )
    else:
        output_func("  Matching ARES Python processes: none")
    _render_lock("Runtime instance", report.runtime_lock, output_func)
    _render_lock("Event history", report.event_history_lock, output_func)
    for message in report.recovery_messages:
        output_func(f"  Safe stale-lock recovery: {message}")
    output_func(
        "  Live microphone-owner conflict: "
        f"{'yes' if report.live_runtime_conflict else 'no'}"
    )


def _render_lock(
    label: str,
    inspection: StoreLockInspection,
    output_func: Callable[[str], None],
) -> None:
    output_func(
        f"  {label} lock: exists={'yes' if inspection.lock_exists else 'no'}; "
        f"pid={inspection.owner_pid or 'unknown'}; "
        f"owner={inspection.owner_process_state}; "
        f"age={inspection.lock_age_seconds:.3f}s; "
        f"metadata={'valid' if inspection.metadata_valid else 'invalid_or_absent'}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect active ARES processes and production ownership locks."
    )
    parser.add_argument("--recover-if-owner-dead", action="store_true")
    parser.add_argument(
        "--stale-after-seconds",
        type=float,
        default=DEFAULT_STALE_LOCK_SECONDS,
    )
    return parser


def run(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = inspect_ares_runtime_state(
            stale_after_seconds=args.stale_after_seconds,
            recover_if_owner_dead=args.recover_if_owner_dead,
        )
    except (OSError, ValueError) as error:
        output_func(f"ARES process/lock inspection failed: {error}")
        return 2
    render_runtime_state_report(report, output_func)
    return 1 if report.live_runtime_conflict else 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
