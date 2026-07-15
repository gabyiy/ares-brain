from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import BrainSessionConfig, BrainSessionManager  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def run_verification(output_func: Callable[[str], None] = print) -> int:
    clock = FakeClock()
    session_ids = iter(("brain-session-manual-1", "brain-session-manual-2"))
    manager = BrainSessionManager(
        config=BrainSessionConfig(
            inactivity_timeout_seconds=30,
            maximum_consecutive_failures=3,
        ),
        clock=clock,
        session_id_factory=lambda: next(session_ids),
    )

    output_func("ARES Brain Session Manager verification")
    output_func(f"Initial state: {manager.state}")

    rejected = manager.begin_processing(reason="manual_illegal_transition")
    if rejected.success or rejected.current_state != "STOPPED":
        output_func("FAIL: illegal transition was not rejected safely")
        return 1
    output_func(
        "Rejected: STOPPED -> PROCESSING "
        f"({rejected.error_code}; state preserved)"
    )

    steps = (
        ("BOOTING", manager.begin_boot),
        ("INITIALIZING", manager.begin_initialization),
        ("STANDBY", manager.enter_standby),
        ("ACTIVE", manager.activate_session),
        ("PROCESSING", manager.begin_processing),
        ("RESPONDING", manager.begin_responding),
        ("ACTIVE", manager.finish_response),
    )
    active_session_id = ""
    for expected_state, operation in steps:
        result = operation()
        if not result.success or result.current_state != expected_state:
            output_func(
                f"FAIL: expected {expected_state}, got {result.current_state} "
                f"({result.error_code})"
            )
            return 1
        if expected_state == "ACTIVE" and not active_session_id:
            active_session_id = result.session_id
        output_func(
            f"{result.source_state} -> {result.current_state} "
            f"[{result.transition_reason}]"
        )

    if not active_session_id:
        output_func("FAIL: activation did not create a session ID")
        return 1
    output_func(f"Active session ID created: {active_session_id}")

    clock.advance(29.999)
    before_boundary = manager.inactivity_expired()
    clock.advance(0.001)
    at_boundary = manager.inactivity_expired()
    if before_boundary or not at_boundary:
        output_func("FAIL: inactivity boundary was not deterministic")
        return 1
    output_func("Inactivity: active before 30s; expired exactly at 30s")

    final_steps = (
        ("RETURNING_TO_STANDBY", manager.request_return_to_standby),
        ("STANDBY", manager.complete_return_to_standby),
        ("SHUTTING_DOWN", manager.begin_shutdown),
        ("STOPPED", manager.mark_stopped),
    )
    for expected_state, operation in final_steps:
        result = operation()
        if not result.success or result.current_state != expected_state:
            output_func(f"FAIL: expected {expected_state}, got {result.current_state}")
            return 1
        output_func(
            f"{result.source_state} -> {result.current_state} "
            f"[{result.transition_reason}]"
        )

    if manager.session_id:
        output_func("FAIL: session ID was not cleared after standby/shutdown")
        return 1
    output_func("Session ID cleared after returning to standby: PASS")

    event_payload = json.dumps(
        [event.to_dict() for event in manager.events()],
        sort_keys=True,
    ).lower()
    forbidden = (
        "transcript",
        "owner_memory_value",
        "favorite color is red",
        "microphone_audio",
        "password",
    )
    leaked = [marker for marker in forbidden if marker in event_payload]
    if leaked:
        output_func(f"FAIL: private content marker present in events: {leaked[0]}")
        return 1
    output_func(f"Lifecycle events emitted: {len(manager.events())}")
    output_func("Event privacy check: PASS")
    output_func("Brain Session Manager verification passed.")
    return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
