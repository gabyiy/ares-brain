from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import BRAIN_ACTIVE, BRAIN_STANDBY, BRAIN_STOPPED  # noqa: E402
from memory.schema_migrations import StoreWriteLock  # noqa: E402
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
    return parser


def run_hardware_verification(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    runtime_factory: Optional[Callable[..., tuple[Any, Any, Any]]] = None,
) -> int:
    with TemporaryDirectory(prefix="ares-wake-verifier-lock-") as directory:
        with StoreWriteLock(
            Path(directory) / "hardware_verifier.runtime",
            owner_kind="ares_wake_hardware_verifier",
        ):
            return _run_hardware_verification_locked(
                argv,
                output_func=output_func,
                runtime_factory=runtime_factory,
            )


def _run_hardware_verification_locked(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    runtime_factory: Optional[Callable[..., tuple[Any, Any, Any]]] = None,
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
        stages = (
            STAGES[:2]
            if args.verification_mode == "reliability"
            else STAGES
        )
        for index, stage in enumerate(stages, start=1):
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
            if stage.label == "B" and reliability_attempts:
                reliable = _run_wake_reliability(
                    runtime,
                    reliability_attempts,
                    output_func=output_func,
                    diagnostic_enabled=bool(args.diagnostic_wake),
                    wake_transcripts=wake_transcripts,
                )
                if not reliable:
                    runtime.shutdown(reason="wake_reliability_target_not_met")
                    output_func("Cleanup completed; wake reliability target was not met.")
                    return 1
        if args.verification_mode == "reliability":
            runtime.shutdown(reason="wake_reliability_verification_complete")
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
        runtime.shutdown(reason="keyboard_interrupt")
        output_func("Verification cancelled; cleanup completed without replaying captured audio.")
        return 130


def _run_wake_reliability(
    runtime: Any,
    attempts: int,
    *,
    output_func: Callable[[str], None],
    diagnostic_enabled: bool,
    wake_transcripts: list[str],
) -> bool:
    required = math.ceil(attempts * 0.9)
    accepted = 0
    rejected = 0
    records: list[str] = []
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
    for attempt in range(1, attempts + 1):
        output_func(
            f"Reliability attempt {attempt}/{attempts}: Say 'Ares' once, then remain silent."
        )
        before = runtime.snapshot()
        request = runtime.build_standby_wake_request()
        wake_result = listener.listen_once(request)
        after = runtime.snapshot()
        diagnostics = getattr(listener, "last_diagnostics", None)
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
            wake_result = listener.listen_once(request)
            after = runtime.snapshot()
            diagnostics = getattr(listener, "last_diagnostics", None)
            transcript = str(getattr(diagnostics, "raw_transcript", "") or "")
            if diagnostic_enabled and transcript and transcript not in wake_transcripts:
                wake_transcripts.append(transcript)
            output_func(
                "  Confirmation result: "
                f"{'accepted' if getattr(wake_result, 'wake_detected', False) else 'rejected'}; "
                f"count={int(getattr(wake_result, 'confirmation_count', 0) or 0)}/"
                f"{int(getattr(wake_result, 'confirmation_required_count', 0) or 0)}"
            )
        success = bool(
            getattr(wake_result, "wake_detected", False)
            and after.current_lifecycle_state == BRAIN_STANDBY
            and not after.session_id
        )
        if success:
            accepted += 1
        else:
            rejected += 1
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
    output_func(
        f"Wake reliability result: {accepted}/{attempts} accepted; "
        f"rejected={rejected}; target={required}/{attempts}."
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
    return accepted >= required


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
