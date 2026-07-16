from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket

from memory.schema_migrations import StoreWriteLock, store_lock_path
from scripts.inspect_ares_runtime_state import (
    AresProcessInfo,
    inspect_ares_runtime_state,
    list_ares_python_processes,
    render_runtime_state_report,
)


def test_procfs_process_inspection_reports_matching_ares_python_processes(tmp_path):
    proc = tmp_path / "proc"
    matching = proc / "123"
    unrelated = proc / "124"
    matching.mkdir(parents=True)
    unrelated.mkdir(parents=True)
    (matching / "cmdline").write_bytes(
        b"python\x00scripts/run_ares_standby_voice.py\x00"
    )
    (unrelated / "cmdline").write_bytes(b"python\x00other_script.py\x00")

    processes = list_ares_python_processes(proc_root=proc, current_pid=999)
    assert len(processes) == 1
    assert processes[0].pid == 123
    assert processes[0].production_runtime
    assert "run_ares_standby_voice.py" in processes[0].command_line


def test_live_runtime_lock_is_reported_and_never_recovered(tmp_path):
    runtime_target = tmp_path / "ares.runtime"
    event_target = tmp_path / "events.json"
    with StoreWriteLock(runtime_target, owner_kind="ares_standby_voice_runtime"):
        report = inspect_ares_runtime_state(
            runtime_target=runtime_target,
            event_history_target=event_target,
            process_provider=lambda: (),
            lock_process_alive=lambda _pid: True,
            recover_if_owner_dead=True,
        )
        assert report.live_runtime_conflict
        assert report.runtime_lock.owner_process_state == "alive"
        assert report.runtime_lock.lock_exists
        assert report.recovery_messages == ()
        assert store_lock_path(runtime_target).exists()


def test_expired_dead_event_lock_is_recovered_without_touching_runtime_lock(tmp_path):
    runtime_target = tmp_path / "ares.runtime"
    event_target = tmp_path / "events.json"
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    lock_path = store_lock_path(event_target)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "schema_name": "ares.file_lock",
                "schema_version": 1,
                "pid": 424242,
                "hostname": socket.gethostname(),
                "owner_token": "dead-owner-token",
                "owner_kind": "event_history_append",
                "created_at": (now - timedelta(seconds=120)).isoformat().replace(
                    "+00:00", "Z"
                ),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    report = inspect_ares_runtime_state(
        runtime_target=runtime_target,
        event_history_target=event_target,
        stale_after_seconds=30,
        recover_if_owner_dead=True,
        process_provider=lambda: (),
        lock_process_alive=lambda _pid: False,
        lock_now=lambda: now,
        hostname=socket.gethostname(),
    )
    assert not report.live_runtime_conflict
    assert not report.event_history_lock.lock_exists
    assert report.recovery_messages == (
        "event_history:recovered_dead_owner_lock",
    )
    assert not lock_path.exists()


def test_process_report_is_actionable_and_includes_both_lock_owners(tmp_path):
    report = inspect_ares_runtime_state(
        runtime_target=tmp_path / "runtime",
        event_history_target=tmp_path / "events.json",
        process_provider=lambda: (
            AresProcessInfo(
                pid=73,
                command_line="python scripts/run_ares_standby_voice.py",
                start_time="2026-07-16T10:00:00Z",
                production_runtime=True,
            ),
        ),
    )
    output: list[str] = []
    render_runtime_state_report(report, output.append)
    text = "\n".join(output)
    assert "PID 73" in text
    assert "Runtime instance lock" in text
    assert "Event history lock" in text
    assert "Live microphone-owner conflict: yes" in text


def test_another_hardware_verifier_is_a_microphone_owner_conflict(tmp_path):
    report = inspect_ares_runtime_state(
        runtime_target=tmp_path / "runtime",
        event_history_target=tmp_path / "events.json",
        process_provider=lambda: (
            AresProcessInfo(
                pid=74,
                command_line="python scripts/manual_verify_standby_wake_hardware.py",
                start_time="2026-07-16T10:00:00Z",
                production_runtime=False,
            ),
        ),
    )

    assert report.live_runtime_conflict
