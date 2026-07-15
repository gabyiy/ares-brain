from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import BRAIN_STOPPED  # noqa: E402
from scripts import run_ares_standby_voice as standby_voice  # noqa: E402
from scripts import run_ares_voice as single_voice_launcher  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = standby_voice.build_parser()
    parser.description = "Run a bounded Raspberry Pi standby-wake hardware verification."
    parser.add_argument("--maximum-listen-cycles", type=int, default=80)
    return parser


def run_hardware_verification(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    if isinstance(args.maximum_listen_cycles, bool) or not 7 <= args.maximum_listen_cycles <= 500:
        output_func("Configuration error: --maximum-listen-cycles must be between 7 and 500.")
        return 2
    issue = standby_voice._validate_static_dependencies(args)
    if issue:
        output_func(f"Dependency error: {issue}")
        return 2
    try:
        runtime, pipeline, request = standby_voice.create_runtime(args, output_func=output_func)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        output_func(f"Setup failed: {error}")
        return 2
    preflight = single_voice_launcher._preflight_pipeline(pipeline, request)
    if not preflight.success:
        output_func("Command voice pipeline health check failed before capture.")
        return 3
    wake_started = runtime.standby_wake_listener.start(runtime_id=runtime.runtime_id)
    wake_health = runtime.standby_wake_listener.health(runtime_id=runtime.runtime_id)
    runtime.standby_wake_listener.stop("preflight_complete")
    if not wake_started.success or not wake_health.success:
        output_func(
            "Wake listener health check failed: "
            f"{wake_health.error_code or wake_started.error_code or 'unhealthy'}."
        )
        return 3

    output_func("ARES bounded standby-wake hardware verification")
    output_func("A. Remain silent for one listen cycle; ARES must stay in STANDBY.")
    output_func("B. Say an unrelated sentence; ARES must stay silent in STANDBY.")
    output_func("C. Say 'Ares'; expect 'Yes Gabi.'")
    output_func("D. Say 'calculate two plus two'; expect 'Result: 4'.")
    output_func("E. Say 'goodbye Ares'; expect return to STANDBY.")
    output_func("F. Say 'Ares' again; expect a new session.")
    output_func("G. Say 'shutdown Ares'; expect clean STOPPED state.")
    output_func("Owner microphone audio is never replayed by this helper.")

    started = runtime.start()
    if not started.success:
        output_func(f"Runtime start failed: {started.error_code or started.status}")
        return 3
    previous_session = ""
    try:
        for cycle in range(1, args.maximum_listen_cycles + 1):
            result = runtime.poll_once()
            snapshot = runtime.snapshot()
            session_changed = snapshot.session_id != previous_session
            safe_session = snapshot.session_id or "none"
            output_func(
                f"Cycle {cycle}: state={snapshot.current_lifecycle_state}; "
                f"status={result.status}; session={safe_session}"
            )
            if session_changed:
                output_func(f"Session changed: {previous_session or 'none'} -> {safe_session}")
                previous_session = snapshot.session_id
            if result.normalized_input:
                output_func(
                    f"Wake/control classification: {result.command_category or 'ordinary'} "
                    f"({result.normalized_input})"
                )
            capture_stop = str(result.data.get("capture_stop_reason") or "")
            wake_result = getattr(runtime.standby_wake_listener, "last_result", None)
            if wake_result is not None and not capture_stop:
                capture_stop = str(getattr(wake_result, "capture_stop_reason", "") or "")
            if capture_stop:
                sample_rate = (
                    getattr(wake_result, "sample_rate_hz", 0)
                    if wake_result is not None
                    else result.data.get("sample_rate_hz", 0)
                )
                duration = (
                    getattr(wake_result, "duration_seconds", 0.0)
                    if wake_result is not None
                    else result.data.get("duration_seconds", 0.0)
                )
                output_func(
                    "Wake capture: "
                    f"stop={capture_stop}; sample_rate={sample_rate}; "
                    f"duration={float(duration):.3f}s; "
                    f"cleanup={getattr(wake_result, 'cleanup_status', 'unknown')}"
                )
            active_result = getattr(runtime.input_adapter, "last_result", None)
            if active_result is not None and result.command_category == "ordinary":
                output_func(
                    "Command capture: "
                    f"stop={getattr(active_result, 'recording_status', '')}; "
                    f"duration={float(getattr(active_result, 'recording_duration_seconds', 0.0)):.3f}s"
                )
            if runtime.session_manager.state == BRAIN_STOPPED:
                output_func("Cleanup: wake listener, active voice pipeline, and playback stopped.")
                return 0
        runtime.shutdown(reason="bounded_hardware_verification_limit")
        output_func("Verification stopped at the bounded listen-cycle limit before Test G completed.")
        return 1
    except KeyboardInterrupt:
        runtime.shutdown(reason="keyboard_interrupt")
        output_func("Verification cancelled; cleanup completed without replaying captured audio.")
        return 130


def main() -> int:
    return run_hardware_verification()


if __name__ == "__main__":
    raise SystemExit(main())
