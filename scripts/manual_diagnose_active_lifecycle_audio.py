from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import sys
import traceback
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    DEFAULT_SHUTDOWN_HIGH_CONFIDENCE,
    DEFAULT_STANDBY_HIGH_CONFIDENCE,
    SingleTurnFinalizedAudioDecision,
    SingleTurnPreBrainDecision,
    normalize_active_lifecycle_command,
)
from core.BrainRuntimeVoiceAdapters import (  # noqa: E402
    ACTIVE_COMMAND_CAPTURE_PROFILE,
    active_command_capture_request,
)
from core.LifecycleControl import LIFECYCLE_ACTION_NONE  # noqa: E402
from core.Contracts import new_correlation_id  # noqa: E402
from memory.schema_migrations import StoreWriteLock  # noqa: E402
from scripts import run_ares_standby_voice as standby_voice  # noqa: E402


DEFAULT_MICROPHONE_DEVICE = standby_voice.DEFAULT_MICROPHONE_DEVICE
DEFAULT_SPEAKER_DEVICE = standby_voice.DEFAULT_SPEAKER_DEVICE
DEFAULT_WHISPER_COMMAND = standby_voice.DEFAULT_COMMAND_WHISPER_COMMAND
DEFAULT_WHISPER_MODEL = standby_voice.DEFAULT_COMMAND_WHISPER_MODEL
DEFAULT_LANGUAGE = "en"
DEFAULT_TIMEOUT_SECONDS = standby_voice.DEFAULT_TIMEOUT_SECONDS
DEFAULT_TRANSCRIPTION_TIMEOUT_SECONDS = (
    standby_voice.DEFAULT_ACTIVE_TRANSCRIPTION_TIMEOUT_SECONDS
)
DEFAULT_TERMINATION_GRACE_SECONDS = (
    standby_voice.DEFAULT_WHISPER_TERMINATION_GRACE_SECONDS
)
DEFAULT_HARD_CLEANUP_DEADLINE_SECONDS = (
    standby_voice.DEFAULT_WHISPER_HARD_CLEANUP_DEADLINE_SECONDS
)
DEFAULT_OUTPUT_DIRECTORY = (
    REPO_ROOT / "data" / "runtime" / "active_lifecycle_audio"
)

DIAGNOSTIC_PHRASES = (
    "goodbye Ares",
    "shutdown Ares",
    "calculate two plus two",
    "remember that I like video games",
)

_EXPECTED_CONSTRAINED_OUTCOMES = {
    "goodbye Ares": ("standby", "goodbye ares", False),
    "shutdown Ares": ("shutdown", "shutdown ares", False),
    "calculate two plus two": ("ordinary", "", True),
    "remember that I like video games": ("ordinary", "", True),
}


@dataclass(frozen=True)
class DiagnosticAttempt:
    expected_phrase: str
    success: bool
    capture: Any = None
    transcription: Any = None
    wav_path: str = ""
    failure_kind: str = ""
    error_message: str = ""
    exception_class: str = ""
    exception_message: str = ""
    exception_traceback: str = ""
    failing_adapter_class: str = ""
    failing_method: str = ""
    process: Any = None
    stream: Any = None
    exception_context: Any = None
    lifecycle_result: Any = None
    constrained_recognition: Any = None
    cleanup_result: str = "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture four bounded ACTIVE-state phrases with the production "
            "recorder profile and classify them without executing ARES actions."
        )
    )
    parser.add_argument("--microphone-device", default=DEFAULT_MICROPHONE_DEVICE)
    parser.add_argument("--speaker-device", default=DEFAULT_SPEAKER_DEVICE)
    parser.add_argument("--command-whisper-command", default=DEFAULT_WHISPER_COMMAND)
    parser.add_argument("--command-whisper-model", default=DEFAULT_WHISPER_MODEL)
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--active-transcription-timeout",
        type=float,
        default=DEFAULT_TRANSCRIPTION_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--whisper-termination-grace",
        type=float,
        default=DEFAULT_TERMINATION_GRACE_SECONDS,
    )
    parser.add_argument(
        "--whisper-hard-cleanup-deadline",
        type=float,
        default=DEFAULT_HARD_CLEANUP_DEADLINE_SECONDS,
    )
    parser.add_argument("--output-directory", default=str(DEFAULT_OUTPUT_DIRECTORY))
    parser.add_argument(
        "--runtime-lock-path",
        default=str(standby_voice.DEFAULT_RUNTIME_LOCK_PATH),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--runtime-lock-stale-seconds",
        type=float,
        default=standby_voice.DEFAULT_RUNTIME_LOCK_STALE_SECONDS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--retain-audio",
        action="store_true",
        help="retain diagnostic WAVs; without this flag every capture is removed",
    )
    parser.add_argument(
        "--diagnostic-active-lifecycle-audio",
        action="store_true",
        help="required owner acknowledgement for this bounded hardware probe",
    )
    return parser


def _production_runtime_args(args: argparse.Namespace) -> argparse.Namespace:
    """Map diagnostic CLI values through the production launcher's parser.

    The diagnostic deliberately does not reconstruct microphone or Whisper
    adapters.  This namespace is consumed only by the authoritative production
    active-audio factory shared with ``run_ares_standby_voice.py``.
    """

    values = [
        "--microphone-device",
        str(args.microphone_device),
        "--speaker-device",
        str(args.speaker_device),
        "--language",
        str(args.language),
        "--command-whisper-command",
        str(args.command_whisper_command),
        "--command-whisper-model",
        str(args.command_whisper_model),
        "--voice-profile",
        standby_voice.DEFAULT_VOICE_PROFILE,
        "--timeout",
        str(args.timeout),
        "--active-transcription-timeout",
        str(args.active_transcription_timeout),
        "--whisper-termination-grace",
        str(args.whisper_termination_grace),
        "--whisper-hard-cleanup-deadline",
        str(args.whisper_hard_cleanup_deadline),
    ]
    if args.retain_audio:
        values.append("--retain-diagnostic-audio")
    return standby_voice.build_parser().parse_args(values)


