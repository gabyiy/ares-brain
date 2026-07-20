from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    BRAIN_ACTIVE,
    BRAIN_STANDBY,
    BRAIN_STOPPED,
    inspect_linux_alsa_capture,
)
from core.StandbyWakeListener import WakeAttemptResult  # noqa: E402
from events import EventHistoryStore  # noqa: E402
from memory.schema_migrations import StoreWriteLock  # noqa: E402
from scripts.inspect_ares_runtime_state import (  # noqa: E402
    inspect_ares_runtime_state,
    render_runtime_state_report,
)
from scripts import run_ares_standby_voice as standby_voice  # noqa: E402
from scripts import run_ares_voice as single_voice_launcher  # noqa: E402


@dataclass(frozen=True)
class HardwareTestStage:
    label: str
    instruction: str
    expected: str


@dataclass(frozen=True)
class _VerifierWakeAttempt:
    attempt_id: str
    candidate_id: str
    stream_generation: int
    capture_valid: bool
    recognizer_invoked: bool
    infrastructure_failure: bool
    lifecycle_state_before: str
    lifecycle_state_after: str
    cleanup_status: str
    result: Any
    diagnostics: Any
    strict_consistency: bool = False


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
    parser.add_argument(
        "--wake-reliability-attempts",
        type=int,
        default=0,
        help="Run the quiet-room wake reliability check with this many prompts (1-20).",
    )
    parser.add_argument(
        "--verification-mode",
        choices=("all", "reliability", "lifecycle"),
        default="all",
    )
    parser.add_argument(
        "--wake-attempt-pause-seconds",
        type=float,
        default=0.75,
        help="Quiet pause between reliability candidates (0.25-3.0 seconds).",
    )
    return parser


def run_hardware_verification(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    runtime_factory: Optional[Callable[..., tuple[Any, Any, Any]]] = None,
) -> int:
    runtime_holder: dict[str, Any] = {}
    try:
        with TemporaryDirectory(prefix="ares-wake-verifier-lock-") as directory:
            support_directory = Path(directory)
            with StoreWriteLock(
                support_directory / "hardware_verifier.runtime",
                owner_kind="ares_wake_hardware_verifier",
            ):
                with standby_voice._termination_signal_scope():
                    return _run_hardware_verification_locked(
                        argv,
                        output_func=output_func,
                        runtime_factory=runtime_factory,
                        support_directory=support_directory,
                        runtime_holder=runtime_holder,
                    )
    except standby_voice.RuntimeTerminationRequested as error:
        runtime = runtime_holder.get("runtime")
        if runtime is not None:
            runtime.shutdown(reason=f"signal_{error.signum}_during_preflight")
        output_func(
            f"Verification terminated by signal {error.signum}; verifier lock released."
        )
        return 128 + error.signum
    except KeyboardInterrupt:
        runtime = runtime_holder.get("runtime")
        if runtime is not None:
            runtime.shutdown(reason="keyboard_interrupt_during_preflight")
        output_func("Verification cancelled during preflight; resources and locks cleaned up.")
        return 130


