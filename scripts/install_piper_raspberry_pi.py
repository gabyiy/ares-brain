from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any, Callable, Dict, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import SafeProcessResult, SafeSubprocessRunner  # noqa: E402


DEFAULT_PIPER_RELEASE_URL = (
    "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/"
    "piper_linux_aarch64.tar.gz"
)
DEFAULT_PIPER_DIR = REPO_ROOT / "external" / "piper"
DEFAULT_VOICE_ID = "en_US-amy-low"
DEFAULT_VOICE_MODEL_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/"
    "en_US-amy-low.onnx"
)
DEFAULT_VOICE_CONFIG_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/"
    "en_US-amy-low.onnx.json"
)
DEFAULT_VOICE_MODEL_PATH = REPO_ROOT / "models" / "piper" / "en_US-amy-low.onnx"
DEFAULT_VOICE_CONFIG_PATH = REPO_ROOT / "models" / "piper" / "en_US-amy-low.onnx.json"
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 1800.0

WARNING = (
    "WARNING: This setup script is for explicit Raspberry Pi preparation only. "
    "It may download a local Piper runtime and an offline voice model when run. "
    "It does not start playback, wake words, background listening, GPT, or a "
    "conversation loop."
)


@dataclass(frozen=True)
class PiperInstallResult:
    success: bool
    status: str
    message: str
    executable_path: str = ""
    model_path: str = ""
    config_path: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "executable_path": self.executable_path,
            "model_path": self.model_path,
            "config_path": self.config_path,
            "data": dict(self.data),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Piper and a small English voice model for Raspberry Pi."
    )
    parser.add_argument(
        "--piper-dir",
        default=str(DEFAULT_PIPER_DIR),
        help="Local Piper runtime directory.",
    )
    parser.add_argument(
        "--piper-release-url",
        default=DEFAULT_PIPER_RELEASE_URL,
        help="Piper Linux ARM64 release archive URL.",
    )
    parser.add_argument(
        "--voice-id",
        default=DEFAULT_VOICE_ID,
        help="Voice identifier used for diagnostics.",
    )
    parser.add_argument(
        "--voice-model-url",
        default=DEFAULT_VOICE_MODEL_URL,
        help="Piper ONNX voice model URL.",
    )
    parser.add_argument(
        "--voice-config-url",
        default=DEFAULT_VOICE_CONFIG_URL,
        help="Piper voice configuration JSON URL.",
    )
    parser.add_argument(
        "--voice-model-output",
        default=str(DEFAULT_VOICE_MODEL_PATH),
        help="ARES-local path for the downloaded ONNX voice model.",
    )
    parser.add_argument(
        "--voice-config-output",
        default=str(DEFAULT_VOICE_CONFIG_PATH),
        help="ARES-local path for the downloaded voice configuration JSON.",
    )
    parser.add_argument(
        "--download-timeout",
        type=float,
        default=DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        help="Timeout for download and extraction subprocesses.",
    )
    return parser


def run_installation(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    runner: Optional[SafeSubprocessRunner] = None,
) -> int:
    args = build_parser().parse_args(list(argv or []))
    result = install_piper(
        piper_dir=Path(args.piper_dir).expanduser(),
        piper_release_url=str(args.piper_release_url),
        voice_id=str(args.voice_id),
        voice_model_url=str(args.voice_model_url),
        voice_config_url=str(args.voice_config_url),
        voice_model_output_path=Path(args.voice_model_output).expanduser(),
        voice_config_output_path=Path(args.voice_config_output).expanduser(),
        download_timeout_seconds=args.download_timeout,
        output_func=output_func,
        runner=runner,
    )
    output_func(f"{'PASS' if result.success else 'FAIL'}: {result.message}")
    if result.executable_path:
        output_func(f"piper: {result.executable_path}")
    if result.model_path:
        output_func(f"voice model: {result.model_path}")
    if result.config_path:
        output_func(f"voice config: {result.config_path}")
    return 0 if result.success else 1