def _diagnostic_request(base_request: Any, output_path: Path) -> Any:
    """Apply diagnostic-only storage/routing metadata to one production request."""

    return active_command_capture_request(
        replace(
            base_request,
            correlation_id=new_correlation_id("active-audio-diagnostic"),
            recording_output_path=str(output_path),
            playback_enabled=False,
            diagnostic_audio=bool(base_request.diagnostic_audio),
            cleanup_policy=(
                "keep" if base_request.diagnostic_audio else "delete_always"
            ),
            metadata={
                **dict(base_request.metadata or {}),
                "source": "manual_diagnose_active_lifecycle_audio",
                "diagnostic_only": True,
                "diagnostic_exception_traceback": True,
                "lifecycle_execution_enabled": False,
                "memory_execution_enabled": False,
            },
        )
    )


def run_active_lifecycle_audio_diagnostic(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    production_factory: Optional[Callable[..., Any]] = None,
    lock_factory: Callable[..., Any] = StoreWriteLock,
) -> int:
    args = build_parser().parse_args(argv)
    issue = _configuration_issue(args)
    if issue:
        output_func(f"Configuration error: {issue}")
        return 2

    try:
        with lock_factory(
            Path(args.runtime_lock_path).expanduser(),
            recover_if_owner_dead=True,
            stale_after_seconds=float(args.runtime_lock_stale_seconds),
            owner_kind="active_lifecycle_audio_diagnostic",
        ):
            return _run_locked_diagnostic(
                args,
                output_func=output_func,
                production_factory=(
                    production_factory
                    or standby_voice.build_production_active_audio_pipeline
                ),
            )
    except KeyboardInterrupt:
        raise
    except Exception as error:
        output_func("Active lifecycle-audio diagnostic failed before capture.")
        output_func(f"Failure type: {error.__class__.__name__}")
        output_func(f"Failure message: {str(error) or '<empty>'}")
        output_func("Diagnostic traceback:")
        for line in traceback.format_exc().rstrip().splitlines():
            output_func(line)
        output_func("Microphone ownership: released")
        return 3


def _run_locked_diagnostic(
    args: argparse.Namespace,
    *,
    output_func: Callable[[str], None],
    production_factory: Callable[..., Any],
) -> int:
    output_directory = Path(args.output_directory).expanduser()
    output_directory.mkdir(parents=True, exist_ok=True)
    production_args = _production_runtime_args(args)
    composition = production_factory(
        production_args,
        output_func=output_func,
    )
    pipeline = composition.pipeline
    base_request = composition.base_request
    gate = composition.voice_io_gate
    lifecycle_audio_recognizer = getattr(
        composition,
        "active_lifecycle_audio_recognizer",
        None,
    )
    if not callable(getattr(lifecycle_audio_recognizer, "recognize_wav", None)):
        raise RuntimeError(
            "production_active_lifecycle_audio_recognizer_unavailable"
        )
    microphone = getattr(pipeline, "microphone_adapter", None)

    output_func("ARES ACTIVE lifecycle-audio diagnostic")
    output_func(
        "Runtime ownership lock: acquired "
        f"({Path(args.runtime_lock_path).expanduser()})"
    )
    output_func(f"Microphone device: {args.microphone_device}")
    output_func(
        "Speaker device (not opened by this diagnostic): "
        f"{args.speaker_device}"
    )
    output_func(f"Production capture profile: {ACTIVE_COMMAND_CAPTURE_PROFILE}")
    output_func(
        "Production active-audio factory: "
        f"{production_factory.__module__}.{production_factory.__name__}"
    )
    output_func(
        "Production microphone adapter: "
        f"{microphone.__class__.__module__}.{microphone.__class__.__name__}"
    )
    output_func(
        "Production Whisper adapter: "
        f"{pipeline.speech_to_text_adapter.__class__.__module__}."
        f"{pipeline.speech_to_text_adapter.__class__.__name__}"
    )
    output_func("PCM stream architecture: bounded one-shot stream per phrase")
    output_func("Lifecycle execution: disabled")
    output_func("CoreService and owner-memory execution: disabled")

    preflight_ok = _run_microphone_preflight(
        microphone,
        gate=gate,
        device=str(args.microphone_device),
        frame_duration_ms=int(base_request.frame_duration_ms),
        output_func=output_func,
    )
    if not preflight_ok:
        _stop_pipeline_safely(pipeline, base_request)
        close_recognizer = getattr(lifecycle_audio_recognizer, "close", None)
        if callable(close_recognizer):
            close_recognizer()
        gate.reset()
        return 3

    failed_attempts = 0
    try:
        for index, phrase in enumerate(DIAGNOSTIC_PHRASES, start=1):
            output_path = output_directory / f"{index:02d}-{_phrase_slug(phrase)}.wav"
            request = _diagnostic_request(base_request, output_path)
            attempt = _capture_and_transcribe(
                phrase=phrase,
                index=index,
                total=len(DIAGNOSTIC_PHRASES),
                request=request,
                pipeline=pipeline,
                lifecycle_audio_recognizer=lifecycle_audio_recognizer,
                gate=gate,
                output_func=output_func,
            )
            cleanup = _cleanup_capture_paths(
                _capture_paths(attempt.capture, output_path, attempt.wav_path),
                retain=bool(args.retain_audio),
            )
            if cleanup == "not_applicable" and attempt.cleanup_result not in {
                "",
                "unknown",
            }:
                cleanup = attempt.cleanup_result
            attempt = replace(attempt, cleanup_result=cleanup)
            attempt = _validate_expected_constrained_outcome(attempt)
            _print_attempt(attempt, request=request, output_func=output_func)
            if not attempt.success:
                failed_attempts += 1
    finally:
        _stop_pipeline_safely(pipeline, base_request)
        close_recognizer = getattr(lifecycle_audio_recognizer, "close", None)
        if callable(close_recognizer):
            close_recognizer()
        gate.reset()

    output_func(
        "Diagnostic result: "
        + (
            "all four phrases passed the constrained lifecycle policy"
            if not failed_attempts
            else f"{failed_attempts} phrase(s) failed"
        )
    )
    output_func("No lifecycle transition, skill, or memory operation was executed.")
    return 0 if not failed_attempts else 3


