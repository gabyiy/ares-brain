from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    CoreService,
    LinuxAlsaMicrophoneAdapter,
    LinuxWhisperSpeechToTextAdapter,
    TranscriptionResult,
    VoiceCommandRouter,
    normalize_transcript,
)
from scripts.manual_verify_single_turn_voice import (  # noqa: E402
    DEFAULT_FRAME_MS,
    DEFAULT_CALIBRATION_SECONDS,
    DEFAULT_MAX_UTTERANCE_SECONDS,
    DEFAULT_MICROPHONE_DEVICE,
    DEFAULT_PRE_ROLL_SECONDS,
    DEFAULT_REQUIRED_SPEECH_FRAMES,
    DEFAULT_REQUIRED_CONTINUE_FRAMES,
    DEFAULT_REQUIRED_SILENCE_FRAMES,
    DEFAULT_SILENCE_RMS,
    DEFAULT_SILENCE_SECONDS,
    DEFAULT_SPEECH_START_RMS,
    DEFAULT_SPEECH_CONTINUE_RMS,
    DEFAULT_SPEECH_WAIT_TIMEOUT,
    DEFAULT_WHISPER_COMMAND,
    DEFAULT_WHISPER_MODEL,
    build_existing_brain_handler,
    create_skill_manager,
)


DEFAULT_OUTPUT = "data/manual_voice_samples/vad_calibration.wav"

