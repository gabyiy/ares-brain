from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import shutil
import sys
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    BrainRuntime,
    BrainRuntimeConfig,
    BrainSessionConfig,
    BrainSessionManager,
    CoreService,
    LinuxAlsaMicrophoneAdapter,
    LinuxStandbyWakeListener,
    LinuxWhisperSpeechToTextAdapter,
    SingleTurnPipelineRuntimeInputAdapter,
    SingleTurnPipelineRuntimeOutputAdapter,
    VoiceRuntimeGate,
    WakeListenerConfig,
)
from scripts import manual_verify_single_turn_voice as single_turn  # noqa: E402
from scripts import run_ares_voice as single_voice_launcher  # noqa: E402


_WAKE_DEFAULTS = WakeListenerConfig()
DEFAULT_MICROPHONE_DEVICE = _WAKE_DEFAULTS.microphone_device
DEFAULT_SPEAKER_DEVICE = single_turn.DEFAULT_SPEAKER_DEVICE
DEFAULT_WAKE_WHISPER_COMMAND = _WAKE_DEFAULTS.whisper_command
DEFAULT_WAKE_WHISPER_MODEL = _WAKE_DEFAULTS.whisper_model
DEFAULT_COMMAND_WHISPER_COMMAND = single_turn.DEFAULT_WHISPER_COMMAND
DEFAULT_COMMAND_WHISPER_MODEL = single_turn.DEFAULT_WHISPER_MODEL
DEFAULT_VOICE_PROFILE = single_voice_launcher._configured_default_voice_profile()
DEFAULT_INACTIVITY_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 300.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the foreground ARES standby wake and active voice runtime."
    )
    parser.add_argument("--microphone-device", default=DEFAULT_MICROPHONE_DEVICE)
    parser.add_argument("--speaker-device", default=DEFAULT_SPEAKER_DEVICE)
    parser.add_argument("--language", default="en")
    parser.add_argument("--wake-whisper-command", default=DEFAULT_WAKE_WHISPER_COMMAND)
    parser.add_argument("--wake-whisper-model", default=DEFAULT_WAKE_WHISPER_MODEL)
    parser.add_argument("--command-whisper-command", default=DEFAULT_COMMAND_WHISPER_COMMAND)
    parser.add_argument("--command-whisper-model", default=DEFAULT_COMMAND_WHISPER_MODEL)
    parser.add_argument("--voice-profile", default=DEFAULT_VOICE_PROFILE)
    parser.add_argument("--inactivity-seconds", type=float, default=DEFAULT_INACTIVITY_SECONDS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--diagnostic-routing", action="store_true")
    parser.add_argument("--retain-diagnostic-audio", action="store_true")
    return parser


def create_runtime(
    args: argparse.Namespace,
    *,
    output_func: Callable[[str], None] = print,
    pipeline_factory: Optional[Callable[..., Any]] = None,
    wake_listener_factory: Optional[Callable[..., Any]] = None,
) -> tuple[BrainRuntime, Any, Any]:
    session_manager = BrainSessionManager(
        config=BrainSessionConfig(
            inactivity_timeout_seconds=args.inactivity_seconds,
            maximum_consecutive_failures=3,
        )
    )
    core_service = CoreService(
        brain_session_manager=session_manager,
        register_default_pc=False,
    )
    skill_manager = single_turn.create_skill_manager(core_service)
    command_handler = single_turn.build_existing_brain_handler(skill_manager)
    if args.diagnostic_routing:
        command_handler = _diagnostic_handler(command_handler, output_func)

    manual_args = _command_pipeline_args(args)
    base_request = single_turn.request_from_args(manual_args)
    base_request = replace(
        base_request,
        playback_enabled=True,
        cleanup_policy="keep" if args.retain_diagnostic_audio else "delete_on_success",
        diagnostic_audio=bool(args.retain_diagnostic_audio),
        metadata={
            **dict(base_request.metadata or {}),
            "source": "run_ares_standby_voice",
            "foreground_runtime": True,
            "background_listening": False,
        },
    )
    factory = pipeline_factory or single_turn.create_pipeline
    pipeline = factory(
        manual_args,
        output_func=lambda _text: None,
        skill_manager=skill_manager,
    )
    gate = VoiceRuntimeGate(settle_delay_seconds=0.35)
    active_input = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=base_request,
        session_id_provider=lambda: session_manager.session_id,
        voice_io_gate=gate,
    )
    voice_output = SingleTurnPipelineRuntimeOutputAdapter(
        pipeline=pipeline,
        base_request=base_request,
        voice_io_gate=gate,
        output_func=lambda text: output_func(f"ARES: {text}"),
    )

    wake_config = WakeListenerConfig(
        microphone_device=args.microphone_device,
        whisper_command=str(_repo_path_or_command(args.wake_whisper_command)),
        whisper_model=str(_repo_path(args.wake_whisper_model)),
        language=args.language,
        playback_settle_delay_seconds=gate.settle_delay_seconds,
        retain_diagnostic_audio=bool(args.retain_diagnostic_audio),
        diagnostic_output_directory="data/runtime/wake_audio",
    )
    wake_microphone = LinuxAlsaMicrophoneAdapter(
        device=args.microphone_device,
        record_seconds=3,
        timeout_seconds=min(float(args.timeout), 30.0),
    )
    wake_stt = LinuxWhisperSpeechToTextAdapter(
        model_path=_repo_path(args.wake_whisper_model),
        whisper_command=str(_repo_path_or_command(args.wake_whisper_command)),
        language=args.language,
        timeout_seconds=min(float(args.timeout), 30.0),
    )
    listener_factory = wake_listener_factory or LinuxStandbyWakeListener
    wake_listener = listener_factory(
        microphone_adapter=wake_microphone,
        speech_to_text_adapter=wake_stt,
        config=wake_config,
        project_root=REPO_ROOT,
        voice_io_gate=gate,
    )
    runtime = BrainRuntime(
        core_service=core_service,
        command_handler=command_handler,
        input_adapter=active_input,
        output_adapter=voice_output,
        config=BrainRuntimeConfig(
            inactivity_timeout_seconds=args.inactivity_seconds,
            maximum_consecutive_failures=3,
            input_polling_interval_seconds=0.25,
            command_timeout_seconds=min(float(args.timeout), 600.0),
        ),
        standby_wake_listener=wake_listener,
    )
    return runtime, pipeline, base_request