def _run_hardware_verification_locked(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    runtime_factory: Optional[Callable[..., tuple[Any, Any, Any]]] = None,
    support_directory: Optional[Path] = None,
    runtime_holder: Optional[dict[str, Any]] = None,
) -> int:
    args = build_parser().parse_args(argv)
    if isinstance(args.attempts_per_test, bool) or not 1 <= args.attempts_per_test <= 5:
        output_func("Configuration error: --attempts-per-test must be between 1 and 5.")
        return 2
    if (
        isinstance(args.wake_reliability_attempts, bool)
        or not 0 <= args.wake_reliability_attempts <= 20
    ):
        output_func(
            "Configuration error: --wake-reliability-attempts must be between 1 and 20, or 0 to skip."
        )
        return 2
    if (
        isinstance(args.wake_attempt_pause_seconds, bool)
        or not math.isfinite(args.wake_attempt_pause_seconds)
        or not 0.25 <= args.wake_attempt_pause_seconds <= 3.0
    ):
        output_func(
            "Configuration error: --wake-attempt-pause-seconds must be between 0.25 and 3.0."
        )
        return 2
    reliability_attempts = args.wake_reliability_attempts
    if args.verification_mode == "reliability" and reliability_attempts == 0:
        reliability_attempts = 10
    if args.retain_diagnostic_audio and not args.diagnostic_wake:
        output_func(
            "Configuration error: --retain-diagnostic-audio requires --diagnostic-wake."
        )
        return 2
    issue = "" if runtime_factory is not None else standby_voice._validate_static_dependencies(args)
    if issue:
        output_func(f"Dependency error: {issue}")
        return 2
    if runtime_factory is None:
        output_func("Phase 1: ARES process and lock preflight.")
        try:
            process_report = inspect_ares_runtime_state(
                recover_if_owner_dead=True,
            )
        except (OSError, ValueError) as error:
            output_func(f"Process and lock preflight failed: {error}")
            return 3
        render_runtime_state_report(process_report, output_func)
        if process_report.live_runtime_conflict:
            output_func(
                "Preflight stopped: another live ARES production runtime owns "
                "or may own the microphone."
            )
            return 3
    try:
        factory = runtime_factory or standby_voice.create_runtime
        if runtime_factory is None:
            support = support_directory or Path(".")
            verifier_history = EventHistoryStore(
                support / "event_history.json",
                warning_callback=output_func,
            )
            runtime, pipeline, request = factory(
                args,
                output_func=output_func,
                event_history_store=verifier_history,
            )
        else:
            runtime, pipeline, request = factory(args, output_func=output_func)
        if runtime_holder is not None:
            runtime_holder["runtime"] = runtime
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        output_func(f"Setup failed: {error}")
        return 2
    preflight = single_voice_launcher._preflight_pipeline(pipeline, request)
    if not preflight.success:
        output_func("Command voice pipeline health check failed before capture.")
        return 3
    calibration_seconds = float(
        getattr(
            getattr(runtime.standby_wake_listener, "config", None),
            "calibration_duration_seconds",
            3.0,
        )
    )
    output_func(
        "Phase 2: Remain silent while ARES calibrates "
        f"(approximately {calibration_seconds:.0f} seconds)."
    )
    wake_started = runtime.standby_wake_listener.start(runtime_id=runtime.runtime_id)
    for line in standby_voice.render_standby_calibration_diagnostics(
        dict(getattr(wake_started, "data", {}) or {}),
        include_energy_summary=bool(args.diagnostic_wake),
    ):
        output_func(line)
    if not wake_started.success:
        component_health = (
            wake_started
            if dict(getattr(wake_started, "data", {}) or {}).get("failing_subsystem")
            else _component_health(runtime.standby_wake_listener, runtime.runtime_id)
        )
        runtime.standby_wake_listener.stop("preflight_failed")
        output_func(
            "Wake listener startup failed: "
            f"{wake_started.error_code or wake_started.status} "
            f"({wake_started.error_message or wake_started.status})."
        )
        _print_component_health(output_func, component_health)
        _print_capture_hardware_diagnostics(output_func, runtime)
        return 3
    wake_health = runtime.standby_wake_listener.health(runtime_id=runtime.runtime_id)
    component_health = _component_health(runtime.standby_wake_listener, runtime.runtime_id)
    _print_component_health(output_func, component_health)
    if not wake_health.success:
        runtime.standby_wake_listener.stop("preflight_failed")
        output_func(
            "Wake listener health check failed: "
            f"{wake_health.error_code or wake_health.status} "
            f"({wake_health.error_message or wake_health.status})."
        )
        return 3
    _print_capture_hardware_diagnostics(output_func, runtime)

    output_func("ARES bounded standby-wake hardware verification")
    output_func(f"Attempts per test: {args.attempts_per_test}")
    output_func("Owner microphone audio is retained only when explicitly requested and never replayed.")
    started = runtime.start()
    if not started.success:
        output_func(f"Runtime start failed: {started.error_code or started.status}")
        runtime.shutdown(reason="hardware_verifier_start_failed")
        return 3

    first_session = ""
    wake_transcripts: list[str] = []
    active_command_transcripts: list[str] = []
    cleanup_done = False

    def shutdown_once(reason: str) -> None:
        nonlocal cleanup_done
        if cleanup_done:
            return
        runtime.shutdown(reason=reason)
        cleanup_done = True

    try:
        stages = (
            STAGES[:2]
            if args.verification_mode == "reliability"
            else STAGES
        )
        for index, stage in enumerate(stages, start=1):
            if stage.label == "C":
                output_func("Phase 4: Full wake, command, standby, and shutdown lifecycle.")
            output_func(f"Test {index}/{len(STAGES)} ({stage.label}): {stage.instruction}")
            output_func(f"Expected: {stage.expected}.")
            passed = False
            for attempt in range(1, args.attempts_per_test + 1):
                before = runtime.snapshot()
                result = runtime.poll_once()
                snapshot = runtime.snapshot()
                standby_attempt = before.current_lifecycle_state == BRAIN_STANDBY
                wake_attempt = (
                    _completed_runtime_attempt(
                        runtime.standby_wake_listener,
                        result,
                        before.current_lifecycle_state,
                        snapshot.current_lifecycle_state,
                    )
                    if standby_attempt
                    else None
                )
                wake_result = wake_attempt.result if wake_attempt is not None else None
                diagnostics = (
                    wake_attempt.diagnostics
                    if wake_attempt is not None
                    else getattr(runtime.input_adapter, "last_diagnostics", None)
                )
                if standby_attempt and wake_attempt is not None:
                    consistency_error = _wake_attempt_consistency_error(wake_attempt)
                    if consistency_error:
                        output_func(
                            "  TEST FRAMEWORK FAILURE: inconsistent wake attempt: "
                            f"{consistency_error}"
                        )
                        shutdown_once("inconsistent_wake_attempt")
                        return 1
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
                if wake_attempt is not None:
                    output_func(
                        "  Attempt identity: "
                        f"attempt={wake_attempt.attempt_id}; "
                        f"generation={wake_attempt.stream_generation}; "
                        f"candidate={wake_attempt.candidate_id}; "
                        f"capture_valid={'yes' if wake_attempt.capture_valid else 'no'}; "
                        f"recognizer_invoked={'yes' if wake_attempt.recognizer_invoked else 'no'}; "
                        f"cleanup={wake_attempt.cleanup_status}"
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
                shutdown_once(f"hardware_verification_{stage.label}_failed")
                output_func("Cleanup completed; verification did not pass.")
                return 1
            if stage.label == "B" and reliability_attempts:
                output_func("Phase 3: Ten-attempt standby wake reliability check.")
                reliable = _run_wake_reliability(
                    runtime,
                    reliability_attempts,
                    output_func=output_func,
                    diagnostic_enabled=bool(args.diagnostic_wake),
                    wake_transcripts=wake_transcripts,
                    pause_seconds=args.wake_attempt_pause_seconds,
                    sleeper=time.sleep,
                )
                if not reliable:
                    shutdown_once("wake_reliability_target_not_met")
                    output_func("Cleanup completed; wake reliability target was not met.")
                    return 1
        if args.verification_mode == "reliability":
            shutdown_once("wake_reliability_verification_complete")
            output_func("Wake reliability verification completed; adapters cleaned up.")
            return 0
        if args.diagnostic_wake:
            _print_transcript_summary(
                output_func,
                wake_transcripts,
                active_command_transcripts,
            )
        output_func("All bounded hardware stages passed; adapters cleaned up.")
        return 0
    except KeyboardInterrupt:
        shutdown_once("keyboard_interrupt")
        output_func("Verification cancelled; cleanup completed without replaying captured audio.")
        return 130
    except standby_voice.RuntimeTerminationRequested as error:
        shutdown_once(f"signal_{error.signum}")
        output_func(
            f"Verification received signal {error.signum}; resources and locks cleaned up."
        )
        return 128 + error.signum
    except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
        shutdown_once("hardware_verifier_error")
        output_func(
            "Verification failed and cleaned up: "
            f"{error.__class__.__name__}:{str(error)[:160]}"
        )
        return 1
    finally:
        if not cleanup_done:
            try:
                state = runtime.snapshot().current_lifecycle_state
            except (AttributeError, RuntimeError, TypeError, ValueError):
                state = ""
            if state != BRAIN_STOPPED:
                shutdown_once("hardware_verifier_finally")


def _run_wake_reliability(
    runtime: Any,
    attempts: int,
    *,
    output_func: Callable[[str], None],
    diagnostic_enabled: bool,
    wake_transcripts: list[str],
    pause_seconds: float = 0.0,
    sleeper: Callable[[float], None] = lambda _seconds: None,
) -> bool:
    required = math.ceil(attempts * 0.9)
    accepted = 0
    rejected = 0
    infrastructure_failures = 0
    maximum_infrastructure_failures = 3
    records: list[str] = []
    failure_categories = {
        "no_speech": 0,
        "maximum_duration_reached": 0,
        "unknown_token": 0,
        "exact_phrase_mismatch": 0,
        "low_confidence": 0,
        "other": 0,
    }
    listener = runtime.standby_wake_listener
    initial_runtime = runtime.snapshot()
    initial_stream = listener.snapshot(runtime_id=runtime.runtime_id)
    baseline_opens = int(getattr(initial_stream, "stream_open_count", 0) or 0)
    baseline_calibrations = int(
        getattr(initial_stream, "calibration_count", 0) or 0
    )
    baseline_stream_id = str(
        getattr(initial_stream, "stream_instance_id", "") or ""
    )
    if (
        initial_runtime.current_lifecycle_state != BRAIN_STANDBY
        or initial_runtime.session_id
        or not bool(getattr(initial_stream, "stream_active", False))
    ):
        output_func(
            "Wake reliability setup failed: runtime must remain in STANDBY "
            "with one active listener stream and no session."
        )
        return False
    output_func(
        f"Wake reliability test: {attempts} prompted attempts; required acceptance={required}."
    )
    output_func(
        "Reliability mode classifies wake candidates without activation playback, "
        "so the same standby stream remains owned for all prompts."
    )
    output_func("The silence and unrelated-speech checks above must also remain false-activation free.")
    valid_attempt = 0
    while valid_attempt < attempts:
        attempt = valid_attempt + 1
        output_func(
            f"Reliability attempt {attempt}/{attempts}: Say 'Ares' once, then remain silent."
        )
        before = runtime.snapshot()
        request = runtime.build_standby_wake_request()
        wake_attempt = _listen_verifier_attempt(
            listener,
            request,
            lifecycle_state_before=before.current_lifecycle_state,
        )
        wake_result = wake_attempt.result
        after = runtime.snapshot()
        diagnostics = wake_attempt.diagnostics
        consistency_error = _wake_attempt_consistency_error(wake_attempt)
        if consistency_error:
            output_func(
                "  TEST FRAMEWORK FAILURE: inconsistent wake attempt: "
                f"{consistency_error}"
            )
            return False
        output_func(
            "  Attempt identity: "
            f"attempt={wake_attempt.attempt_id}; "
            f"generation={wake_attempt.stream_generation}; "
            f"candidate={wake_attempt.candidate_id}; "
            f"capture_valid={'yes' if wake_attempt.capture_valid else 'no'}; "
            f"recognizer_invoked={'yes' if wake_attempt.recognizer_invoked else 'no'}; "
            f"cleanup={wake_attempt.cleanup_status}"
        )
        if wake_attempt.infrastructure_failure:
            infrastructure_failures += 1
            output_func(
                "  Infrastructure failure (excluded from recognition denominator): "
                f"{getattr(wake_result, 'error_code', '') or getattr(wake_result, 'stop_reason', '') or getattr(wake_result, 'status', '')}"
            )
            if infrastructure_failures >= maximum_infrastructure_failures:
                output_func(
                    "  Infrastructure retry budget exhausted before ten valid wake attempts."
                )
                return False
            continue
        valid_attempt += 1
        transcript = str(getattr(diagnostics, "raw_transcript", "") or "")
        if diagnostic_enabled and transcript and transcript not in wake_transcripts:
            wake_transcripts.append(transcript)
        if bool(getattr(wake_result, "confirmation_required", False)):
            first_confidence = getattr(wake_result, "recognition_confidence", None)
            first_decision = str(
                getattr(wake_result, "classification_reason", "")
                or getattr(wake_result, "rejection_reason", "")
                or "medium_confidence_confirmation_required"
            )
            output_func("  Low-confidence wake detected. Say Ares once more.")
            output_func(
                "  Confirmation request: "
                f"transcript={transcript or '<unavailable>'}; "
                "confidence="
                + (
                    f"{float(first_confidence):.3f}"
                    if first_confidence is not None
                    else "unavailable"
                )
                + f"; decision={first_decision}"
            )
            records.append(
                f"{attempt}.1:confirmation_required:"
                f"{transcript or '<unavailable>'}:"
                + (
                    f"{float(first_confidence):.3f}"
                    if first_confidence is not None
                    else "unavailable"
                )
                + f":{first_decision}"
            )
            confirmation_attempt = _listen_verifier_attempt(
                listener,
                request,
                lifecycle_state_before=before.current_lifecycle_state,
            )
            consistency_error = _wake_attempt_consistency_error(confirmation_attempt)
            if consistency_error:
                output_func(
                    "  TEST FRAMEWORK FAILURE: inconsistent confirmation attempt: "
                    f"{consistency_error}"
                )
                return False
            wake_result = confirmation_attempt.result
            after = runtime.snapshot()
            diagnostics = confirmation_attempt.diagnostics
            if confirmation_attempt.infrastructure_failure:
                infrastructure_failures += 1
                valid_attempt -= 1
                output_func(
                    "  Confirmation infrastructure failure "
                    "(excluded from recognition denominator): "
                    f"{getattr(wake_result, 'error_code', '') or getattr(wake_result, 'stop_reason', '') or getattr(wake_result, 'status', '')}"
                )
                if infrastructure_failures >= maximum_infrastructure_failures:
                    output_func(
                        "  Infrastructure retry budget exhausted before ten valid wake attempts."
                    )
                    return False
                continue
            transcript = str(getattr(diagnostics, "raw_transcript", "") or "")
            if diagnostic_enabled and transcript and transcript not in wake_transcripts:
                wake_transcripts.append(transcript)
            output_func(
                "  Confirmation result: "
                f"{'accepted' if getattr(wake_result, 'wake_detected', False) else 'rejected'}; "
                f"count={int(getattr(wake_result, 'confirmation_count', 0) or 0)}/"
                f"{int(getattr(wake_result, 'confirmation_required_count', 0) or 0)}"
            )
        _print_wake_capture_metrics(output_func, wake_result, diagnostics)
        success = bool(
            getattr(wake_result, "wake_detected", False)
            and after.current_lifecycle_state == BRAIN_STANDBY
            and not after.session_id
        )
        if success:
            accepted += 1
        else:
            rejected += 1
            failure_categories[_wake_failure_category(wake_result)] += 1
        confidence = getattr(wake_result, "recognition_confidence", None)
        clipped = bool(getattr(diagnostics, "beginning_clipped", False))
        decision = str(
            getattr(wake_result, "classification_reason", "")
            or getattr(wake_result, "rejection_reason", "")
            or "unclassified"
        )
        confidence_text = (
            f"{float(confidence):.3f}" if confidence is not None else "unavailable"
        )
        records.append(
            f"{attempt}:{'accepted' if success else 'rejected'}:"
            f"{transcript or '<unavailable>'}:{confidence_text}:{decision}"
        )
        output_func(
            "  Result: "
            f"{'accepted' if success else 'rejected'}; "
            f"transcript={transcript if diagnostic_enabled and transcript else '<local diagnostics disabled>'}; "
            f"confidence={confidence_text}; decision={decision}"
        )
        output_func(
            "  Capture: "
            f"candidate={float(getattr(wake_result, 'duration_seconds', 0.0)):.3f}s; "
            f"beginning_clipped={'yes' if clipped else 'no'}; "
            f"stream_opens={int(getattr(wake_result, 'stream_open_count', 0))}; "
            f"calibrations={int(getattr(wake_result, 'calibration_count', 0))}; "
            f"stream_id={getattr(wake_result, 'stream_instance_id', '') or 'unknown'}; "
            f"handle_id={getattr(wake_result, 'alsa_handle_id', '') or 'unknown'}"
        )
        output_func(
            "  Lifecycle: "
            f"{before.current_lifecycle_state}->{after.current_lifecycle_state}; "
            "session_created=no (classification-only reliability probe)"
        )
        current_stream = listener.snapshot(runtime_id=runtime.runtime_id)
        opens = int(getattr(current_stream, "stream_open_count", 0) or 0)
        calibrations = int(getattr(current_stream, "calibration_count", 0) or 0)
        stream_id = str(getattr(current_stream, "stream_instance_id", "") or "")
        if (
            opens != baseline_opens
            or calibrations != baseline_calibrations
            or (baseline_stream_id and stream_id != baseline_stream_id)
        ):
            output_func(
                "  FAIL: standby stream changed during classification-only reliability "
                f"probe (opens {baseline_opens}->{opens}, calibrations "
                f"{baseline_calibrations}->{calibrations}, stream "
                f"{baseline_stream_id or 'unknown'}->{stream_id or 'unknown'})."
            )
            _print_stream_reason_summary(output_func, current_stream)
            return False
        if valid_attempt < attempts and pause_seconds > 0:
            output_func(
                f"  Pausing {pause_seconds:.2f}s so candidate audio cannot carry into the next prompt."
            )
            sleeper(pause_seconds)
    output_func(
        f"Wake reliability result: {accepted}/{attempts} accepted; "
        f"rejected={rejected}; infrastructure_failures={infrastructure_failures}; "
        f"target={required}/{attempts}."
    )
    final_stream = listener.snapshot(runtime_id=runtime.runtime_id)
    output_func(
        "Wake reliability stream result: "
        f"opens={getattr(final_stream, 'stream_open_count', 0)}; "
        f"calibrations={getattr(final_stream, 'calibration_count', 0)}; "
        f"stream_id={getattr(final_stream, 'stream_instance_id', '') or 'unknown'}."
    )
    _print_stream_reason_summary(output_func, final_stream)
    output_func("Wake reliability attempt decisions:")
    for record in records:
        output_func(f"  {record}")
    output_func(
        "Wake reliability failure categories: "
        + "; ".join(
            f"{name}={count}" for name, count in failure_categories.items()
        )
    )
    return accepted >= required


def _listen_verifier_attempt(
    listener: Any,
    request: Any,
    *,
    lifecycle_state_before: str,
) -> WakeAttemptResult | _VerifierWakeAttempt:
    listen_attempt = getattr(listener, "listen_attempt", None)
    if callable(listen_attempt):
        attempt = listen_attempt(request)
        if not isinstance(attempt, WakeAttemptResult):
            raise RuntimeError("wake listener returned malformed WakeAttemptResult")
        return attempt
    result = listener.listen_once(request)
    diagnostics = getattr(listener, "last_diagnostics", None)
    candidate = int(getattr(result, "candidate_number", 0) or 0)
    return _VerifierWakeAttempt(
        attempt_id=str(getattr(result, "attempt_id", "") or f"legacy-attempt-{candidate}"),
        candidate_id=str(
            getattr(result, "candidate_id", "") or f"legacy-candidate-{candidate}"
        ),
        stream_generation=int(getattr(result, "stream_generation", 0) or 0),
        capture_valid=bool(
            getattr(result, "capture_valid", False)
            or float(getattr(result, "duration_seconds", 0.0) or 0.0) > 0
        ),
        recognizer_invoked=bool(
            getattr(result, "recognizer_invoked", False)
            or getattr(result, "recognizer_name", "")
            or getattr(diagnostics, "raw_transcript", "")
        ),
        infrastructure_failure=bool(
            getattr(result, "infrastructure_failure", False)
        ),
        lifecycle_state_before=lifecycle_state_before,
        lifecycle_state_after=lifecycle_state_before,
        cleanup_status=str(getattr(result, "cleanup_status", "not_required")),
        result=result,
        diagnostics=diagnostics,
        strict_consistency=False,
    )


def _completed_runtime_attempt(
    listener: Any,
    runtime_result: Any,
    lifecycle_state_before: str,
    lifecycle_state_after: str,
) -> Optional[WakeAttemptResult | _VerifierWakeAttempt]:
    data = dict(getattr(runtime_result, "data", {}) or {})
    attempt_id = str(data.get("wake_attempt_id") or "")
    completed = getattr(listener, "completed_attempt", None)
    if attempt_id and callable(completed):
        attempt = completed(attempt_id)
        if not isinstance(attempt, WakeAttemptResult):
            raise RuntimeError(
                f"completed wake attempt {attempt_id} is unavailable or malformed"
            )
        return attempt
    current = getattr(listener, "last_attempt", None)
    if isinstance(current, WakeAttemptResult):
        if attempt_id and current.attempt_id != attempt_id:
            raise RuntimeError("runtime wake attempt ID does not match listener result")
        return current
    result = getattr(listener, "last_result", None)
    if result is None:
        return None
    diagnostics = getattr(listener, "last_diagnostics", None)
    candidate = int(getattr(result, "candidate_number", 0) or 0)
    return _VerifierWakeAttempt(
        attempt_id=str(getattr(result, "attempt_id", "") or f"legacy-attempt-{candidate}"),
        candidate_id=str(
            getattr(result, "candidate_id", "") or f"legacy-candidate-{candidate}"
        ),
        stream_generation=int(getattr(result, "stream_generation", 0) or 0),
        capture_valid=bool(
            getattr(result, "capture_valid", False)
            or float(getattr(result, "duration_seconds", 0.0) or 0.0) > 0
        ),
        recognizer_invoked=bool(
            getattr(result, "recognizer_invoked", False)
            or getattr(diagnostics, "raw_transcript", "")
        ),
        infrastructure_failure=bool(
            getattr(result, "infrastructure_failure", False)
        ),
        lifecycle_state_before=lifecycle_state_before,
        lifecycle_state_after=lifecycle_state_after,
        cleanup_status=str(getattr(result, "cleanup_status", "not_required")),
        result=result,
        diagnostics=diagnostics,
        strict_consistency=False,
    )


def _wake_attempt_consistency_error(attempt: Any) -> str:
    if not isinstance(attempt, WakeAttemptResult):
        return ""
    result = attempt.result
    diagnostics = attempt.diagnostics
    if result.attempt_id != attempt.attempt_id:
        return "result_attempt_id_mismatch"
    if result.candidate_id != attempt.candidate_id:
        return "result_candidate_id_mismatch"
    if result.stream_generation != attempt.stream_generation:
        return "result_stream_generation_mismatch"
    if diagnostics is not None:
        if diagnostics.attempt_id != attempt.attempt_id:
            return "diagnostic_attempt_id_mismatch"
        if diagnostics.candidate_id != attempt.candidate_id:
            return "diagnostic_candidate_id_mismatch"
        if diagnostics.stream_generation != attempt.stream_generation:
            return "diagnostic_stream_generation_mismatch"
    raw_text = str(getattr(diagnostics, "raw_transcript", "") or "")
    raw_result = str(getattr(diagnostics, "raw_recognition_result", "") or "")
    if not attempt.capture_valid:
        if attempt.recognizer_invoked:
            return "recognizer_invoked_for_invalid_capture"
        if raw_text or raw_result:
            return "recognition_leaked_into_invalid_capture"
        if result.recognition_confidence is not None:
            return "confidence_leaked_into_invalid_capture"
    if attempt.recognizer_invoked:
        if not attempt.capture_valid:
            return "recognizer_invoked_without_valid_capture"
        if float(result.duration_seconds or 0.0) <= 0:
            return "recognition_has_zero_duration_audio"
        if (
            result.sample_rate_hz != 16000
            or result.channels != 1
            or result.sample_width_bytes != 2
        ):
            return "recognition_audio_format_not_canonical"
    if result.wake_detected and not attempt.recognizer_invoked:
        return "wake_detected_without_recognizer"
    return ""


def _component_health(listener: Any, runtime_id: str) -> Any:
    component_health = getattr(listener, "component_health", None)
    if callable(component_health):
        return component_health(runtime_id=runtime_id)
    return listener.health(runtime_id=runtime_id)


def _print_component_health(
    output_func: Callable[[str], None],
    health: Any,
) -> None:
    data = dict(getattr(health, "data", {}) or {})
    output_func("Wake component health:")
    output_func(
        "  Vosk model healthy: "
        f"{'yes' if data.get('wake_model_healthy') else 'no'}"
    )
    output_func(
        "  Microphone adapter healthy: "
        f"{'yes' if data.get('microphone_adapter_healthy') else 'no'}"
    )
    output_func(
        "  ALSA device open: "
        f"{'yes' if data.get('alsa_device_open') else 'no'}"
    )
    output_func(
        "  ALSA open attempt succeeded / closed during cleanup: "
        f"{'yes' if data.get('alsa_device_open_attempt_succeeded') else 'no'} / "
        f"{'yes' if data.get('alsa_device_closed_during_cleanup') else 'no'}"
    )
    output_func(
        "  Valid PCM received: "
        f"{'yes' if data.get('valid_pcm_received') else 'no'}"
    )
    output_func(
        "  Calibration successful: "
        f"{'yes' if data.get('calibration_healthy') else 'no'}"
    )
    output_func(
        "  Calibration quality passed: "
        f"{'yes' if data.get('calibration_quality_passed') else 'no'}; "
        f"attempts={int(data.get('calibration_attempt_count', 0) or 0)}; "
        f"reason={data.get('calibration_error_code') or 'none'}"
    )
    output_func(
        "  Stream state: "
        f"{data.get('stream_state') or 'unknown'}; "
        f"generation={data.get('stream_generation', 0)}; "
        f"last recovery={data.get('stream_open_reason') or 'none'}; "
        f"failing subsystem={data.get('failing_subsystem') or 'none'}"
    )


def _print_wake_capture_metrics(
    output_func: Callable[[str], None],
    wake_result: Any,
    diagnostics: Any,
) -> None:
    output_func(
        "  Wake VAD: "
        f"noise_floor={float(getattr(wake_result, 'ambient_noise_floor', 0.0) or 0.0):.1f}; "
        f"start={float(getattr(wake_result, 'speech_start_threshold', 0.0) or 0.0):.1f}; "
        f"continue={float(getattr(wake_result, 'speech_continue_threshold', 0.0) or 0.0):.1f}; "
        f"end={float(getattr(wake_result, 'speech_end_threshold', 0.0) or 0.0):.1f}; "
        f"speech_frames={int(getattr(wake_result, 'speech_frame_count', 0) or 0)}; "
        f"quiet_frames={int(getattr(wake_result, 'terminal_quiet_frame_count', 0) or 0)}; "
        f"terminal_silence={float(getattr(wake_result, 'terminal_silence_duration_seconds', 0.0) or 0.0):.3f}s"
    )
    output_func(
        "  Wake audio: "
        f"raw={float(getattr(wake_result, 'raw_capture_duration_seconds', 0.0) or 0.0):.3f}s; "
        f"assembled={float(getattr(wake_result, 'assembled_duration_seconds', 0.0) or 0.0):.3f}s; "
        f"trimmed={float(getattr(wake_result, 'trimmed_duration_seconds', 0.0) or 0.0):.3f}s; "
        f"trim_leading={float(getattr(wake_result, 'leading_trimmed_seconds', 0.0) or 0.0):.3f}s; "
        f"trim_trailing={float(getattr(wake_result, 'trailing_trimmed_seconds', 0.0) or 0.0):.3f}s; "
        f"pre_roll={int(getattr(wake_result, 'pre_roll_frames_retained', 0) or 0)}; "
        f"post_roll={int(getattr(wake_result, 'post_roll_frame_count', 0) or 0)}; "
        f"duplicate_pcm={int(getattr(wake_result, 'duplicate_pcm_frame_count', 0) or 0)}; "
        f"stale_discarded={int(getattr(wake_result, 'stale_pcm_frames_discarded', 0) or 0)}; "
        f"stop={getattr(wake_result, 'capture_stop_reason', '') or 'unknown'}"
    )
    output_func(
        "  Vosk decision: "
        f"raw_tokens={getattr(diagnostics, 'normalized_transcript', '') or '<unavailable>'}; "
        f"minimum_confidence={_format_optional_confidence(getattr(wake_result, 'minimum_word_confidence', None))}; "
        f"mean_confidence={_format_optional_confidence(getattr(wake_result, 'mean_word_confidence', None))}; "
        f"canonical_confidence={_format_optional_confidence(getattr(wake_result, 'canonical_confidence', None))}; "
        f"canonical_phrase={getattr(wake_result, 'canonical_wake_phrase', '') or '<none>'}; "
        f"duplicate_collapse={'yes' if getattr(wake_result, 'duplicate_collapse_used', False) else 'no'}; "
        f"decision={getattr(wake_result, 'classification_reason', '') or getattr(wake_result, 'rejection_reason', '') or 'unclassified'}"
    )


def _wake_failure_category(wake_result: Any) -> str:
    if not bool(getattr(wake_result, "speech_detected", False)):
        return "no_speech"
    stop = str(getattr(wake_result, "capture_stop_reason", "") or "")
    reason = str(
        getattr(wake_result, "rejection_reason", "")
        or getattr(wake_result, "classification_reason", "")
        or ""
    )
    if stop == "maximum_duration_reached":
        return "maximum_duration_reached"
    if "unknown_token" in reason:
        return "unknown_token"
    if "exact_constrained_phrase_not_matched" in reason:
        return "exact_phrase_mismatch"
    if "confidence" in reason:
        return "low_confidence"
    return "other"


def _format_optional_confidence(value: Any) -> str:
    return "unavailable" if value is None else f"{float(value):.3f}"


def _print_capture_hardware_diagnostics(
    output_func: Callable[[str], None],
    runtime: Any,
) -> None:
    listener = getattr(runtime, "standby_wake_listener", None)
    config = getattr(listener, "config", None)
    microphone = getattr(listener, "microphone_adapter", None)
    device = str(
        getattr(config, "microphone_device", "")
        or getattr(microphone, "device", "")
        or ""
    )
    diagnostics = inspect_linux_alsa_capture(device)
    output_func(
        "ALSA capture diagnostics: "
        f"device={diagnostics.get('capture_device', 'unknown')}; "
        f"format={diagnostics.get('sample_rate_hz', 0)}Hz/"
        f"{diagnostics.get('channels', 0)}ch/"
        f"{diagnostics.get('sample_width_bytes', 0)}B; "
        f"mixer={diagnostics.get('status', 'unknown')}"
    )
    output_func(
        "ALSA capture levels: "
        + (" | ".join(diagnostics.get("mixer_capture_levels", [])) or "not detected")
    )
    output_func(
        "ALSA microphone boost/input gain: "
        + (" | ".join(diagnostics.get("input_gain_controls", [])) or "not detected")
    )
    output_func(
        "ALSA automatic gain controls: "
        + (" | ".join(diagnostics.get("automatic_gain_controls", [])) or "not detected")
    )
    output_func("ALSA mixer settings were inspected only; no setting was changed.")
    if diagnostics.get("extreme_gain_warning"):
        output_func(
            "WARNING: a capture/boost control appears near its maximum; inspect the "
            "mixer setting manually if calibration remains noisy. No setting was changed."
        )


def _print_stream_reason_summary(
    output_func: Callable[[str], None],
    snapshot: Any,
) -> None:
    output_func(
        "  Stream reasons: opens="
        f"{list(getattr(snapshot, 'stream_open_reasons', []) or [])}; "
        f"closes={list(getattr(snapshot, 'stream_close_reasons', []) or [])}; "
        f"calibrations={list(getattr(snapshot, 'calibration_reasons', []) or [])}"
    )
    output_func(
        "  Ownership handoffs: "
        f"{list(getattr(snapshot, 'ownership_handoffs', []) or [])}"
    )


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
        confidence_tier = str(
            getattr(diagnostics, "confidence_tier", "")
            or getattr(wake_result, "confidence_tier", "")
            or "none"
        )
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
        confidence_tier = "not_applicable"
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
    output_func(f"  Confidence tier: {confidence_tier}")
    output_func(f"  Rejection reason: {rejection}")
    if before_state == BRAIN_STANDBY:
        output_func(
            "  Standby stream: "
            f"opens={int(getattr(wake_result, 'stream_open_count', 0) or 0)}; "
            f"calibrations={int(getattr(wake_result, 'calibration_count', 0) or 0)}; "
            f"candidate={int(getattr(wake_result, 'candidate_number', 0) or 0)}; "
            f"stream_id={getattr(wake_result, 'stream_instance_id', '') or 'unknown'}; "
            f"handle_id={getattr(wake_result, 'alsa_handle_id', '') or 'unknown'}"
        )
        output_func(
            "  Stream transition: "
            f"open_reason={getattr(wake_result, 'stream_open_reason', '') or 'none'}; "
            f"close_reason={getattr(wake_result, 'stream_close_reason', '') or 'none'}; "
            f"calibration_reason={getattr(wake_result, 'calibration_reason', '') or 'none'}; "
            f"handoff={getattr(wake_result, 'ownership_handoff_source', '') or 'none'}"
            "->"
            f"{getattr(wake_result, 'ownership_handoff_destination', '') or 'none'}"
        )
    else:
        output_func(
            "  Active audio: "
            f"capture={float(getattr(diagnostics, 'raw_capture_duration_seconds', 0.0)):.3f}s; "
            "finalized="
            f"{float(getattr(diagnostics, 'finalized_candidate_duration_seconds', 0.0)):.3f}s; "
            f"bytes={int(getattr(diagnostics, 'wav_byte_size', 0) or 0)}; "
            f"format={int(getattr(diagnostics, 'wav_sample_rate_hz', 0) or 0)}Hz/"
            f"{int(getattr(diagnostics, 'wav_channels', 0) or 0)}ch/"
            f"{int(getattr(diagnostics, 'wav_sample_width_bytes', 0) or 0)}B"
        )
        if diagnostic_enabled:
            output_func(
                "  Active WAV path: "
                f"{getattr(diagnostics, 'wav_path', '') or '<unavailable>'}"
            )
        output_func(
            "  Active transcription: "
            f"backend={getattr(diagnostics, 'transcription_backend', '') or 'unknown'}; "
            f"start={getattr(diagnostics, 'transcription_started_at', '') or 'unknown'}; "
            f"completion={getattr(diagnostics, 'transcription_completed_at', '') or 'timeout/not_completed'}; "
            f"elapsed={float(getattr(diagnostics, 'whisper_processing_duration_seconds', 0.0)):.3f}s; "
            f"status={getattr(diagnostics, 'transcription_status', '') or 'unknown'}"
        )
        output_func(
            "  Active routing: "
            f"result={getattr(result, 'status', '') or 'unknown'}; "
            f"lifecycle={getattr(diagnostics, 'lifecycle_state_before', '') or before_state}"
            "->"
            f"{getattr(diagnostics, 'lifecycle_state_after', '') or getattr(result, 'current_lifecycle_state', '') or 'unknown'}; "
            f"cleanup={getattr(diagnostics, 'temporary_audio_cleanup_status', '') or 'unknown'}"
        )


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
