from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import BRAIN_ACTIVE, BRAIN_STANDBY, BRAIN_STOPPED  # noqa: E402
from scripts import run_ares_standby_voice as standby_voice  # noqa: E402
from scripts import run_ares_voice as single_voice_launcher  # noqa: E402


@dataclass(frozen=True)
class HardwareTestStage:
    label: str
    instruction: str
    expected: str


STAGES = (
    HardwareTestStage("A", "Remain silent.", "no speech and STANDBY"),
    HardwareTestStage("B", "Say an unrelated sentence.", "speech rejected in STANDBY"),
    HardwareTestStage("C", "Say 'Ares'.", "ACTIVE and one 'Yes Gabi.' acknowledgement"),
    HardwareTestStage("D", "Say 'calculate two plus two'.", "spoken 'Result: 4'"),
    HardwareTestStage("E", "Say 'goodbye Ares'.", "return to STANDBY"),
    HardwareTestStage("F", "Say 'Ares' again.", "ACTIVE with a new session"),
    HardwareTestStage("G", "Say 'shutdown Ares'.", "clean STOPPED state"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = standby_voice.build_parser()
    parser.description = "Run a bounded Raspberry Pi standby-wake hardware verification."
    parser.add_argument("--attempts-per-test", type=int, default=3)
    return parser


def run_hardware_verification(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    runtime_factory: Optional[Callable[..., tuple[Any, Any, Any]]] = None,
) -> int:
    args = build_parser().parse_args(argv)
    if isinstance(args.attempts_per_test, bool) or not 1 <= args.attempts_per_test <= 5:
        output_func("Configuration error: --attempts-per-test must be between 1 and 5.")
        return 2
    if args.retain_diagnostic_audio and not args.diagnostic_wake:
        output_func(
            "Configuration error: --retain-diagnostic-audio requires --diagnostic-wake."
        )
        return 2
    issue = "" if runtime_factory is not None else standby_voice._validate_static_dependencies(args)
    if issue:
        output_func(f"Dependency error: {issue}")
        return 2
    try:
        factory = runtime_factory or standby_voice.create_runtime
        runtime, pipeline, request = factory(args, output_func=output_func)
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
    output_func(f"Attempts per test: {args.attempts_per_test}")
    output_func("Owner microphone audio is retained only when explicitly requested and never replayed.")
    started = runtime.start()
    if not started.success:
        output_func(f"Runtime start failed: {started.error_code or started.status}")
        return 3

    first_session = ""
    recognized_candidates: list[str] = []
    try:
        for index, stage in enumerate(STAGES, start=1):
            output_func(f"Test {index}/7 ({stage.label}): {stage.instruction}")
            output_func(f"Expected: {stage.expected}.")
            passed = False
            for attempt in range(1, args.attempts_per_test + 1):
                result = runtime.poll_once()
                snapshot = runtime.snapshot()
                wake_result = getattr(runtime.standby_wake_listener, "last_result", None)
                diagnostics = getattr(runtime.standby_wake_listener, "last_diagnostics", None)
                if args.diagnostic_wake and diagnostics is not None:
                    transcript = str(getattr(diagnostics, "raw_transcript", "") or "")
                    if transcript and transcript not in recognized_candidates:
                        recognized_candidates.append(transcript)
                candidate_detected = bool(
                    getattr(wake_result, "speech_detected", False)
                    if wake_result is not None
                    else result.data.get("speech_detected", False)
                )
                output_func(
                    f"  Attempt {attempt}/{args.attempts_per_test}: "
                    f"candidate={'detected' if candidate_detected else 'no_speech'}; "
                    f"classification={_classification(wake_result)}; "
                    f"state={snapshot.current_lifecycle_state}; "
                    f"session={snapshot.session_id or 'none'}; status={result.status}"
                )
                if wake_result is not None:
                    output_func(
                        "  Capture: "
                        f"stop={getattr(wake_result, 'capture_stop_reason', '') or 'unknown'}; "
                        f"sample_rate={getattr(wake_result, 'sample_rate_hz', 0)}; "
                        f"candidate={float(getattr(wake_result, 'duration_seconds', 0.0)):.3f}s; "
                        f"raw={float(getattr(wake_result, 'raw_capture_duration_seconds', 0.0)):.3f}s; "
                        f"cleanup={getattr(wake_result, 'cleanup_status', 'unknown')}"
                    )
                passed, first_session = _stage_passed(
                    stage.label,
                    result,
                    snapshot,
                    wake_result,
                    first_session,
                )
                if passed:
                    output_func(f"  Test {stage.label}: PASS")
                    break
                if attempt < args.attempts_per_test:
                    output_func(f"  Next action: {stage.instruction}")
            if not passed:
                output_func(f"Test {stage.label}: FAIL after {args.attempts_per_test} attempts.")
                if args.diagnostic_wake:
                    output_func(
                        "Recognized local wake transcripts: "
                        + (" | ".join(recognized_candidates) if recognized_candidates else "none")
                    )
                runtime.shutdown(reason=f"hardware_verification_{stage.label}_failed")
                output_func("Cleanup completed; verification did not pass.")
                return 1
        output_func("All bounded hardware stages passed; adapters cleaned up.")
        return 0
    except KeyboardInterrupt:
        runtime.shutdown(reason="keyboard_interrupt")
        output_func("Verification cancelled; cleanup completed without replaying captured audio.")
        return 130


def _classification(wake_result: Any) -> str:
    if wake_result is None:
        return "active_command_or_none"
    if bool(getattr(wake_result, "wake_detected", False)):
        return "accepted"
    if bool(getattr(wake_result, "speech_detected", False)):
        return "rejected"
    return "no_speech"


def _stage_passed(
    label: str,
    result: Any,
    snapshot: Any,
    wake_result: Any,
    first_session: str,
) -> tuple[bool, str]:
    state = snapshot.current_lifecycle_state
    if label == "A":
        return (
            state == BRAIN_STANDBY
            and not bool(getattr(wake_result, "speech_detected", False)),
            first_session,
        )
    if label == "B":
        return (
            state == BRAIN_STANDBY
            and bool(getattr(wake_result, "speech_detected", False))
            and not bool(getattr(wake_result, "wake_detected", False)),
            first_session,
        )
    if label == "C":
        session = str(snapshot.session_id or "")
        return state == BRAIN_ACTIVE and bool(session), session or first_session
    if label == "D":
        response = str(getattr(result, "response_text", "") or "")
        return state == BRAIN_ACTIVE and "result: 4" in response.casefold(), first_session
    if label == "E":
        return state == BRAIN_STANDBY and not snapshot.session_id, first_session
    if label == "F":
        session = str(snapshot.session_id or "")
        return state == BRAIN_ACTIVE and bool(session) and session != first_session, first_session
    if label == "G":
        return state == BRAIN_STOPPED, first_session
    return False, first_session


def main() -> int:
    return run_hardware_verification()


if __name__ == "__main__":
    raise SystemExit(main())