def install_piper(
    piper_dir: Path = DEFAULT_PIPER_DIR,
    piper_release_url: str = DEFAULT_PIPER_RELEASE_URL,
    voice_id: str = DEFAULT_VOICE_ID,
    voice_model_url: str = DEFAULT_VOICE_MODEL_URL,
    voice_config_url: str = DEFAULT_VOICE_CONFIG_URL,
    voice_model_output_path: Path = DEFAULT_VOICE_MODEL_PATH,
    voice_config_output_path: Path = DEFAULT_VOICE_CONFIG_PATH,
    download_timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    output_func: Callable[[str], None] = print,
    runner: Optional[SafeSubprocessRunner] = None,
) -> PiperInstallResult:
    runner = runner or SafeSubprocessRunner()
    piper_dir = piper_dir.expanduser()
    voice_model_output_path = voice_model_output_path.expanduser()
    voice_config_output_path = voice_config_output_path.expanduser()
    timeout_seconds = _positive_timeout(download_timeout_seconds, "download_timeout_seconds")
    voice_id = str(voice_id or DEFAULT_VOICE_ID).strip()
    if not voice_id:
        return _failure("invalid_voice_id", "Voice id cannot be empty.")

    output_func(WARNING)
    dependency = _check_install_dependencies(runner)
    if not dependency.success:
        return dependency

    runtime = _ensure_piper_runtime(
        piper_dir=piper_dir,
        piper_release_url=str(piper_release_url),
        timeout_seconds=timeout_seconds,
        runner=runner,
        output_func=output_func,
    )
    if not runtime.success:
        return runtime

    model = _download_file_if_missing(
        url=str(voice_model_url),
        output_path=voice_model_output_path,
        timeout_seconds=timeout_seconds,
        runner=runner,
        output_func=output_func,
        status_prefix="voice_model",
    )
    if not model.success:
        return model

    config = _download_file_if_missing(
        url=str(voice_config_url),
        output_path=voice_config_output_path,
        timeout_seconds=timeout_seconds,
        runner=runner,
        output_func=output_func,
        status_prefix="voice_config",
    )
    if not config.success:
        return config

    executable_path = _find_piper_executable(piper_dir)
    if not executable_path:
        return _failure(
            "piper_missing_after_install",
            "Piper runtime setup finished but piper executable was not found.",
            data={"piper_dir": str(piper_dir)},
        )
    if not _file_exists_nonempty(voice_model_output_path):
        return _failure(
            "voice_model_missing_after_download",
            "Voice model download finished but the model was not found.",
            executable_path=str(executable_path),
            model_path=str(voice_model_output_path),
        )
    if not _file_exists_nonempty(voice_config_output_path):
        return _failure(
            "voice_config_missing_after_download",
            "Voice config download finished but the config was not found.",
            executable_path=str(executable_path),
            model_path=str(voice_model_output_path),
            config_path=str(voice_config_output_path),
        )

    return PiperInstallResult(
        success=True,
        status="installed",
        message="Piper runtime and offline voice model are available.",
        executable_path=str(executable_path),
        model_path=str(voice_model_output_path),
        config_path=str(voice_config_output_path),
        data={
            "piper_dir": str(piper_dir),
            "voice_id": voice_id,
            "piper_release_url": str(piper_release_url),
        },
    )


def _check_install_dependencies(runner: SafeSubprocessRunner) -> PiperInstallResult:
    missing = [
        executable
        for executable in ("curl", "tar")
        if not runner.which(executable)
    ]
    if missing:
        return _failure(
            "missing_install_dependency",
            "Missing required setup command(s): " + ", ".join(missing),
            data={"missing": missing},
        )
    return PiperInstallResult(
        success=True,
        status="dependencies_available",
        message="Required setup commands are available.",
    )


def _ensure_piper_runtime(
    piper_dir: Path,
    piper_release_url: str,
    timeout_seconds: float,
    runner: SafeSubprocessRunner,
    output_func: Callable[[str], None],
) -> PiperInstallResult:
    existing = _find_piper_executable(piper_dir)
    if existing:
        output_func(f"Using existing Piper runtime: {existing}")
        return PiperInstallResult(
            success=True,
            status="runtime_exists",
            message="Piper runtime already exists.",
            executable_path=str(existing),
        )

    piper_dir.mkdir(parents=True, exist_ok=True)
    archive_path = piper_dir / "piper_linux_aarch64.tar.gz"
    output_func(f"Downloading Piper runtime into {archive_path}...")
    download = runner.run(
        ["curl", "-L", "--fail", "-o", str(archive_path), str(piper_release_url)],
        timeout_seconds=timeout_seconds,
    )
    if download.timed_out:
        return _failure("runtime_download_timeout", "Timed out downloading Piper.", download)
    if download.returncode != 0:
        return _failure("runtime_download_failed", "Piper runtime download failed.", download)
    if not _file_exists_nonempty(archive_path):
        return _failure(
            "runtime_archive_missing",
            "Piper archive download completed but the archive is missing or empty.",
            data={"archive_path": str(archive_path), "process": _safe_process_data(download)},
        )

    output_func("Extracting Piper runtime...")
    extract = runner.run(
        ["tar", "-xzf", str(archive_path), "-C", str(piper_dir)],
        timeout_seconds=timeout_seconds,
    )
    if extract.timed_out:
        return _failure("runtime_extract_timeout", "Timed out extracting Piper.", extract)
    if extract.returncode != 0:
        return _failure("runtime_extract_failed", "Piper runtime extraction failed.", extract)

    executable = _find_piper_executable(piper_dir)
    if not executable:
        return _failure(
            "runtime_executable_missing",
            "Piper archive extracted but no piper executable was found.",
            data={
                "piper_dir": str(piper_dir),
                "download": _safe_process_data(download),
                "extract": _safe_process_data(extract),
            },
        )
    return PiperInstallResult(
        success=True,
        status="runtime_installed",
        message="Piper runtime installed.",
        executable_path=str(executable),
        data={"download": _safe_process_data(download), "extract": _safe_process_data(extract)},
    )