def run_standby_voice(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    runtime_factory: Optional[Callable[..., tuple[BrainRuntime, Any, Any]]] = None,
) -> int:
    args = build_parser().parse_args(argv)
    dependency_error = "" if runtime_factory is not None else _validate_static_dependencies(args)
    if dependency_error:
        output_func(f"ARES standby voice configuration error: {dependency_error}")
        return 2
    try:
        factory = runtime_factory or create_runtime
        runtime, pipeline, request = factory(args, output_func=output_func)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        output_func(f"ARES standby voice setup failed: {error}")
        return 2

    preflight = single_voice_launcher._preflight_pipeline(pipeline, request)
    if not preflight.success:
        output_func("ARES command voice dependency check failed before microphone capture.")
        for issue in preflight.issues:
            output_func(f"- {issue.component}: {issue.status} ({issue.reason}).")
        return 3
    wake_started = runtime.standby_wake_listener.start(runtime_id=runtime.runtime_id)
    wake_health = runtime.standby_wake_listener.health(runtime_id=runtime.runtime_id)
    runtime.standby_wake_listener.stop("preflight_complete")
    if not wake_started.success or not wake_health.success:
        issue = wake_health if not wake_health.success else wake_started
        output_func(
            "ARES wake listener dependency check failed before capture: "
            f"{issue.error_code or issue.status} ({issue.error_message or issue.status})."
        )
        return 3

    output_func("ARES foreground standby voice runtime starting...")
    output_func("Say 'Ares' to activate, 'goodbye Ares' for standby, or 'shutdown Ares' to stop.")
    try:
        result = runtime.run()
    except KeyboardInterrupt:
        runtime.shutdown(reason="keyboard_interrupt")
        output_func("ARES standby voice runtime cancelled and cleaned up.")
        return 130
    if result.success and result.status == "stopped":
        output_func("ARES standby voice runtime stopped cleanly.")
        return 0
    output_func(
        f"ARES standby voice runtime stopped with {result.status}: "
        f"{result.error_code or result.stop_reason}."
    )
    return 1


def _command_pipeline_args(args: argparse.Namespace) -> argparse.Namespace:
    values = [
        "--microphone-device",
        str(args.microphone_device),
        "--speaker-device",
        str(args.speaker_device),
        "--language",
        str(args.language),
        "--whisper-command",
        str(_repo_path_or_command(args.command_whisper_command)),
        "--whisper-model",
        str(_repo_path(args.command_whisper_model)),
        "--voice-profile",
        str(args.voice_profile),
        "--timeout",
        str(args.timeout),
        "--auto-stop",
        "--playback",
        "--recording-output",
        str(REPO_ROOT / "data" / "runtime" / "standby_voice" / "command.wav"),
    ]
    if args.retain_diagnostic_audio:
        values.append("--preserve-diagnostic-audio")
    if args.diagnostic_routing:
        values.append("--diagnostic-routing")
    return single_turn.build_parser().parse_args(values)


def _validate_static_dependencies(args: argparse.Namespace) -> str:
    for label, value in (
        ("wake Whisper command", args.wake_whisper_command),
        ("command Whisper command", args.command_whisper_command),
    ):
        resolved = _repo_path_or_command(value)
        if isinstance(resolved, Path) and not resolved.is_file():
            return f"{label} is missing: {resolved}"
        if isinstance(resolved, str) and not shutil.which(resolved):
            return f"{label} is not available: {resolved}"
    for label, value in (
        ("wake Whisper model", args.wake_whisper_model),
        ("command Whisper model", args.command_whisper_model),
    ):
        resolved = _repo_path(value)
        if not resolved.is_file():
            return f"{label} is missing: {resolved}"
    if not str(args.voice_profile or "").strip():
        return "voice profile is not configured"
    return ""


def _repo_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def _repo_path_or_command(value: str) -> str | Path:
    text = str(value or "").strip()
    if any(separator in text for separator in ("/", "\\")):
        return _repo_path(text)
    return text


def _diagnostic_handler(handler: Callable[[str], Any], output_func: Callable[[str], None]):
    def handle(text: str) -> Any:
        response = handler(text)
        metadata = dict(getattr(response, "metadata", {}) or {})
        diagnostics = dict(metadata.get("routing_diagnostics") or {})
        output_func(
            "Routing: intent={intent}; skill={skill}; planner={planner}; rejection={rejection}".format(
                intent=diagnostics.get("parsed_intent") or metadata.get("detected_intent") or "unknown",
                skill=getattr(response, "skill", "") or "none",
                planner=diagnostics.get("planner_decision") or "none",
                rejection=diagnostics.get("rejection_reason") or "none",
            )
        )
        return response

    return handle


def main() -> int:
    return run_standby_voice()


if __name__ == "__main__":
    raise SystemExit(main())
