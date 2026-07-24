from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    LinuxAlsaMicrophoneAdapter,
    LinuxWhisperSpeechToTextAdapter,
    VoiceRuntimeGate,
    WhisperSubprocessRunner,
)
from core.WavAudio import analyze_wav_audio, validate_canonical_wav  # noqa: E402


DEFAULT_MICROPHONE_DEVICE = "plughw:2,0"
DEFAULT_SPEAKER_DEVICE = "plughw:CARD=Device,DEV=0"
DEFAULT_WHISPER_COMMAND = "external/whisper.cpp/build/bin/whisper-cli"
DEFAULT_WHISPER_MODEL = "models/whisper/ggml-base.en.bin"
DEFAULT_LANGUAGE = "en"
DEFAULT_TRANSCRIPTION_TIMEOUT_SECONDS = 15.0
DEFAULT_TERMINATION_GRACE_SECONDS = 1.0
DEFAULT_HARD_CLEANUP_DEADLINE_SECONDS = 3.0
DEFAULT_OUTPUT_PATH = (
    REPO_ROOT / "data" / "runtime" / "active_transcription" / "command.wav"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture one active command and run it through the exact bounded "
            "production Whisper helper without changing ARES runtime state."
        )
    )
    parser.add_argument("--microphone-device", default=DEFAULT_MICROPHONE_DEVICE)
    parser.add_argument("--speaker-device", default=DEFAULT_SPEAKER_DEVICE)
    parser.add_argument("--whisper-command", default=DEFAULT_WHISPER_COMMAND)
    parser.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL)
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument(
        "--transcription-timeout",
        type=float,
        default=DEFAULT_TRANSCRIPTION_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--termination-grace-seconds",
        type=float,
        default=DEFAULT_TERMINATION_GRACE_SECONDS,
    )
    parser.add_argument(
        "--hard-cleanup-deadline-seconds",
        type=float,
        default=DEFAULT_HARD_CLEANUP_DEADLINE_SECONDS,
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument(
        "--retain-audio",
        action="store_true",
        help="retain the current diagnostic capture instead of removing it",
    )
    parser.add_argument(
        "--diagnostic-active-transcription",
        action="store_true",
        help="required owner acknowledgement for this foreground hardware probe",
    )
    return parser


def run_active_transcription_diagnostic(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    microphone_factory: Callable[..., Any] = LinuxAlsaMicrophoneAdapter,
    stt_factory: Callable[..., Any] = LinuxWhisperSpeechToTextAdapter,
    runner_factory: Callable[..., Any] = WhisperSubprocessRunner,
    gate_factory: Callable[..., Any] = VoiceRuntimeGate,
) -> int:
    args = build_parser().parse_args(argv)
    issue = _configuration_issue(args)
    if issue:
        output_func(f"Configuration error: {issue}")
        return 2

    requested_output = Path(args.output).expanduser()
    microphone = microphone_factory(
        device=str(args.microphone_device),
        record_seconds=5,
        # Matches the production auto-stop envelope:
        # 0.75 s calibration + 10 s wait + 15 s utterance + 5 s cleanup.
        timeout_seconds=30.75,
    )
    gate = gate_factory(settle_delay_seconds=0.0)
    capture = None
    microphone_start_attempted = False
    microphone_released = False
    gate_owned = False
    capture_paths: set[Path] = {requested_output}
    gate_released = True

    output_func("ARES active-command transcription diagnostic")
    output_func(f"Microphone device: {args.microphone_device}")
    output_func(
        "Speaker device (not opened by this diagnostic): "
        f"{args.speaker_device}"
    )
    output_func("Say one active command when capture starts.")

    try:
        microphone_start_attempted = True
        started = microphone.start()
        if not bool(getattr(started, "success", False)):
            detail = str(
                getattr(started, "error_message", "")
                or getattr(started, "status", "")
                or "microphone_start_failed"
            )
            output_func(f"Active microphone start failed: {detail[:160]}")
            return 3
        gate.begin_capture("diagnostic_active_command")
        gate_owned = True
        output_func("Active microphone capture started")
        capture = microphone.record_until_silence(
            requested_output,
            device=str(args.microphone_device),
            diagnostic_audio=bool(args.retain_audio),
        )
        capture_paths.update(_capture_paths(capture))
        if not bool(getattr(capture, "success", False)):
            detail = str(
                getattr(capture, "error_message", "")
                or getattr(capture, "status", "")
                or "active_command_capture_failed"
            )
            output_func(f"Active command capture failed: {detail[:160]}")
            output_func(
                "Temporary audio cleanup: "
                + _cleanup_capture_paths(
                    capture_paths,
                    retain=bool(args.retain_audio),
                )
            )
            return 3
        output_func("Command captured")
    except KeyboardInterrupt:
        output_func("Active transcription diagnostic cancelled.")
        _report_audio_cleanup(
            capture_paths,
            retain=bool(args.retain_audio),
            output_func=output_func,
        )
        return 130
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        output_func(
            "Active command capture failed safely: "
            f"{error.__class__.__name__}:{str(error)[:160]}"
        )
        _report_audio_cleanup(
            capture_paths,
            retain=bool(args.retain_audio),
            output_func=output_func,
        )
        return 3
    finally:
        if gate_owned:
            gate.end_capture("diagnostic_active_command")
        if microphone_start_attempted:
            _cancel_microphone(microphone)
            microphone_released = _stop_microphone(microphone, output_func)
        gate_released = not bool(gate.snapshot().get("capture_active"))
        output_func(f"Microphone gate released: {'yes' if gate_released else 'no'}")
        output_func(
            f"Microphone adapter released: {'yes' if microphone_released else 'no'}"
        )
    if not gate_released or not microphone_released:
        _report_audio_cleanup(
            capture_paths,
            retain=bool(args.retain_audio),
            output_func=output_func,
        )
        return 3

    wav_path = Path(
        str(
            getattr(capture, "final_whisper_input_path", "")
            or getattr(capture, "normalized_wav_path", "")
            or getattr(capture, "wav_path", "")
            or ""
        )
    ).expanduser()
    if str(wav_path) in {"", "."}:
        output_func("Finalized WAV path is unavailable.")
        _report_audio_cleanup(
            capture_paths,
            retain=bool(args.retain_audio),
            output_func=output_func,
        )
        return 3
    capture_paths.add(wav_path)
    wav = analyze_wav_audio(wav_path)
    output_func(f"Finalized WAV path: {wav_path}")
    if not bool(wav.get("success")):
        output_func(
            "Finalized WAV is invalid: "
            f"{str(wav.get('error_message') or 'wav_validation_failed')[:160]}"
        )
        _report_audio_cleanup(
            capture_paths,
            retain=bool(args.retain_audio),
            output_func=output_func,
        )
        return 3
    canonical = validate_canonical_wav(wav_path)
    if not bool(canonical.get("success")):
        output_func(
            "Finalized WAV is not canonical active-command PCM: "
            f"{str(canonical.get('error_message') or 'canonical_wav_validation_failed')[:160]}"
        )
        _report_audio_cleanup(
            capture_paths,
            retain=bool(args.retain_audio),
            output_func=output_func,
        )
        return 3
    output_func(
        "Finalized WAV format: "
        f"{int(wav.get('sample_rate_hz', 0))} Hz, "
        f"{int(wav.get('channels', 0))} channel(s), "
        f"{int(wav.get('sample_width_bytes', 0)) * 8}-bit PCM, "
        f"{float(wav.get('duration_seconds', 0.0)):.3f}s, "
        f"{int(wav.get('byte_count', 0))} bytes"
    )

    exit_code = 4
    stt = None
    try:
        runner = runner_factory(
            termination_grace_seconds=float(args.termination_grace_seconds),
            hard_cleanup_deadline_seconds=float(
                args.hard_cleanup_deadline_seconds
            ),
            status_callback=output_func,
        )
        stt = stt_factory(
            model_path=_repo_path(args.whisper_model),
            whisper_command=_repo_path_or_command(args.whisper_command),
            language=str(args.language),
            timeout_seconds=float(args.transcription_timeout),
            runner=runner,
        )
        output_func("Transcribing command")
        output_func(f"Whisper timeout: {float(args.transcription_timeout):g} seconds")
        result = stt.transcribe_wav(
            wav_path,
            language=str(args.language),
            timeout_seconds=float(args.transcription_timeout),
        )
        cleanup_confirmed = _print_transcription_result(
            result,
            timeout_seconds=float(args.transcription_timeout),
            output_func=output_func,
        )
        exit_code = (
            0
            if bool(getattr(result, "success", False)) and cleanup_confirmed
            else 4
        )
    except KeyboardInterrupt:
        _cancel_stt(stt)
        output_func("Active transcription diagnostic cancelled.")
        exit_code = 130
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        _cancel_stt(stt)
        output_func(
            "Whisper diagnostic failed safely: "
            f"{error.__class__.__name__}:{str(error)[:160]}"
        )
        exit_code = 4
    finally:
        _cancel_stt(stt)
        _report_audio_cleanup(
            capture_paths,
            retain=bool(args.retain_audio),
            output_func=output_func,
        )
    return exit_code


def _print_transcription_result(
    result: Any,
    *,
    timeout_seconds: float,
    output_func: Callable[[str], None],
) -> bool:
    data = dict(getattr(result, "data", {}) or {})
    process = dict(data.get("process") or {})
    metadata = dict(process.get("metadata") or {})
    pid = int(metadata.get("pid") or metadata.get("process_pid") or 0)
    pgid = int(
        metadata.get("pgid")
        or metadata.get("process_group_id")
        or metadata.get("process_pgid")
        or 0
    )
    elapsed = float(
        metadata.get("elapsed_seconds")
        or getattr(result, "processing_time_seconds", 0.0)
        or data.get("processing_time_seconds")
        or 0.0
    )
    returncode_value = process.get("returncode")
    returncode = (
        int(returncode_value)
        if returncode_value is not None
        else None
    )
    status = str(getattr(result, "status", "") or "")
    timed_out = bool(process.get("timed_out")) or status == "transcription_timeout"
    terminated = bool(
        metadata.get("term_sent")
        or metadata.get("terminated")
        or metadata.get("process_group_terminated")
    )
    killed = bool(
        metadata.get("kill_sent")
        or metadata.get("killed")
        or metadata.get("process_group_killed")
    )
    reaped = bool(metadata.get("reaped"))
    handles_closed = bool(
        metadata.get("handles_closed")
        or metadata.get("output_handles_closed")
        or metadata.get("pipes_closed")
        or (
            metadata.get("stdin_closed")
            and metadata.get("stdout_closed")
            and metadata.get("stderr_closed")
        )
    )
    cleanup_completed = bool(
        metadata.get("cleanup_completed")
        or (reaped and handles_closed)
    )
    process_group_started = bool(metadata.get("process_group_started"))

    output_func(f"Whisper process: pid={pid or 'unknown'}, pgid={pgid or 'unknown'}")
    output_func(
        "Whisper process group started: "
        f"{'yes' if process_group_started else 'no'}"
    )
    output_func(f"Whisper command: {_format_command(process.get('args') or [])}")
    output_func(
        "Whisper completed: "
        f"exit={returncode if returncode is not None else 'unknown'}, "
        f"elapsed={elapsed:.3f} seconds"
    )
    output_func(f"Whisper result status: {status or 'unknown'}")
    if timed_out:
        output_func(
            f"Whisper transcription timed out after {timeout_seconds:g} seconds"
        )
        output_func(
            "Whisper termination: "
            f"SIGTERM={'sent' if terminated else 'not_sent'}, "
            f"SIGKILL={'sent' if killed else 'not_sent'}"
        )
    output_func(f"Whisper child reaped: {'yes' if reaped else 'no'}")
    output_func(
        f"Whisper output handles closed: {'yes' if handles_closed else 'no'}"
    )
    output_func(
        "Whisper process cleanup: "
        f"{'completed' if cleanup_completed else 'incomplete'}"
    )
    output_func(f"Transcript: {str(getattr(result, 'text', '') or '<empty>')}")
    return bool(process_group_started and reaped and handles_closed and cleanup_completed)


def _capture_paths(capture: Any) -> set[Path]:
    values = (
        getattr(capture, "raw_wav_path", ""),
        getattr(capture, "assembled_wav_path", ""),
        getattr(capture, "normalized_wav_path", ""),
        getattr(capture, "final_whisper_input_path", ""),
        getattr(capture, "wav_path", ""),
    )
    return {
        Path(str(value)).expanduser()
        for value in values
        if str(value or "").strip()
    }


def _cleanup_capture_paths(paths: set[Path], *, retain: bool) -> str:
    existing = [path for path in paths if path.exists() and path.is_file()]
    if retain:
        return "retained" if existing else "not_applicable"
    failed = False
    removed = False
    for path in existing:
        try:
            path.unlink()
            removed = True
        except OSError:
            failed = True
    if failed:
        return "incomplete"
    return "removed" if removed else "not_applicable"


def _report_audio_cleanup(
    paths: set[Path],
    *,
    retain: bool,
    output_func: Callable[[str], None],
) -> str:
    status = _cleanup_capture_paths(paths, retain=retain)
    output_func(f"Temporary audio cleanup: {status}")
    return status


def _cancel_microphone(microphone: Any) -> None:
    cancel = getattr(microphone, "cancel_current", None)
    if callable(cancel):
        try:
            cancel()
        except (OSError, RuntimeError, TypeError, ValueError):
            pass


def _stop_microphone(
    microphone: Any,
    output_func: Callable[[str], None],
) -> bool:
    try:
        stopped = microphone.stop()
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        output_func(
            "Microphone cleanup failed: "
            f"{error.__class__.__name__}:{str(error)[:120]}"
        )
        return False
    if not bool(getattr(stopped, "success", False)):
        output_func(
            "Microphone cleanup failed: "
            f"{str(getattr(stopped, 'error_message', '') or getattr(stopped, 'status', ''))[:120]}"
        )
        return False
    return True


def _cancel_stt(stt: Any) -> None:
    cancel = getattr(stt, "cancel_current", None)
    if callable(cancel):
        try:
            cancel()
        except (OSError, RuntimeError, TypeError, ValueError):
            pass


def _configuration_issue(args: argparse.Namespace) -> str:
    if not bool(args.diagnostic_active_transcription):
        return "--diagnostic-active-transcription is required"
    for label, value in (
        ("microphone device", args.microphone_device),
        ("speaker device", args.speaker_device),
        ("Whisper command", args.whisper_command),
        ("Whisper model", args.whisper_model),
        ("language", args.language),
        ("output path", args.output),
    ):
        clean = str(value or "").strip()
        if not clean or len(clean) > 4096:
            return f"{label} is invalid"
    for label, value, minimum, maximum in (
        (
            "transcription timeout",
            args.transcription_timeout,
            0.1,
            300.0,
        ),
        (
            "termination grace",
            args.termination_grace_seconds,
            0.1,
            10.0,
        ),
        (
            "hard cleanup deadline",
            args.hard_cleanup_deadline_seconds,
            0.1,
            10.0,
        ),
    ):
        if (
            isinstance(value, bool)
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
        ):
            return f"{label} must be between {minimum:g} and {maximum:g} seconds"
    if float(args.hard_cleanup_deadline_seconds) < float(
        args.termination_grace_seconds
    ):
        return "hard cleanup deadline must not be shorter than termination grace"
    return ""


def _repo_path(value: str) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def _repo_path_or_command(value: str) -> str:
    clean = str(value or "").strip()
    path = Path(clean).expanduser()
    if path.is_absolute():
        return str(path)
    if len(path.parts) > 1:
        return str(REPO_ROOT / path)
    return clean


def _format_command(args: Any) -> str:
    values = [str(value) for value in list(args or [])]
    return " ".join(values) if values else "<unavailable>"


def main() -> int:
    return run_active_transcription_diagnostic()


if __name__ == "__main__":
    raise SystemExit(main())
