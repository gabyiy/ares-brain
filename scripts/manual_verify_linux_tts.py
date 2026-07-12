from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    DEFAULT_PIPER_LANGUAGE,
    DEFAULT_PIPER_MODEL_CONFIG_PATH,
    DEFAULT_PIPER_MODEL_PATH,
    DEFAULT_PIPER_OUTPUT_DIR,
    DEFAULT_PIPER_TIMEOUT_SECONDS,
    DEFAULT_PIPER_VOICE_ID,
    LinuxAlsaSpeakerAdapter,
    LinuxPiperTextToSpeechAdapter,
    TextToSpeechRequestV1,
    analyze_wav_audio,
)


WARNING = (
    "WARNING: This is a controlled Raspberry Pi TTS verification script. It "
    "generates speech audio from typed text. It does not record microphone "
    "audio, start wake words, use GPT, or play audio unless --playback is set."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a Piper WAV sample and optionally play it through ALSA."
    )
    parser.add_argument("--text", required=True, help="Text to synthesize.")
    parser.add_argument(
        "--piper-command",
        default="",
        help="Path to piper executable. Defaults to external/piper if present, else piper on PATH.",
    )
    parser.add_argument(
        "--model",
        default=str(REPO_ROOT / DEFAULT_PIPER_MODEL_PATH),
        help="Path to Piper ONNX voice model.",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / DEFAULT_PIPER_MODEL_CONFIG_PATH),
        help="Path to Piper voice configuration JSON.",
    )
    parser.add_argument("--voice-id", default=DEFAULT_PIPER_VOICE_ID)
    parser.add_argument("--language", default=DEFAULT_PIPER_LANGUAGE)
    parser.add_argument("--rate", type=float, default=1.0, help="Speaking rate.")
    parser.add_argument(
        "--output",
        default="",
        help="Output WAV path. Defaults to data/manual_tts_samples/ares_tts_<timestamp>.wav.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_PIPER_TIMEOUT_SECONDS,
        help="TTS and playback timeout in seconds.",
    )
    parser.add_argument(
        "--playback",
        action="store_true",
        help="Explicitly play the generated WAV through ALSA.",
    )
    parser.add_argument(
        "--device",
        default="",
        help="Optional ALSA playback device, for example plughw:CARD=Device,DEV=0.",
    )
    parser.add_argument(
        "--aplay-command",
        default="aplay",
        help="Path or command name for aplay.",
    )
    return parser


def run_manual_verification(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(list(argv or []))
    output_func(WARNING)
    if not args.playback:
        output_func("Playback is disabled. Add --playback to speak through the speaker.")

    speaker = LinuxAlsaSpeakerAdapter(
        device=str(args.device or "").strip() or None,
        aplay_command=str(args.aplay_command or "aplay"),
    )
    adapter = LinuxPiperTextToSpeechAdapter(
        piper_command=_default_piper_command(args.piper_command),
        model_path=Path(args.model).expanduser(),
        model_config_path=Path(args.config).expanduser(),
        voice_id=str(args.voice_id),
        language=str(args.language),
        output_dir=REPO_ROOT / DEFAULT_PIPER_OUTPUT_DIR,
        timeout_seconds=float(args.timeout),
        speaker_adapter=speaker,
    )
    health = adapter.health_check().to_dict()
    output_func(f"health: {health}")
    if not health.get("healthy"):
        output_func("FAIL: TTS health check failed.")
        return 2

    request = TextToSpeechRequestV1(
        text=str(args.text),
        language=str(args.language),
        voice_id=str(args.voice_id),
        speaking_rate=float(args.rate),
        output_wav_path=str(Path(args.output).expanduser()) if args.output else None,
        timeout_seconds=float(args.timeout),
        playback_enabled=bool(args.playback),
    )
    result = adapter.synthesize(request)
    output_func(f"status: {result.status}")
    output_func(f"success: {result.success}")
    output_func(f"engine: {result.engine}")
    output_func(f"voice: {result.voice_id}")
    output_func(f"playback_status: {result.playback_status}")
    output_func(f"processing_time_seconds: {result.processing_time_seconds}")
    if result.error_message:
        output_func(f"error: {result.error_message}")
    if result.generated_audio_path:
        output_func(f"generated_audio_path: {result.generated_audio_path}")
        wav = analyze_wav_audio(Path(result.generated_audio_path))
        output_func(f"wav: {wav}")
    process = result.data.get("process") if isinstance(result.data, dict) else None
    if process:
        output_func(f"piper_command: {process.get('command', '')}")
        output_func(f"piper_exit_code: {process.get('returncode', '')}")
        if not result.success:
            output_func(f"piper_stdout: {process.get('stdout_preview', '')}")
            output_func(f"piper_stderr: {process.get('stderr_preview', '')}")
    speaker_result = result.data.get("speaker_result") if isinstance(result.data, dict) else None
    if speaker_result:
        output_func(f"speaker_result: {speaker_result}")

    output_func("PASS: TTS verification completed." if result.success else "FAIL: TTS verification failed.")
    return 0 if result.success else 1


def _default_piper_command(explicit: str) -> str:
    if str(explicit or "").strip():
        return str(explicit).strip()
    local = REPO_ROOT / "external" / "piper" / "piper" / "piper"
    if local.exists():
        return str(local)
    direct = REPO_ROOT / "external" / "piper" / "piper"
    if direct.exists():
        return str(direct)
    return "piper"


def main() -> None:
    raise SystemExit(run_manual_verification(sys.argv[1:]))


if __name__ == "__main__":
    main()
