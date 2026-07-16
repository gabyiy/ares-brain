from __future__ import annotations

import argparse
from dataclasses import replace
import importlib.util
from pathlib import Path
import shutil
import shlex
import sys
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    AresNamePolicy,
    ActiveCommandLocalDiagnostics,
    BrainRuntime,
    BrainRuntimeConfig,
    BrainSessionConfig,
    BrainSessionManager,
    CoreService,
    LinuxAlsaMicrophoneAdapter,
    LinuxStandbyWakeListener,
    SingleTurnPipelineRuntimeInputAdapter,
    SingleTurnPipelineRuntimeOutputAdapter,
    VoiceRuntimeGate,
    WakeLocalDiagnostics,
    WakeListenerConfig,
    VOSK_MODEL_INSTALL_COMMAND,
    VoskWakeRecognizer,
)
from events import EventHistoryStore  # noqa: E402
from memory.schema_migrations import MigrationError, StoreWriteLock  # noqa: E402
from scripts import manual_verify_single_turn_voice as single_turn  # noqa: E402
from scripts import run_ares_voice as single_voice_launcher  # noqa: E402


_WAKE_DEFAULTS = WakeListenerConfig()
DEFAULT_MICROPHONE_DEVICE = _WAKE_DEFAULTS.microphone_device
DEFAULT_SPEAKER_DEVICE = single_turn.DEFAULT_SPEAKER_DEVICE
DEFAULT_WAKE_VOSK_MODEL = _WAKE_DEFAULTS.vosk_model_path
DEFAULT_WAKE_MINIMUM_CONFIDENCE = _WAKE_DEFAULTS.minimum_recognition_confidence
DEFAULT_WAKE_MEDIUM_CONFIDENCE = _WAKE_DEFAULTS.medium_recognition_confidence
DEFAULT_COMMAND_WHISPER_COMMAND = single_turn.DEFAULT_WHISPER_COMMAND
DEFAULT_COMMAND_WHISPER_MODEL = single_turn.DEFAULT_WHISPER_MODEL
DEFAULT_VOICE_PROFILE = single_voice_launcher._configured_default_voice_profile()
DEFAULT_INACTIVITY_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_RUNTIME_LOCK_PATH = REPO_ROOT / "data" / "runtime" / "ares_standby_voice.runtime"
DEFAULT_RUNTIME_LOCK_STALE_SECONDS = 30.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the foreground ARES standby wake and active voice runtime."
    )
    parser.add_argument("--microphone-device", default=DEFAULT_MICROPHONE_DEVICE)
    parser.add_argument("--speaker-device", default=DEFAULT_SPEAKER_DEVICE)
    parser.add_argument("--language", default="en")
    parser.add_argument("--vosk-model", default=DEFAULT_WAKE_VOSK_MODEL)
    parser.add_argument(
        "--wake-min-confidence",
        type=float,
        default=DEFAULT_WAKE_MINIMUM_CONFIDENCE,
    )
    parser.add_argument(
        "--wake-medium-confidence",
        type=float,
        default=DEFAULT_WAKE_MEDIUM_CONFIDENCE,
    )
    parser.add_argument(
        "--wake-medium-confirmations",
        type=int,
        default=_WAKE_DEFAULTS.medium_confidence_confirmation_count,
    )
    parser.add_argument(
        "--wake-recalibration-seconds",
        type=float,
        default=_WAKE_DEFAULTS.recalibration_interval_seconds,
    )
    parser.add_argument("--command-whisper-command", default=DEFAULT_COMMAND_WHISPER_COMMAND)
    parser.add_argument("--command-whisper-model", default=DEFAULT_COMMAND_WHISPER_MODEL)
    parser.add_argument("--voice-profile", default=DEFAULT_VOICE_PROFILE)
    parser.add_argument("--inactivity-seconds", type=float, default=DEFAULT_INACTIVITY_SECONDS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--diagnostic-routing", action="store_true")
    parser.add_argument("--diagnostic-wake", action="store_true")
    parser.add_argument("--retain-diagnostic-audio", action="store_true")
    parser.add_argument(
        "--runtime-lock-path",
        default=str(DEFAULT_RUNTIME_LOCK_PATH),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--runtime-lock-stale-seconds",
        type=float,
        default=DEFAULT_RUNTIME_LOCK_STALE_SECONDS,
        help=argparse.SUPPRESS,
    )
    return parser


def create_runtime(
    args: argparse.Namespace,
    *,
    output_func: Callable[[str], None] = print,
    pipeline_factory: Optional[Callable[..., Any]] = None,
    wake_listener_factory: Optional[Callable[..., Any]] = None,
    event_history_store: Optional[EventHistoryStore] = None,
) -> tuple[BrainRuntime, Any, Any]:
    history = event_history_store or EventHistoryStore(warning_callback=output_func)
    name_policy = AresNamePolicy()
    session_manager = BrainSessionManager(
        config=BrainSessionConfig(
            inactivity_timeout_seconds=args.inactivity_seconds,
            maximum_consecutive_failures=3,
        ),
        event_history_store=history,
    )
    core_service = CoreService(
        brain_session_manager=session_manager,
        event_history_store=history,
        register_default_pc=False,
    )
    skill_manager = single_turn.create_skill_manager(
        core_service,
        event_history_store=history,
    )
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
        event_history_store=history,
    )
    gate = VoiceRuntimeGate(settle_delay_seconds=0.35)
    active_input = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=base_request,
        session_id_provider=lambda: session_manager.session_id,
        voice_io_gate=gate,
        diagnostic_callback=(
            _active_command_diagnostic_callback(output_func)
            if args.diagnostic_wake
            else None
        ),
        status_callback=output_func,
    )
    voice_output = SingleTurnPipelineRuntimeOutputAdapter(
        pipeline=pipeline,
        base_request=base_request,
        voice_io_gate=gate,
        output_func=lambda text: output_func(f"ARES: {text}"),
    )

    wake_config = WakeListenerConfig(
        microphone_device=args.microphone_device,
        vosk_model_path=str(_repo_path(args.vosk_model)),
        minimum_recognition_confidence=args.wake_min_confidence,
        medium_recognition_confidence=args.wake_medium_confidence,
        medium_confidence_confirmation_count=args.wake_medium_confirmations,
        recalibration_interval_seconds=args.wake_recalibration_seconds,
        language=args.language,
        wake_phrase_aliases=name_policy.aliases,
        playback_settle_delay_seconds=gate.settle_delay_seconds,
        diagnostic_wake=bool(args.diagnostic_wake),
        retain_diagnostic_audio=bool(args.retain_diagnostic_audio),
        diagnostic_output_directory="data/runtime/wake_audio",
    )
    wake_microphone = LinuxAlsaMicrophoneAdapter(
        device=args.microphone_device,
        record_seconds=wake_config.maximum_utterance_seconds,
        timeout_seconds=min(float(args.timeout), 30.0),
    )
    wake_recognizer = VoskWakeRecognizer(
        model_path=_repo_path(args.vosk_model),
        minimum_confidence=args.wake_min_confidence,
    )
    listener_factory = wake_listener_factory or LinuxStandbyWakeListener
    wake_listener = listener_factory(
        microphone_adapter=wake_microphone,
        wake_recognizer=wake_recognizer,
        config=wake_config,
        project_root=REPO_ROOT,
        voice_io_gate=gate,
        diagnostic_callback=(
            _wake_diagnostic_callback(output_func, args.speaker_device)
            if args.diagnostic_wake
            else None
        ),
    )
    runtime = BrainRuntime(
        core_service=core_service,
        command_handler=command_handler,
        input_adapter=active_input,
        output_adapter=voice_output,
        config=BrainRuntimeConfig(
            ares_name_aliases=name_policy.aliases,
            inactivity_timeout_seconds=args.inactivity_seconds,
            maximum_consecutive_failures=3,
            input_polling_interval_seconds=0.25,
            command_timeout_seconds=min(float(args.timeout), 600.0),
        ),
        standby_wake_listener=wake_listener,
        event_history_store=history,
    )
    return runtime, pipeline, base_request


