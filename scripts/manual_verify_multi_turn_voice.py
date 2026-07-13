from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    CAPTURE_MODE_AUTO_STOP,
    CAPTURE_MODE_FIXED_DURATION,
    DEFAULT_CONVERSATION_STOP_PHRASES,
    MultiTurnVoiceSession,
    MultiTurnVoiceSessionRequestV1,
)
from scripts.manual_verify_single_turn_voice import (  # noqa: E402
    DEFAULT_MICROPHONE_DEVICE,
    DEFAULT_CALIBRATION_SECONDS,
    DEFAULT_PIPELINE_TIMEOUT,
    DEFAULT_FRAME_MS,
    DEFAULT_MAX_UTTERANCE_SECONDS,
    DEFAULT_PRE_ROLL_SECONDS,
    DEFAULT_RECORD_SECONDS,
    DEFAULT_REQUIRED_SPEECH_FRAMES,
    DEFAULT_REQUIRED_CONTINUE_FRAMES,
    DEFAULT_REQUIRED_SILENCE_FRAMES,
    DEFAULT_DURATION_LOSS_TOLERANCE_SECONDS,
    DEFAULT_SILENCE_RMS,
    DEFAULT_SILENCE_SECONDS,
    DEFAULT_SPEECH_START_RMS,
    DEFAULT_SPEECH_CONTINUE_RMS,
    DEFAULT_SPEECH_WAIT_TIMEOUT,
    DEFAULT_SPEAKER_DEVICE,
    DEFAULT_WHISPER_COMMAND,
    DEFAULT_WHISPER_MODEL,
    create_pipeline,
)


DEFAULT_MAX_TURNS = 5
DEFAULT_MAX_SESSION_SECONDS = 180.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_INTER_TURN_DELAY = 0.75
DEFAULT_GREETING = "Hello Gabriel. I am listening."
DEFAULT_CLOSING = "Goodbye Gabriel."
DEFAULT_RECORDING_DIRECTORY = "data/manual_voice_samples"

