from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    LinuxAlsaMicrophoneAdapter,
    LinuxWhisperSpeechToTextAdapter,
    WhisperSubprocessRunner,
    normalize_active_lifecycle_command,
)
from core.BrainRuntimeVoiceAdapters import (  # noqa: E402
    ACTIVE_COMMAND_CAPTURE_PROFILE,
    active_command_capture_request,
)
from core.LifecycleControl import LIFECYCLE_ACTION_NONE  # noqa: E402
from core.WavAudio import validate_canonical_wav  # noqa: E402
from scripts import manual_verify_single_turn_voice as single_turn  # noqa: E402
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


@dataclass(frozen=True)
class DiagnosticAttempt:
    expected_phrase: str
    success: bool
    capture: Any = None
    transcription: Any = None
    wav_path: str = ""
    error_message: str = ""


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


def production_active_request(
    args: argparse.Namespace,
    *,
    output_path: Path,
):
    """Build the same active request profile as the production launcher.

    The launcher remains the source of its CLI-to-pipeline mapping, while
    ``active_command_capture_request`` remains the source of the ACTIVE timing
    profile.  The diagnostic changes only the output path and disables output
    playback/action execution.
    """

    production_args = SimpleNamespace(
        microphone_device=str(args.microphone_device),
        speaker_device=str(args.speaker_device),
        language=str(args.language),
        command_whisper_command=str(args.command_whisper_command),
        command_whisper_model=str(args.command_whisper_model),
        voice_profile=standby_voice.DEFAULT_VOICE_PROFILE,
        timeout=float(args.timeout),
        active_transcription_timeout=float(args.active_transcription_timeout),
        retain_diagnostic_audio=bool(args.retain_audio),
        diagnostic_routing=False,
    )
    pipeline_args = standby_voice._command_pipeline_args(production_args)
    base_request = single_turn.request_from_args(pipeline_args)
    return active_command_capture_request(
        replace(
            base_request,
            recording_output_path=str(output_path),
            playback_enabled=False,
            diagnostic_audio=bool(args.retain_audio),
            cleanup_policy="keep" if args.retain_audio else "delete_always",
            metadata={
                **dict(base_request.metadata or {}),
                "source": "manual_diagnose_active_lifecycle_audio",
                "diagnostic_only": True,
                "lifecycle_execution_enabled": False,
                "memory_execution_enabled": False,
            },
        )
    )


def run_active_lifecycle_audio_diagnostic(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    microphone_factory: Callable[..., Any] = LinuxAlsaMicrophoneAdapter,
    stt_factory: Callable[..., Any] = LinuxWhisperSpeechToTextAdapter,
    runner_factory: Callable[..., Any] = WhisperSubprocessRunner,
) -> int:
    args = build_parser().parse_args(argv)
    issue = _configuration_issue(args)
    if issue:
        output_func(f"Configuration error: {issue}")
        return 2

    output_directory = Path(args.output_directory).expanduser()
    output_func("ARES ACTIVE lifecycle-audio diagnostic")
    output_func(f"Microphone device: {args.microphone_device}")
    output_func(
        "Speaker device (not opened by this diagnostic): "
        f"{args.speaker_device}"
    )
    output_func(f"Production capture profile: {ACTIVE_COMMAND_CAPTURE_PROFILE}")
    output_func("Lifecycle execution: disabled")
    output_func("CoreService and owner-memory execution: disabled")

    failed_attempts = 0
    for index, phrase in enumerate(DIAGNOSTIC_PHRASES, start=1):
        output_path = output_directory / f"{index:02d}-{_phrase_slug(phrase)}.wav"
        request = production_active_request(args, output_path=output_path)
        attempt = _capture_and_transcribe(
            phrase=phrase,
            index=index,
            total=len(DIAGNOSTIC_PHRASES),
            request=request,
            args=args,
            output_func=output_func,
            microphone_factory=microphone_factory,
            stt_factory=stt_factory,
            runner_factory=runner_factory,
        )
        _print_attempt(attempt, request=request, output_func=output_func)
        cleanup = _cleanup_capture_paths(
            _capture_paths(attempt.capture, output_path, attempt.wav_path),
            retain=bool(args.retain_audio),
        )
        output_func(f"Audio cleanup: {cleanup}")
        if not attempt.success:
            failed_attempts += 1

    output_func(
        "Diagnostic result: "
        + ("all four phrases captured and classified" if not failed_attempts else f"{failed_attempts} phrase(s) failed")
    )
    output_func("No lifecycle transition, skill, or memory operation was executed.")
    return 0 if not failed_attempts else 3