def _validate_expected_constrained_outcome(
    attempt: DiagnosticAttempt,
) -> DiagnosticAttempt:
    if not attempt.success:
        return attempt
    expected = _EXPECTED_CONSTRAINED_OUTCOMES.get(attempt.expected_phrase)
    if expected is None:
        return replace(
            attempt,
            success=False,
            failure_kind="lifecycle_recognition_mismatch",
            error_message="diagnostic phrase has no constrained-policy expectation",
        )
    expected_classification, expected_canonical, expected_fallback = expected
    evidence = dict(attempt.constrained_recognition or {})
    classification = str(evidence.get("classification") or "")
    canonical = str(evidence.get("canonical_phrase") or "")
    fallback = bool(evidence.get("whisper_fallback_required", True))
    confidence_tier = str(evidence.get("confidence_tier") or "")
    selected_action = str(evidence.get("selected_lifecycle_action") or "none")
    action_expected = expected_classification in {"standby", "shutdown"}
    matches = (
        classification == expected_classification
        and canonical == expected_canonical
        and fallback is expected_fallback
        and (
            confidence_tier == "high"
            and selected_action == expected_classification
            and _high_confidence_constrained_action(evidence)
            if action_expected
            else selected_action == "none"
        )
    )
    if matches:
        return attempt
    return replace(
        attempt,
        success=False,
        failure_kind="lifecycle_recognition_mismatch",
        error_message=(
            "expected constrained outcome "
            f"classification={expected_classification}, "
            f"canonical={expected_canonical or '<none>'}, "
            f"whisper_fallback={'yes' if expected_fallback else 'no'}; "
            "observed "
            f"classification={classification or '<empty>'}, "
            f"canonical={canonical or '<none>'}, "
            f"confidence_tier={confidence_tier or '<none>'}, "
            f"action={selected_action or 'none'}, "
            f"whisper_fallback={'yes' if fallback else 'no'}"
        )[:500],
    )


def _capture_and_transcribe(
    *,
    phrase: str,
    index: int,
    total: int,
    request: Any,
    pipeline: Any,
    lifecycle_audio_recognizer: Any,
    gate: Any,
    output_func: Callable[[str], None],
) -> DiagnosticAttempt:
    ready_announced = False
    constrained_evidence: dict[str, Any] = {}

    def announce_ready(details: dict[str, Any]) -> None:
        nonlocal ready_announced
        if ready_announced:
            return
        ready_announced = True
        reason = str(dict(details or {}).get("capture_start_reason") or "stream_ready")
        output_func(
            f"Ready {index}/{total}. Say '{phrase}' now. "
            f"(capture_start_reason={reason})"
        )

    unsubscribe = None
    add_ready_observer = getattr(pipeline, "add_capture_ready_observer", None)
    if callable(add_ready_observer):
        unsubscribe = add_ready_observer(announce_ready)

    def intercept_raw_transcript(text: str) -> SingleTurnPreBrainDecision:
        return SingleTurnPreBrainDecision(
            handled=True,
            status="diagnostic_transcript_captured",
            response_text="",
            continue_to_output=False,
            data={
                "diagnostic_only": True,
                "lifecycle_execution_enabled": False,
            },
        )

    def inspect_finalized_audio(audio_chunk: Any) -> SingleTurnFinalizedAudioDecision:
        metadata = dict(getattr(audio_chunk, "metadata", {}) or {})
        wav_path = str(
            metadata.get("final_whisper_input_path")
            or metadata.get("wav_path")
            or ""
        )
        try:
            recognition = lifecycle_audio_recognizer.recognize_wav(wav_path)
            payload = _constrained_recognition_payload(recognition)
        except Exception as error:
            payload = {
                "classification": "uncertain",
                "canonical_phrase": "",
                "recognized_text": "",
                "recognized_tokens": [],
                "confidence": None,
                "confidence_available": False,
                "recognition_backend": lifecycle_audio_recognizer.__class__.__name__,
                "rejection_reason": (
                    f"diagnostic_lifecycle_recognizer_error:"
                    f"{error.__class__.__name__}:{str(error)[:200]}"
                ),
                "confirmation_required": False,
                "proposed_classification": "",
                "selected_lifecycle_action": "none",
                "whisper_fallback_required": True,
            }
        constrained_evidence.update(payload)
        # Diagnostic mode deliberately continues to Whisper for a side-by-side
        # comparison. Production uses the same recognition result to bypass
        # Whisper only when the bounded policy authorizes a lifecycle action.
        return SingleTurnFinalizedAudioDecision(
            handled=False,
            continue_to_whisper=True,
            status="diagnostic_lifecycle_audio_observed",
            data={"active_lifecycle_audio": dict(payload)},
        )

    owner = "diagnostic_active_capture"
    result = None
    try:
        if not gate.wait_for_capture(
            timeout_seconds=float(request.recording_timeout_seconds or 1.0)
        ):
            return DiagnosticAttempt(
                expected_phrase=phrase,
                success=False,
                failure_kind="microphone_open_error",
                error_message="microphone_gate_wait_timeout",
                failing_adapter_class=gate.__class__.__name__,
                failing_method="wait_for_capture",
            )
        gate.begin_capture(owner)
        result = pipeline.run_once(
            request,
            raw_transcript_hook=intercept_raw_transcript,
            finalized_audio_hook=inspect_finalized_audio,
        )
    except KeyboardInterrupt:
        raise
    except Exception as error:
        return DiagnosticAttempt(
            expected_phrase=phrase,
            success=False,
            capture=result,
            failure_kind="VAD_error",
            error_message=f"{error.__class__.__name__}:{str(error)}",
            exception_class=error.__class__.__name__,
            exception_message=str(error),
            exception_traceback=traceback.format_exc(),
            failing_adapter_class=pipeline.__class__.__name__,
            failing_method="run_once",
            exception_context={
                "start_called": True,
                "open_called": False,
                "read_called": False,
                "stream_lifecycle_state": "unknown",
            },
        )
    finally:
        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception:
                pass
        gate.end_capture(owner)

    return _attempt_from_pipeline_result(
        phrase,
        result,
        request=request,
        constrained_recognition=constrained_evidence,
    )


