from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from pathlib import Path
import sys
from typing import Any, Callable, Dict, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    DEFAULT_VOICE_PROFILE_CONFIG_PATH,
    SingleTurnVoiceRequestV1,
    SingleTurnVoiceResultV1,
    VoiceProfileError,
    load_voice_profile_registry,
)
from scripts import manual_verify_single_turn_voice as single_turn  # noqa: E402


DEFAULT_MICROPHONE_DEVICE = single_turn.DEFAULT_MICROPHONE_DEVICE
DEFAULT_SPEAKER_DEVICE = single_turn.DEFAULT_SPEAKER_DEVICE
DEFAULT_WHISPER_COMMAND = single_turn.DEFAULT_WHISPER_COMMAND
DEFAULT_WHISPER_MODEL = single_turn.DEFAULT_WHISPER_MODEL
DEFAULT_TIMEOUT_SECONDS = single_turn.DEFAULT_PIPELINE_TIMEOUT
DEFAULT_RECORD_SECONDS = single_turn.DEFAULT_RECORD_SECONDS

EXIT_SUCCESS = 0
EXIT_INPUT_REJECTED = 2
EXIT_DEPENDENCY_FAILURE = 3
EXIT_PIPELINE_FAILURE = 4
EXIT_CANCELLED = 130

_INPUT_REJECTION_STATUSES = frozenset(
    {
        "blank_transcription",
        "invalid_recording",
        "no_speech_timeout",
        "silent_audio",
        "transcript_rejected",
        "transcription_rejected",
    }
)
_ALL_DIAGNOSTIC_STAGES = (
    "raw capture",
    "assembled utterance",
    "normalized Whisper input",
)


@dataclass(frozen=True)
class DependencyIssue:
    component: str
    status: str
    reason: str


@dataclass(frozen=True)
class LauncherPreflightResult:
    success: bool
    issues: Tuple[DependencyIssue, ...] = ()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one owner-triggered local ARES voice interaction.",
    )
    parser.add_argument("--microphone-device", default=DEFAULT_MICROPHONE_DEVICE)
    parser.add_argument("--speaker-device", default=DEFAULT_SPEAKER_DEVICE)
    parser.add_argument("--language", default="en")
    parser.add_argument("--whisper-command", default=DEFAULT_WHISPER_COMMAND)
    parser.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL)
    parser.add_argument("--voice-profile", default=_configured_default_voice_profile())
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--fixed-duration", action="store_true")
    parser.add_argument("--record-seconds", type=int, default=DEFAULT_RECORD_SECONDS)
    parser.add_argument("--diagnostic-routing", action="store_true")
    parser.add_argument("--retain-diagnostic-audio", action="store_true")
    parser.add_argument("--play-diagnostic-audio", action="store_true")
    parser.add_argument("--no-playback", action="store_true")
    return parser


