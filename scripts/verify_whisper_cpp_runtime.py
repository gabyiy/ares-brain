from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import os
import sys
from typing import Any, Callable, Dict, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    DEFAULT_WHISPER_MODEL_PATH,
    LinuxWhisperSpeechToTextAdapter,
    SafeSubprocessRunner,
)


DEFAULT_WHISPER_CPP_DIR = REPO_ROOT / "external" / "whisper.cpp"
DEFAULT_SAMPLE_DIRS = (
    REPO_ROOT / "data" / "manual_alsa_samples",
    REPO_ROOT / "data" / "manual_whisper_samples",
)

WARNING = (
    "WARNING: This verifier transcribes an existing local WAV sample only. It "
    "does not record audio, start wake word detection, run background listening, "
    "call GPT, access the internet, run TTS, or start a conversation loop."
)


@dataclass(frozen=True)
class WhisperRuntimeVerificationResult:
    success: bool
    status: str
    message: str
    whisper_command: str = ""
    model_path: str = ""
    wav_path: str = ""
    transcription_text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "whisper_command": self.whisper_command,
            "model_path": self.model_path,
            "wav_path": self.wav_path,
            "transcription_text": self.transcription_text,
            "data": dict(self.data),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a local whisper.cpp runtime against a recorded WAV sample."
    )
    parser.add_argument(
        "--whisper-command",
        default="",
        help="Optional whisper-cli command or path. Auto-located when omitted.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Optional GGML model path. Defaults to ARES_WHISPER_MODEL_PATH or models/whisper/ggml-tiny.en.bin.",
    )
    parser.add_argument(
        "--wav",
        default="",
        help="Optional recorded WAV sample. Defaults to the newest manual ALSA/Whisper sample.",
    )
    parser.add_argument("--language", default="auto", help="Whisper language, or auto.")
    parser.add_argument("--timeout", type=float, default=120.0, help="Transcription timeout.")
    return parser


def run_runtime_verification(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    runner: Optional[SafeSubprocessRunner] = None,
    stt_factory=LinuxWhisperSpeechToTextAdapter,
) -> int:
    args = build_parser().parse_args(list(argv or []))
    result = verify_whisper_runtime(
        whisper_command=args.whisper_command,
        model_path=args.model,
        wav_path=args.wav,
        language=args.language,
        timeout_seconds=args.timeout,
        output_func=output_func,
        runner=runner,
        stt_factory=stt_factory,
    )
    output_func(f"{'PASS' if result.success else 'FAIL'}: {result.message}")
    if result.whisper_command:
        output_func(f"whisper-cli: {result.whisper_command}")
    if result.model_path:
        output_func(f"model: {result.model_path}")
    if result.wav_path:
        output_func(f"wav: {result.wav_path}")
    if result.success:
        output_func(f"Recognized text: {result.transcription_text}")
        output_func(
            "Processing time: "
            f"{result.data.get('processing_time_seconds', 0.0)} seconds"
        )
    return 0 if result.success else 1