def _run_microphone_preflight(
    microphone: Any,
    *,
    gate: Any,
    device: str,
    frame_duration_ms: int,
    output_func: Callable[[str], None],
) -> bool:
    """Cross the real production ALSA boundary before prompting the owner."""

    owner = "diagnostic_active_capture"
    started = None
    preflight = None
    stopped = None
    gate_acquired = False
    failing_adapter = gate
    failing_method = "begin_capture"
    try:
        gate.begin_capture(owner)
        gate_acquired = True
        failing_adapter = microphone
        failing_method = "start"
        started = microphone.start()
        if bool(getattr(started, "success", False)):
            preflight_method = getattr(microphone, "preflight_pcm_stream", None)
            if not callable(preflight_method):
                raise RuntimeError("production_microphone_preflight_unsupported")
            failing_method = "preflight_pcm_stream"
            preflight = preflight_method(
                device=device,
                frame_duration_ms=frame_duration_ms,
                frame_read_timeout_seconds=1.0,
                diagnostic_traceback=True,
                owner=owner,
            )
        else:
            preflight = started
    except KeyboardInterrupt:
        raise
    except Exception as error:
        preflight = _exception_result(
            error,
            adapter=failing_adapter,
            method=failing_method,
        )
    finally:
        try:
            failing_adapter = microphone
            failing_method = "stop"
            stopped = microphone.stop()
        except Exception as error:
            if preflight is None or bool(getattr(preflight, "success", False)):
                preflight = _exception_result(
                    error,
                    adapter=microphone,
                    method="stop",
                    failure_kind="microphone_cleanup_error",
                )
        if gate_acquired:
            gate.end_capture(owner)

    start_data = dict(getattr(started, "data", {}) or {})
    health = dict(start_data.get("health", {}) or {})
    health_data = dict(health.get("data", {}) or {})
    data = dict(getattr(preflight, "data", {}) or {})
    requested_format = dict(data.get("requested_pcm_format", {}) or {})
    gate_snapshot = dict(gate.snapshot() or {})
    start_success = bool(getattr(started, "success", False))
    preflight_success = bool(getattr(preflight, "success", False))
    stop_success = bool(getattr(stopped, "success", False))
    ownership_released = (
        not bool(gate_snapshot.get("capture_active"))
        and bool(data.get("microphone_ownership_released", True))
    )
    success = start_success and preflight_success and stop_success and ownership_released
    device_exists = bool(
        health_data.get("device_count", 0)
        or data.get("open_success", False)
    )

    output_func("")
    output_func("Microphone preflight:")
    output_func(f"  device: {device}")
    output_func(
        "  ALSA device resolved: "
        f"{data.get('resolved_capture_device') or device or '<none>'}"
    )
    output_func(f"  device exists: {'yes' if device_exists else 'no'}")
    output_func(
        "  format: "
        f"{int(requested_format.get('sample_rate_hz', 16000) or 16000)} Hz / "
        f"{'mono' if int(requested_format.get('channels', 1) or 1) == 1 else str(requested_format.get('channels')) + ' channels'} / "
        f"{requested_format.get('sample_format') or 'S16_LE'}"
    )
    output_func(f"  adapter start: {'success' if start_success else 'failed'}")
    output_func(
        "  open called: "
        f"{'yes' if data.get('open_called') else 'no'}"
    )
    output_func(f"  open: {'success' if data.get('open_success') else 'failed'}")
    output_func(
        "  read called: "
        f"{'yes' if data.get('read_called') else 'no'}"
    )
    output_func(
        "  first PCM read: "
        f"{'success' if data.get('first_pcm_read_success') else 'failed'}"
    )
    output_func(
        "  frame bytes: "
        f"{int(data.get('first_frame_byte_count', 0) or 0)}"
    )
    output_func(
        "  expected frame bytes: "
        f"{int(data.get('expected_frame_byte_count', 640) or 640)}"
    )
    output_func(
        "  frame nonzero: "
        f"{'yes' if data.get('first_frame_nonzero') else 'no'}"
    )
    output_func(f"  stream health: {'healthy' if preflight_success else 'failed'}")
    output_func(
        "  stream lifecycle state: "
        f"{data.get('stream_lifecycle_state') or 'unknown'}"
    )
    output_func(
        "  ownership: "
        f"{owner} -> {'released' if ownership_released else 'not_released'}"
    )
    output_func(
        "  adapter class: "
        f"{data.get('adapter_class') or microphone.__class__.__name__}"
    )
    output_func(
        "  failing method: "
        f"{data.get('failing_method') or ('start' if not start_success else '<none>')}"
    )
    output_func(f"  process PID: {int(data.get('process_id', os.getpid()) or os.getpid())}")
    output_func(
        "  ALSA child PID: "
        f"{int(data.get('alsa_child_process_id', 0) or 0) or '<none>'}"
    )
    output_func(
        "  ALSA exit status: "
        f"{data.get('alsa_process_exit_status') if data.get('alsa_process_exit_status') is not None else '<running-at-read>'}"
    )
    output_func(
        "  exact capture command: "
        f"{' '.join(str(part) for part in data.get('exact_capture_command', []) or []) or '<unavailable>'}"
    )
    output_func(f"  ALSA stderr: {data.get('alsa_stderr') or '<empty>'}")
    stderr_lower = str(data.get("alsa_stderr") or "").casefold()
    ownership_conflict = (
        "yes"
        if "device or resource busy" in stderr_lower
        or "resource busy" in stderr_lower
        else "no"
        if preflight_success
        else "unknown"
    )
    output_func(f"  external microphone owner detected: {ownership_conflict}")
    output_func(f"  cleanup: {data.get('cleanup_result') or 'not_applicable'}")
    output_func(f"  adapter stop: {'success' if stop_success else 'failed'}")
    if not success:
        output_func(
            "  failure category: "
            f"{data.get('failure_reason') or 'microphone_open_error'}"
        )
        output_func(
            "  exception class: "
            f"{data.get('exception_class') or getattr(preflight, 'exception_class', '') or '<none>'}"
        )
        output_func(
            "  exception message: "
            f"{data.get('exception_message') or getattr(preflight, 'exception_message', '') or getattr(preflight, 'error_message', '') or '<empty>'}"
        )
        diagnostic_traceback = str(
            data.get("traceback")
            or getattr(preflight, "exception_traceback", "")
            or ""
        )
        if diagnostic_traceback:
            output_func("  traceback:")
            for line in diagnostic_traceback.rstrip().splitlines():
                output_func(f"    {line}")
    return success