WARNING = (
    "WARNING: This performs one foreground ALSA microphone calibration capture. "
    "Whisper and Brain routing run only with --transcribe/--route. It never starts "
    "TTS, playback, wake words, or background listening."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate bounded PCM-RMS voice activity capture on Raspberry Pi."
    )
    parser.add_argument("--microphone-device", default=DEFAULT_MICROPHONE_DEVICE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
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
    parser.add_argument("--transcribe", action="store_true")
    parser.add_argument("--route", action="store_true")
    parser.add_argument("--language", default="en")
    parser.add_argument("--whisper-command", default=DEFAULT_WHISPER_COMMAND)
    parser.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--verbose", action="store_true")
    return parser


def run_manual_verification(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    adapter: Optional[LinuxAlsaMicrophoneAdapter] = None,
    speech_to_text_adapter: Optional[LinuxWhisperSpeechToTextAdapter] = None,
    command_router: Optional[VoiceCommandRouter] = None,
) -> int:
    args = build_parser().parse_args(argv)
    output_func(WARNING)
    active_adapter = adapter or LinuxAlsaMicrophoneAdapter(device=args.microphone_device)
    devices = active_adapter.list_capture_devices()
    output_func(f"Capture device: {args.microphone_device}")
    output_func(f"Device discovery: {devices.status}")
    health = active_adapter.health_check()
    output_func(f"Microphone health: {health.status}")
    if not health.success:
        output_func(f"FAIL: {health.error_message or health.status}")
        return 2

    start = active_adapter.start()
    if not start.success:
        output_func(f"FAIL: {start.error_message or start.status}")
        return 2
    try:
        result = active_adapter.record_until_silence(
            _repo_path(args.output),
            device=args.microphone_device,
            calibration_enabled=bool(args.calibration_enabled),
            calibration_duration_seconds=args.calibration_seconds,
            speech_start_rms=args.speech_start_rms,
            speech_continue_rms=args.speech_continue_rms,
            silence_rms=args.silence_rms,
            required_speech_frames=args.required_speech_frames,
            required_continue_frames=args.required_continue_frames,
            required_silence_frames=args.required_silence_frames,
            silence_seconds=args.silence_seconds,
            speech_wait_timeout_seconds=args.speech_wait_timeout,
            maximum_utterance_seconds=args.max_utterance_seconds,
            pre_roll_seconds=args.pre_roll_seconds,
            frame_duration_ms=args.frame_ms,
            frame_debug_enabled=bool(args.frame_debug),
            diagnostic_audio=bool(args.diagnostic_audio),
        )
    except KeyboardInterrupt:
        active_adapter.cancel_current()
        output_func("Voice activity capture cancelled safely.")
        return 130
    finally:
        active_adapter.stop()

    process = dict(result.data.get("process") or {})
    output_func(f"arecord command: {_format_command(process.get('args') or [])}")
    output_func(f"Capture status: {result.status}")
    output_func(f"Requested microphone device: {result.requested_device or args.microphone_device}")
    output_func(f"Resolved capture device: {result.resolved_capture_device or result.selected_device}")
    output_func(f"Requested sample rate: {result.requested_sample_rate_hz} Hz")
    output_func(f"Actual captured sample rate: {result.actual_sample_rate_hz} Hz")
    output_func(f"Actual captured channels: {result.actual_channels}")
    output_func(f"Actual captured sample width: {result.actual_sample_width_bytes} bytes")
    output_func(f"Normalized sample rate: {result.normalized_sample_rate_hz} Hz")
    output_func(f"Normalized channels: {result.normalized_channels}")
    output_func(f"Normalized sample width: {result.normalized_sample_width_bytes} bytes")
    output_func(f"Raw WAV path: {result.raw_wav_path or '(not retained)'}")
    output_func(f"Normalized WAV path: {result.normalized_wav_path or result.wav_path}")
    output_func(f"Raw duration: {result.raw_duration_seconds:.3f}s")
    output_func(f"Normalized duration: {result.normalized_duration_seconds:.3f}s")
    output_func(
        f"Final Whisper input path: {result.final_whisper_input_path or result.wav_path}"
    )
    output_func(f"Stop reason: {result.stop_reason}")
    output_func(f"Calibration enabled: {result.calibration_enabled}")
    output_func(f"Calibration duration: {result.calibration_duration_seconds:.3f}s")
    output_func(f"Ambient RMS: {result.ambient_rms:.3f}")
    output_func(
        "Ambient statistics: "
        f"mean={result.ambient_rms_mean:.3f}, median={result.ambient_rms_median:.3f}, "
        f"p90={result.ambient_rms_percentile:.3f}, peak={result.ambient_rms_peak:.3f}, "
        f"noise_floor={result.ambient_noise_floor:.3f}"
    )
    output_func(f"Speech RMS: {result.speech_rms:.3f}")
    output_func(f"Peak amplitude: {result.peak_amplitude}")
    output_func(
        "Selected thresholds: "
        f"start={result.derived_speech_start_rms or args.speech_start_rms}, "
        f"continue={result.derived_speech_continue_rms or args.speech_continue_rms}, "
        f"silence={result.derived_silence_rms or args.silence_rms}"
    )
    output_func(f"Speech frames: {result.speech_frame_count}")
    output_func(f"Trailing-silence frames: {result.trailing_silence_frame_count}")
    output_func(f"Captured duration: {result.duration_seconds:.3f}s")
    output_func(f"Speech duration estimate: {result.speech_duration_seconds:.3f}s")
    output_func(f"WAV path: {result.wav_path or '(none)'}")
    if result.error_message:
        output_func(f"Failure: {result.error_message}")
    if args.frame_debug:
        for transition in result.data.get("transitions", []):
            output_func(
                f"{transition.get('from')} -> {transition.get('to')} "
                f"(frame={transition.get('frame')}, rms={transition.get('rms')})"
            )
    if args.verbose:
        output_func(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if result.success and (args.transcribe or args.route):
        stt = speech_to_text_adapter or LinuxWhisperSpeechToTextAdapter(
            model_path=_repo_path(args.whisper_model),
            whisper_command=_repo_path_or_command(args.whisper_command),
            language=args.language,
            timeout_seconds=args.timeout,
        )
        stt_health = stt.health_check()
        output_func(f"Whisper health: {stt_health.status}")
        if not stt_health.success:
            output_func(f"FAIL: {stt_health.error_message or stt_health.status}")
            return 2
        transcription = stt.transcribe_wav(
            result.wav_path,
            language=args.language,
            timeout_seconds=args.timeout,
        )
        output_func(f"Raw transcript: {transcription.text or '(none)'}")
        if not transcription.success:
            output_func(f"FAIL: {transcription.error_message or transcription.status}")
            return 2
        normalization = normalize_transcript(transcription.text)
        output_func(f"Cleaned transcript: {normalization.cleaned_transcript or '(none)'}")
        output_func(f"Normalized command: {normalization.normalized_command or '(none)'}")
        if not normalization.success:
            output_func(f"FAIL: {normalization.rejection_reason}")
            return 2
        if args.route:
            router = command_router
            if router is None:
                core_service = CoreService()
                manager = create_skill_manager(core_service)
                router = VoiceCommandRouter(
                    command_handler=build_existing_brain_handler(manager),
                    core_service=core_service,
                )
            routing = router.route(
                TranscriptionResult(
                    success=True,
                    status="normalized_transcription",
                    text=normalization.normalized_command,
                    confidence=transcription.confidence,
                )
            )
            output_func(f"Brain route status: {routing.status}")
            output_func(f"ARES response: {routing.response_text or '(none)'}")
            if not routing.success:
                output_func(f"FAIL: {routing.error_message or routing.status}")
                return 2
    output_func("PASS" if result.success else "FAIL")
    return 0 if result.success else 2


def _repo_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def _repo_path_or_command(value: str) -> str:
    clean = str(value or "").strip()
    if not clean or ("/" not in clean and "\\" not in clean):
        return clean
    return str(_repo_path(clean))


def _format_command(args: Sequence[str]) -> str:
    return " ".join(str(value) for value in args) if args else "(not started)"


def main() -> None:
    raise SystemExit(run_manual_verification())


if __name__ == "__main__":
    main()