def run_ares_voice(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    pipeline: Any = None,
    pipeline_factory: Optional[Callable[..., Any]] = None,
) -> int:
    args = build_parser().parse_args(argv)
    manual_argv = _single_turn_arguments(args)
    manual_args = single_turn.build_parser().parse_args(manual_argv)
    request = single_turn.request_from_args(manual_args)
    request = replace(
        request,
        metadata={
            **dict(request.metadata),
            "source": "run_ares_voice",
            "owner_triggered": True,
            "interaction_mode": "single_turn",
        },
    )
    active_pipeline = pipeline
    if active_pipeline is None:
        factory = pipeline_factory or single_turn.create_pipeline
        try:
            active_pipeline = factory(manual_args, output_func=output_func)
        except Exception as error:
            output_func("ARES voice dependency setup failed before microphone capture.")
            output_func(
                "- single_turn_pipeline: construction_failed "
                f"({error.__class__.__name__}). "
                "Verify the local voice profile configuration and installed "
                "Whisper/Piper runtime paths."
            )
            return EXIT_DEPENDENCY_FAILURE

    preflight = _preflight_pipeline(active_pipeline, request)
    if not preflight.success:
        _print_preflight_failure(preflight, args, output_func)
        return EXIT_DEPENDENCY_FAILURE

    output_func("ARES is listening...")
    try:
        result = active_pipeline.run_once(request)
    except KeyboardInterrupt:
        active_pipeline.stop(request)
        output_func("ARES voice turn cancelled safely.")
        return EXIT_CANCELLED
    except Exception as error:
        active_pipeline.stop(request)
        output_func(
            "ARES voice pipeline failed safely: "
            f"{error.__class__.__name__}."
        )
        return EXIT_PIPELINE_FAILURE

    diagnostic_audio = bool(
        args.retain_diagnostic_audio or args.play_diagnostic_audio
    )
    if diagnostic_audio:
        transcript_path = single_turn._write_diagnostic_transcript(result)
        output_func(
            "Diagnostic transcript: "
            f"{transcript_path or '(not written)'}"
        )
    if args.diagnostic_routing:
        single_turn._print_routing_diagnostics(result, output_func)

    diagnostic_playback_success = True
    if args.play_diagnostic_audio:
        diagnostic_playback_success = single_turn._play_diagnostic_audio_stages(
            active_pipeline,
            result,
            args.speaker_device,
            args.timeout,
            output_func,
            _ALL_DIAGNOSTIC_STAGES,
        )

    _print_summary(result, output_func)
    if not diagnostic_playback_success:
        return EXIT_PIPELINE_FAILURE
    return _exit_code_for_result(result)


def _single_turn_arguments(args: argparse.Namespace) -> list[str]:
    values = [
        "--microphone-device",
        str(args.microphone_device),
        "--speaker-device",
        str(args.speaker_device),
        "--language",
        str(args.language),
        "--whisper-command",
        str(args.whisper_command),
        "--whisper-model",
        str(args.whisper_model),
        "--voice-profile",
        str(args.voice_profile),
        "--timeout",
        str(args.timeout),
        "--record-seconds",
        str(args.record_seconds),
        "--fixed-duration" if args.fixed_duration else "--auto-stop",
    ]
    if not args.no_playback:
        values.append("--playback")
    if args.diagnostic_routing:
        values.append("--diagnostic-routing")
    if args.retain_diagnostic_audio:
        values.append("--preserve-diagnostic-audio")
    if args.play_diagnostic_audio:
        values.append("--play-diagnostic-audio")
    return values


def _preflight_pipeline(
    pipeline: Any,
    request: SingleTurnVoiceRequestV1,
) -> LauncherPreflightResult:
    started = None
    health = None
    stopped = None
    try:
        started = pipeline.start(request)
        if not _result_success(started):
            return LauncherPreflightResult(
                False,
                (_issue_from_result("single_turn_pipeline", started),),
            )
        health = pipeline.health_check(request)
    except Exception as error:
        return LauncherPreflightResult(
            False,
            (
                DependencyIssue(
                    "single_turn_pipeline",
                    "preflight_exception",
                    error.__class__.__name__,
                ),
            ),
        )
    finally:
        if started is not None and _result_success(started):
            try:
                stopped = pipeline.stop(request)
            except Exception as error:
                stopped = {
                    "success": False,
                    "status": "preflight_cleanup_exception",
                    "error_message": error.__class__.__name__,
                }

    if not _result_success(health):
        issues = _component_issues(health)
        return LauncherPreflightResult(
            False,
            issues or (_issue_from_result("single_turn_pipeline", health),),
        )
    if not _result_success(stopped):
        return LauncherPreflightResult(
            False,
            (_issue_from_result("preflight_cleanup", stopped),),
        )
    return LauncherPreflightResult(True)


def _component_issues(health: Any) -> Tuple[DependencyIssue, ...]:
    payload = _result_mapping(health)
    external = dict(dict(payload.get("data") or {}).get("external_result") or {})
    components = dict(external.get("components") or {})
    issues = []
    for component in sorted(components):
        value = dict(components.get(component) or {})
        if bool(value.get("success")):
            continue
        issues.append(
            DependencyIssue(
                component=component,
                status=str(value.get("status") or "unavailable"),
                reason=str(
                    value.get("error_message")
                    or value.get("status")
                    or "dependency_unavailable"
                ),
            )
        )
    return tuple(issues)


