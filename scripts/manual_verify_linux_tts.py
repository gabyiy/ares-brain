from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Callable, Dict, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    ALSA_SPEAKER_STATUS_PLAYED,
    DEFAULT_PIPER_OUTPUT_DIR,
    DEFAULT_PIPER_TIMEOUT_SECONDS,
    DEFAULT_VOICE_PROFILE_CONFIG_PATH,
    LinuxAlsaSpeakerAdapter,
    LinuxPiperTextToSpeechAdapter,
    TextToSpeechRequestV1,
    VoiceProfileError,
    VoiceProfileRegistry,
    analyze_wav_audio,
    load_voice_profile_registry,
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
    parser.add_argument("--text", default="", help="Text to synthesize.")
    parser.add_argument(
        "--piper-command",
        default="",
        help="Path to piper executable. Defaults to external/piper if present, else piper on PATH.",
    )
    parser.add_argument(
        "--voice-profile",
        default="",
        help="Voice profile identifier. Defaults to the configured ARES voice.",
    )
    parser.add_argument(
        "--voice-profiles-config",
        default=str(REPO_ROOT / DEFAULT_VOICE_PROFILE_CONFIG_PATH),
        help="Validated local voice profile configuration.",
    )
    parser.add_argument(
        "--list-voices",
        action="store_true",
        help="List configured voice profiles without synthesis or playback.",
    )
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
    try:
        registry = load_voice_profile_registry(
            args.voice_profiles_config,
            project_root=REPO_ROOT,
        )
    except VoiceProfileError as error:
        output_func(f"FAIL: Voice profile config is invalid: {error.code}: {error.message}")
        return 2
    if args.list_voices:
        _print_voice_profiles(registry, output_func)
        return 0
    if not str(args.text or "").strip():
        output_func("FAIL: --text is required unless --list-voices is used.")
        return 2
    try:
        selected_profile = registry.resolve(args.voice_profile)
    except VoiceProfileError as error:
        output_func(f"FAIL: Voice profile selection failed: {error.code}: {error.message}")
        return 2
    if not args.playback:
        output_func("Playback is disabled. Add --playback to speak through the speaker.")

    piper_command = _default_piper_command(args.piper_command)
    model_path = registry.model_path(selected_profile)
    config_path = registry.config_path(selected_profile)
    output_path = _resolve_output_path(args.output)
    speaker_device = str(args.device or "").strip()
    output_func(f"piper_binary: {piper_command}")
    output_func(f"requested_voice_profile: {args.voice_profile or '(configured default)'}")
    output_func(f"resolved_voice_profile: {selected_profile.profile_id}")
    output_func(f"voice_display_name: {selected_profile.display_name}")
    output_func(f"model_path: {model_path}")
    output_func(f"config_path: {config_path}")
    output_func(f"output_wav_path: {output_path}")
    output_func(f"speaker_device: {speaker_device or 'default'}")
    output_func(f"aplay_binary: {args.aplay_command}")

    speaker = LinuxAlsaSpeakerAdapter(
        device=speaker_device or None,
        aplay_command=str(args.aplay_command or "aplay"),
    )
    adapter = LinuxPiperTextToSpeechAdapter(
        piper_command=piper_command,
        voice_registry=registry,
        project_root=REPO_ROOT,
        output_dir=output_path.parent,
        timeout_seconds=float(args.timeout),
        speaker_adapter=speaker,
    )
    health_result = adapter.health_check(str(args.voice_profile or ""))
    health = health_result.to_dict()
    _print_health_diagnostics(health, output_func)
    if not _is_healthy_tts_result(health):
        _print_nested_health_process(health, output_func)
        output_func("piper_command: not_run (health check failed)")
        output_func("aplay_command: not_run (health check failed)")
        output_func("FAIL: TTS health check failed.")
        return 2

    request = TextToSpeechRequestV1(
        text=str(args.text),
        language=selected_profile.locale,
        voice_profile_id=str(args.voice_profile or ""),
        speaking_rate=float(args.rate),
        output_wav_path=str(output_path),
        timeout_seconds=float(args.timeout),
        playback_enabled=bool(args.playback),
    )
    result = adapter.synthesize(request)
    output_func(f"status: {result.status}")
    output_func(f"success: {result.success}")
    output_func(f"engine: {result.engine}")
    output_func(f"requested_voice_profile: {result.requested_voice_profile or '(configured default)'}")
    output_func(f"resolved_voice_profile: {result.resolved_voice_profile}")
    output_func(f"voice_display_name: {result.voice_display_name}")
    output_func(f"voice_language: {result.language}")
    output_func(f"voice_locale: {result.locale}")
    output_func(f"voice_gender: {result.gender}")
    output_func(f"voice_quality: {result.quality}")
    output_func(f"playback_status: {result.playback_status}")
    output_func(f"processing_time_seconds: {result.processing_time_seconds}")
    if result.error_message:
        output_func(f"error: {result.error_message}")
    wav: Dict[str, Any] = {
        "success": False,
        "error_message": "generated_audio_path_missing",
    }
    if result.generated_audio_path:
        output_func(f"generated_audio_path: {result.generated_audio_path}")
        wav = analyze_wav_audio(Path(result.generated_audio_path))
    _print_wav_diagnostics(wav, output_func)
    process = result.data.get("process") if isinstance(result.data, dict) else None
    if process:
        _print_process_diagnostics("piper", process, output_func)
    else:
        output_func("piper_command: not_run")

    playback = result.data.get("playback") if isinstance(result.data, dict) else None
    if args.playback and isinstance(playback, dict):
        output_func(f"speaker_result: {playback}")
        speaker_process = playback.get("data", {}).get("process", {})
        if speaker_process:
            _print_process_diagnostics("aplay", speaker_process, output_func)
        else:
            output_func("aplay_command: not_run")
    elif args.playback:
        output_func("aplay_command: not_run")
    else:
        output_func("aplay_command: not_run (playback disabled)")

    valid_wav = bool(wav.get("success")) and int(wav.get("byte_count", 0)) > 0
    playback_ok = not args.playback or result.playback_status == ALSA_SPEAKER_STATUS_PLAYED
    verified = bool(result.success and valid_wav and playback_ok)
    output_func("PASS: TTS verification completed." if verified else "FAIL: TTS verification failed.")
    return 0 if verified else 1


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


