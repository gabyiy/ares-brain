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
    HardwareTestStage(
        "E",
        "Say 'goodbye Ares' once, then remain silent.",
        "return to STANDBY without ordinary routing",
    ),
    HardwareTestStage("F", "Say 'Ares'.", "ACTIVE with a new session"),
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
    if not wake_started.success or not wake_health.success:
        runtime.standby_wake_listener.stop("preflight_failed")
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
    wake_transcripts: list[str] = []
    active_command_transcripts: list[str] = []
    try:
        for index, stage in enumerate(STAGES, start=1):
            output_func(f"Test {index}/{len(STAGES)} ({stage.label}): {stage.instruction}")
            output_func(f"Expected: {stage.expected}.")
            passed = False
            for attempt in range(1, args.attempts_per_test + 1):
                before = runtime.snapshot()
                result = runtime.poll_once()
                snapshot = runtime.snapshot()
                standby_attempt = before.current_lifecycle_state == BRAIN_STANDBY
                wake_result = (
                    getattr(runtime.standby_wake_listener, "last_result", None)
                    if standby_attempt
                    else None
                )
                diagnostics = (
                    getattr(runtime.standby_wake_listener, "last_diagnostics", None)
                    if standby_attempt
                    else getattr(runtime.input_adapter, "last_diagnostics", None)
                )
                if args.diagnostic_wake and diagnostics is not None:
                    transcript = str(getattr(diagnostics, "raw_transcript", "") or "")
                    target = wake_transcripts if standby_attempt else active_command_transcripts
                    if transcript and transcript not in target:
                        target.append(transcript)
                candidate_detected = bool(
                    getattr(wake_result, "speech_detected", False)
                    if wake_result is not None
                    else result.data.get("speech_detected", False)
                )
                output_func(
                    f"  Attempt {attempt}/{args.attempts_per_test}: "
                    f"candidate={'detected' if candidate_detected else 'no_speech'}; "
                    f"classification={_classification(wake_result)}; "
                    f"path={getattr(wake_result, 'classification_path', '') or 'none'}; "
                    f"state={before.current_lifecycle_state}->{snapshot.current_lifecycle_state}; "
                    f"session={snapshot.session_id or 'none'}; status={result.status}"
                )
                _print_recognition_summary(
                    output_func,
                    diagnostics=diagnostics,
                    wake_result=wake_result,
                    result=result,
                    before_state=before.current_lifecycle_state,
                    diagnostic_enabled=bool(args.diagnostic_wake),
                )
                output_func(
                    "  Active session created: "
                    + (
                        "yes"
                        if not before.session_id and bool(snapshot.session_id)
                        else "no"
                    )
                )
                if standby_attempt and wake_result is not None:
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
                if not _retry_allowed(stage.label, result):
                    output_func(
                        "  Retry refused: a recognized non-lifecycle command is not "
                        "treated as no speech or failed transcription."
                    )
                    break
                if attempt < args.attempts_per_test:
                    output_func(f"  Next action: {stage.instruction}")
            if not passed:
                output_func(f"Test {stage.label}: FAIL after {args.attempts_per_test} attempts.")
                if args.diagnostic_wake:
                    _print_transcript_summary(
                        output_func,
                        wake_transcripts,
                        active_command_transcripts,
                    )
                runtime.shutdown(reason=f"hardware_verification_{stage.label}_failed")
                output_func("Cleanup completed; verification did not pass.")
                return 1
        if args.diagnostic_wake:
            _print_transcript_summary(
                output_func,
                wake_transcripts,
                active_command_transcripts,
            )
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
        return (
            state == BRAIN_STANDBY
            and not snapshot.session_id
            and str(getattr(result, "response_text", "") or "")
            != "I cannot handle that request yet."
            and bool(dict(getattr(result, "data", {}) or {}).get("core_service_bypassed")),
            first_session,
        )
    if label == "F":
        session = str(snapshot.session_id or "")
        return (
            state == BRAIN_ACTIVE
            and bool(session)
            and session != first_session,
            first_session,
        )
    if label == "G":
        return state == BRAIN_STOPPED, first_session
    return False, first_session


def _print_recognition_summary(
    output_func: Callable[[str], None],
    *,
    diagnostics: Any,
    wake_result: Any,
    result: Any,
    before_state: str,
    diagnostic_enabled: bool,
) -> None:
    if before_state == BRAIN_STANDBY:
        recognizer = str(
            getattr(diagnostics, "recognizer_name", "")
            or getattr(wake_result, "recognizer_name", "")
            or "vosk_constrained_grammar"
        )
        raw = str(getattr(diagnostics, "raw_recognition_result", "") or "")
        normalized = str(getattr(diagnostics, "normalized_transcript", "") or "")
        confidence = getattr(diagnostics, "recognition_confidence", None)
        available = bool(
            getattr(diagnostics, "recognition_confidence_available", False)
        )
        rejection = str(getattr(wake_result, "rejection_reason", "") or "none")
        classification = _classification(wake_result)
    else:
        recognizer = "whisper_active_command"
        raw = str(getattr(diagnostics, "raw_transcript", "") or "")
        normalized = str(
            getattr(diagnostics, "alias_canonicalized_transcript", "")
            or getattr(result, "normalized_input", "")
            or ""
        )
        confidence = None
        available = False
        rejection = str(getattr(result, "error_code", "") or "none")
        classification = str(getattr(result, "command_category", "") or "ordinary")
    output_func(f"  Recognizer used: {recognizer}")
    output_func(
        "  Raw recognition result: "
        + (raw if diagnostic_enabled and raw else "<diagnostics disabled or unavailable>")
    )
    output_func(f"  Normalized phrase: {normalized or '<none>'}")
    output_func(
        "  Confidence: "
        + (f"{float(confidence):.3f}" if available and confidence is not None else "unavailable")
    )
    output_func(f"  Classification result: {classification}")
    output_func(f"  Rejection reason: {rejection}")


def _retry_allowed(label: str, result: Any) -> bool:
    if label != "E":
        return True
    return str(getattr(result, "status", "") or "") in {
        "input_timeout",
        "transcription_failed",
    }


def _print_transcript_summary(
    output_func: Callable[[str], None],
    wake_transcripts: Sequence[str],
    active_command_transcripts: Sequence[str],
) -> None:
    output_func(
        "Wake transcripts: "
        + (" | ".join(wake_transcripts) if wake_transcripts else "none")
    )
    output_func(
        "Active-command transcripts: "
        + (
            " | ".join(active_command_transcripts)
            if active_command_transcripts
            else "none"
        )
    )


def main() -> int:
    return run_hardware_verification()


if __name__ == "__main__":
    raise SystemExit(main())
