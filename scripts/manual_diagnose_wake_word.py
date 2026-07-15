from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    LinuxAlsaMicrophoneAdapter,
    LinuxStandbyWakeListener,
    VOSK_MODEL_INSTALL_COMMAND,
    VoskWakeRecognizer,
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
    parser.add_argument("--vosk-model", default=defaults.vosk_model_path)
    parser.add_argument(
        "--wake-min-confidence",
        type=float,
        default=defaults.minimum_recognition_confidence,
    )
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
        vosk_model_path=str(standby_voice._repo_path(args.vosk_model)),
        minimum_recognition_confidence=args.wake_min_confidence,
        language=args.language,
        diagnostic_wake=True,
        retain_diagnostic_audio=bool(args.retain_diagnostic_audio),
        diagnostic_output_directory="data/runtime/wake_audio",
    )
    microphone = LinuxAlsaMicrophoneAdapter(
        device=args.microphone_device,
        record_seconds=config.maximum_utterance_seconds,
        timeout_seconds=min(args.timeout, 30.0),
    )
    wake_recognizer = VoskWakeRecognizer(
        model_path=standby_voice._repo_path(args.vosk_model),
        minimum_confidence=args.wake_min_confidence,
    )
    callback = standby_voice._wake_diagnostic_callback(output_func, args.speaker_device)
    factory = listener_factory or LinuxStandbyWakeListener
    listener = factory(
        microphone_adapter=microphone,
        wake_recognizer=wake_recognizer,
        config=config,
        project_root=REPO_ROOT,
        diagnostic_callback=callback,
    )
    output_func("Say Ares once, then remain silent.")
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
                f"({result.rejection_reason or result.stop_reason or 'wake phrase not matched'})."
            )
            diagnostics = getattr(listener, "last_diagnostics", None)
            output_func(
                "Confidence check: "
                + (
                    f"{result.recognition_confidence:.3f}"
                    if result.recognition_confidence_available
                    and result.recognition_confidence is not None
                    else "unavailable"
                )
            )
            output_func(
                "Suggested action: "
                + _rejection_suggestion(
                    result.rejection_reason,
                    normalized_transcript=str(
                        getattr(diagnostics, "normalized_transcript", "") or ""
                    ),
                )
            )
        else:
            output_func(
                f"Wake result: failed ({result.error_code or result.stop_reason or result.status})."
            )
        return 1
    finally:
        listener.stop("wake_diagnostic_complete")


def _validate_dependencies(args: argparse.Namespace) -> str:
    if importlib.util.find_spec("vosk") is None:
        return "Vosk is not installed. Run: python -m pip install -r requirements.txt"
    model = standby_voice._repo_path(args.vosk_model)
    if not model.is_dir():
        return (
            f"Vosk wake model is missing: {model}. Recommended Raspberry Pi model: "
            "vosk-model-small-en-us-0.15. Install it with: "
            f"{VOSK_MODEL_INSTALL_COMMAND}"
        )
    return ""


def _rejection_suggestion(reason: str, *, normalized_transcript: str = "") -> str:
    suggestions = {
        "unknown_token_result": (
            "say only 'Ares' once, then remain silent; [unk] and unrelated words are rejected"
        ),
        "exact_constrained_phrase_not_matched": (
            "say one configured phrase: 'Ares', 'Hey Ares', or 'Okay Ares'"
        ),
        "wake_confidence_below_threshold": (
            "speak clearly and closer to the microphone; confidence was below the safe threshold"
        ),
        "missing_word_confidence": (
            "the recognizer returned no usable word confidence, so activation was refused"
        ),
        "wake_alias_missing": "say the configured wake name 'Ares' once",
        "empty_wake_transcript": "speak closer to the microphone and say 'Ares' once",
    }
    suggestion = suggestions.get(
        str(reason or ""),
        "say 'Ares' once, then remain silent, and review the transcript above",
    )
    if normalized_transcript:
        return f"{suggestion}."
    return f"{suggestion}; no usable normalized transcript was available."


def main() -> int:
    return run_diagnostic()


if __name__ == "__main__":
    raise SystemExit(main())
