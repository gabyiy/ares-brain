from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import signal
import socket
import subprocess
import sys

import pytest

from core.BoundedSubprocess import BoundedProcessRunner
from core.ForegroundSignalCoordinator import (
    ForegroundSignalCoordinator,
    ForegroundTerminationRequested,
)
from scripts import cleanup_ares_voice_processes as cleanup_script
from memory import schema_migrations
from memory.schema_migrations import (
    LOCK_METADATA_SCHEMA,
    LOCK_METADATA_VERSION,
    StoreWriteLock,
    store_lock_path,
)


def test_bounded_process_exits_normally_and_closes_output_handles():
    runner = BoundedProcessRunner()

    result = runner.run(
        [sys.executable, "-c", "print('done')"],
        timeout_seconds=2.0,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "done"
    assert result.metadata["reaped"] is True
    assert result.metadata["output_handles_closed"] is True
    assert runner.active_pid == 0


def test_bounded_process_timeout_terminates_and_reaps_child():
    runner = BoundedProcessRunner(
        termination_grace_seconds=0.1,
        hard_cleanup_deadline_seconds=0.3,
    )

    result = runner.run(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        timeout_seconds=0.05,
    )

    assert result.timed_out is True
    assert result.error_message == "process_timeout"
    assert result.metadata["terminated"] or result.metadata["killed"]
    assert result.metadata["reaped"] is True
    assert result.metadata["cleanup_completed"] is True
    assert runner.active_pid == 0


def test_keyboard_interrupt_during_process_poll_cleans_and_propagates():
    class InterruptedProcess:
        pid = 1234
        returncode = None
        stdin = None

        def poll(self):
            if self.returncode is None:
                raise KeyboardInterrupt
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    process = InterruptedProcess()
    runner = BoundedProcessRunner(process_factory=lambda *args, **kwargs: process)

    with pytest.raises(KeyboardInterrupt):
        runner.run(["fake-process"], timeout_seconds=1.0)

    assert process.returncode is not None
    assert runner.active_pid == 0


def test_foreground_signal_coordinator_requests_cleanup_once_and_is_idempotent():
    coordinator = ForegroundSignalCoordinator()
    reasons = []
    coordinator.register(reasons.append)

    coordinator.request(signum=2, reason="signal_2")
    coordinator.request(signum=2, reason="signal_2_again")

    assert reasons == ["signal_2"]
    assert coordinator.cancellation_requested is True
    with pytest.raises(ForegroundTerminationRequested) as error:
        coordinator.raise_if_requested()
    assert error.value.signum == 2


def test_emergency_cleanup_inspect_only_never_terminates(monkeypatch):
    process = cleanup_script.VoiceProcess(
        pid=321,
        ppid=1,
        pgid=321,
        executable="whisper-cli",
        command_line="whisper-cli -m models/whisper/model.bin",
        kind="voice_child_candidate",
    )
    terminated = []
    monkeypatch.setattr(
        cleanup_script,
        "discover_owned_voice_processes",
        lambda: (process,),
    )
    monkeypatch.setattr(
        cleanup_script,
        "terminate_owned_processes",
        lambda *args, **kwargs: terminated.append(True),
    )
    output = []

    code = cleanup_script.run(["--inspect-only"], output_func=output.append)

    assert code == 0
    assert terminated == []
    assert any("PID 321" in line for line in output)


def test_emergency_cleanup_rejects_unbounded_grace_without_signalling(monkeypatch):
    monkeypatch.setattr(
        cleanup_script,
        "discover_owned_voice_processes",
        lambda: (_ for _ in ()).throw(AssertionError("must not inspect")),
    )

    assert cleanup_script.run(["--grace-seconds", "60"]) == 2


def test_emergency_cleanup_uses_term_then_kill_only_for_supplied_ares_rows(
    monkeypatch,
):
    row = cleanup_script.VoiceProcess(
        pid=7123,
        ppid=7000,
        pgid=7123,
        executable="whisper-cli",
        command_line="whisper-cli -m local-model -f candidate.wav",
        kind="voice_child_candidate",
    )
    alive = {row.pid: True}
    signals = []

    def signal_group(pgid, signum):
        signals.append((pgid, signum))
        if signum == int(getattr(signal, "SIGKILL", 9)):
            alive[row.pid] = False

    monkeypatch.setattr(cleanup_script, "_signal_group", signal_group)
    monkeypatch.setattr(cleanup_script, "_pid_alive", lambda pid: alive.get(pid, False))

    remaining = cleanup_script.terminate_owned_processes(
        [row],
        grace_seconds=0.1,
        output_func=lambda _line: None,
    )

    assert remaining == ()
    assert signals == [
        (row.pgid, int(getattr(signal, "SIGTERM", 15))),
        (row.pgid, int(getattr(signal, "SIGKILL", 9))),
    ]


def test_emergency_cleanup_recovers_only_expired_dead_owner_locks(
    tmp_path,
    monkeypatch,
):
    runtime_target = tmp_path / "runtime.instance"
    event_target = tmp_path / "events.json"
    lock_path = store_lock_path(event_target)
    lock_path.write_text(
        json.dumps(
            {
                "schema_name": LOCK_METADATA_SCHEMA,
                "schema_version": LOCK_METADATA_VERSION,
                "pid": 987654321,
                "hostname": socket.gethostname(),
                "owner_token": "dead-emergency-cleanup-owner",
                "owner_kind": "event_history_append",
                "created_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=5)
                ).isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cleanup_script, "DEFAULT_RUNTIME_TARGET", runtime_target)
    monkeypatch.setattr(cleanup_script, "DEFAULT_EVENT_HISTORY_TARGET", event_target)
    monkeypatch.setattr(schema_migrations, "_process_alive", lambda _pid: False)
    output = []

    assert cleanup_script._recover_stale_locks(output.append) is True
    assert not lock_path.exists()
    assert any("recovered_dead_owner_lock" in line for line in output)


def test_emergency_cleanup_preserves_lock_owned_by_live_process(tmp_path, monkeypatch):
    runtime_target = tmp_path / "runtime.instance"
    event_target = tmp_path / "events.json"
    monkeypatch.setattr(cleanup_script, "DEFAULT_RUNTIME_TARGET", runtime_target)
    monkeypatch.setattr(cleanup_script, "DEFAULT_EVENT_HISTORY_TARGET", event_target)
    output = []

    with StoreWriteLock(runtime_target, owner_kind="ares_standby_voice_runtime"):
        assert cleanup_script._recover_stale_locks(output.append) is False
        assert store_lock_path(runtime_target).exists()

    assert any("owner=alive" in line for line in output)