def _resolved_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _resolve_output_path(explicit: str) -> Path:
    if str(explicit or "").strip():
        return _resolved_path(str(explicit).strip())
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    return (REPO_ROOT / DEFAULT_PIPER_OUTPUT_DIR / f"ares_tts_{timestamp}.wav").resolve()


def _print_voice_profiles(
    registry: VoiceProfileRegistry,
    output_func: Callable[[str], None],
) -> None:
    output_func("Configured Piper voice profiles:")
    for profile in registry.list_profiles():
        metadata = registry.profile_metadata(profile)
        output_func(
            " | ".join(
                (
                    f"profile_id={profile.profile_id}",
                    f"display_name={profile.display_name}",
                    f"language={profile.locale}",
                    f"gender={profile.gender}",
                    f"quality={profile.quality}",
                    f"installed={metadata['installed']}",
                    f"default={profile.is_default}",
                    f"enabled={profile.enabled}",
                    f"model_path={metadata['resolved_model_path']}",
                )
            )
        )


def _is_healthy_tts_result(health: Dict[str, Any]) -> bool:
    speaker = health.get("data", {}).get("speaker", {})
    return bool(
        health.get("success") is True
        and str(health.get("status", "")) == "healthy"
        and isinstance(speaker, dict)
        and speaker.get("success") is True
        and str(speaker.get("status", "")) == "healthy"
    )


def _print_health_diagnostics(
    health: Dict[str, Any],
    output_func: Callable[[str], None],
) -> None:
    data = health.get("data", {}) if isinstance(health.get("data"), dict) else {}
    speaker = data.get("speaker", {}) if isinstance(data.get("speaker"), dict) else {}
    speaker_data = speaker.get("data", {}) if isinstance(speaker.get("data"), dict) else {}
    output_func(f"tts_health_success: {health.get('success', False)}")
    output_func(f"tts_health_status: {health.get('status', '')}")
    output_func(f"resolved_piper_binary: {data.get('piper_binary_path', '')}")
    output_func(f"speaker_health_success: {speaker.get('success', False)}")
    output_func(f"speaker_health_status: {speaker.get('status', '')}")
    output_func(f"resolved_aplay_binary: {speaker_data.get('aplay_path', '')}")
    output_func(f"speaker_device_available: {speaker_data.get('device_available', '')}")
    output_func(f"health: {health}")


def _print_nested_health_process(
    health: Dict[str, Any],
    output_func: Callable[[str], None],
) -> None:
    data = health.get("data", {}) if isinstance(health.get("data"), dict) else {}
    speaker = data.get("speaker", {}) if isinstance(data.get("speaker"), dict) else {}
    speaker_data = speaker.get("data", {}) if isinstance(speaker.get("data"), dict) else {}
    process = speaker_data.get("process", {})
    if isinstance(process, dict) and process:
        _print_process_diagnostics("speaker_health", process, output_func)


def _print_wav_diagnostics(
    wav: Dict[str, Any],
    output_func: Callable[[str], None],
) -> None:
    output_func(f"wav_valid: {bool(wav.get('success'))}")
    output_func(f"wav_file_size_bytes: {wav.get('byte_count', 0)}")
    output_func(f"wav_duration_seconds: {wav.get('duration_seconds', 0.0)}")
    output_func(f"wav_sample_rate_hz: {wav.get('sample_rate_hz', 0)}")
    output_func(f"wav_channels: {wav.get('channels', 0)}")
    output_func(f"wav_sample_width_bytes: {wav.get('sample_width_bytes', 0)}")
    if not wav.get("success"):
        output_func(f"wav_error: {wav.get('error_message', 'invalid_wav')}")


def _print_process_diagnostics(
    label: str,
    process: Dict[str, Any],
    output_func: Callable[[str], None],
) -> None:
    command = process.get("command") or " ".join(
        str(arg) for arg in process.get("args", [])
    )
    output_func(f"{label}_command: {command}")
    output_func(f"{label}_exit_code: {process.get('returncode', '')}")
    failed = bool(
        process.get("timed_out")
        or process.get("error_message")
        or process.get("returncode") not in (0, None, "")
    )
    if failed:
        stdout = process.get("stdout", process.get("stdout_preview", ""))
        stderr = process.get("stderr", process.get("stderr_preview", ""))
        output_func(f"{label}_stdout: {stdout}")
        output_func(f"{label}_stderr: {stderr}")


def main() -> None:
    raise SystemExit(run_manual_verification(sys.argv[1:]))


if __name__ == "__main__":
    main()
