from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import importlib.util
import math
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
    PIPELINE_CLEANUP_DELETE_ALWAYS,
    SingleTurnPipelineRuntimeInputAdapter,
    SingleTurnPipelineRuntimeOutputAdapter,
    SingleTurnVoiceRequestV1,
    VoiceRuntimeGate,
    WakeLocalDiagnostics,
    WakeListenerConfig,
    VOSK_MODEL_INSTALL_COMMAND,
    VoskWakeRecognizer,
    active_command_capture_request,
    normalize_active_lifecycle_command,
)
from core.ActiveLifecycleAudioRecognizer import (  # noqa: E402
    ActiveLifecycleAudioRecognizer,
)
from core.ForegroundSignalCoordinator import (  # noqa: E402
    ForegroundSignalCoordinator,
    ForegroundTerminationRequested,
)
from core.BrainRuntimeVoiceAdapters import (  # noqa: E402
    ActiveLifecycleAudioTurnController,
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
DEFAULT_ACTIVE_TRANSCRIPTION_TIMEOUT_SECONDS = 15.0
DEFAULT_WHISPER_TERMINATION_GRACE_SECONDS = 1.0
DEFAULT_WHISPER_HARD_CLEANUP_DEADLINE_SECONDS = 3.0
DEFAULT_RUNTIME_LOCK_PATH = REPO_ROOT / "data" / "runtime" / "ares_standby_voice.runtime"
DEFAULT_RUNTIME_LOCK_STALE_SECONDS = 30.0
ACTIVE_VOICE_COMPOSITION_REVISION = "constrained_active_lifecycle_alias_slot_v2"


RuntimeTerminationRequested = ForegroundTerminationRequested


@dataclass(frozen=True)
class ProductionActiveAudioPipeline:
    """The authoritative production ACTIVE audio composition.

    Both the foreground runtime and bounded hardware diagnostics use this
    boundary so request construction cannot drift away from the concrete ALSA,
    VAD, and Whisper pipeline selected for production.
    """

    pipeline_args: argparse.Namespace
    base_request: SingleTurnVoiceRequestV1
    pipeline: Any
    voice_io_gate: VoiceRuntimeGate
    active_lifecycle_audio_recognizer: ActiveLifecycleAudioRecognizer


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
    parser.add_argument(
        "--active-transcription-timeout",
        type=float,
        default=DEFAULT_ACTIVE_TRANSCRIPTION_TIMEOUT_SECONDS,
        help="hard timeout for each active-command Whisper subprocess",
    )
    parser.add_argument(
        "--whisper-termination-grace",
        type=float,
        default=DEFAULT_WHISPER_TERMINATION_GRACE_SECONDS,
        help="bounded SIGTERM grace before active Whisper receives SIGKILL",
    )
    parser.add_argument(
        "--whisper-hard-cleanup-deadline",
        type=float,
        default=DEFAULT_WHISPER_HARD_CLEANUP_DEADLINE_SECONDS,
        help="absolute bound for active Whisper process-group cleanup",
    )
    parser.add_argument("--diagnostic-routing", action="store_true")
    parser.add_argument("--diagnostic-wake", action="store_true")
    parser.add_argument(
        "--wake-vad-sensitive",
        action="store_true",
        help="use the validated sensitive wake-only VAD profile",
    )
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


def build_production_active_audio_pipeline(
    args: argparse.Namespace,
    *,
    output_func: Callable[[str], None] = print,
    skill_manager: Any = None,
    event_history_store: Optional[EventHistoryStore] = None,
    pipeline_factory: Optional[Callable[..., Any]] = None,
    active_lifecycle_recognizer_factory: Optional[Callable[..., Any]] = None,
) -> ProductionActiveAudioPipeline:
    """Build the exact ACTIVE microphone/VAD/Whisper production boundary.

    Routing dependencies remain injectable, but the CLI mapping, active capture
    profile, pipeline construction, and voice ownership gate have one shared
    source of truth.  A diagnostic may replace only action/output fields on the
    returned request; it must not reconstruct the audio adapters independently.
    """

    pipeline_args = _command_pipeline_args(args)
    base_request = single_turn.request_from_args(pipeline_args)
    base_request = active_command_capture_request(
        replace(
            base_request,
            playback_enabled=True,
            cleanup_policy=(
                "keep"
                if args.retain_diagnostic_audio
                else PIPELINE_CLEANUP_DELETE_ALWAYS
            ),
            diagnostic_audio=bool(args.retain_diagnostic_audio),
            metadata={
                **dict(base_request.metadata or {}),
                "source": "run_ares_standby_voice",
                "foreground_runtime": True,
                "background_listening": False,
            },
        )
    )
    factory = pipeline_factory or single_turn.create_pipeline
    pipeline = factory(
        pipeline_args,
        output_func=lambda _text: None,
        skill_manager=skill_manager,
        event_history_store=event_history_store,
        whisper_status_callback=output_func,
        whisper_diagnostic_progress=bool(args.diagnostic_routing),
        whisper_termination_grace_seconds=args.whisper_termination_grace,
        whisper_hard_cleanup_deadline_seconds=(
            args.whisper_hard_cleanup_deadline
        ),
    )
    lifecycle_recognizer_factory = (
        active_lifecycle_recognizer_factory or ActiveLifecycleAudioRecognizer
    )
    active_lifecycle_audio_recognizer = lifecycle_recognizer_factory(
        model_path=_repo_path(args.vosk_model)
    )
    return ProductionActiveAudioPipeline(
        pipeline_args=pipeline_args,
        base_request=base_request,
        pipeline=pipeline,
        voice_io_gate=VoiceRuntimeGate(settle_delay_seconds=0.35),
        active_lifecycle_audio_recognizer=active_lifecycle_audio_recognizer,
    )


def create_runtime(
    args: argparse.Namespace,
    *,
    output_func: Callable[[str], None] = print,
    pipeline_factory: Optional[Callable[..., Any]] = None,
    wake_listener_factory: Optional[Callable[..., Any]] = None,
    event_history_store: Optional[EventHistoryStore] = None,
    active_lifecycle_recognizer_factory: Optional[Callable[..., Any]] = None,
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

    active_audio = build_production_active_audio_pipeline(
        args,
        output_func=output_func,
        skill_manager=skill_manager,
        event_history_store=history,
        pipeline_factory=pipeline_factory,
        active_lifecycle_recognizer_factory=(
            active_lifecycle_recognizer_factory
        ),
    )
    base_request = active_audio.base_request
    pipeline = active_audio.pipeline
    gate = active_audio.voice_io_gate
    lifecycle_audio_controller = ActiveLifecycleAudioTurnController(
        recognizer=active_audio.active_lifecycle_audio_recognizer,
        session_id_provider=lambda: session_manager.session_id,
        lifecycle_state_provider=lambda: session_manager.state,
    )
    active_input = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=base_request,
        session_id_provider=lambda: session_manager.session_id,
        lifecycle_state_provider=lambda: session_manager.state,
        voice_io_gate=gate,
        active_lifecycle_audio_controller=lifecycle_audio_controller,
        diagnostic_callback=(
            _active_command_diagnostic_callback(output_func)
            if args.diagnostic_wake or args.diagnostic_routing
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
        wake_vad_sensitivity=(
            "sensitive" if args.wake_vad_sensitive else "normal"
        ),
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
    signal_coordinator = ForegroundSignalCoordinator()
    runtime_holder: dict[str, Any] = {"signal_coordinator": signal_coordinator}
    try:
        runtime_lock_path = _repo_path(args.runtime_lock_path)
        with StoreWriteLock(
            runtime_lock_path,
            recover_if_owner_dead=True,
            stale_after_seconds=args.runtime_lock_stale_seconds,
            owner_kind="ares_standby_voice_runtime",
        ):
            with signal_coordinator.signal_scope():
                return _run_standby_voice_locked(
                    args,
                    output_func=output_func,
                    runtime_factory=runtime_factory,
                    runtime_holder=runtime_holder,
                )
    except MigrationError as error:
        error_path = Path(error.path).resolve() if error.path else None
        if error.status == "store_locked" and error_path == runtime_lock_path.resolve():
            lock = dict(error.details.get("lock") or {})
            owner_pid = int(lock.get("owner_pid", 0) or 0)
            suffix = f" (PID {owner_pid})" if owner_pid > 0 else ""
            output_func(f"ARES is already running{suffix}")
            return 4
        output_func(f"ARES runtime lock failed: {error}")
        return 2
    except OSError as error:
        output_func(f"ARES runtime lock failed: {error}")
        return 2
    except RuntimeTerminationRequested as error:
        runtime = runtime_holder.get("runtime")
        if runtime is not None:
            runtime.shutdown(reason=f"signal_{error.signum}_during_preflight")
        output_func("ARES runtime terminal reason: owner_cancellation.")
        output_func(
            f"ARES standby voice runtime terminated by signal {error.signum}; "
            "ownership lock released."
        )
        return 128 + error.signum
    except KeyboardInterrupt:
        runtime = runtime_holder.get("runtime")
        if runtime is not None:
            runtime.shutdown(reason="owner_cancellation")
        output_func("ARES runtime terminal reason: owner_cancellation.")
        output_func("ARES standby voice runtime cancelled and cleaned up.")
        return 130
    except ValueError as error:
        output_func(f"ARES standby voice configuration error: {error}")
        return 2


def _run_standby_voice_locked(
    args: argparse.Namespace,
    *,
    output_func: Callable[[str], None],
    runtime_factory: Optional[Callable[..., tuple[BrainRuntime, Any, Any]]],
    runtime_holder: Optional[dict[str, Any]] = None,
) -> int:
    if (
        isinstance(args.active_transcription_timeout, bool)
        or not math.isfinite(float(args.active_transcription_timeout))
        or not 1.0 <= float(args.active_transcription_timeout) <= 300.0
    ):
        output_func(
            "ARES standby voice configuration error: active transcription timeout "
            "must be between 1 and 300 seconds."
        )
        return 2
    if (
        isinstance(args.whisper_termination_grace, bool)
        or not math.isfinite(float(args.whisper_termination_grace))
        or not 0.1 <= float(args.whisper_termination_grace) <= 10.0
    ):
        output_func(
            "ARES standby voice configuration error: Whisper termination grace "
            "must be between 0.1 and 10 seconds."
        )
        return 2
    if (
        isinstance(args.whisper_hard_cleanup_deadline, bool)
        or not math.isfinite(float(args.whisper_hard_cleanup_deadline))
        or not float(args.whisper_termination_grace)
        <= float(args.whisper_hard_cleanup_deadline)
        <= 30.0
    ):
        output_func(
            "ARES standby voice configuration error: Whisper hard cleanup deadline "
            "must be between the termination grace and 30 seconds."
        )
        return 2
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
        if runtime_holder is not None:
            runtime_holder["runtime"] = runtime
            coordinator = runtime_holder.get("signal_coordinator")
            if isinstance(coordinator, ForegroundSignalCoordinator):
                coordinator.register(
                    lambda reason: _cancel_foreground_voice_resources(
                        runtime,
                        pipeline,
                        reason=reason,
                    )
                )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        output_func(f"ARES standby voice setup failed: {error}")
        return 2

    if args.diagnostic_routing or args.diagnostic_wake:
        for line in render_production_composition_diagnostics(runtime, pipeline):
            output_func(line)

    preflight = single_voice_launcher._preflight_pipeline(pipeline, request)
    if not preflight.success:
        output_func("ARES command voice dependency check failed before microphone capture.")
        for issue in preflight.issues:
            output_func(f"- {issue.component}: {issue.status} ({issue.reason}).")
        return 3
    calibration_seconds = float(
        getattr(
            getattr(runtime.standby_wake_listener, "config", None),
            "calibration_duration_seconds",
            _WAKE_DEFAULTS.calibration_duration_seconds,
        )
    )
    output_func(
        "Calibrating standby microphone; remain silent for approximately "
        f"{calibration_seconds:.0f} seconds."
    )
    wake_started = runtime.standby_wake_listener.start(runtime_id=runtime.runtime_id)
    for line in render_standby_calibration_diagnostics(
        dict(getattr(wake_started, "data", {}) or {}),
        include_energy_summary=bool(args.diagnostic_wake),
    ):
        output_func(line)
    if not wake_started.success:
        runtime.standby_wake_listener.stop("preflight_failed")
        output_func(
            "ARES wake listener dependency check failed before capture: "
            f"{wake_started.error_code or wake_started.status} "
            f"({wake_started.error_message or wake_started.status})."
        )
        return 3
    wake_health = runtime.standby_wake_listener.health(runtime_id=runtime.runtime_id)
    if not wake_health.success:
        runtime.standby_wake_listener.stop("preflight_failed")
        output_func(
            "ARES wake listener dependency check failed before capture: "
            f"{wake_health.error_code or wake_health.status} "
            f"({wake_health.error_message or wake_health.status})."
        )
        return 3

    output_func("ARES foreground standby voice runtime starting...")
    output_func("Say 'Ares' to activate, 'goodbye Ares' for standby, or 'shutdown Ares' to stop.")
    try:
        result = runtime.run()
    except KeyboardInterrupt:
        runtime.shutdown(reason="owner_cancellation")
        output_func("ARES runtime terminal reason: owner_cancellation.")
        output_func("ARES standby voice runtime cancelled and cleaned up.")
        return 130
    except RuntimeTerminationRequested as error:
        runtime.shutdown(reason=f"signal_{error.signum}")
        output_func("ARES runtime terminal reason: owner_cancellation.")
        output_func(
            f"ARES standby voice runtime received signal {error.signum} and cleaned up."
        )
        return 128 + error.signum
    except (OSError, RuntimeError, TimeoutError) as error:
        runtime.shutdown(reason="foreground_runtime_error")
        output_func("ARES runtime terminal reason: unrecoverable_failure.")
        output_func(
            "ARES standby voice runtime failed and cleaned up: "
            f"{error.__class__.__name__}:{str(error)[:160]}"
        )
        return 1
    terminal_reason = _reported_terminal_reason(result)
    output_func(f"ARES runtime terminal reason: {terminal_reason}.")
    if (
        result.success
        and result.status == "stopped"
        and terminal_reason == "explicit_shutdown_command"
    ):
        output_func("ARES standby voice runtime stopped cleanly.")
        return 0
    output_func(
        f"ARES standby voice runtime stopped with {result.status}: "
        f"{result.error_code or result.stop_reason}."
    )
    return 1


def _reported_terminal_reason(result: Any) -> str:
    reason = str(getattr(result, "stop_reason", "") or "").strip()
    if reason in {"explicit_shutdown_command", "owner_shutdown_phrase"}:
        return "explicit_shutdown_command"
    if reason in {"input_cancelled", "owner_cancellation"}:
        return "owner_cancellation"
    return reason or "unrecoverable_failure"


def render_production_composition_diagnostics(
    runtime: Any,
    pipeline: Any,
) -> list[str]:
    """Identify the privacy-safe implementation graph used by the launcher.

    These lines make a stale checkout or alternate launcher visible on hardware
    without exposing owner audio or transcripts.  Behavior is still proved by
    the production-composition test; the revision is only an operator marker.
    """

    input_adapter = getattr(runtime, "input_adapter", None)
    output_adapter = getattr(runtime, "output_adapter", None)
    lifecycle_audio_controller = getattr(
        input_adapter,
        "active_lifecycle_audio_controller",
        None,
    )
    input_gate = getattr(input_adapter, "voice_io_gate", None)
    output_gate = getattr(output_adapter, "voice_io_gate", None)
    return [
        "PRODUCTION VOICE COMPOSITION",
        f"  Routing revision: {ACTIVE_VOICE_COMPOSITION_REVISION}",
        f"  Runtime: {_qualified_implementation(runtime)}",
        "  Lifecycle authority: "
        f"{_qualified_implementation(getattr(runtime, 'session_manager', None))}",
        f"  Standby listener: "
        f"{_qualified_implementation(getattr(runtime, 'standby_wake_listener', None))}",
        f"  Active input adapter: {_qualified_implementation(input_adapter)}",
        f"  Active voice pipeline: {_qualified_implementation(pipeline)}",
        "  Active microphone adapter: "
        f"{_qualified_implementation(getattr(pipeline, 'microphone_adapter', None))}",
        "  Active Whisper adapter: "
        f"{_qualified_implementation(getattr(pipeline, 'speech_to_text_adapter', None))}",
        "  Active lifecycle normalizer: "
        f"{_qualified_implementation(normalize_active_lifecycle_command)}",
        "  Constrained lifecycle audio controller: "
        f"{_qualified_implementation(lifecycle_audio_controller)}",
        "  Constrained lifecycle audio recognizer: "
        f"{_qualified_implementation(getattr(lifecycle_audio_controller, 'recognizer', None))}",
        "  Input/output gate shared: "
        f"{'yes' if input_gate is not None and input_gate is output_gate else 'no'}",
    ]


def _qualified_implementation(value: Any) -> str:
    if value is None:
        return "unavailable"
    target = value if isinstance(value, type) else getattr(value, "__class__", type(value))
    if callable(value) and hasattr(value, "__module__") and hasattr(value, "__qualname__"):
        target = value
    module = str(getattr(target, "__module__", "") or "")
    name = str(
        getattr(target, "__qualname__", "")
        or getattr(target, "__name__", "")
        or type(value).__name__
    )
    return f"{module}.{name}" if module else name


def _cancel_foreground_voice_resources(runtime: Any, pipeline: Any, *, reason: str) -> None:
    """Request bounded cancellation without changing lifecycle in a signal handler."""

    adapters = (
        getattr(pipeline, "speech_to_text_adapter", None),
        getattr(pipeline, "microphone_adapter", None),
        getattr(pipeline, "text_to_speech_adapter", None),
        getattr(pipeline, "speaker_adapter", None),
        getattr(runtime, "standby_wake_listener", None),
    )
    for adapter in adapters:
        if adapter is None:
            continue
        cancel = getattr(adapter, "cancel_current", None)
        if not callable(cancel):
            cancel = getattr(adapter, "cancel", None)
        if callable(cancel):
            try:
                cancel(reason)
            except TypeError:
                cancel()


def _termination_signal_scope():
    """Compatibility wrapper retained for tests and downstream scripts."""

    return ForegroundSignalCoordinator().signal_scope()


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
        "--transcription-timeout",
        str(args.active_transcription_timeout),
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
        "ACTIVE COMMAND DIAGNOSTIC",
        f"  Raw Whisper transcript: {diagnostics.raw_transcript or '<empty>'}",
        f"  Cleaned transcript: {diagnostics.cleaned_transcript or '<empty>'}",
        "  Audio capture start reason: "
        f"{diagnostics.audio_capture_start_reason or '<unknown>'}",
        "  First speech frame: "
        f"{diagnostics.first_speech_frame or 'absent'}",
        "  Last speech frame: "
        f"{diagnostics.last_speech_frame or 'absent'}",
        "  Pre-roll frames retained: "
        f"{diagnostics.pre_roll_frames_retained}/"
        f"{diagnostics.expected_pre_roll_frames}",
        f"  Beginning clipped: {diagnostics.beginning_clipped}",
        "  Candidate duration: "
        f"{diagnostics.finalized_candidate_duration_seconds:.3f}s",
        f"  Raw capture duration: {diagnostics.raw_capture_duration_seconds:.3f}s",
        "  Leading audio trimmed: "
        f"{diagnostics.leading_audio_trimmed_seconds:.3f}s",
        "  Trailing audio trimmed: "
        f"{diagnostics.trailing_audio_trimmed_seconds:.3f}s",
        "  Assistant alias detected: "
        f"{diagnostics.matched_assistant_alias or '<none>'}",
        f"  Assistant alias position: {diagnostics.alias_position}",
        "  Transcript after alias removal: "
        f"{diagnostics.lifecycle_normalized_transcript or '<empty>'}",
        "  Lifecycle-normalized transcript: "
        f"{diagnostics.lifecycle_normalized_transcript or '<empty>'}",
        "  Canonicalized transcript: "
        f"{diagnostics.alias_canonicalized_transcript or '<empty>'}",
        "  Matched assistant alias: "
        f"{diagnostics.matched_assistant_alias or '<none>'}",
        f"  Alias type: {diagnostics.assistant_alias_type or '<none>'}",
        "  Assistant alias removed: "
        f"{diagnostics.assistant_alias_removed or '<none>'}",
        f"  Alias position: {diagnostics.alias_position}",
        "  Canonical assistant name: "
        f"{diagnostics.canonical_name or '<none>'}",
        "  Negation detected: "
        f"{'yes' if diagnostics.negation_detected else 'no'}",
        f"  Lifecycle classification: {diagnostics.lifecycle_classification}",
        f"  Selected lifecycle action: {diagnostics.selected_lifecycle_action}",
        "  Matched complete phrase: "
        f"{diagnostics.matched_lifecycle_phrase or '<none>'}",
        "  Lifecycle rejection reason: "
        f"{diagnostics.lifecycle_rejection_reason or '<none>'}",
        "  CoreService routing bypassed: "
        f"{'yes' if diagnostics.core_service_bypassed else 'no'}",
        "  Lifecycle state: "
        f"{diagnostics.lifecycle_state_before or 'unknown'} -> "
        f"{diagnostics.lifecycle_state_after or 'unknown'}",
        "  Active session: "
        f"{diagnostics.session_id_before or 'none'} -> "
        f"{diagnostics.session_id_after or 'none'}",
        f"  State before: {diagnostics.lifecycle_state_before or 'unknown'}",
        f"  State after: {diagnostics.lifecycle_state_after or 'unknown'}",
        f"  Session before: {diagnostics.session_id_before or 'none'}",
        f"  Session after: {diagnostics.session_id_after or 'none'}",
        "  Runtime state before routing: "
        f"{diagnostics.lifecycle_state_before or 'unknown'}",
        "  Session ID before routing: "
        f"{diagnostics.session_id_before or 'none'}",
        "  Activation handler called: "
        f"{'yes' if diagnostics.activation_handler_called else 'no'}",
        f"  Pipeline status: {diagnostics.pipeline_status}",
        f"  Runtime terminal: {'yes' if diagnostics.runtime_terminal else 'no'}",
        f"  Runtime terminal reason: {diagnostics.runtime_terminal_reason}",
        f"  Capture stop reason: {diagnostics.capture_stop_reason or 'unknown'}",
        "  Finalized candidate duration: "
        f"{diagnostics.finalized_candidate_duration_seconds:.3f}s",
        "  Audio finalization: "
        f"{diagnostics.audio_finalization_started_at or 'unknown'} -> "
        f"{diagnostics.audio_finalization_completed_at or 'unknown'}",
        f"  WAV path: {diagnostics.wav_path or '<unavailable>'}",
        f"  WAV byte size: {diagnostics.wav_byte_size}",
        "  WAV format: "
        f"{diagnostics.wav_sample_rate_hz} Hz, "
        f"{diagnostics.wav_channels} channel(s), "
        f"{diagnostics.wav_sample_width_bytes}-byte samples",
        f"  Transcription backend: {diagnostics.transcription_backend}",
        "  Transcription timing: "
        f"{diagnostics.transcription_started_at or 'unknown'} -> "
        f"{diagnostics.transcription_completed_at or 'unknown'}",
        f"  Transcription status: {diagnostics.transcription_status}",
        "  Transcription hard timeout: "
        f"{diagnostics.transcription_timeout_seconds:.3f}s",
        "  Whisper processing duration: "
        f"{diagnostics.whisper_processing_duration_seconds:.3f}s",
        "  Whisper PID / PGID: "
        f"{diagnostics.whisper_process_pid or 'unavailable'} / "
        f"{diagnostics.whisper_process_group_id or 'unavailable'}",
        "  Whisper exit / elapsed: "
        f"{diagnostics.whisper_process_exit_code if diagnostics.whisper_process_exit_code is not None else 'unavailable'} / "
        f"{diagnostics.whisper_process_elapsed_seconds:.3f}s",
        "  Whisper TERM / KILL / reaped: "
        f"{'yes' if diagnostics.whisper_process_terminated else 'no'} / "
        f"{'yes' if diagnostics.whisper_process_killed else 'no'} / "
        f"{'yes' if diagnostics.whisper_process_reaped else 'no'}",
        "  Whisper cleanup / output handles closed: "
        f"{'completed' if diagnostics.whisper_process_cleanup_completed else 'incomplete'} / "
        f"{'yes' if diagnostics.whisper_output_handles_closed else 'no'}",
        f"  Transcript parsing: {diagnostics.transcript_parsing_status}",
        "  Routing timing: "
        f"{diagnostics.routing_started_at or 'not_started'} -> "
        f"{diagnostics.routing_completed_at or 'not_completed'}",
        "  Microphone gate released before inference: "
        f"{'yes' if diagnostics.microphone_gate_released_before_inference else 'no'}",
        "  Temporary audio cleanup: "
        f"{diagnostics.temporary_audio_cleanup_status}",
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
    speech_started = diagnostics.first_speech_frame > 0
    beginning_clipped_status = (
        diagnostics.beginning_clipped_status
        if diagnostics.beginning_clipped_status
        else ("yes" if diagnostics.beginning_clipped else "no")
    )
    lines = [
        "Wake diagnostic:",
        f"  Attempt ID: {diagnostics.attempt_id or 'unknown'}",
        f"  Candidate ID: {diagnostics.candidate_id or 'unknown'}",
        f"  Stream generation: {diagnostics.stream_generation}",
        "  Capture valid / recognizer invoked / infrastructure failure: "
        f"{'yes' if diagnostics.capture_valid else 'no'} / "
        f"{'yes' if diagnostics.recognizer_invoked else 'no'} / "
        f"{'yes' if diagnostics.infrastructure_failure else 'no'}",
        f"  Recognizer used: {diagnostics.recognizer_name or 'unknown'}",
        "  Raw recognition result: "
        f"{diagnostics.raw_recognition_result or '<empty>'}",
        f"  Raw recognized phrase: {diagnostics.raw_transcript or '<empty>'}",
        f"  Normalized phrase: {diagnostics.normalized_transcript or '<empty>'}",
        f"  Confidence: {confidence}",
        "  Minimum/mean/canonical confidence: "
        f"{_format_wake_confidence(diagnostics.minimum_word_confidence)} / "
        f"{_format_wake_confidence(diagnostics.mean_word_confidence)} / "
        f"{_format_wake_confidence(diagnostics.canonical_confidence)}",
        f"  Confidence tier: {diagnostics.confidence_tier or 'none'}",
        "  Medium-confidence confirmation: "
        f"{diagnostics.confirmation_count}/{diagnostics.confirmation_required_count}",
        f"  Selected alias: {diagnostics.selected_alias or 'none'}",
        f"  Selected wake phrase: {diagnostics.selected_wake_phrase or 'none'}",
        f"  Wake classification: {diagnostics.classification}",
        f"  Classification path: {diagnostics.classification_path or 'none'}",
        f"  Classification reason: {diagnostics.classification_reason or 'none'}",
        "  Canonical duplicate collapse: "
        f"{'used' if diagnostics.duplicate_collapse_used else 'not used'}",
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
        f"  Beginning clipped: {beginning_clipped_status}",
        "  First speech frame: "
        f"{diagnostics.first_speech_frame if speech_started else 'absent'}",
        "  Last speech frame: "
        f"{diagnostics.last_speech_frame if speech_started else 'absent'}",
        "  Wait before speech / active speech window: "
        f"{diagnostics.waiting_duration_before_speech_seconds:.3f}s / "
        f"{diagnostics.active_speech_window_seconds:.3f}s",
        "  Speech-start monotonic timestamp: "
        f"{f'{diagnostics.speech_start_timestamp_monotonic:.6f}' if speech_started else 'absent'}",
        "  Terminal silence duration: "
        f"{diagnostics.terminal_silence_duration_seconds:.3f}s",
        "  Terminal silence confirmed / resets: "
        f"{'yes' if diagnostics.terminal_silence_confirmed else 'no'} / "
        f"{diagnostics.terminal_silence_reset_count}",
        f"  Capture completion reason: {diagnostics.capture_completion_reason or 'unknown'}",
        f"  Terminal quiet frames: {diagnostics.terminal_quiet_frame_count}",
        f"  Speech frames: {diagnostics.speech_frame_count}",
        f"  Post-roll frames: {diagnostics.post_roll_frame_count}",
        f"  Duplicate PCM appends: {diagnostics.duplicate_pcm_frame_count}",
        f"  Stale PCM frames discarded: {diagnostics.stale_pcm_frames_discarded}",
        "  Noise floor/start/continue/end RMS: "
        f"{diagnostics.ambient_noise_floor:.1f} / "
        f"{diagnostics.speech_start_threshold:.1f} / "
        f"{diagnostics.speech_continue_threshold:.1f} / "
        f"{diagnostics.speech_end_threshold:.1f}",
        f"  Wake VAD sensitivity: {diagnostics.wake_vad_sensitivity}",
        "  Post-calibration source sequence: "
        f"{diagnostics.source_read_sequence_start}->"
        f"{diagnostics.source_read_sequence_end}; "
        f"frames={diagnostics.source_frames_read_delta}; "
        f"live_frames={diagnostics.source_live_frames_read_delta}",
        "  Post-calibration PCM bytes / live bytes: "
        f"{diagnostics.source_bytes_read_delta} / "
        f"{diagnostics.source_live_bytes_read_delta}",
        "  PCM integrity reads/full/partial/empty/errors: "
        f"{diagnostics.total_low_level_reads} / "
        f"{diagnostics.valid_full_pcm_frames} / "
        f"{diagnostics.partial_reads} / {diagnostics.empty_reads} / "
        f"{diagnostics.read_errors}",
        "  PCM integrity discarded/zero-filled/repeated/mutable-reuse: "
        f"{diagnostics.discarded_bytes} / {diagnostics.zero_filled_bytes} / "
        f"{diagnostics.repeated_frame_hashes} / "
        f"{diagnostics.mutable_buffer_reuse_detected}",
        "  Valid PCM bytes delivered to VAD (all/fresh): "
        f"{diagnostics.valid_microphone_bytes_delivered_to_vad} / "
        f"{diagnostics.fresh_microphone_bytes_delivered_to_vad}",
        "  Threshold crossings / maximum consecutive evidence / maximum RMS: "
        f"{diagnostics.speech_start_threshold_crossing_count} / "
        f"{diagnostics.maximum_consecutive_speech_evidence} / "
        f"{diagnostics.maximum_observed_rms:.1f}",
        f"  Listening duration: {diagnostics.listening_duration_seconds:.3f}s",
        f"  Capture stage: {diagnostics.capture_failure_stage or 'none'}",
        f"  Bounded RMS samples: {len(diagnostics.rms_trace)}",
        "  Speech-start to activation: "
        f"{diagnostics.speech_to_activation_seconds:.3f}s",
        f"  VAD transition count: {len(diagnostics.vad_transitions)}",
        f"  Raw capture duration: {diagnostics.raw_capture_duration_seconds:.3f}s",
        f"  Assembled duration: {diagnostics.assembled_duration_seconds:.3f}s",
        f"  Normalized duration: {diagnostics.normalized_duration_seconds:.3f}s",
        f"  Recognizer input duration: {diagnostics.whisper_input_duration_seconds:.3f}s",
        f"  Trimmed Vosk input duration: {diagnostics.trimmed_duration_seconds:.3f}s",
        "  Leading/trailing audio trimmed: "
        f"{diagnostics.leading_trimmed_seconds:.3f}s / "
        f"{diagnostics.trailing_trimmed_seconds:.3f}s",
        f"  Capture stop reason: {diagnostics.capture_stop_reason or 'unknown'}",
        f"  Recognition status: {diagnostics.recognition_status or 'unknown'}",
        "  Recognition processing duration: "
        f"{diagnostics.recognition_processing_time_seconds:.3f}s",
        "  Original/canonical Vosk tokens: "
        f"{list(diagnostics.original_vosk_tokens)} / "
        f"{list(diagnostics.canonical_tokens_after_collapse)}",
        f"  Vosk model path: {diagnostics.recognizer_model_path or diagnostics.wake_model_path}",
        f"  Lifecycle state: {diagnostics.lifecycle_state}",
    ]
    for frame in diagnostics.frame_trace:
        lines.append(
            "  VAD frame "
            f"{int(frame.get('frame', 0) or 0)} "
            f"(source={int(frame.get('source_frame_sequence', 0) or 0)}, "
            f"read={int(frame.get('source_read_sequence', 0) or 0)}): "
            f"rms={float(frame.get('rms', 0.0) or 0.0):.1f}; "
            f"start={float(frame.get('speech_start_threshold', 0.0) or 0.0):.1f}; "
            f"exceeded={'yes' if frame.get('exceeded_speech_start') else 'no'}; "
            f"evidence={int(frame.get('consecutive_speech_evidence', 0) or 0)}; "
            f"state={frame.get('state_before') or 'unknown'}; "
            f"transition={frame.get('vad_state_transition') or 'none'}; "
            f"bytes={int(frame.get('bytes_read', 0) or 0)}; "
            f"timestamp={float(frame.get('read_timestamp', 0.0) or 0.0):.6f}"
        )
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


def render_standby_calibration_diagnostics(
    data: dict[str, Any],
    *,
    include_energy_summary: bool = False,
) -> list[str]:
    diagnostics = dict(data.get("calibration_diagnostics") or {})
    thresholds = dict(data.get("calibration_thresholds") or {})
    if not diagnostics:
        return []
    lines = [
        "Standby calibration:",
        "  Vosk model / microphone adapter healthy: "
        f"{'yes' if data.get('wake_model_healthy') else 'no'} / "
        f"{'yes' if data.get('microphone_adapter_healthy') else 'no'}",
        "  Device open attempt / currently open / closed during cleanup: "
        f"{'yes' if data.get('alsa_device_open_attempt_succeeded') else 'no'} / "
        f"{'yes' if data.get('alsa_device_open') else 'no'} / "
        f"{'yes' if data.get('alsa_device_closed_during_cleanup') else 'no'}",
        "  Valid PCM / quality passed / listener healthy: "
        f"{'yes' if data.get('valid_pcm_received') else 'no'} / "
        f"{'yes' if diagnostics.get('quality_passed') else 'no'} / "
        f"{'yes' if data.get('standby_listener_healthy') else 'no'}",
        "  Frames / frame duration: "
        f"{int(diagnostics.get('frame_count', 0) or 0)} / "
        f"{float(diagnostics.get('frame_duration_seconds', 0.0) or 0.0):.3f}s",
        "  RMS min / median / p20 / p80 / max: "
        f"{float(diagnostics.get('minimum_rms', 0.0) or 0.0):.1f} / "
        f"{float(diagnostics.get('median_rms', 0.0) or 0.0):.1f} / "
        f"{float(diagnostics.get('percentile_20_rms', 0.0) or 0.0):.1f} / "
        f"{float(diagnostics.get('percentile_80_rms', 0.0) or 0.0):.1f} / "
        f"{float(diagnostics.get('maximum_rms', 0.0) or 0.0):.1f}",
        "  Speech / non-speech / longest non-speech run: "
        f"{int(diagnostics.get('speech_frame_count', 0) or 0)} / "
        f"{int(diagnostics.get('non_speech_frame_count', 0) or 0)} / "
        f"{int(diagnostics.get('longest_non_speech_sequence', 0) or 0)}",
        "  Quiet samples / bootstrap threshold / selected noise floor: "
        f"{int(diagnostics.get('quiet_sample_count', 0) or 0)} "
        f"({float(diagnostics.get('quiet_sample_fraction', 0.0) or 0.0):.1%}) / "
        f"{float(diagnostics.get('bootstrap_threshold_rms', 0.0) or 0.0):.1f} / "
        f"{float(diagnostics.get('selected_noise_floor_rms', 0.0) or 0.0):.1f}",
        "  Derived start / continuation / silence thresholds: "
        f"{float(thresholds.get('speech_start_rms', 0.0) or 0.0):.1f} / "
        f"{float(thresholds.get('speech_continue_rms', 0.0) or 0.0):.1f} / "
        f"{float(thresholds.get('silence_rms', 0.0) or 0.0):.1f}",
        f"  Wake VAD sensitivity: {data.get('wake_vad_sensitivity') or 'normal'}",
        "  Clipped / zero frames: "
        f"{int(diagnostics.get('clipped_frame_count', 0) or 0)} / "
        f"{int(diagnostics.get('zero_frame_count', 0) or 0)}",
        f"  Quality decision: {diagnostics.get('quality_reason') or 'unknown'}",
        f"  Failing subsystem: {data.get('failing_subsystem') or 'none'}",
    ]
    if include_energy_summary:
        for summary in list(diagnostics.get("rms_summary") or []):
            lines.append(
                "  RMS frames "
                f"{summary.get('first_frame', 0)}-{summary.get('last_frame', 0)}: "
                f"min={float(summary.get('minimum_rms', 0.0) or 0.0):.1f}; "
                f"mean={float(summary.get('mean_rms', 0.0) or 0.0):.1f}; "
                f"max={float(summary.get('maximum_rms', 0.0) or 0.0):.1f}"
            )
    return lines


def _format_wake_confidence(value: Optional[float]) -> str:
    return "unavailable" if value is None else f"{float(value):.3f}"


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