def _capture_and_transcribe(
    *,
    phrase: str,
    index: int,
    total: int,
    request: Any,
    args: argparse.Namespace,
    output_func: Callable[[str], None],
    microphone_factory: Callable[..., Any],
    stt_factory: Callable[..., Any],
    runner_factory: Callable[..., Any],
) -> DiagnosticAttempt:
    microphone = microphone_factory(
        device=request.microphone_device,
        record_seconds=request.recording_duration_seconds,
        timeout_seconds=float(
            request.recording_timeout_seconds
            or request.recording_duration_seconds + 5.0
        ),
    )
    capture = None
    microphone_started = False
    ready_announced = False

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

    try:
        started = microphone.start()
        microphone_started = True
        if not bool(getattr(started, "success", False)):
            return DiagnosticAttempt(
                expected_phrase=phrase,
                success=False,
                error_message=str(
                    getattr(started, "error_message", "")
                    or getattr(started, "status", "")
                    or "microphone_start_failed"
                ),
            )
        capture = microphone.record_until_silence(
            request.recording_output_path,
            device=request.microphone_device or None,
            calibration_enabled=request.calibration_enabled,
            calibration_duration_seconds=request.calibration_duration_seconds,
            speech_start_rms=request.speech_start_rms,
            speech_continue_rms=request.speech_continue_rms,
            silence_rms=request.silence_rms,
            required_speech_frames=request.required_speech_frames,
            required_continue_frames=request.required_continue_frames,
            required_silence_frames=request.required_silence_frames,
            silence_seconds=request.silence_duration_seconds,
            speech_wait_timeout_seconds=request.speech_wait_timeout_seconds,
            maximum_utterance_seconds=request.maximum_utterance_seconds,
            pre_roll_seconds=request.pre_roll_seconds,
            frame_duration_ms=request.frame_duration_ms,
            minimum_speech_start_rms=request.minimum_speech_start_rms,
            maximum_speech_start_rms=request.maximum_speech_start_rms,
            minimum_speech_continue_rms=request.minimum_speech_continue_rms,
            maximum_speech_continue_rms=request.maximum_speech_continue_rms,
            minimum_silence_rms=request.minimum_silence_rms,
            maximum_silence_rms=request.maximum_silence_rms,
            duration_loss_tolerance_seconds=request.duration_loss_tolerance_seconds,
            frame_debug_enabled=request.frame_debug_enabled,
            capture_profile=str(dict(request.metadata or {}).get("capture_profile") or ""),
            diagnostic_audio=request.diagnostic_audio,
            frame_read_timeout_seconds=min(
                1.0,
                float(request.recording_timeout_seconds or 1.0),
            ),
            capture_ready_callback=announce_ready,
            correlation_id=request.correlation_id,
            session_id=request.session_id,
        )
    except KeyboardInterrupt:
        raise
    except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
        return DiagnosticAttempt(
            expected_phrase=phrase,
            success=False,
            capture=capture,
            error_message=f"capture_exception:{error.__class__.__name__}:{str(error)[:120]}",
        )
    finally:
        if microphone_started:
            _cancel_adapter(microphone)
            _stop_adapter(microphone)

    if not bool(getattr(capture, "success", False)):
        return DiagnosticAttempt(
            expected_phrase=phrase,
            success=False,
            capture=capture,
            error_message=str(
                getattr(capture, "error_message", "")
                or getattr(capture, "status", "")
                or "active_capture_failed"
            ),
        )

    wav_path = _final_wav_path(capture, request.recording_output_path)
    wav = validate_canonical_wav(wav_path)
    if not bool(wav.get("success")):
        return DiagnosticAttempt(
            expected_phrase=phrase,
            success=False,
            capture=capture,
            wav_path=str(wav_path),
            error_message=str(wav.get("error_message") or "invalid_canonical_wav"),
        )

    runner = runner_factory(
        termination_grace_seconds=float(args.whisper_termination_grace),
        hard_cleanup_deadline_seconds=float(args.whisper_hard_cleanup_deadline),
        status_callback=output_func,
    )
    stt = stt_factory(
        model_path=request.whisper_model_profile,
        whisper_command=request.whisper_executable_path,
        language=request.language,
        timeout_seconds=float(request.transcription_timeout_seconds or 15.0),
        minimum_rms=request.minimum_rms,
        runner=runner,
    )
    output_func(f"Transcribing phrase {index}/{total}")
    try:
        transcription = stt.transcribe_wav(
            wav_path,
            language=request.language,
            timeout_seconds=float(request.transcription_timeout_seconds or 15.0),
        )
    except KeyboardInterrupt:
        _cancel_adapter(stt)
        raise
    except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
        _cancel_adapter(stt)
        return DiagnosticAttempt(
            expected_phrase=phrase,
            success=False,
            capture=capture,
            wav_path=str(wav_path),
            error_message=f"transcription_exception:{error.__class__.__name__}:{str(error)[:120]}",
        )
    finally:
        _cancel_adapter(stt)

    success = bool(getattr(transcription, "success", False))
    return DiagnosticAttempt(
        expected_phrase=phrase,
        success=success,
        capture=capture,
        transcription=transcription,
        wav_path=str(wav_path),
        error_message=(
            ""
            if success
            else str(
                getattr(transcription, "error_message", "")
                or getattr(transcription, "status", "")
                or "active_transcription_failed"
            )
        ),
    )


