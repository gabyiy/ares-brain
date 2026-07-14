from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    CAPTURE_MODE_AUTO_STOP,
    CAPTURE_MODE_FIXED_DURATION,
    CoreService,
    EventBus,
    LinuxAlsaMicrophoneAdapter,
    LinuxAlsaSpeakerAdapter,
    LinuxPiperTextToSpeechAdapter,
    LinuxWhisperSpeechToTextAdapter,
    SingleTurnVoicePipeline,
    SingleTurnVoiceRequestV1,
    get_global_conversation_context,
)
from events import EventHistoryStore, get_global_bus  # noqa: E402
from memory import (  # noqa: E402
    GoalsStore,
    MemoryStore,
    NotesStore,
    TasksStore,
    UserProfileStore,
)
from skills import SkillManager, create_builtin_skill_manager  # noqa: E402
from skills.base import SkillResponse  # noqa: E402


DEFAULT_MICROPHONE_DEVICE = "plughw:2,0"
DEFAULT_SPEAKER_DEVICE = "plughw:CARD=Device,DEV=0"
DEFAULT_WHISPER_MODEL = "models/whisper/ggml-base.en.bin"
DEFAULT_WHISPER_COMMAND = "external/whisper.cpp/build/bin/whisper-cli"
DEFAULT_RECORDING_OUTPUT = "data/manual_voice_samples/single_turn_input.wav"
DEFAULT_RECORD_SECONDS = 5
DEFAULT_PIPELINE_TIMEOUT = 300.0
DEFAULT_SPEECH_START_RMS = 200.0
DEFAULT_SPEECH_CONTINUE_RMS = 160.0
DEFAULT_SILENCE_RMS = 120.0
DEFAULT_CALIBRATION_SECONDS = 0.75
DEFAULT_SILENCE_SECONDS = 0.9
DEFAULT_SPEECH_WAIT_TIMEOUT = 10.0
DEFAULT_MAX_UTTERANCE_SECONDS = 15.0
DEFAULT_PRE_ROLL_SECONDS = 0.25
DEFAULT_FRAME_MS = 20
DEFAULT_REQUIRED_SPEECH_FRAMES = 3
DEFAULT_REQUIRED_CONTINUE_FRAMES = 3
DEFAULT_REQUIRED_SILENCE_FRAMES = 5
DEFAULT_DURATION_LOSS_TOLERANCE_SECONDS = 0.05

