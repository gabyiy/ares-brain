from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    LinuxAlsaMicrophoneAdapter,
    LinuxStandbyWakeListener,
    LinuxWhisperSpeechToTextAdapter,
    WakeListenerConfig,
    WakeListenerRequestV1,
)
from scripts import run_ares_standby_voice as standby_voice  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    defaults = WakeListenerConfig()
    parser = argparse.ArgumentParser(
        description="Capture and classify one bounded local ARES wake-word candidate."
    )
    parser.add_argument("--microphone-device", default=defaults.microphone_device)
    parser.add_argument("--speaker-device", default=standby_voice.DEFAULT_SPEAKER_DEVICE)
    parser.add_argument("--wake-whisper-command", default=defaults.whisper_command)
    parser.add_argument("--wake-whisper-model", default=defaults.whisper_model)
    parser.add_argument("--language", default=defaults.language)
    parser.add_argument("--diagnostic-wake", action="store_true")
    parser.add_argument("--retain-diagnostic-audio", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def run_diagnostic(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    listener_factory: Optional[Callable[..., LinuxStandbyWakeListener]] = None,
) -> int:
    args = build_parser().parse_args(argv)
    if isinstance(args.timeout, bool) or not 1.0 <= args.timeout <= 300.0:
        output_func("Configuration error: --timeout must be between 1 and 300 seconds.")
        return 2
    if args.retain_diagnostic_audio and not args.diagnostic_wake:
        output_func(
            "Configuration error: --retain-diagnostic-audio requires --diagnostic-wake."
        )
        return 2
    issue = _validate_dependencies(args)
    if issue and listener_factory is None:
        output_func(f"Dependency error: {issue}")
        return 2

    config = WakeListenerConfig(
        microphone_device=args.microphone_device,
        whisper_command=str(standby_voice._repo_path_or_command(args.wake_whisper_command)),
        whisper_model=str(standby_voice._repo_path(args.wake_whisper_model)),
        language=args.language,
        diagnostic_wake=True,
        retain_diagnostic_audio=bool(args.retain_diagnostic_audio),
        diagnostic_output_directory="data/runtime/wake_audio",
    )
    microphone = LinuxAlsaMicrophoneAdapter(
        device=args.microphone_device,
        record_seconds=3,
        timeout_seconds=min(args.timeout, 30.0),
    )
    speech_to_text = LinuxWhisperSpeechToTextAdapter(
        model_path=standby_voice._repo_path(args.wake_whisper_model),
        whisper_command=str(standby_voice._repo_path_or_command(args.wake_whisper_command)),
        language=args.language,
        timeout_seconds=min(args.timeout, 30.0),
    )
    callback = standby_voice._wake_diagnostic_callback(output_func, args.speaker_device)
    factory = listener_factory or LinuxStandbyWakeListener
    listener = factory(
        microphone_adapter=microphone,
        speech_to_text_adapter=speech_to_text,
        config=config,
        project_root=REPO_ROOT,
        diagnostic_callback=callback,
    )
    output_func("Say Ares now.")
    started = listener.start(runtime_id="wake-diagnostic")
    if not started.success:
        output_func(f"Wake listener start failed: {started.error_code or started.status}.")
        return 3
    try:
        health = listener.health(runtime_id="wake-diagnostic")
        if not health.success:
            output_func(f"Wake listener health failed: {health.error_code or health.status}.")
            return 3
        result = listener.listen_once(
            WakeListenerRequestV1(
                runtime_id="wake-diagnostic",
                lifecycle_state="STANDBY",
                listener_timeout_seconds=min(config.speech_wait_timeout_seconds, args.timeout),
                microphone_device=args.microphone_device,
                language=args.language,
                wake_phrase_aliases=list(config.wake_phrase_aliases),
                wake_phrase_prefixes=list(config.wake_phrase_prefixes),
                diagnostic_wake=True,
                retain_diagnostic_audio=bool(args.retain_diagnostic_audio),
                metadata={"safe": True, "contains_transcript": False},
            )
        )
        if result.wake_detected:
            output_func(
                "Wake result: accepted "
                f"({result.selected_wake_phrase} -> {result.canonical_wake_phrase})."
            )
            return 0
        if result.status == "no_speech":
            output_func("Wake result: no speech detected before the bounded timeout.")
        elif result.success:
            output_func(
                "Wake result: rejected "
                f"({result.rejection_reason or result.stop_reason or 'exact phrase not matched'})."
            )
        else:
            output_func(
                f"Wake result: failed ({result.error_code or result.stop_reason or result.status})."
            )
        return 1
    finally:
        listener.stop("wake_diagnostic_complete")


def _validate_dependencies(args: argparse.Namespace) -> str:
    command = standby_voice._repo_path_or_command(args.wake_whisper_command)
    if isinstance(command, Path) and not command.is_file():
        return f"wake Whisper command is missing: {command}"
    if isinstance(command, str) and not shutil.which(command):
        return f"wake Whisper command is unavailable: {command}"
    model = standby_voice._repo_path(args.wake_whisper_model)
    if not model.is_file():
        return f"wake Whisper model is missing: {model}"
    return ""


def main() -> int:
    return run_diagnostic()


if __name__ == "__main__":
    raise SystemExit(main())