def _print_attempt(
    attempt: DiagnosticAttempt,
    *,
    request: Any,
    output_func: Callable[[str], None],
) -> None:
    capture = attempt.capture
    capture_data = dict(getattr(capture, "data", {}) or {})
    raw = str(getattr(attempt.transcription, "text", "") or "")
    lifecycle = normalize_active_lifecycle_command(raw)
    action = str(lifecycle.action or LIFECYCLE_ACTION_NONE)
    classification = action if action != LIFECYCLE_ACTION_NONE else "ordinary"
    retained_frames = int(
        getattr(capture, "pre_roll_frames_retained", 0)
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
    candidate_duration = float(
        getattr(capture, "whisper_input_duration_seconds", 0.0)
        or getattr(capture, "normalized_duration_seconds", 0.0)
        or getattr(capture, "assembled_duration_seconds", 0.0)
        or getattr(capture, "duration_seconds", 0.0)
        or 0.0
    )
    beginning_clipped = _yes_no_not_applicable(
        capture_data.get("beginning_clipped", "not_applicable")
        if capture is not None
        else "not_applicable"
    )
    wav_path = attempt.wav_path or _final_wav_path(capture, request.recording_output_path)

    output_func("")
    output_func(f"ACTIVE LIFECYCLE AUDIO RESULT {attempt.expected_phrase!r}")
    output_func(f"Raw transcript: {raw or '<empty>'}")
    output_func(
        "Normalized transcript: "
        f"{lifecycle.normalized_transcript or '<empty>'}"
    )
    output_func(
        "Alias removal: "
        f"{lifecycle.assistant_alias_removed or 'none'}"
    )
    output_func(f"Alias position: {lifecycle.alias_position or 'none'}")
    output_func(f"Lifecycle classification: {classification}")
    output_func(f"Lifecycle action that would be selected: {action}")
    output_func(f"Beginning clipped: {beginning_clipped}")
    output_func(
        "Pre-roll duration: "
        f"{retained_seconds:.3f}s retained; "
        f"{float(request.pre_roll_seconds):.3f}s configured "
        f"({retained_frames}/{expected_frames} frames)"
    )
    output_func(f"Candidate duration: {candidate_duration:.3f}s")
    output_func(f"WAV path: {wav_path}")
    output_func("Lifecycle action executed: no (diagnostic-only)")
    output_func(
        "Attempt status: "
        + ("captured_and_classified" if attempt.success else "failed")
    )
    if attempt.error_message:
        output_func(f"Failure reason: {attempt.error_message[:200]}")


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
    ):
        if not str(value or "").strip() or len(str(value)) > 4096:
            return f"{label} is invalid"
    for label, value, minimum, maximum in (
        ("timeout", args.timeout, 1.0, 3600.0),
        ("active transcription timeout", args.active_transcription_timeout, 0.1, 300.0),
        ("Whisper termination grace", args.whisper_termination_grace, 0.1, 10.0),
        ("Whisper hard cleanup deadline", args.whisper_hard_cleanup_deadline, 0.1, 10.0),
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
    values = (
        requested_path,
        wav_path,
        getattr(capture, "raw_wav_path", ""),
        getattr(capture, "assembled_wav_path", ""),
        getattr(capture, "normalized_wav_path", ""),
        getattr(capture, "final_whisper_input_path", ""),
        getattr(capture, "wav_path", ""),
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
    data = dict(getattr(capture, "data", {}) or {})
    value = (
        getattr(capture, "final_whisper_input_path", "")
        or getattr(capture, "normalized_wav_path", "")
        or getattr(capture, "wav_path", "")
        or data.get("final_whisper_input_path")
        or data.get("normalized_wav_path")
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


def _cancel_adapter(adapter: Any) -> None:
    cancel = getattr(adapter, "cancel_current", None)
    if callable(cancel):
        try:
            cancel()
        except (OSError, RuntimeError, TypeError, ValueError):
            pass


def _stop_adapter(adapter: Any) -> None:
    stop = getattr(adapter, "stop", None)
    if callable(stop):
        try:
            stop()
        except (OSError, RuntimeError, TypeError, ValueError):
            pass


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