WARNING = (
    "WARNING: This runs exactly one owner-triggered local ARES voice turn and exits. "
    "It does not start wake words, background listening, GPT, or an ongoing loop."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one controlled local ARES voice interaction.")
    parser.add_argument("--record-seconds", type=int, default=DEFAULT_RECORD_SECONDS)
    capture_mode = parser.add_mutually_exclusive_group()
    capture_mode.add_argument(
        "--auto-stop",
        dest="capture_mode",
        action="store_const",
        const=CAPTURE_MODE_AUTO_STOP,
    )
    capture_mode.add_argument(
        "--fixed-duration",
        dest="capture_mode",
        action="store_const",
        const=CAPTURE_MODE_FIXED_DURATION,
    )
    parser.set_defaults(capture_mode=CAPTURE_MODE_FIXED_DURATION)
    parser.add_argument("--microphone-device", default=DEFAULT_MICROPHONE_DEVICE)
    parser.add_argument("--speaker-device", default=DEFAULT_SPEAKER_DEVICE)
    parser.add_argument("--language", default="en")
    parser.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL)
    parser.add_argument("--whisper-command", default=DEFAULT_WHISPER_COMMAND)
    parser.add_argument("--voice-profile", default="")
    parser.add_argument("--min-rms", type=float, default=0.0)
    calibration = parser.add_mutually_exclusive_group()
    calibration.add_argument(
        "--auto-calibration",
        dest="calibration_enabled",
        action="store_true",
    )
    calibration.add_argument(
        "--no-auto-calibration",
        dest="calibration_enabled",
        action="store_false",
    )
    parser.set_defaults(calibration_enabled=True)
    parser.add_argument(
        "--calibration-seconds",
        type=float,
        default=DEFAULT_CALIBRATION_SECONDS,
    )
    parser.add_argument("--speech-start-rms", type=float, default=DEFAULT_SPEECH_START_RMS)
    parser.add_argument(
        "--speech-continue-rms",
        type=float,
        default=DEFAULT_SPEECH_CONTINUE_RMS,
    )
    parser.add_argument("--silence-rms", type=float, default=DEFAULT_SILENCE_RMS)
    parser.add_argument("--silence-seconds", type=float, default=DEFAULT_SILENCE_SECONDS)
    parser.add_argument(
        "--speech-wait-timeout",
        type=float,
        default=DEFAULT_SPEECH_WAIT_TIMEOUT,
    )
    parser.add_argument(
        "--max-utterance-seconds",
        type=float,
        default=DEFAULT_MAX_UTTERANCE_SECONDS,
    )
    parser.add_argument("--pre-roll-seconds", type=float, default=DEFAULT_PRE_ROLL_SECONDS)
    parser.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS)
    parser.add_argument(
        "--required-speech-frames",
        type=int,
        default=DEFAULT_REQUIRED_SPEECH_FRAMES,
    )
    parser.add_argument(
        "--required-continue-frames",
        type=int,
        default=DEFAULT_REQUIRED_CONTINUE_FRAMES,
    )
    parser.add_argument(
        "--required-silence-frames",
        type=int,
        default=DEFAULT_REQUIRED_SILENCE_FRAMES,
    )
    parser.add_argument("--frame-debug", action="store_true")
    parser.add_argument(
        "--preserve-diagnostic-audio",
        action="store_true",
        help="retain raw, assembled, and normalized capture files without playing them",
    )
    parser.add_argument(
        "--diagnostic-audio",
        action="store_true",
        help="legacy alias for --preserve-diagnostic-audio",
    )
    parser.add_argument(
        "--duration-loss-tolerance",
        type=float,
        default=DEFAULT_DURATION_LOSS_TOLERANCE_SECONDS,
    )
    parser.add_argument("--play-diagnostic-capture", action="store_true")
    parser.add_argument("--play-diagnostic-assembled", action="store_true")
    parser.add_argument("--play-diagnostic-normalized", action="store_true")
    parser.add_argument(
        "--play-diagnostic-audio",
        action="store_true",
        help="play all retained microphone diagnostic stages after the turn",
    )
    parser.add_argument(
        "--playback-debug-stages",
        action="store_true",
        help="legacy alias for --play-diagnostic-audio",
    )
    parser.add_argument("--diagnostic-routing", action="store_true")
    parser.add_argument("--playback", action="store_true")
    parser.add_argument("--keep-audio", action="store_true")
    parser.add_argument("--timeout", type=float, default=DEFAULT_PIPELINE_TIMEOUT)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--text-input", default="")
    parser.add_argument("--recording-output", default=DEFAULT_RECORDING_OUTPUT)
    return parser


def create_skill_manager(
    core_service: CoreService,
    event_history_store: Optional[EventHistoryStore] = None,
    event_bus: Any = None,
    memory_store: Any = None,
    profile_store: Any = None,
    goals_store: Any = None,
    notes_store: Any = None,
    tasks_store: Any = None,
    conversation_context: Any = None,
) -> SkillManager:
    bus = event_bus or get_global_bus()
    return create_builtin_skill_manager(
        event_bus=bus,
        memory_store=memory_store or MemoryStore(event_bus=bus),
        profile_store=profile_store or UserProfileStore(event_bus=bus),
        goals_store=goals_store or GoalsStore(event_bus=bus),
        notes_store=notes_store or NotesStore(event_bus=bus),
        tasks_store=tasks_store or TasksStore(event_bus=bus),
        event_history_store=event_history_store,
        conversation_context=conversation_context or get_global_conversation_context(),
        core_service=core_service,
    )