def verify_whisper_runtime(
    whisper_command: str = "",
    model_path: str | Path = "",
    wav_path: str | Path = "",
    language: str = "auto",
    timeout_seconds: float = 120.0,
    output_func: Callable[[str], None] = print,
    runner: Optional[SafeSubprocessRunner] = None,
    stt_factory=LinuxWhisperSpeechToTextAdapter,
) -> WhisperRuntimeVerificationResult:
    runner = runner or SafeSubprocessRunner()
    output_func(WARNING)

    located_command = locate_whisper_cli(whisper_command, runner=runner)
    if not located_command:
        return _failure(
            "whisper_cli_missing",
            "Could not locate whisper-cli. Run scripts/install_whisper_cpp_raspberry_pi.py first.",
        )

    located_model = locate_whisper_model(model_path)
    if not located_model:
        return _failure(
            "model_missing",
            "Could not locate GGML model. Run scripts/install_whisper_cpp_raspberry_pi.py first.",
            whisper_command=located_command,
        )

    located_wav = locate_wav_sample(wav_path)
    if not located_wav:
        return _failure(
            "wav_sample_missing",
            "Could not locate a recorded WAV sample. Run manual ALSA recording first or pass --wav.",
            whisper_command=located_command,
            model_path=str(located_model),
        )

    output_func("Running offline Whisper transcription...")
    stt = stt_factory(
        model_path=located_model,
        whisper_command=located_command,
        language=language,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    transcription = stt.transcribe_wav(located_wav, timeout_seconds=timeout_seconds)
    if not transcription.success:
        return _failure(
            "transcription_failed",
            transcription.error_message or transcription.status,
            whisper_command=located_command,
            model_path=str(located_model),
            wav_path=str(located_wav),
            data={"transcription": transcription.to_dict()},
        )
    if not transcription.text:
        return _failure(
            "empty_transcription",
            "Whisper ran but produced no transcription text.",
            whisper_command=located_command,
            model_path=str(located_model),
            wav_path=str(located_wav),
            data={"transcription": transcription.to_dict()},
        )
    return WhisperRuntimeVerificationResult(
        success=True,
        status="transcribed",
        message="Offline Whisper runtime transcribed the recorded WAV sample.",
        whisper_command=located_command,
        model_path=str(located_model),
        wav_path=str(located_wav),
        transcription_text=transcription.text,
        data={
            "processing_time_seconds": transcription.data.get("processing_time_seconds", 0.0),
            "language": transcription.data.get("language")
            or transcription.data.get("language_requested", ""),
            "transcription": transcription.to_dict(),
        },
    )


def locate_whisper_cli(
    requested_command: str = "",
    runner: Optional[SafeSubprocessRunner] = None,
) -> str:
    runner = runner or SafeSubprocessRunner()
    candidates = [
        str(requested_command or "").strip(),
        str(os.environ.get("ARES_WHISPER_COMMAND", "")).strip(),
        str(DEFAULT_WHISPER_CPP_DIR / "build" / "bin" / "whisper-cli"),
        str(DEFAULT_WHISPER_CPP_DIR / "build" / "bin" / "Release" / "whisper-cli"),
        str(DEFAULT_WHISPER_CPP_DIR / "build" / "bin" / "Release" / "whisper-cli.exe"),
        "whisper-cli",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        found = runner.which(candidate)
        if found:
            return str(found)
        path = Path(candidate).expanduser()
        if _file_exists_nonempty(path):
            return str(path)
    return ""


def locate_whisper_model(model_path: str | Path = "") -> Optional[Path]:
    candidates = [
        Path(str(model_path)).expanduser() if str(model_path or "").strip() else None,
        Path(os.environ["ARES_WHISPER_MODEL_PATH"]).expanduser()
        if os.environ.get("ARES_WHISPER_MODEL_PATH")
        else None,
        REPO_ROOT / DEFAULT_WHISPER_MODEL_PATH,
    ]
    for candidate in candidates:
        if candidate and _file_exists_nonempty(candidate):
            return candidate
    return None


def locate_wav_sample(wav_path: str | Path = "") -> Optional[Path]:
    requested = Path(str(wav_path)).expanduser() if str(wav_path or "").strip() else None
    if requested and _file_exists_nonempty(requested):
        return requested

    candidates = []
    for directory in DEFAULT_SAMPLE_DIRS:
        if directory.exists():
            candidates.extend(path for path in directory.glob("*.wav") if path.is_file())
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def _file_exists_nonempty(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _failure(
    status: str,
    message: str,
    whisper_command: str = "",
    model_path: str = "",
    wav_path: str = "",
    data: Optional[Dict[str, Any]] = None,
) -> WhisperRuntimeVerificationResult:
    return WhisperRuntimeVerificationResult(
        success=False,
        status=status,
        message=message,
        whisper_command=whisper_command,
        model_path=model_path,
        wav_path=wav_path,
        data=dict(data or {}),
    )


def main() -> None:
    raise SystemExit(run_runtime_verification(sys.argv[1:]))


if __name__ == "__main__":
    main()
