from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    LinuxAlsaMicrophoneAdapter,
    LinuxWhisperSpeechToTextAdapter,
    SafeSubprocessRunner,
    analyze_wav_audio,
)


WARNING = (
    "WARNING: Offline Whisper STT verification records a short local WAV sample "
    "only when --record is provided. It does not start wake word detection, "
    "background listening, GPT, internet access, TTS, or conversation loops."
)


def _default_output_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return REPO_ROOT / "data" / "manual_whisper_samples" / f"ares_whisper_sample_{timestamp}.wav"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manually verify Raspberry Pi ALSA microphone capture plus offline Whisper STT."
    )
    parser.add_argument("--record", action="store_true", help="Explicitly record and transcribe.")
    parser.add_argument("--device", default="", help="Optional ALSA device, for example hw:1,0")
    parser.add_argument("--seconds", type=int, default=3, help="Seconds to record.")
    parser.add_argument("--output", default="", help="Output WAV path for the recorded sample.")
    parser.add_argument(
        "--model",
        required=True,
        help="Path to local offline Whisper model, for example models/whisper/ggml-tiny.en.bin",
    )
    parser.add_argument(
        "--whisper-command",
        default="whisper-cli",
        help="Local Whisper executable, for example whisper-cli or /path/to/whisper-cli.",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Whisper language. Default is en for the recommended ggml-tiny.en.bin model.",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="Whisper timeout in seconds.")
    parser.add_argument(
        "--min-rms",
        type=float,
        default=50.0,
        help="Minimum RMS amplitude required before running Whisper.",
    )
    parser.add_argument(
        "--playback",
        action="store_true",
        help="Explicitly play the recorded WAV with aplay after transcription diagnostics.",
    )
    parser.add_argument("--aplay-command", default="aplay", help="Local aplay executable.")
    return parser


def run_manual_verification(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    microphone_factory=LinuxAlsaMicrophoneAdapter,
    stt_factory=LinuxWhisperSpeechToTextAdapter,
    runner: Optional[SafeSubprocessRunner] = None,
) -> int:
    args = build_parser().parse_args(list(argv or []))
    runner = runner or SafeSubprocessRunner()
    output_func(WARNING)

    microphone = microphone_factory(
        device=args.device or None,
        record_seconds=args.seconds,
        timeout_seconds=args.seconds + 5,
    )
    stt = stt_factory(
        model_path=args.model,
        whisper_command=args.whisper_command,
        language=args.language,
        timeout_seconds=args.timeout,
        minimum_rms=args.min_rms,
    )

    output_func("Checking ALSA microphone health...")
    mic_health = microphone.health_check()
    output_func(f"Microphone health: {mic_health.status}")
    if not mic_health.success:
        output_func(mic_health.error_message or mic_health.text)
        return 2

    output_func("Checking offline Whisper health...")
    stt_health = stt.health_check()
    output_func(f"Whisper health: {stt_health.status}")
    if not stt_health.success:
        output_func(stt_health.error_message or stt_health.text)
        return 3

    if not args.record:
        output_func("No recording requested. Re-run with --record to capture and transcribe.")
        return 0

    output_path = Path(args.output).expanduser() if args.output else _default_output_path()
    output_func(f"Recording {args.seconds} second(s) to {output_path}...")
    record = microphone.record_wav(
        output_path=output_path,
        seconds=args.seconds,
        timeout_seconds=args.seconds + 5,
        overwrite=False,
    )
    output_func(f"Recording status: {record.status}")
    if not record.success:
        output_func(record.error_message or record.text)
        return 4

    wav_diagnostics = analyze_wav_audio(output_path)
    _print_wav_diagnostics(wav_diagnostics, output_func)
    if not wav_diagnostics.get("success"):
        output_func(str(wav_diagnostics.get("error_message", "invalid_wav")))
        return 5
    if int(wav_diagnostics.get("peak_amplitude", 0)) <= 0:
        output_func("Recorded WAV is silent.")
        return 5
    if args.min_rms > 0 and float(wav_diagnostics.get("rms_amplitude", 0.0)) < args.min_rms:
        output_func(
            f"Recorded WAV RMS is below threshold: {wav_diagnostics.get('rms_amplitude')} < {args.min_rms}"
        )
        return 5

    output_func("Transcribing recorded WAV with offline Whisper...")
    transcription = stt.transcribe_wav(output_path)
    output_func(f"Transcription status: {transcription.status}")
    output_func(
        f"Processing time: {transcription.data.get('processing_time_seconds', 0.0)} seconds"
    )
    language = transcription.data.get("language") or transcription.data.get("language_requested")
    output_func(f"Language: {language or 'unknown'}")
    if transcription.data.get("language_requested") or transcription.data.get("language_effective"):
        output_func(f"Requested language: {transcription.data.get('language_requested', '')}")
        output_func(f"Effective language: {transcription.data.get('language_effective', '')}")
    _print_process_diagnostics(transcription.data, output_func)

    playback_code = 0
    if args.playback:
        playback_code = _playback_wav(output_path, args.aplay_command, runner, output_func)

    if transcription.success and transcription.text:
        output_func(f"Recognized text: {transcription.text}")
        return playback_code

    output_func(transcription.error_message or transcription.text)
    return 5


def _print_wav_diagnostics(wav_diagnostics: dict, output_func: Callable[[str], None]) -> None:
    output_func(f"WAV path: {wav_diagnostics.get('path', '')}")
    output_func(f"WAV size: {wav_diagnostics.get('byte_count', 0)} bytes")
    output_func(f"Duration: {wav_diagnostics.get('duration_seconds', 0.0)} seconds")
    output_func(f"Sample rate: {wav_diagnostics.get('sample_rate_hz', 0)} Hz")
    output_func(f"Channels: {wav_diagnostics.get('channels', 0)}")
    output_func(f"Sample width: {wav_diagnostics.get('sample_width_bytes', 0)} bytes")
    output_func(f"Peak amplitude: {wav_diagnostics.get('peak_amplitude', 0)}")
    output_func(f"RMS amplitude: {wav_diagnostics.get('rms_amplitude', 0.0)}")


def _print_process_diagnostics(transcription_data: dict, output_func: Callable[[str], None]) -> None:
    process = dict(transcription_data.get("process") or {})
    if not process:
        return
    output_func(f"Whisper command: {process.get('command', '')}")
    output_func(f"Whisper exit code: {process.get('returncode', '')}")
    if process.get("stdout_preview"):
        output_func(f"Raw stdout: {process.get('stdout_preview')}")
    if process.get("stderr_preview"):
        output_func(f"Raw stderr: {process.get('stderr_preview')}")


def _playback_wav(
    wav_path: Path,
    aplay_command: str,
    runner: SafeSubprocessRunner,
    output_func: Callable[[str], None],
) -> int:
    aplay_path = runner.which(aplay_command)
    if not aplay_path:
        output_func("Playback requested but aplay was not found.")
        return 6
    command = [aplay_path, str(wav_path)]
    output_func(f"Playback command: {' '.join(command)}")
    result = runner.run(command, timeout_seconds=120.0)
    output_func(f"Playback exit code: {result.returncode}")
    if result.returncode != 0 or result.timed_out:
        output_func(result.error_message or result.stderr or "aplay_failed")
        return 6
    return 0


def main() -> None:
    raise SystemExit(run_manual_verification(sys.argv[1:]))


if __name__ == "__main__":
    main()
