from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    DEFAULT_CONVERSATION_STOP_PHRASES,
    MultiTurnVoiceSession,
    MultiTurnVoiceSessionRequestV1,
)
from scripts.manual_verify_single_turn_voice import (  # noqa: E402
    DEFAULT_MICROPHONE_DEVICE,
    DEFAULT_PIPELINE_TIMEOUT,
    DEFAULT_RECORD_SECONDS,
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
        cleanup_policy="keep" if args.keep_audio else "delete_on_success",
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
            output_func(f"Recognized: {payload.get('recognized_text') or '(none)'}")
            if payload.get("response_text"):
                output_func(f"ARES: {payload['response_text']}")
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