def _exception_result(
    error: BaseException,
    *,
    adapter: Any,
    method: str,
    failure_kind: str = "microphone_open_error",
) -> Any:
    """Create a tiny result object without hiding a diagnostic exception."""

    class ExceptionResult:
        success = False
        status = failure_kind
        exception_class = error.__class__.__name__
        exception_message = str(error)
        exception_traceback = traceback.format_exc()
        error_message = f"{error.__class__.__name__}:{str(error)}"
        data = {
            "failure_reason": failure_kind,
            "exception_class": error.__class__.__name__,
            "exception_message": str(error),
            "traceback": exception_traceback,
            "adapter_class": (
                f"{adapter.__class__.__module__}.{adapter.__class__.__qualname__}"
            ),
            "failing_method": method,
            "process_id": os.getpid(),
            "cleanup_result": "not_applicable",
        }

    return ExceptionResult()


def _attempt_from_pipeline_result(
    phrase: str,
    result: Any,
    *,
    request: Any,
    constrained_recognition: Optional[dict[str, Any]] = None,
) -> DiagnosticAttempt:
    recording = _recording_contract(result)
    recording_data = dict(recording.get("data", {}) or {})
    pcm_exception = dict(recording_data.get("pcm_exception", {}) or {})
    pcm_cleanup = dict(recording_data.get("pcm_stream_cleanup", {}) or {})
    if not pcm_exception and pcm_cleanup.get("exception_class"):
        source_after_close = dict(
            pcm_cleanup.get("source_snapshot_after_close", {}) or {}
        )
        pcm_exception = {
            "exception_class": str(pcm_cleanup.get("exception_class") or ""),
            "exception_message": str(pcm_cleanup.get("exception_message") or ""),
            "traceback": str(pcm_cleanup.get("traceback") or ""),
            "failing_adapter_class": str(
                recording_data.get("adapter_class") or ""
            ),
            "failing_method": "close",
            "open_called": True,
            "read_called": bool(
                source_after_close.get("total_low_level_reads", 0)
                or source_after_close.get("read_sequence", 0)
            ),
            "stream_lifecycle_state": (
                "closed"
                if source_after_close.get("closed")
                else "ended"
                if source_after_close.get("stream_ended")
                else "open"
            ),
            "cleanup_result": str(pcm_cleanup.get("status") or "incomplete"),
            "source_snapshot_after_close": source_after_close,
        }
    process = dict(recording_data.get("process", {}) or {})
    stream = dict(
        pcm_exception.get("source_snapshot_after_close")
        or pcm_exception.get("source_snapshot")
        or recording_data.get("pcm_source_snapshot_after_close")
        or {}
    )
    raw = str(getattr(result, "raw_transcript", "") or "")
    candidate_duration = _candidate_duration(recording, result)
    result_success = bool(getattr(result, "success", False))
    error_message = str(
        getattr(result, "error_reason", "")
        or getattr(result, "error_message", "")
        or recording.get("error_message")
        or ""
    )
    failure_kind = _pipeline_failure_kind(
        result,
        recording=recording,
        pcm_exception=pcm_exception,
        candidate_duration=candidate_duration,
        raw_transcript=raw,
    )
    constrained_action_authorized = _high_confidence_constrained_action(
        constrained_recognition
    )
    comparison_whisper_optional_failure = bool(
        constrained_action_authorized
        and failure_kind in {"empty_transcript", "whisper_error"}
    )
    if comparison_whisper_optional_failure:
        # Production would have bypassed Whisper for this exact high-confidence
        # lifecycle result.  Diagnostic compare-only mode still runs Whisper for
        # side-by-side evidence, but its optional failure cannot invalidate the
        # already-complete candidate or the production authorization decision.
        failure_kind = ""
    success = bool(
        candidate_duration > 0.0
        and (
            (result_success and raw.strip())
            or comparison_whisper_optional_failure
        )
        and not failure_kind
    )
    if result_success and candidate_duration <= 0.0:
        error_message = error_message or "zero_length_candidate"
    elif result_success and not raw.strip() and not constrained_action_authorized:
        error_message = error_message or "empty_transcript"

    lifecycle_result = None
    lifecycle_error = None
    lifecycle_traceback = ""
    if raw.strip() and candidate_duration > 0.0:
        try:
            lifecycle_result = normalize_active_lifecycle_command(raw)
        except (TypeError, ValueError) as error:
            lifecycle_error = error
            lifecycle_traceback = traceback.format_exc()
            success = False
            failure_kind = "lifecycle_parse_error"
            error_message = f"{error.__class__.__name__}:{str(error)}"

    cleanup = dict(dict(getattr(result, "data", {}) or {}).get("cleanup", {}) or {})
    wav_path = str(
        getattr(result, "recorded_wav_path", "")
        or recording.get("final_whisper_input_path")
        or recording.get("normalized_wav_path")
        or recording.get("wav_path")
        or request.recording_output_path
    )
    return DiagnosticAttempt(
        expected_phrase=phrase,
        success=success,
        capture=result,
        transcription=result,
        wav_path=wav_path,
        failure_kind=failure_kind,
        error_message=error_message,
        exception_class=(
            lifecycle_error.__class__.__name__
            if lifecycle_error is not None
            else str(pcm_exception.get("exception_class") or "")
        ),
        exception_message=(
            str(lifecycle_error)
            if lifecycle_error is not None
            else str(pcm_exception.get("exception_message") or "")
        ),
        exception_traceback=(
            lifecycle_traceback
            if lifecycle_error is not None
            else str(pcm_exception.get("traceback") or "")
        ),
        failing_adapter_class=str(
            (
                f"{normalize_active_lifecycle_command.__module__}."
                "normalize_active_lifecycle_command"
                if lifecycle_error is not None
                else pcm_exception.get("failing_adapter_class")
                or recording_data.get("adapter_class")
                or ""
            )
        ),
        failing_method=(
            "normalize_active_lifecycle_command"
            if lifecycle_error is not None
            else str(pcm_exception.get("failing_method") or "")
        ),
        process=process,
        stream=stream,
        exception_context=pcm_exception,
        lifecycle_result=lifecycle_result,
        constrained_recognition=dict(constrained_recognition or {}),
        cleanup_result=str(
            pcm_cleanup.get("status")
            if pcm_cleanup.get("status") not in {None, "", "completed"}
            else cleanup.get("status")
            or pcm_cleanup.get("status")
            or "pipeline_cleanup_completed"
        ),
    )