def build_existing_brain_handler(skill_manager: SkillManager):
    def handle_text(text: str) -> SkillResponse:
        intent = skill_manager.parse_intent(text)
        candidates = skill_manager.candidate_diagnostics(
            intent,
            run_before_intents=True,
        )
        response = skill_manager.handle(text, run_before_intents=True)
        response_metadata = dict(response.metadata or {}) if response else {}
        plan = response_metadata.get("plan")
        if not isinstance(plan, dict) and skill_manager.last_plan is not None:
            plan = skill_manager.last_plan.to_dict()
        execution = response_metadata.get("execution")
        selected_skill = response.skill if response else ""
        candidates = [
            {**candidate, "selected": candidate["skill"] == selected_skill}
            for candidate in candidates
        ]
        diagnostics = _build_routing_diagnostics(
            intent=intent,
            candidates=candidates,
            selected_skill=selected_skill,
            plan=plan if isinstance(plan, dict) else {},
            execution=execution if isinstance(execution, dict) else {},
            response_metadata=response_metadata,
        )
        if response is None:
            return SkillResponse(
                text="I cannot handle that request yet.",
                skill="unknown",
                metadata={
                    "detected_intent": intent.intent_name,
                    "candidate_skills": candidates,
                    "routing_diagnostics": diagnostics,
                    "rejection_reason": diagnostics["rejection_reason"],
                },
            )
        return SkillResponse(
            text=response.text,
            skill=response.skill,
            metadata={
                **response_metadata,
                "detected_intent": intent.intent_name,
                "candidate_skills": candidates,
                "routing_diagnostics": diagnostics,
            },
        )

    return handle_text