def _issue_from_result(component: str, result: Any) -> DependencyIssue:
    payload = _result_mapping(result)
    return DependencyIssue(
        component=component,
        status=str(payload.get("status") or "unavailable"),
        reason=str(
            payload.get("error_message")
            or payload.get("status")
            or "dependency_unavailable"
        ),
    )


def _result_mapping(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return dict(payload) if isinstance(payload, dict) else {}
    return {
        "success": bool(getattr(result, "success", False)),
        "status": str(getattr(result, "status", "")),
        "error_message": str(getattr(result, "error_message", "")),
        "data": dict(getattr(result, "data", {}) or {}),
    }


def _result_success(result: Any) -> bool:
    return bool(_result_mapping(result).get("success"))


def _print_preflight_failure(
    preflight: LauncherPreflightResult,
    args: argparse.Namespace,
    output_func: Callable[[str], None],
) -> None:
    output_func("ARES voice dependency check failed before microphone capture.")
    for issue in preflight.issues:
        output_func(
            f"- {issue.component}: {issue.status} ({issue.reason}). "
            f"{_dependency_action(issue, args)}"
        )


def _dependency_action(issue: DependencyIssue, args: argparse.Namespace) -> str:
    detail = f"{issue.status} {issue.reason}".casefold()
    if issue.component == "speech_to_text":
        if "model" in detail:
            return f"Verify --whisper-model points to {args.whisper_model}."
        return f"Verify --whisper-command points to {args.whisper_command}."
    if issue.component == "text_to_speech":
        if "config" in detail:
            return (
                "Verify config/voice_profiles.json and the selected voice model "
                "configuration file."
            )
        if "model" in detail or "voice" in detail:
            return f"Install or verify voice profile {args.voice_profile}."
        return "Run scripts/install_piper_raspberry_pi.py and verify Piper locally."
    if issue.component == "microphone":
        return f"Verify arecord and ALSA capture device {args.microphone_device}."
    if issue.component == "speaker":
        return f"Verify aplay and ALSA speaker device {args.speaker_device}."
    if issue.component == "preflight_cleanup":
        return "Confirm no voice process is still active, then retry."
    return "Review the local dependency status and retry; no capture was started."


def _print_summary(
    result: SingleTurnVoiceResultV1,
    output_func: Callable[[str], None],
) -> None:
    output_func("")
    output_func("ARES voice turn complete")
    output_func(f"Recognized: {result.recognized_text or '(none)'}")
    output_func(f"Intent: {result.detected_intent or 'unknown'}")
    output_func(f"Skill: {result.routed_skill or 'none'}")
    output_func(f"ARES: {result.brain_text_response or '(no response)'}")
    output_func(f"Status: {result.status or 'failed'}")
    output_func(f"Total time: {result.total_processing_time_seconds:.3f}s")
    if result.error_reason:
        output_func(f"Failure: {result.error_stage}: {result.error_reason}")


def _exit_code_for_result(result: SingleTurnVoiceResultV1) -> int:
    if result.success:
        return EXIT_SUCCESS
    if result.status in _INPUT_REJECTION_STATUSES:
        return EXIT_INPUT_REJECTED
    if result.error_stage in {
        "recording_validation",
        "transcript_normalization",
        "transcription",
    }:
        return EXIT_INPUT_REJECTED
    return EXIT_PIPELINE_FAILURE


def _configured_default_voice_profile() -> str:
    try:
        registry = load_voice_profile_registry(
            DEFAULT_VOICE_PROFILE_CONFIG_PATH,
            project_root=REPO_ROOT,
        )
        return registry.default_profile().profile_id
    except (VoiceProfileError, OSError):
        return ""


def main() -> None:
    raise SystemExit(run_ares_voice())


if __name__ == "__main__":
    main()