def _pipeline_failure_kind(
    result: Any,
    *,
    recording: dict[str, Any],
    pcm_exception: dict[str, Any],
    candidate_duration: float,
    raw_transcript: str,
) -> str:
    if bool(getattr(result, "success", False)):
        if candidate_duration <= 0.0:
            return "invalid_frame_error"
        if not raw_transcript.strip():
            return "empty_transcript"
        return ""
    status = str(getattr(result, "status", "") or "").casefold()
    stage = str(getattr(result, "error_stage", "") or "").casefold()
    message = str(
        getattr(result, "error_reason", "")
        or getattr(result, "error_message", "")
        or recording.get("error_message")
        or ""
    ).casefold()
    exception_message = str(pcm_exception.get("exception_message") or "").casefold()
    source = dict(pcm_exception.get("source_snapshot", {}) or {})
    delivered_frames = int(
        source.get("valid_pcm_frames_delivered_to_vad", 0)
        or source.get("delivered_frame_count", 0)
        or recording.get("frame_count", 0)
        or 0
    )
    if status in {"silent_audio", "no_speech_timeout"} or "no_speech" in message:
        return "no_speech_timeout"
    if status in {
        "blank_transcription",
        "empty_transcript",
        "no_usable_speech",
        "transcription_rejected",
        "transcript_rejected",
    } or "empty_transcript" in message or "blank_transcription" in message:
        return "empty_transcript"
    if stage == "recording_start":
        return "microphone_open_error"
    if "transcrib" in stage or "whisper" in message or "transcription" in status:
        return "whisper_error"
    if pcm_exception:
        if delivered_frames == 0 and (
            "arecord_process_exited" in exception_message
            or "stdout_closed" in exception_message
            or not bool(pcm_exception.get("open_called", True))
        ):
            return "microphone_open_error"
        return "pcm_read_error"
    if "invalid_pcm" in message or "incomplete frame" in message or "odd-byte" in message:
        return "invalid_frame_error"
    if "wav" in stage or "wav" in message or "audio_duration_invariant" in message:
        return "wav_write_error"
    if "record" in stage or "vad" in message or status in {"recording_failed", "invalid_recording"}:
        return "VAD_error"
    return "whisper_error" if "transcript" in stage else "VAD_error"


def _high_confidence_constrained_action(value: Any) -> bool:
    evidence = dict(value or {}) if isinstance(value, dict) else {}
    classification = str(evidence.get("classification") or "").strip().casefold()
    action = str(
        evidence.get("selected_lifecycle_action") or ""
    ).strip().casefold()
    canonical = str(evidence.get("canonical_phrase") or "").strip().casefold()
    confidence = evidence.get("confidence")
    if (
        classification not in {"standby", "shutdown"}
        or action != classification
        or str(evidence.get("confidence_tier") or "") != "high"
        or bool(evidence.get("whisper_fallback_required", True))
        or not bool(evidence.get("confidence_available", False))
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
    ):
        return False
    expected_canonical = (
        "goodbye ares" if classification == "standby" else "shutdown ares"
    )
    minimum = (
        DEFAULT_STANDBY_HIGH_CONFIDENCE
        if classification == "standby"
        else DEFAULT_SHUTDOWN_HIGH_CONFIDENCE
    )
    return canonical == expected_canonical and float(confidence) >= minimum


def _recording_contract(result: Any) -> dict[str, Any]:
    data = dict(getattr(result, "data", {}) or {})
    recording = data.get("recording")
    if isinstance(recording, dict):
        return dict(recording)
    capture = data.get("capture")
    if isinstance(capture, dict):
        nested = capture.get("data")
        if isinstance(nested, dict) and "recording" in nested:
            value = nested.get("recording")
            return dict(value) if isinstance(value, dict) else dict(capture)
        return dict(capture)
    return {}


def _candidate_duration(recording: dict[str, Any], result: Any) -> float:
    data = dict(recording.get("data", {}) or {})
    return float(
        recording.get("whisper_input_duration_seconds", 0.0)
        or recording.get("normalized_duration_seconds", 0.0)
        or recording.get("assembled_duration_seconds", 0.0)
        or recording.get("duration_seconds", 0.0)
        or data.get("whisper_input_duration_seconds", 0.0)
        or getattr(result, "recording_duration_seconds", 0.0)
        or 0.0
    )


def _stop_pipeline_safely(pipeline: Any, request: Any) -> None:
    stop = getattr(pipeline, "stop", None)
    if callable(stop):
        try:
            stop(request)
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError):
            pass