WARNING = (
    "WARNING: This starts one bounded, owner-triggered ARES conversation session. "
    "It does not enable wake words, background listening, GPT, cloud services, or boot startup."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one bounded local ARES multi-turn voice session."
    )
    parser.add_argument("--microphone-device", default=DEFAULT_MICROPHONE_DEVICE)
    parser.add_argument("--speaker-device", default=DEFAULT_SPEAKER_DEVICE)
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
    parser.add_argument("--language", default="en")
    parser.add_argument("--whisper-command", default=DEFAULT_WHISPER_COMMAND)
    parser.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL)
    parser.add_argument("--voice-profile", default="")
    parser.add_argument("--playback", action="store_true")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument(
        "--max-session-seconds",
        type=float,
        default=DEFAULT_MAX_SESSION_SECONDS,
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
    )
    parser.add_argument(
        "--inter-turn-delay",
        type=float,
        default=DEFAULT_INTER_TURN_DELAY,
    )
    parser.add_argument("--stop-phrase", action="append", default=[])
    parser.add_argument("--greeting", default=DEFAULT_GREETING)
    parser.add_argument("--no-greeting", action="store_true")
    parser.add_argument("--closing-phrase", default=DEFAULT_CLOSING)
    parser.add_argument("--no-closing-phrase", action="store_true")
    parser.add_argument("--keep-audio", action="store_true")
    parser.add_argument("--timeout", type=float, default=DEFAULT_PIPELINE_TIMEOUT)
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
    parser.add_argument("--diagnostic-audio", action="store_true")
    parser.add_argument(
        "--duration-loss-tolerance",
        type=float,
        default=DEFAULT_DURATION_LOSS_TOLERANCE_SECONDS,
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--text-turn", action="append", default=[])
    parser.add_argument("--interactive-text", action="store_true")
    parser.add_argument(
        "--recording-output-directory",
        default=DEFAULT_RECORDING_DIRECTORY,
    )
    return parser


def request_from_args(args: argparse.Namespace) -> MultiTurnVoiceSessionRequestV1:
    stop_phrases = args.stop_phrase or list(DEFAULT_CONVERSATION_STOP_PHRASES)
    return MultiTurnVoiceSessionRequestV1(
        microphone_device=args.microphone_device,
        speaker_device=args.speaker_device,
        recording_duration_seconds=args.record_seconds,
        recording_output_directory=str(_repo_path(args.recording_output_directory)),
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
        diagnostic_audio=bool(args.diagnostic_audio),
        tts_voice_profile=args.voice_profile,
        playback_enabled=bool(args.playback),
        maximum_turns=args.max_turns,
        maximum_session_duration_seconds=args.max_session_seconds,
        maximum_consecutive_failures=args.max_consecutive_failures,
        inter_turn_delay_seconds=args.inter_turn_delay,
        stop_phrases=list(stop_phrases),
        greeting_enabled=not args.no_greeting,
        greeting_text=args.greeting,
        closing_phrase_enabled=not args.no_closing_phrase,
        closing_phrase_text=args.closing_phrase,
        cleanup_policy=(
            "keep" if args.keep_audio or args.diagnostic_audio else "delete_on_success"
        ),
        per_turn_timeout_seconds=args.timeout,
        total_session_timeout_seconds=args.max_session_seconds,
        verbose_diagnostics=bool(args.verbose),
        simulated_text_turns=list(args.text_turn),
        interactive_text=bool(args.interactive_text),
        metadata={
            "source": "manual_verify_multi_turn_voice",
            "owner_triggered": True,
            "background_listening": False,
        },
    )


def run_manual_verification(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    session: Optional[MultiTurnVoiceSession] = None,
) -> int:
    args = build_parser().parse_args(argv)
    output_func(WARNING)
    if args.text_turn and args.interactive_text:
        output_func("FAIL: --text-turn and --interactive-text cannot be used together.")
        return 2
    if not args.playback:
        output_func("Speaker playback is disabled. Add --playback to hear ARES responses.")

    output_func("Starting ARES conversation session")
    output_func(f"Maximum turns: {args.max_turns}")
    if args.capture_mode == CAPTURE_MODE_AUTO_STOP:
        output_func(
            "Automatic end-of-speech capture: "
            f"calibration={args.calibration_enabled}, "
            f"start_rms={args.speech_start_rms}, "
            f"continue_rms={args.speech_continue_rms}, "
            f"silence_rms={args.silence_rms}, "
            f"silence_seconds={args.silence_seconds}, frame_ms={args.frame_ms}"
        )
    request = request_from_args(args)
    output_func(f"Session ID: {request.session_id or '(assigned at start)'}")

    active_session = session
    if active_session is None:
        pipeline = create_pipeline(args, output_func=lambda _: None)
        pipeline.stage_callback = None
        active_session = MultiTurnVoiceSession(
            pipeline,
            text_input_provider=_interactive_input if args.interactive_text else None,
            progress_callback=_progress_printer(output_func),
        )

    try:
        result = active_session.run_session(request)
    except KeyboardInterrupt:
        active_session.request_stop("keyboard_interrupt")
        active_session.stop(request)
        output_func("ARES conversation session cancelled safely.")
        return 130

    output_func("")
    output_func("Session completed" if result.success else "Session stopped")
    output_func(f"Turns attempted: {result.attempted_turns}")
    output_func(f"Turns completed: {result.successful_turns}")
    output_func(f"Stop reason: {result.stop_reason or result.status}")
    output_func(f"Total time: {result.total_duration_seconds:.3f}s")
    output_func(f"Final status: {result.status}")
    if result.error_reason:
        output_func(f"Failure stage: {result.error_stage}")
        output_func(f"Failure reason: {result.error_reason}")
    if args.verbose:
        output_func(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if result.cancelled:
        return 130
    return 0 if result.success else 2


def _progress_printer(output_func: Callable[[str], None]) -> Callable[[str, dict], None]:
    def report(event_type: str, payload: dict) -> None:
        if event_type == "turn_started":
            output_func("")
            output_func(f"Turn {payload['turn_number']}")
            output_func("Simulated input..." if payload.get("simulated_input") else "Listening...")
        elif event_type == "turn_result":
            output_func(f"Raw transcript: {payload.get('raw_transcript') or '(none)'}")
            output_func(f"Cleaned transcript: {payload.get('cleaned_transcript') or '(none)'}")
            output_func(f"Normalized command: {payload.get('normalized_command') or '(none)'}")
            output_func(f"Detected intent: {payload.get('detected_intent') or 'unknown'}")
            selected = payload.get("routed_skill")
            output_func(f"Selected skill: {selected if selected not in {'', 'unknown', None} else 'none'}")
            output_func(f"Planner decision: {payload.get('planner_decision') or '(none)'}")
            output_func(
                f"Execution result: {payload.get('execution_result') or payload.get('status')}"
            )
            output_func(f"Rejection reason: {payload.get('rejection_reason') or '(none)'}")
            if payload.get("response_text"):
                output_func(f"ARES: {payload['response_text']}")
            if payload.get("capture_mode") == CAPTURE_MODE_AUTO_STOP:
                output_func(
                    "Capture: "
                    f"stop={payload.get('capture_stop_reason') or '(none)'}, "
                    f"ambient_rms={float(payload.get('ambient_rms', 0.0)):.3f}, "
                    f"speech_rms={float(payload.get('speech_rms', 0.0)):.3f}, "
                    f"peak={int(payload.get('peak_amplitude', 0))}, "
                    f"thresholds={float(payload.get('speech_start_rms', 0.0)):.1f}/"
                    f"{float(payload.get('speech_continue_rms', 0.0)):.1f}/"
                    f"{float(payload.get('silence_rms', 0.0)):.1f}"
                )
            if payload.get("resolved_capture_device"):
                output_func(
                    "Audio format: "
                    f"requested_device={payload.get('requested_device') or '(default)'}, "
                    f"resolved_device={payload.get('resolved_capture_device')}, "
                    f"actual_rate={payload.get('actual_sample_rate_hz')} Hz, "
                    f"normalized_rate={payload.get('normalized_sample_rate_hz')} Hz"
                )
                if payload.get("raw_wav_path"):
                    output_func(f"Raw diagnostic WAV: {payload['raw_wav_path']}")
                output_func(
                    f"Normalized WAV: {payload.get('normalized_wav_path') or '(none)'}"
                )
                output_func(
                    "Final Whisper input: "
                    f"{payload.get('final_whisper_input_path') or '(none)'}"
                )
        elif event_type == "stop_phrase_detected":
            output_func(f"Stop phrase detected: {payload['matched_stop_phrase']}")

    return report


def _interactive_input(turn_number: int) -> Optional[str]:
    try:
        return input(f"Turn {turn_number} text> ")
    except EOFError:
        return None


def _repo_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def _repo_path_or_command(value: str) -> str:
    clean = str(value or "").strip()
    if not clean or ("/" not in clean and "\\" not in clean):
        return clean
    return str(_repo_path(clean))


def main() -> None:
    raise SystemExit(run_manual_verification())


if __name__ == "__main__":
    main()
