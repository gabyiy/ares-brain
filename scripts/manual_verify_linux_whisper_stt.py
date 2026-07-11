from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import LinuxAlsaMicrophoneAdapter, LinuxWhisperSpeechToTextAdapter  # noqa: E402


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
    parser.add_argument("--language", default="auto", help="Whisper language, or auto.")
    parser.add_argument("--timeout", type=float, default=120.0, help="Whisper timeout in seconds.")
    return parser


def run_manual_verification(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    microphone_factory=LinuxAlsaMicrophoneAdapter,
    stt_factory=LinuxWhisperSpeechToTextAdapter,
) -> int:
    args = build_parser().parse_args(list(argv or []))
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

    output_func("Transcribing recorded WAV with offline Whisper...")
    transcription = stt.transcribe_wav(output_path)
    output_func(f"Transcription status: {transcription.status}")
    output_func(
        f"Processing time: {transcription.data.get('processing_time_seconds', 0.0)} seconds"
    )
    language = transcription.data.get("language") or transcription.data.get("language_requested")
    output_func(f"Language: {language or 'unknown'}")
    if transcription.success:
        output_func(f"Recognized text: {transcription.text}")
        return 0

    output_func(transcription.error_message or transcription.text)
    return 5


def main() -> None:
    raise SystemExit(run_manual_verification(sys.argv[1:]))


if __name__ == "__main__":
    main()