def _print_attempt(
    attempt: DiagnosticAttempt,
    *,
    request: Any,
    output_func: Callable[[str], None],
) -> None:
    capture = attempt.capture
    recording = _recording_contract(capture)
    capture_data = dict(recording.get("data", {}) or {})
    raw = str(
        getattr(attempt.transcription, "raw_transcript", "")
        or getattr(attempt.transcription, "recognized_text", "")
        or ""
    )
    comparison_whisper_status = str(
        getattr(attempt.transcription, "status", "") or "not_available"
    )
    comparison_whisper_reason = str(
        getattr(attempt.transcription, "error_reason", "")
        or getattr(attempt.transcription, "error_message", "")
        or ""
    )
    candidate_duration = _candidate_duration(recording, capture)
    lifecycle_evaluated = bool(candidate_duration > 0.0 and raw.strip())
    lifecycle = attempt.lifecycle_result if lifecycle_evaluated else None
    action = (
        str(lifecycle.action or LIFECYCLE_ACTION_NONE)
        if lifecycle is not None
        else "not_evaluated"
    )
    classification = (
        "lifecycle_parse_error"
        if attempt.failure_kind == "lifecycle_parse_error"
        else action
        if action != LIFECYCLE_ACTION_NONE
        else "ordinary"
    )
    retained_frames = int(
        recording.get("pre_roll_frames_retained", 0)
        or capture_data.get("pre_roll_frames_retained", 0)
        or 0
    )
    expected_frames = int(
        capture_data.get("expected_pre_roll_frames", 0)
        or capture_data.get("pre_roll_frames", 0)
        or math.ceil(
            float(request.pre_roll_seconds)
            / (float(request.frame_duration_ms) / 1000.0)
        )
    )
    retained_seconds = retained_frames * float(request.frame_duration_ms) / 1000.0
    beginning_clipped = _yes_no_not_applicable(
        capture_data.get("beginning_clipped", "not_applicable")
        if recording
        else "not_applicable"
    )
    wav_path = attempt.wav_path or _final_wav_path(capture, request.recording_output_path)
    process = dict(attempt.process or capture_data.get("process", {}) or {})
    stream = dict(attempt.stream or {})
    exception_context = dict(attempt.exception_context or {})
    constrained = dict(attempt.constrained_recognition or {})
    constrained_classification = str(
        constrained.get("classification") or "not_evaluated"
    )
    constrained_action = str(
        constrained.get("selected_lifecycle_action") or "none"
    )
    constrained_confidence = constrained.get("confidence")
    confidence_text = (
        f"{float(constrained_confidence):.3f}"
        if isinstance(constrained_confidence, (int, float))
        and not isinstance(constrained_confidence, bool)
        else "unavailable"
    )
    token_text = " ".join(
        str(token) for token in list(constrained.get("recognized_tokens") or [])
    )

    output_func("")
    output_func(f"ACTIVE LIFECYCLE AUDIO RESULT {attempt.expected_phrase!r}")
    output_func(f"Raw Whisper transcript: {raw or '<empty>'}")
    output_func(f"Comparison Whisper status: {comparison_whisper_status}")
    output_func(
        "Comparison Whisper reason: "
        f"{comparison_whisper_reason or '<none>'}"
    )
    output_func(f"Raw transcript: {raw or '<empty>'}")
    output_func(
        "Normalized transcript: "
        f"{(lifecycle.normalized_transcript if lifecycle is not None else '') or '<empty>'}"
    )
    output_func(
        "Alias removal: "
        f"{(lifecycle.assistant_alias_removed if lifecycle is not None else '') or 'none'}"
    )
    output_func(
        "Alias position: "
        f"{(lifecycle.alias_position if lifecycle is not None else '') or 'none'}"
    )
    output_func(f"Lifecycle classification: {classification}")
    output_func(f"Lifecycle action that would be selected: {action}")
    output_func(
        "Constrained lifecycle recognizer transcript: "
        f"{constrained.get('recognized_text') or '<empty>'}"
    )
    output_func(
        "Constrained lifecycle recognizer tokens: "
        f"{token_text or '<empty>'}"
    )
    output_func(f"Constrained lifecycle confidence: {confidence_text}")
    output_func(
        "Constrained lifecycle confidence tier: "
        f"{constrained.get('confidence_tier') or '<none>'}"
    )
    output_func(
        "Constrained lifecycle recognition backend: "
        f"{constrained.get('recognition_backend') or '<unknown>'}"
    )
    output_func(
        "Constrained lifecycle classification: "
        f"{constrained_classification}"
    )
    output_func(
        "Canonical lifecycle phrase: "
        f"{constrained.get('canonical_phrase') or '<none>'}"
    )
    output_func(f"Selected lifecycle action: {constrained_action}")
    output_func(
        "Whisper fallback would run in production: "
        f"{'yes' if constrained.get('whisper_fallback_required', True) else 'no'}"
    )
    output_func(
        "Constrained recognition decision: "
        + (
            "accepted"
            if constrained_action in {"standby", "shutdown"}
            else "confirmation required"
            if constrained.get("confirmation_required")
            else "rejected"
        )
        + "; reason="
        + str(constrained.get("rejection_reason") or "high_confidence_exact_phrase")
    )
    output_func(f"Beginning clipped: {beginning_clipped}")
    output_func(
        "Pre-roll retained: "
        f"{retained_seconds:.3f}s / {retained_frames} of {expected_frames} frames "
        f"({float(request.pre_roll_seconds):.3f}s configured)"
    )
    output_func(f"Candidate duration: {candidate_duration:.3f}s")
    output_func(f"WAV path: {wav_path}")
    output_func("Lifecycle action executed: no (diagnostic-only)")
    output_func(
        "Attempt status: "
        + ("captured_and_classified" if attempt.success else "failed")
    )
    output_func(f"Audio cleanup: {attempt.cleanup_result}")
    if not attempt.success:
        output_func(f"Failure category: {attempt.failure_kind or 'VAD_error'}")
        output_func(f"Failure reason: {attempt.error_message or '<empty>'}")
        output_func(
            "Exception class: "
            f"{attempt.exception_class or '<not_available>'}"
        )
        output_func(
            "Exception message: "
            f"{attempt.exception_message or attempt.error_message or '<empty>'}"
        )
        output_func(
            "Failing adapter class: "
            f"{attempt.failing_adapter_class or '<not_available>'}"
        )
        output_func(
            "Failing method: "
            f"{attempt.failing_method or '<not_available>'}"
        )
        output_func(f"Microphone device: {request.microphone_device or '<none>'}")
        requested_rate = int(
            recording.get("requested_sample_rate_hz", 0)
            or capture_data.get("requested_sample_rate_hz", 0)
            or 16000
        )
        requested_channels = int(
            recording.get("actual_channels", 0)
            or capture_data.get("actual_channels", 0)
            or 1
        )
        requested_width = int(
            recording.get("actual_sample_width_bytes", 0)
            or capture_data.get("actual_sample_width_bytes", 0)
            or 2
        )
        output_func(
            "Requested PCM format: "
            f"{requested_rate} Hz / {requested_channels} channel(s) / "
            f"{requested_width * 8}-bit S16_LE"
        )
        output_func(
            "Stream lifecycle state: "
            f"{exception_context.get('stream_lifecycle_state') or ('closed' if stream.get('closed') else 'ended' if stream.get('stream_ended') else 'unknown')}"
        )
        output_func(
            "Open/read called: "
            f"{_yes_no_unknown(exception_context.get('open_called'))} / "
            f"{_yes_no_unknown(exception_context.get('read_called'))}"
        )
        output_func(f"Process PID: {os.getpid()}")
        output_func(
            "ALSA child PID: "
            f"{stream.get('process_pid') or '<not_available>'}"
        )
        output_func(
            "ALSA exit status: "
            f"{process.get('returncode') if process.get('returncode') is not None else stream.get('process_exit_status', '<not_available>')}"
        )
        output_func(
            "ALSA stderr: "
            f"{process.get('stderr') or stream.get('stderr_preview') or '<empty>'}"
        )
        if attempt.exception_traceback:
            output_func("Diagnostic traceback:")
            for line in attempt.exception_traceback.rstrip().splitlines():
                output_func(line)