def _build_routing_diagnostics(
    intent: Any,
    candidates: list[dict[str, Any]],
    selected_skill: str,
    plan: dict[str, Any],
    execution: dict[str, Any],
    response_metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    intent_name = str(getattr(intent, "intent_name", "") or "unknown")
    intent_ok = intent_name != "unknown"
    selected_ok = bool(selected_skill and selected_skill != "unknown")
    plan_steps = [step for step in list(plan.get("steps") or []) if isinstance(step, dict)]
    plan_errors = [str(error) for error in list(plan.get("errors") or [])]
    planner_ok = bool(plan_steps) if selected_ok else False
    execution_started = bool(execution)
    execution_ok = bool(execution.get("success")) if execution_started else False

    stages = [
        {
            "stage": "intent_parser",
            "success": intent_ok,
            "reason": "" if intent_ok else "intent_parser_returned_unknown",
        },
        {
            "stage": "skill_selection",
            "success": selected_ok,
            "reason": "" if selected_ok else "no_registered_skill_matched_normalized_command",
        },
        {
            "stage": "planner",
            "success": planner_ok,
            "reason": "" if planner_ok else "; ".join(plan_errors) or "no_executable_plan",
        },
        {
            "stage": "execution_pipeline",
            "success": execution_ok,
            "reason": ""
            if execution_ok
            else str(execution.get("error_message") or "execution_not_started"),
        },
    ]
    rejection_reason = next(
        (stage["reason"] for stage in stages if not stage["success"] and stage["reason"]),
        "",
    )
    targets = [str(step.get("target") or "") for step in plan_steps]
    planner_decision = (
        f"{len(plan_steps)} step(s): {', '.join(target for target in targets if target)}"
        if plan_steps
        else "no executable steps"
    )
    execution_result = (
        "success"
        if execution_ok
        else str(execution.get("error_message") or "not_started")
    )
    intent_data = intent.to_dict() if hasattr(intent, "to_dict") else {}
    diagnostics = {
        "parsed_intent": {
            "name": intent_name,
            "confidence": float(getattr(intent, "confidence", 0.0) or 0.0),
            "entities": dict(intent_data.get("extracted_entities") or {}),
        },
        "candidate_skills": [dict(candidate) for candidate in candidates],
        "selected_skill": selected_skill or "none",
        "planner_decision": planner_decision,
        "execution_result": execution_result,
        "rejection_reason": rejection_reason,
        "stages": stages,
    }
    owner_memory = dict(
        (response_metadata or {}).get("owner_memory_diagnostics") or {}
    )
    if owner_memory:
        diagnostics["owner_memory"] = owner_memory
    return diagnostics


def create_pipeline(
    args: argparse.Namespace,
    output_func: Callable[[str], None] = print,
    skill_manager: Optional[SkillManager] = None,
    event_history_store: Optional[EventHistoryStore] = None,
    microphone_adapter: Any = None,
    speech_to_text_adapter: Any = None,
    text_to_speech_adapter: Any = None,
    speaker_adapter: Any = None,
) -> SingleTurnVoicePipeline:
    core_service = (
        skill_manager.core_service
        if skill_manager is not None
        else CoreService(register_default_pc=False)
    )
    history = event_history_store or EventHistoryStore()
    manager = skill_manager or create_skill_manager(core_service, history)
    speaker = speaker_adapter or LinuxAlsaSpeakerAdapter(
        device=args.speaker_device,
        timeout_seconds=args.timeout,
    )
    microphone = microphone_adapter or LinuxAlsaMicrophoneAdapter(
        device=args.microphone_device,
        record_seconds=args.record_seconds,
        timeout_seconds=min(args.timeout, _recording_timeout(args)),
    )
    speech_to_text = speech_to_text_adapter or LinuxWhisperSpeechToTextAdapter(
        model_path=_repo_path(args.whisper_model),
        whisper_command=_repo_path_or_command(args.whisper_command),
        language=args.language,
        timeout_seconds=args.timeout,
        minimum_rms=args.min_rms,
    )
    text_to_speech = text_to_speech_adapter or LinuxPiperTextToSpeechAdapter(
        piper_command=_default_piper_command(),
        speaker_adapter=speaker,
        timeout_seconds=args.timeout,
    )
    return SingleTurnVoicePipeline(
        microphone_adapter=microphone,
        speech_to_text_adapter=speech_to_text,
        text_to_speech_adapter=text_to_speech,
        speaker_adapter=speaker,
        command_handler=build_existing_brain_handler(manager),
        core_service=core_service,
        event_bus=EventBus(),
        event_history_store=history,
        stage_callback=_stage_printer(output_func),
    )


def request_from_args(args: argparse.Namespace) -> SingleTurnVoiceRequestV1:
    timeout = float(args.timeout)
    diagnostic_audio = _diagnostic_audio_requested(args)
    return SingleTurnVoiceRequestV1(
        microphone_device=args.microphone_device,
        recording_duration_seconds=args.record_seconds,
        recording_output_path=str(_repo_path(args.recording_output)),
        language=args.language,
        whisper_executable_path=str(_repo_path_or_command(args.whisper_command)),
        whisper_model_profile=str(_repo_path(args.whisper_model)),
        minimum_rms=args.min_rms,
        capture_mode=args.capture_mode,
        calibration_enabled=bool(args.calibration_enabled),
        calibration_duration_seconds=args.calibration_seconds,
        speech_start_rms=args.speech_start_rms,
        speech_continue_rms=args.speech_continue_rms,
        silence_rms=args.silence_rms,
        required_speech_frames=args.required_speech_frames,
        required_continue_frames=args.required_continue_frames,
        required_silence_frames=args.required_silence_frames,
        silence_duration_seconds=args.silence_seconds,
        speech_wait_timeout_seconds=args.speech_wait_timeout,
        maximum_utterance_seconds=args.max_utterance_seconds,
        pre_roll_seconds=args.pre_roll_seconds,
        frame_duration_ms=args.frame_ms,
        duration_loss_tolerance_seconds=args.duration_loss_tolerance,
        frame_debug_enabled=bool(args.frame_debug),
        diagnostic_audio=diagnostic_audio,
        tts_voice_profile=args.voice_profile,
        speaker_device=args.speaker_device,
        playback_enabled=bool(args.playback),
        timeout_seconds=timeout,
        recording_timeout_seconds=min(timeout, _recording_timeout(args)),
        transcription_timeout_seconds=timeout,
        brain_timeout_seconds=min(timeout, 30.0),
        synthesis_timeout_seconds=timeout,
        playback_timeout_seconds=timeout,
        cleanup_policy=(
            "keep" if args.keep_audio or diagnostic_audio else "delete_on_success"
        ),
        text_input=args.text_input,
        metadata={
            "source": "manual_verify_single_turn_voice",
            "owner_triggered": True,
        },
    )


def run_manual_verification(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    pipeline: Optional[SingleTurnVoicePipeline] = None,
) -> int:
    args = build_parser().parse_args(argv)
    output_func(WARNING)
    diagnostic_audio = _diagnostic_audio_requested(args)
    diagnostic_playback_stages = _selected_diagnostic_playback_stages(args)
    if not args.playback:
        output_func("Speaker playback is disabled. Add --playback to hear the response.")
    request = request_from_args(args)
    active_pipeline = pipeline or create_pipeline(args, output_func=output_func)
    try:
        result = active_pipeline.run_once(request)
    except KeyboardInterrupt:
        active_pipeline.stop(request)
        output_func("Single-turn voice verification cancelled safely.")
        return 130

    output_func("")
    output_func("Single-turn summary")
    output_func(f"Raw transcript: {result.raw_transcript or '(none)'}")
    output_func(f"Cleaned transcript: {result.cleaned_transcript or '(none)'}")
    output_func(f"Normalized command: {result.normalized_command or '(none)'}")
    output_func(f"Recognized text: {result.recognized_text or '(none)'}")
    output_func(f"Detected intent: {result.detected_intent or 'unknown'}")
    selected_skill = result.routed_skill if result.routed_skill not in {"", "unknown"} else "none"
    output_func(f"Selected skill: {selected_skill}")
    detected = result.detected_intent or result.routed_skill or "unknown"
    output_func(f"Detected intent/skill: {detected}")
    output_func(f"Planner decision: {result.planner_decision or '(none)'}")
    output_func(f"Execution result: {result.execution_result or result.brain_execution_status}")
    output_func(f"Rejection reason: {result.rejection_reason or '(none)'}")
    if args.diagnostic_routing:
        _print_routing_diagnostics(result, output_func)
    output_func(f"ARES response: {result.brain_text_response or '(none)'}")
    output_func(f"Total processing time: {result.total_processing_time_seconds:.3f}s")
    output_func(f"Final status: {result.status}")
    _print_capture_diagnostics(
        result,
        output_func,
        frame_debug=bool(args.frame_debug or args.verbose),
        diagnostic_audio=diagnostic_audio,
    )
    transcript_output_path = ""
    if diagnostic_audio:
        transcript_output_path = _write_diagnostic_transcript(result)
        output_func(
            "Whisper transcript output path: "
            f"{transcript_output_path or '(not written)'}"
        )
    playback_debug_success = True
    if diagnostic_playback_stages:
        playback_debug_success = _play_diagnostic_audio_stages(
            active_pipeline,
            result,
            args.speaker_device,
            args.timeout,
            output_func,
            diagnostic_playback_stages,
        )
    if result.error_reason:
        output_func(f"Failure stage: {result.error_stage}")
        output_func(f"Failure reason: {result.error_reason}")
    if result.generated_speech_wav_path and result.status in {"tts_failed", "playback_failed"}:
        output_func(f"Generated speech WAV: {result.generated_speech_wav_path}")
    if args.verbose:
        output_func(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.success and playback_debug_success else 2


def _stage_printer(output_func: Callable[[str], None]):
    seen = set()

    def print_stage(index: int, total: int, label: str, status: str) -> None:
        if index in seen:
            return
        seen.add(index)
        output_func(f"[{index}/{total}] {label}")

    return print_stage


def _print_routing_diagnostics(
    result: SingleTurnVoiceResultV1,
    output_func: Callable[[str], None],
) -> None:
    considered = [
        candidate
        for candidate in result.candidate_skills
        if candidate.get("considered")
    ]
    candidate_summary = ", ".join(
        f"{candidate.get('skill')} "
        f"(confidence={float(candidate.get('confidence', 0.0)):.3f}, "
        f"{'selected' if candidate.get('selected') else candidate.get('reason')})"
        for candidate in considered
    )
    output_func("")
    output_func("Routing diagnostics")
    output_func(f"Raw transcript: {result.raw_transcript or '(none)'}")
    output_func(f"Cleaned transcript: {result.cleaned_transcript or '(none)'}")
    output_func(f"Normalized command: {result.normalized_command or '(none)'}")
    output_func(
        "Extracted calculator expression: "
        f"{result.extracted_calculator_expression or '(none)'}"
    )
    output_func(
        f"Transcript cleanup rule: {result.transcript_cleanup_rule or 'none'}"
    )
    output_func(f"Parsed intent: {result.detected_intent or 'unknown'}")
    owner_memory = dict(result.routing_diagnostics.get("owner_memory") or {})
    output_func(
        "Parsed owner-memory action: "
        f"{owner_memory.get('action') or '(none)'}"
    )
    output_func(
        "Extracted owner-memory key: "
        f"{owner_memory.get('normalized_key') or '(none)'}"
    )
    output_func(
        "Extracted owner-memory value: "
        f"{owner_memory.get('extracted_value') or '(none)'}"
    )
    output_func(
        "Extracted explicit-memory phrase: "
        f"{owner_memory.get('extracted_memory_phrase') or '(none)'}"
    )
    output_func(
        "Normalized memory trigger: "
        f"{owner_memory.get('normalized_memory_trigger') or '(none)'}"
    )
    output_func(
        "Extracted owner-memory fact: "
        f"{owner_memory.get('extracted_fact_text') or '(none)'}"
    )
    output_func(
        "Owner-memory routing reason: "
        f"{owner_memory.get('routing_reason') or '(none)'}"
    )
    output_func(
        "Owner-memory type: "
        f"{owner_memory.get('memory_type') or '(none)'}"
    )
    output_func(
        "Owner-memory persistence: "
        f"{owner_memory.get('persistence') or '(none)'}"
    )
    output_func(f"Candidate skills: {candidate_summary or 'none'}")
    output_func(f"Selected skill: {result.routed_skill or 'none'}")
    output_func(
        "Resolved owner-profile path: "
        f"{owner_memory.get('profile_path') or '(none)'}"
    )
    file_existed = owner_memory.get("file_existed_before")
    output_func(
        "Owner-profile file existed before operation: "
        f"{file_existed if file_existed is not None else '(none)'}"
    )
    output_func(
        "Owner-memory operation result: "
        f"{owner_memory.get('operation_result') or '(none)'}"
    )
    output_func(
        "Owner-memory id: "
        f"{owner_memory.get('memory_id') or '(none)'}"
    )
    output_func(f"Planner decision: {result.planner_decision or '(none)'}")
    output_func(f"Execution result: {result.execution_result or 'not_started'}")
    output_func(f"Rejection reason: {result.rejection_reason or '(none)'}")


def _repo_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def _repo_path_or_command(value: str) -> str:
    clean = str(value or "").strip()
    if not clean or ("/" not in clean and "\\" not in clean):
        return clean
    return str(_repo_path(clean))


def _default_piper_command() -> str:
    nested = REPO_ROOT / "external" / "piper" / "piper" / "piper"
    direct = REPO_ROOT / "external" / "piper" / "piper"
    if nested.exists():
        return str(nested)
    if direct.exists():
        return str(direct)
    return "piper"


def _recording_timeout(args: argparse.Namespace) -> float:
    if args.capture_mode == CAPTURE_MODE_AUTO_STOP:
        return float(
            args.calibration_seconds
            + args.speech_wait_timeout
            + args.max_utterance_seconds
            + 5.0
        )
    return float(args.record_seconds + 5)


def _print_capture_diagnostics(
    result: Any,
    output_func: Callable[[str], None],
    frame_debug: bool = False,
    diagnostic_audio: bool = False,
) -> None:
    recording = dict(result.data.get("recording") or {})
    if not recording:
        return
    data = dict(recording.get("data") or {})

    def value(name: str, default: Any = "") -> Any:
        direct = recording.get(name)
        return direct if direct not in (None, "") else data.get(name, default)

    output_func(f"Requested microphone device: {value('requested_device', '(default)')}")
    output_func(f"Resolved capture device: {value('resolved_capture_device', '(default)')}")
    output_func(f"Requested sample rate: {value('requested_sample_rate_hz', 16000)} Hz")
    output_func(f"Actual captured sample rate: {value('actual_sample_rate_hz', 0)} Hz")
    output_func(f"Actual captured channels: {value('actual_channels', 0)}")
    output_func(f"Actual captured sample width: {value('actual_sample_width_bytes', 0)} bytes")
    output_func(f"Normalized sample rate: {value('normalized_sample_rate_hz', 0)} Hz")
    output_func(f"Normalized channels: {value('normalized_channels', 0)}")
    output_func(
        f"Normalized sample width: {value('normalized_sample_width_bytes', 0)} bytes"
    )
    output_func(f"Raw WAV path: {value('raw_wav_path', '(not retained)') or '(not retained)'}")
    output_func(
        f"Assembled WAV path: {value('assembled_wav_path', '(not retained)') or '(not retained)'}"
    )
    output_func(f"Normalized WAV path: {value('normalized_wav_path', result.recorded_wav_path)}")
    output_func(f"Raw duration: {float(value('raw_duration_seconds', 0.0)):.3f}s")
    output_func(
        "Assembled duration before normalization: "
        f"{float(value('assembled_duration_seconds', 0.0)):.3f}s"
    )
    output_func(
        f"Normalized duration: {float(value('normalized_duration_seconds', 0.0)):.3f}s"
    )
    output_func(
        "Whisper input duration: "
        f"{float(value('whisper_input_duration_seconds', 0.0)):.3f}s"
    )
    output_func(f"Total ALSA frames read: {int(value('total_frames_read', 0))}")
    output_func(f"Total raw samples: {int(value('total_raw_samples', 0))}")
    output_func(f"Raw PCM byte count: {int(value('raw_byte_count', 0))}")
    output_func(
        f"Pre-roll frames retained: {int(value('pre_roll_frames_retained', 0))}"
    )
    output_func(
        f"Speech frames retained: {int(value('speech_frames_retained', 0))}"
    )
    output_func(
        "Possible-silence frames retained: "
        f"{int(value('possible_silence_frames_retained', 0))}"
    )
    output_func(
        f"Final assembled frame count: {int(value('final_assembled_frame_count', 0))}"
    )
    output_func(
        f"Final assembled sample count: {int(value('final_assembled_sample_count', 0))}"
    )
    output_func(
        f"Final assembled byte count: {int(value('final_assembled_byte_count', 0))}"
    )
    output_func(f"Normalized sample count: {int(value('normalized_sample_count', 0))}")
    output_func(f"Normalized byte count: {int(value('normalized_byte_count', 0))}")
    output_func(
        "Leading silence trimmed: "
        f"{float(value('leading_silence_trimmed_seconds', 0.0)):.3f}s"
    )
    output_func(
        "Trailing silence trimmed: "
        f"{float(value('trailing_silence_trimmed_seconds', 0.0)):.3f}s"
    )
    output_func(
        f"Duration invariant: {value('duration_invariant_status', 'not_checked')}"
    )
    output_func(
        f"Final Whisper input path: {value('final_whisper_input_path', result.recorded_wav_path)}"
    )
    if recording.get("metadata", {}).get("source") != "rms_voice_activity_capture":
        return
    output_func(f"Capture stop reason: {recording.get('stop_reason') or recording.get('status')}")
    output_func(f"Ambient RMS: {float(recording.get('ambient_rms', 0.0)):.3f}")
    output_func(
        "Ambient statistics: "
        f"mean={float(recording.get('ambient_rms_mean', 0.0)):.3f}, "
        f"median={float(recording.get('ambient_rms_median', 0.0)):.3f}, "
        f"p90={float(recording.get('ambient_rms_percentile', 0.0)):.3f}, "
        f"peak={float(recording.get('ambient_rms_peak', 0.0)):.3f}"
    )
    output_func(f"Speech RMS: {float(recording.get('speech_rms', 0.0)):.3f}")
    output_func(f"Peak amplitude: {int(recording.get('peak_amplitude', 0))}")
    output_func(
        "Selected thresholds: "
        f"start={data.get('speech_start_rms')}, "
        f"continue={data.get('speech_continue_rms')}, "
        f"silence={data.get('silence_rms')}"
    )
    if (frame_debug or diagnostic_audio) and data.get("transitions"):
        output_func("Capture transitions:")
        for transition in data["transitions"]:
            output_func(
                f"  {transition.get('from')} -> {transition.get('to')} "
                f"(frame={transition.get('frame')}, rms={transition.get('rms')})"
            )


def _diagnostic_audio_paths(result: Any) -> dict[str, str]:
    recording = dict(result.data.get("recording") or {})
    data = dict(recording.get("data") or {})

    def value(name: str) -> str:
        return str(recording.get(name) or data.get(name) or "")

    return {
        "raw capture": value("raw_wav_path"),
        "assembled utterance": value("assembled_wav_path"),
        "normalized Whisper input": value("normalized_wav_path")
        or str(result.recorded_wav_path or ""),
    }


def _diagnostic_audio_requested(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "preserve_diagnostic_audio", False)
        or getattr(args, "diagnostic_audio", False)
        or _selected_diagnostic_playback_stages(args)
    )


def _selected_diagnostic_playback_stages(
    args: argparse.Namespace,
) -> tuple[str, ...]:
    ordered = (
        ("raw capture", "play_diagnostic_capture"),
        ("assembled utterance", "play_diagnostic_assembled"),
        ("normalized Whisper input", "play_diagnostic_normalized"),
    )
    play_all = bool(
        getattr(args, "play_diagnostic_audio", False)
        or getattr(args, "playback_debug_stages", False)
    )
    return tuple(
        label
        for label, attribute in ordered
        if play_all or getattr(args, attribute, False)
    )


def _write_diagnostic_transcript(result: Any) -> str:
    normalized_path = _diagnostic_audio_paths(result)["normalized Whisper input"]
    transcript = str(result.raw_transcript or result.recognized_text or "")
    if not normalized_path or not transcript:
        return ""
    directory = Path(normalized_path).expanduser().parent
    output_path = directory / "whisper_transcript.txt"
    temporary_path = directory / ".whisper_transcript.txt.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(transcript + "\n", encoding="utf-8")
        temporary_path.replace(output_path)
    except OSError:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        return ""
    return str(output_path)


def _play_diagnostic_audio_stages(
    pipeline: Any,
    result: Any,
    speaker_device: str,
    timeout_seconds: float,
    output_func: Callable[[str], None],
    selected_stages: Sequence[str],
) -> bool:
    speaker = getattr(pipeline, "speaker_adapter", None)
    if speaker is None:
        output_func("FAIL: diagnostic playback speaker adapter is unavailable.")
        return False
    paths = _diagnostic_audio_paths(result)
    selected_paths = {
        label: paths[label]
        for label in selected_stages
        if label in paths
    }
    if len(selected_paths) != len(selected_stages) or any(
        not path or not Path(path).expanduser().exists()
        for path in selected_paths.values()
    ):
        output_func("FAIL: one or more selected diagnostic audio stages are unavailable.")
        return False
    started = speaker.start()
    if not getattr(started, "success", False):
        output_func("FAIL: diagnostic playback speaker could not start.")
        return False
    success = True
    try:
        for label, path in selected_paths.items():
            output_func(f"Playing diagnostic {label}: {path}")
            playback = speaker.play_wav(
                path,
                device=speaker_device,
                timeout_seconds=timeout_seconds,
            )
            if not getattr(playback, "success", False):
                output_func(
                    f"FAIL: diagnostic {label} playback failed: "
                    f"{getattr(playback, 'error_message', 'playback_failed')}"
                )
                success = False
                break
    finally:
        speaker.stop()
    return success


def main() -> None:
    raise SystemExit(run_manual_verification())


if __name__ == "__main__":
    main()
