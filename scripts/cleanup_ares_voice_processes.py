from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import signal
import sys
import time
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from memory.schema_migrations import (  # noqa: E402
    DEFAULT_STALE_LOCK_SECONDS,
    inspect_store_lock,
    recover_store_lock,
)
from scripts.inspect_ares_runtime_state import (  # noqa: E402
    DEFAULT_EVENT_HISTORY_TARGET,
    DEFAULT_RUNTIME_TARGET,
    list_ares_python_processes,
)


_VOICE_EXECUTABLES = frozenset({"arecord", "whisper-cli", "piper", "aplay"})


@dataclass(frozen=True)
class VoiceProcess:
    pid: int
    ppid: int
    pgid: int
    executable: str
    command_line: str
    kind: str


def discover_owned_voice_processes(
    proc_root: Path = Path("/proc"),
    *,
    process_group_getter: Optional[Callable[[int], int]] = None,
) -> tuple[VoiceProcess, ...]:
    root = Path(proc_root)
    if not root.is_dir():
        return ()
    group_getter = process_group_getter or getattr(os, "getpgid", lambda pid: pid)
    runtime_pids = {item.pid for item in list_ares_python_processes(proc_root=root)}
    process_rows: dict[int, VoiceProcess] = {}
    for entry in root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = " ".join(
                part.decode("utf-8", errors="replace")
                for part in (entry / "cmdline").read_bytes().split(b"\x00")
                if part
            ).strip()
            stat_fields = (entry / "stat").read_text(encoding="utf-8").split()
            ppid = int(stat_fields[3])
            pid = int(entry.name)
            pgid = int(group_getter(pid))
            executable = Path((entry / "exe").resolve()).name.casefold()
        except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
            continue
        if pid in runtime_pids:
            process_rows[pid] = VoiceProcess(pid, ppid, pgid, executable, cmdline[:1024], "ares_runtime")
        elif executable in _VOICE_EXECUTABLES:
            process_rows[pid] = VoiceProcess(pid, ppid, pgid, executable, cmdline[:1024], "voice_child_candidate")

    owned = set(runtime_pids)
    changed = True
    while changed:
        changed = False
        for row in process_rows.values():
            if row.ppid in owned and row.pid not in owned:
                owned.add(row.pid)
                changed = True
    for row in process_rows.values():
        if row.kind != "voice_child_candidate" or row.pid in owned:
            continue
        try:
            cwd = (root / str(row.pid) / "cwd").resolve()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        # A crashed ARES parent may leave a child reparented to init. The exact
        # executable allowlist plus repository cwd is the bounded ownership proof.
        if row.ppid == 1 and cwd == REPO_ROOT.resolve():
            owned.add(row.pid)
    return tuple(sorted((process_rows[pid] for pid in owned if pid in process_rows), key=lambda item: item.pid))


def terminate_owned_processes(
    processes: Sequence[VoiceProcess],
    *,
    grace_seconds: float = 2.0,
    output_func: Callable[[str], None] = print,
) -> tuple[int, ...]:
    rows = tuple(processes)
    if not rows:
        output_func("ARES-owned voice processes found: none")
        return ()
    for row in rows:
        output_func(
            f"Found PID {row.pid}; PGID {row.pgid}; kind={row.kind}; "
            f"executable={row.executable}; command={row.command_line}"
        )
    child_groups = sorted(
        {row.pgid for row in rows if row.kind != "ares_runtime" and row.pgid > 0}
    )
    runtime_pids = sorted(row.pid for row in rows if row.kind == "ares_runtime")
    for pgid in child_groups:
        _signal_group(pgid, getattr(signal, "SIGTERM", 15))
    for pid in runtime_pids:
        _signal_pid(pid, getattr(signal, "SIGTERM", 15))
    deadline = time.monotonic() + max(0.1, float(grace_seconds))
    while time.monotonic() < deadline and any(_pid_alive(row.pid) for row in rows):
        time.sleep(0.05)
    remaining = [row for row in rows if _pid_alive(row.pid)]
    for row in remaining:
        if row.kind == "ares_runtime":
            _signal_pid(row.pid, getattr(signal, "SIGKILL", 9))
        else:
            _signal_group(row.pgid, getattr(signal, "SIGKILL", 9))
    final_deadline = time.monotonic() + max(0.1, float(grace_seconds))
    while time.monotonic() < final_deadline and any(_pid_alive(row.pid) for row in rows):
        time.sleep(0.05)
    remaining_pids = tuple(row.pid for row in rows if _pid_alive(row.pid))
    output_func(
        "Remaining ARES-owned voice processes: "
        + (", ".join(str(pid) for pid in remaining_pids) if remaining_pids else "none")
    )
    return remaining_pids


def _signal_group(pgid: int, signum: int) -> None:
    if pgid <= 0 or (hasattr(os, "getpgrp") and pgid == os.getpgrp()):
        return
    try:
        os.killpg(pgid, int(signum))
    except ProcessLookupError:
        return
    except OSError as error:
        if getattr(error, "errno", None) != errno.ESRCH:
            raise


def _signal_pid(pid: int, signum: int) -> None:
    try:
        os.kill(int(pid), int(signum))
    except ProcessLookupError:
        return
    except OSError as error:
        if getattr(error, "errno", None) != errno.ESRCH:
            raise


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except OSError as error:
        return getattr(error, "errno", None) != errno.ESRCH


def _recover_stale_locks(output_func: Callable[[str], None]) -> bool:
    success = True
    for label, target in (
        ("runtime", DEFAULT_RUNTIME_TARGET),
        ("event_history", DEFAULT_EVENT_HISTORY_TARGET),
    ):
        inspection = inspect_store_lock(
            target,
            stale_after_seconds=DEFAULT_STALE_LOCK_SECONDS,
        )
        if not inspection.lock_exists:
            output_func(f"{label} lock: absent")
            continue
        if not inspection.safe_recovery_possible:
            output_func(
                f"{label} lock preserved: owner={inspection.owner_process_state}; "
                f"pid={inspection.owner_pid or 'unknown'}; age={inspection.lock_age_seconds:.3f}s"
            )
            success = False
            continue
        recovery = recover_store_lock(
            target,
            stale_after_seconds=DEFAULT_STALE_LOCK_SECONDS,
        )
        output_func(f"{label} lock recovery: {recovery.status}")
        success = success and recovery.recovered
    return success


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Terminate only identified ARES foreground voice processes and children."
    )
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--recover-stale-locks", action="store_true")
    parser.add_argument("--grace-seconds", type=float, default=2.0)
    return parser


def run(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    if not 0.1 <= float(args.grace_seconds) <= 10.0:
        output_func("grace seconds must be between 0.1 and 10")
        return 2
    processes = discover_owned_voice_processes()
    if args.inspect_only:
        for row in processes:
            output_func(f"PID {row.pid}; PGID {row.pgid}; {row.kind}; {row.command_line}")
        if not processes:
            output_func("ARES-owned voice processes found: none")
        return 0
    remaining = terminate_owned_processes(
        processes,
        grace_seconds=float(args.grace_seconds),
        output_func=output_func,
    )
    lock_ok = True
    if args.recover_stale_locks:
        lock_ok = _recover_stale_locks(output_func)
    return 0 if not remaining and lock_ok else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