def _constrained_recognition_payload(recognition: Any) -> dict[str, Any]:
    classification = str(getattr(recognition, "classification", "") or "")
    selected_action = str(
        getattr(recognition, "selected_lifecycle_action", "") or "none"
    )
    fallback = bool(
        getattr(recognition, "whisper_fallback_required", True)
    )
    return {
        "classification": classification,
        "canonical_phrase": str(
            getattr(recognition, "canonical_phrase", "") or ""
        ),
        "recognized_text": str(
            getattr(recognition, "recognized_text", "") or ""
        ),
        "recognized_tokens": [
            str(token or "")
            for token in tuple(getattr(recognition, "recognized_tokens", ()) or ())
        ],
        "confidence": getattr(recognition, "confidence", None),
        "confidence_available": bool(
            getattr(recognition, "confidence_available", False)
        ),
        "confidence_tier": str(
            getattr(recognition, "confidence_tier", "") or ""
        ),
        "recognition_backend": str(
            getattr(recognition, "recognition_backend", "") or ""
        ),
        "rejection_reason": str(
            getattr(recognition, "rejection_reason", "") or ""
        ),
        "confirmation_required": bool(
            getattr(recognition, "confirmation_required", False)
        ),
        "proposed_classification": str(
            getattr(recognition, "proposed_classification", "") or ""
        ),
        "selected_lifecycle_action": selected_action,
        "whisper_fallback_required": fallback,
    }


def _configuration_issue(args: argparse.Namespace) -> str:
    if not bool(args.diagnostic_active_lifecycle_audio):
        return "--diagnostic-active-lifecycle-audio is required"
    for label, value in (
        ("microphone device", args.microphone_device),
        ("speaker device", args.speaker_device),
        ("Whisper command", args.command_whisper_command),
        ("Whisper model", args.command_whisper_model),
        ("language", args.language),
        ("output directory", args.output_directory),
        ("runtime lock path", args.runtime_lock_path),
    ):
        if not str(value or "").strip() or len(str(value)) > 4096:
            return f"{label} is invalid"
    for label, value, minimum, maximum in (
        ("timeout", args.timeout, 1.0, 3600.0),
        ("active transcription timeout", args.active_transcription_timeout, 0.1, 300.0),
        ("Whisper termination grace", args.whisper_termination_grace, 0.1, 10.0),
        ("Whisper hard cleanup deadline", args.whisper_hard_cleanup_deadline, 0.1, 10.0),
        ("runtime lock stale seconds", args.runtime_lock_stale_seconds, 1.0, 3600.0),
    ):
        if (
            isinstance(value, bool)
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
        ):
            return f"{label} must be between {minimum:g} and {maximum:g} seconds"
    if float(args.whisper_hard_cleanup_deadline) < float(
        args.whisper_termination_grace
    ):
        return "Whisper hard cleanup deadline must not be shorter than termination grace"
    return ""


def _capture_paths(capture: Any, requested_path: Path, wav_path: str) -> set[Path]:
    recording = _recording_contract(capture)
    recording_data = dict(recording.get("data", {}) or {})
    values = (
        requested_path,
        wav_path,
        getattr(capture, "raw_wav_path", ""),
        getattr(capture, "assembled_wav_path", ""),
        getattr(capture, "normalized_wav_path", ""),
        getattr(capture, "final_whisper_input_path", ""),
        getattr(capture, "wav_path", ""),
        recording.get("raw_wav_path", ""),
        recording.get("assembled_wav_path", ""),
        recording.get("normalized_wav_path", ""),
        recording.get("final_whisper_input_path", ""),
        recording.get("wav_path", ""),
        recording_data.get("raw_wav_path", ""),
        recording_data.get("assembled_wav_path", ""),
        recording_data.get("normalized_wav_path", ""),
        recording_data.get("final_whisper_input_path", ""),
    )
    return {
        Path(str(value)).expanduser()
        for value in values
        if str(value or "").strip() and str(value) != "."
    }


def _cleanup_capture_paths(paths: set[Path], *, retain: bool) -> str:
    existing = [path for path in paths if path.exists() and path.is_file()]
    if retain:
        return "retained" if existing else "not_applicable"
    removed = False
    failed = False
    for path in existing:
        try:
            path.unlink()
            removed = True
        except OSError:
            failed = True
    if failed:
        return "incomplete"
    return "removed" if removed else "not_applicable"


def _final_wav_path(capture: Any, fallback: str | Path) -> Path:
    if capture is None:
        return Path(fallback).expanduser()
    recording = _recording_contract(capture)
    recording_data = dict(recording.get("data", {}) or {})
    data = dict(getattr(capture, "data", {}) or {})
    value = (
        getattr(capture, "final_whisper_input_path", "")
        or getattr(capture, "normalized_wav_path", "")
        or getattr(capture, "wav_path", "")
        or data.get("final_whisper_input_path")
        or data.get("normalized_wav_path")
        or recording.get("final_whisper_input_path")
        or recording.get("normalized_wav_path")
        or recording.get("wav_path")
        or recording_data.get("final_whisper_input_path")
        or recording_data.get("normalized_wav_path")
        or fallback
    )
    return Path(str(value)).expanduser()


def _yes_no_not_applicable(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    clean = str(value or "not_applicable").strip().casefold()
    if clean in {"yes", "true", "1"}:
        return "yes"
    if clean in {"no", "false", "0"}:
        return "no"
    return "not_applicable"


def _yes_no_unknown(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "unknown"


def _phrase_slug(value: str) -> str:
    return "-".join(part for part in str(value).casefold().split() if part)


def main() -> int:
    try:
        return run_active_lifecycle_audio_diagnostic()
    except KeyboardInterrupt:
        print("Active lifecycle-audio diagnostic cancelled cleanly.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