def _download_file_if_missing(
    url: str,
    output_path: Path,
    timeout_seconds: float,
    runner: SafeSubprocessRunner,
    output_func: Callable[[str], None],
    status_prefix: str,
) -> PiperInstallResult:
    if _file_exists_nonempty(output_path):
        output_func(f"Using existing {status_prefix.replace('_', ' ')}: {output_path}")
        return PiperInstallResult(
            success=True,
            status=f"{status_prefix}_exists",
            message=f"{status_prefix.replace('_', ' ').title()} already exists.",
            model_path=str(output_path) if status_prefix == "voice_model" else "",
            config_path=str(output_path) if status_prefix == "voice_config" else "",
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_func(f"Downloading {status_prefix.replace('_', ' ')} into {output_path}...")
    result = runner.run(
        ["curl", "-L", "--fail", "-o", str(output_path), str(url)],
        timeout_seconds=timeout_seconds,
    )
    if result.timed_out:
        return _failure(
            f"{status_prefix}_download_timeout",
            f"Timed out downloading {status_prefix.replace('_', ' ')}.",
            result,
        )
    if result.returncode != 0:
        return _failure(
            f"{status_prefix}_download_failed",
            f"{status_prefix.replace('_', ' ').title()} download failed.",
            result,
        )
    if not _file_exists_nonempty(output_path):
        return _failure(
            f"{status_prefix}_missing_after_download",
            f"{status_prefix.replace('_', ' ').title()} was not found after download.",
            data={"output_path": str(output_path), "process": _safe_process_data(result)},
        )
    return PiperInstallResult(
        success=True,
        status=f"{status_prefix}_downloaded",
        message=f"{status_prefix.replace('_', ' ').title()} downloaded.",
        model_path=str(output_path) if status_prefix == "voice_model" else "",
        config_path=str(output_path) if status_prefix == "voice_config" else "",
        data={"process": _safe_process_data(result)},
    )


def _find_piper_executable(piper_dir: Path) -> Optional[Path]:
    candidates = [
        piper_dir / "piper",
        piper_dir / "piper" / "piper",
        piper_dir / "piper.exe",
        piper_dir / "piper" / "piper.exe",
    ]
    for candidate in candidates:
        if _file_exists_nonempty(candidate):
            return candidate
    if piper_dir.exists():
        for candidate in sorted(piper_dir.rglob("piper")):
            if _file_exists_nonempty(candidate):
                return candidate
    return None


def _file_exists_nonempty(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _positive_timeout(value: Any, name: str) -> float:
    timeout = float(value)
    if timeout <= 0 or timeout > 7200:
        raise ValueError(f"{name} must be > 0 and <= 7200")
    return timeout


def _failure(
    status: str,
    message: str,
    process: Optional[SafeProcessResult] = None,
    executable_path: str = "",
    model_path: str = "",
    config_path: str = "",
    data: Optional[Dict[str, Any]] = None,
) -> PiperInstallResult:
    payload = dict(data or {})
    if process is not None:
        payload["process"] = _safe_process_data(process)
    return PiperInstallResult(
        success=False,
        status=status,
        message=message,
        executable_path=executable_path,
        model_path=model_path,
        config_path=config_path,
        data=payload,
    )


def _safe_process_data(result: SafeProcessResult) -> Dict[str, Any]:
    return {
        "args": list(result.args),
        "returncode": result.returncode,
        "stdout_preview": str(result.stdout or "")[:500],
        "stderr_preview": str(result.stderr or "")[:500],
        "timed_out": result.timed_out,
        "error_message": result.error_message,
    }


def main() -> None:
    raise SystemExit(run_installation(sys.argv[1:]))


if __name__ == "__main__":
    main()