def run_standby_voice(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    runtime_factory: Optional[Callable[..., tuple[BrainRuntime, Any, Any]]] = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime_lock_path = _repo_path(args.runtime_lock_path)
        with StoreWriteLock(
            runtime_lock_path,
            recover_if_owner_dead=True,
            stale_after_seconds=args.runtime_lock_stale_seconds,
            owner_kind="ares_standby_voice_runtime",
        ):
            return _run_standby_voice_locked(
                args,
                output_func=output_func,
                runtime_factory=runtime_factory,
            )
    except MigrationError as error:
        error_path = Path(error.path).resolve() if error.path else None
        if error.status == "store_locked" and error_path == runtime_lock_path.resolve():
            output_func("ARES is already running")
            return 4
        output_func(f"ARES runtime lock failed: {error}")
        return 2
    except OSError as error:
        output_func(f"ARES runtime lock failed: {error}")
        return 2
    except ValueError as error:
        output_func(f"ARES standby voice configuration error: {error}")
        return 2


def _run_standby_voice_locked(
    args: argparse.Namespace,
    *,
    output_func: Callable[[str], None],
    runtime_factory: Optional[Callable[..., tuple[BrainRuntime, Any, Any]]],
) -> int:
    if args.retain_diagnostic_audio and not args.diagnostic_wake:
        output_func(
            "ARES standby voice configuration error: --retain-diagnostic-audio "
            "requires --diagnostic-wake."
        )
        return 2
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
    if not wake_started.success or not wake_health.success:
        issue = wake_health if not wake_health.success else wake_started
        runtime.standby_wake_listener.stop("preflight_failed")
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
    confidence = args.wake_min_confidence
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.4 <= float(confidence) <= 1.0
    ):
        return "wake minimum confidence must be between 0.4 and 1.0"
    if importlib.util.find_spec("vosk") is None:
        return (
            "Vosk is not installed. Run: python -m pip install -r requirements.txt"
        )
    vosk_model = _repo_path(args.vosk_model)
    if not vosk_model.is_dir():
        return (
            f"Vosk wake model is missing: {vosk_model}. Recommended Raspberry Pi model: "
            "vosk-model-small-en-us-0.15. Install it with: "
            f"{VOSK_MODEL_INSTALL_COMMAND}"
        )
    for label, value in (
        ("command Whisper command", args.command_whisper_command),
    ):
        resolved = _repo_path_or_command(value)
        if isinstance(resolved, Path) and not resolved.is_file():
            return f"{label} is missing: {resolved}"
        if isinstance(resolved, str) and not shutil.which(resolved):
            return f"{label} is not available: {resolved}"
    for label, value in (
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


def render_active_command_diagnostics(
    diagnostics: ActiveCommandLocalDiagnostics,
) -> list[str]:
    return [
        "Active-command diagnostic:",
        f"  Raw Whisper transcript: {diagnostics.raw_transcript or '<empty>'}",
        f"  Cleaned transcript: {diagnostics.cleaned_transcript or '<empty>'}",
        "  Alias-canonicalized transcript: "
        f"{diagnostics.alias_canonicalized_transcript or '<empty>'}",
        f"  Lifecycle classification: {diagnostics.lifecycle_classification}",
        f"  Selected lifecycle action: {diagnostics.selected_lifecycle_action}",
        "  CoreService routing bypassed: "
        f"{'yes' if diagnostics.core_service_bypassed else 'no'}",
        "  Lifecycle state: "
        f"{diagnostics.lifecycle_state_before or 'unknown'} -> "
        f"{diagnostics.lifecycle_state_after or 'unknown'}",
        "  Active session: "
        f"{diagnostics.session_id_before or 'none'} -> "
        f"{diagnostics.session_id_after or 'none'}",
        f"  Capture stop reason: {diagnostics.capture_stop_reason or 'unknown'}",
        f"  Raw capture duration: {diagnostics.raw_capture_duration_seconds:.3f}s",
        "  Finalized candidate duration: "
        f"{diagnostics.finalized_candidate_duration_seconds:.3f}s",
        "  Whisper processing duration: "
        f"{diagnostics.whisper_processing_duration_seconds:.3f}s",
        f"  Terminal-silence status: {diagnostics.terminal_silence_status}",
    ]


def _active_command_diagnostic_callback(
    output_func: Callable[[str], None],
) -> Callable[[ActiveCommandLocalDiagnostics], None]:
    def emit(diagnostics: ActiveCommandLocalDiagnostics) -> None:
        for line in render_active_command_diagnostics(diagnostics):
            output_func(line)

    return emit


def render_wake_diagnostics(
    diagnostics: WakeLocalDiagnostics,
    *,
    speaker_device: str,
) -> list[str]:
    confidence = (
        f"{diagnostics.recognition_confidence:.3f}"
        if diagnostics.recognition_confidence_available
        and diagnostics.recognition_confidence is not None
        else "unavailable"
    )
    lines = [
        "Wake diagnostic:",
        f"  Recognizer used: {diagnostics.recognizer_name or 'unknown'}",
        "  Raw recognition result: "
        f"{diagnostics.raw_recognition_result or '<empty>'}",
        f"  Raw recognized phrase: {diagnostics.raw_transcript or '<empty>'}",
        f"  Normalized phrase: {diagnostics.normalized_transcript or '<empty>'}",
        f"  Confidence: {confidence}",
        f"  Confidence tier: {diagnostics.confidence_tier or 'none'}",
        "  Medium-confidence confirmation: "
        f"{diagnostics.confirmation_count}/{diagnostics.confirmation_required_count}",
        f"  Selected alias: {diagnostics.selected_alias or 'none'}",
        f"  Selected wake phrase: {diagnostics.selected_wake_phrase or 'none'}",
        f"  Wake classification: {diagnostics.classification}",
        f"  Classification path: {diagnostics.classification_path or 'none'}",
        f"  Classification reason: {diagnostics.classification_reason or 'none'}",
        f"  Rejection reason: {diagnostics.rejection_reason or 'none'}",
        f"  Candidate duration: {diagnostics.capture_duration_seconds:.3f}s",
        f"  Candidate number: {diagnostics.candidate_number}",
        f"  Stream open count: {diagnostics.stream_open_count}",
        f"  Stream close count: {diagnostics.stream_close_count}",
        f"  Calibration count: {diagnostics.calibration_count}",
        f"  Stream instance ID: {diagnostics.stream_instance_id or 'unknown'}",
        f"  ALSA handle ID: {diagnostics.alsa_handle_id or 'unknown'}",
        f"  Stream open reason: {diagnostics.stream_open_reason or 'none'}",
        f"  Stream close reason: {diagnostics.stream_close_reason or 'none'}",
        f"  Calibration reason: {diagnostics.calibration_reason or 'none'}",
        "  Ownership handoff: "
        f"{diagnostics.ownership_handoff_source or 'none'} -> "
        f"{diagnostics.ownership_handoff_destination or 'none'}",
        "  Pre-roll frames retained: "
        f"{diagnostics.pre_roll_frames_retained}/{diagnostics.expected_pre_roll_frames}",
        f"  Beginning clipped: {'yes' if diagnostics.beginning_clipped else 'no'}",
        f"  First speech frame: {diagnostics.first_speech_frame}",
        "  Terminal silence duration: "
        f"{diagnostics.terminal_silence_duration_seconds:.3f}s",
        "  Speech-start to activation: "
        f"{diagnostics.speech_to_activation_seconds:.3f}s",
        f"  VAD transition count: {len(diagnostics.vad_transitions)}",
        f"  Raw capture duration: {diagnostics.raw_capture_duration_seconds:.3f}s",
        f"  Assembled duration: {diagnostics.assembled_duration_seconds:.3f}s",
        f"  Normalized duration: {diagnostics.normalized_duration_seconds:.3f}s",
        f"  Recognizer input duration: {diagnostics.whisper_input_duration_seconds:.3f}s",
        f"  Capture stop reason: {diagnostics.capture_stop_reason or 'unknown'}",
        f"  Recognition status: {diagnostics.recognition_status or 'unknown'}",
        "  Recognition processing duration: "
        f"{diagnostics.recognition_processing_time_seconds:.3f}s",
        f"  Vosk model path: {diagnostics.recognizer_model_path or diagnostics.wake_model_path}",
        f"  Lifecycle state: {diagnostics.lifecycle_state}",
    ]
    if diagnostics.retained_audio_path:
        lines.extend(
            [
                f"  Retained wake candidate: {diagnostics.retained_audio_path}",
                "  Manual playback (not run automatically): "
                f"aplay -D {shlex.quote(str(speaker_device))} "
                f"{shlex.quote(diagnostics.retained_audio_path)}",
            ]
        )
    return lines


def _wake_diagnostic_callback(
    output_func: Callable[[str], None],
    speaker_device: str,
) -> Callable[[WakeLocalDiagnostics], None]:
    def emit(diagnostics: WakeLocalDiagnostics) -> None:
        for line in render_wake_diagnostics(
            diagnostics,
            speaker_device=speaker_device,
        ):
            output_func(line)

    return emit


def main() -> int:
    return run_standby_voice()


if __name__ == "__main__":
    raise SystemExit(main())
