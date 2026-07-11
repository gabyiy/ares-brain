from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import shutil
import sys
from typing import Any, Callable, Dict, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import SafeProcessResult, SafeSubprocessRunner  # noqa: E402


DEFAULT_WHISPER_CPP_REPO_URL = "https://github.com/ggml-org/whisper.cpp.git"
DEFAULT_WHISPER_CPP_DIR = REPO_ROOT / "external" / "whisper.cpp"
DEFAULT_MODEL_NAME = "tiny.en"
DEFAULT_MODEL_FILENAME = "ggml-tiny.en.bin"
DEFAULT_MODEL_PATH = REPO_ROOT / "models" / "whisper" / DEFAULT_MODEL_FILENAME
DEFAULT_BUILD_TIMEOUT_SECONDS = 1800.0
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 1800.0

WARNING = (
    "WARNING: This setup script is for explicit Raspberry Pi preparation only. "
    "It may clone whisper.cpp and download a local GGML model when run. It does "
    "not start wake word detection, background listening, GPT, TTS, or a "
    "conversation loop."
)


@dataclass(frozen=True)
class WhisperCppInstallResult:
    success: bool
    status: str
    message: str
    executable_path: str = ""
    model_path: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "executable_path": self.executable_path,
            "model_path": self.model_path,
            "data": dict(self.data),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install whisper.cpp and a tiny English GGML model for Raspberry Pi."
    )
    parser.add_argument(
        "--repo-dir",
        default=str(DEFAULT_WHISPER_CPP_DIR),
        help="Local whisper.cpp checkout directory.",
    )
    parser.add_argument(
        "--repo-url",
        default=DEFAULT_WHISPER_CPP_REPO_URL,
        help="whisper.cpp Git repository URL.",
    )
    parser.add_argument(
        "--model-name",
        default=DEFAULT_MODEL_NAME,
        help="Model name passed to models/download-ggml-model.sh.",
    )
    parser.add_argument(
        "--model-output",
        default=str(DEFAULT_MODEL_PATH),
        help="ARES-local path for the downloaded GGML model.",
    )
    parser.add_argument(
        "--build-timeout",
        type=float,
        default=DEFAULT_BUILD_TIMEOUT_SECONDS,
        help="Timeout for clone/build subprocesses.",
    )
    parser.add_argument(
        "--download-timeout",
        type=float,
        default=DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        help="Timeout for the model download subprocess.",
    )
    return parser


def run_installation(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    runner: Optional[SafeSubprocessRunner] = None,
) -> int:
    args = build_parser().parse_args(list(argv or []))
    result = install_whisper_cpp(
        repo_dir=Path(args.repo_dir).expanduser(),
        repo_url=args.repo_url,
        model_name=args.model_name,
        model_output_path=Path(args.model_output).expanduser(),
        build_timeout_seconds=args.build_timeout,
        download_timeout_seconds=args.download_timeout,
        output_func=output_func,
        runner=runner,
    )
    output_func(f"{'PASS' if result.success else 'FAIL'}: {result.message}")
    if result.executable_path:
        output_func(f"whisper-cli: {result.executable_path}")
    if result.model_path:
        output_func(f"model: {result.model_path}")
    return 0 if result.success else 1


def install_whisper_cpp(
    repo_dir: Path = DEFAULT_WHISPER_CPP_DIR,
    repo_url: str = DEFAULT_WHISPER_CPP_REPO_URL,
    model_name: str = DEFAULT_MODEL_NAME,
    model_output_path: Path = DEFAULT_MODEL_PATH,
    build_timeout_seconds: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
    download_timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    output_func: Callable[[str], None] = print,
    runner: Optional[SafeSubprocessRunner] = None,
) -> WhisperCppInstallResult:
    runner = runner or SafeSubprocessRunner()
    repo_dir = repo_dir.expanduser()
    model_output_path = model_output_path.expanduser()
    model_name = str(model_name or DEFAULT_MODEL_NAME).strip()
    if not model_name:
        return _failure("invalid_model_name", "Model name cannot be empty.")

    output_func(WARNING)
    dependency = _check_install_dependencies(runner)
    if not dependency.success:
        return dependency

    clone = _clone_repo_if_missing(
        repo_dir=repo_dir,
        repo_url=str(repo_url),
        timeout_seconds=_positive_timeout(build_timeout_seconds, "build_timeout_seconds"),
        runner=runner,
        output_func=output_func,
    )
    if not clone.success:
        return clone

    build = _build_whisper_cli(
        repo_dir=repo_dir,
        timeout_seconds=_positive_timeout(build_timeout_seconds, "build_timeout_seconds"),
        runner=runner,
        output_func=output_func,
    )
    if not build.success:
        return build

    model = _ensure_model(
        repo_dir=repo_dir,
        model_name=model_name,
        model_output_path=model_output_path,
        timeout_seconds=_positive_timeout(download_timeout_seconds, "download_timeout_seconds"),
        runner=runner,
        output_func=output_func,
    )
    if not model.success:
        return model

    executable_path = _find_built_whisper_cli(repo_dir)
    if not executable_path:
        return _failure(
            "whisper_cli_missing_after_build",
            "Build finished but whisper-cli was not found.",
            data={"repo_dir": str(repo_dir)},
        )
    if not _file_exists_nonempty(model_output_path):
        return _failure(
            "model_missing_after_download",
            "Model download finished but the GGML model was not found.",
            executable_path=str(executable_path),
            model_path=str(model_output_path),
        )

    return WhisperCppInstallResult(
        success=True,
        status="installed",
        message="whisper.cpp runtime and GGML model are available.",
        executable_path=str(executable_path),
        model_path=str(model_output_path),
        data={
            "repo_dir": str(repo_dir),
            "model_name": model_name,
            "repo_url": str(repo_url),
        },
    )


def _check_install_dependencies(runner: SafeSubprocessRunner) -> WhisperCppInstallResult:
    missing = [
        executable
        for executable in ("git", "cmake", "bash")
        if not runner.which(executable)
    ]
    if missing:
        return _failure(
            "missing_install_dependency",
            "Missing required setup command(s): " + ", ".join(missing),
            data={"missing": missing},
        )
    return WhisperCppInstallResult(
        success=True,
        status="dependencies_available",
        message="Required setup commands are available.",
    )


def _clone_repo_if_missing(
    repo_dir: Path,
    repo_url: str,
    timeout_seconds: float,
    runner: SafeSubprocessRunner,
    output_func: Callable[[str], None],
) -> WhisperCppInstallResult:
    if repo_dir.exists():
        if not (repo_dir / "models" / "download-ggml-model.sh").exists():
            return _failure(
                "invalid_existing_repo",
                "Existing whisper.cpp directory is missing models/download-ggml-model.sh.",
                data={"repo_dir": str(repo_dir)},
            )
        output_func(f"Using existing whisper.cpp checkout: {repo_dir}")
        return WhisperCppInstallResult(
            success=True,
            status="repo_exists",
            message="whisper.cpp checkout already exists.",
            data={"repo_dir": str(repo_dir)},
        )

    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    output_func(f"Cloning whisper.cpp into {repo_dir}...")
    result = runner.run(
        ["git", "clone", "--depth", "1", repo_url, str(repo_dir)],
        timeout_seconds=timeout_seconds,
    )
    if result.timed_out:
        return _failure("clone_timeout", "Timed out while cloning whisper.cpp.", result)
    if result.returncode != 0:
        return _failure("clone_failed", "git clone failed.", result)
    return WhisperCppInstallResult(
        success=True,
        status="repo_cloned",
        message="whisper.cpp cloned.",
        data={"repo_dir": str(repo_dir), "process": _safe_process_data(result)},
    )


def _build_whisper_cli(
    repo_dir: Path,
    timeout_seconds: float,
    runner: SafeSubprocessRunner,
    output_func: Callable[[str], None],
) -> WhisperCppInstallResult:
    output_func("Configuring whisper.cpp with CMake...")
    configure = runner.run(
        ["cmake", "-S", str(repo_dir), "-B", str(repo_dir / "build")],
        timeout_seconds=timeout_seconds,
    )
    if configure.timed_out:
        return _failure("cmake_configure_timeout", "Timed out while configuring whisper.cpp.", configure)
    if configure.returncode != 0:
        return _failure("cmake_configure_failed", "CMake configure failed.", configure)

    output_func("Building whisper-cli...")
    build = runner.run(
        ["cmake", "--build", str(repo_dir / "build"), "--config", "Release"],
        timeout_seconds=timeout_seconds,
    )
    if build.timed_out:
        return _failure("cmake_build_timeout", "Timed out while building whisper-cli.", build)
    if build.returncode != 0:
        return _failure("cmake_build_failed", "CMake build failed.", build)

    executable_path = _find_built_whisper_cli(repo_dir)
    if not executable_path:
        return _failure(
            "whisper_cli_missing_after_build",
            "CMake build completed but whisper-cli was not found.",
            data={"repo_dir": str(repo_dir)},
        )
    return WhisperCppInstallResult(
        success=True,
        status="built",
        message="whisper-cli built.",
        executable_path=str(executable_path),
        data={
            "configure": _safe_process_data(configure),
            "build": _safe_process_data(build),
        },
    )


def _ensure_model(
    repo_dir: Path,
    model_name: str,
    model_output_path: Path,
    timeout_seconds: float,
    runner: SafeSubprocessRunner,
    output_func: Callable[[str], None],
) -> WhisperCppInstallResult:
    if _file_exists_nonempty(model_output_path):
        output_func(f"Using existing GGML model: {model_output_path}")
        return WhisperCppInstallResult(
            success=True,
            status="model_exists",
            message="GGML model already exists.",
            model_path=str(model_output_path),
        )

    download_script = repo_dir / "models" / "download-ggml-model.sh"
    if not download_script.exists():
        return _failure(
            "model_download_script_missing",
            "whisper.cpp model download script is missing.",
            data={"download_script": str(download_script)},
        )

    model_output_path.parent.mkdir(parents=True, exist_ok=True)
    output_func(f"Downloading GGML model: {model_name}...")
    download = runner.run(
        ["bash", str(download_script), model_name, str(model_output_path.parent)],
        timeout_seconds=timeout_seconds,
    )
    if download.timed_out:
        return _failure("model_download_timeout", "Timed out while downloading GGML model.", download)
    if download.returncode != 0:
        return _failure("model_download_failed", "GGML model download failed.", download)

    downloaded_model = model_output_path.parent / f"ggml-{model_name}.bin"
    if not _file_exists_nonempty(downloaded_model):
        return _failure(
            "downloaded_model_missing",
            "Model download completed but expected GGML file was not found.",
            data={"expected_model": str(downloaded_model), "process": _safe_process_data(download)},
        )

    if downloaded_model.resolve() != model_output_path.resolve():
        shutil.copyfile(downloaded_model, model_output_path)
    if not _file_exists_nonempty(model_output_path):
        return _failure(
            "model_copy_failed",
            "Downloaded GGML model could not be copied into the ARES model path.",
            data={"source": str(downloaded_model), "target": str(model_output_path)},
        )
    return WhisperCppInstallResult(
        success=True,
        status="model_downloaded",
        message="GGML model downloaded and copied into the ARES model path.",
        model_path=str(model_output_path),
        data={
            "downloaded_model": str(downloaded_model),
            "process": _safe_process_data(download),
        },
    )


def _find_built_whisper_cli(repo_dir: Path) -> Optional[Path]:
    candidates = [
        repo_dir / "build" / "bin" / "whisper-cli",
        repo_dir / "build" / "bin" / "Release" / "whisper-cli",
        repo_dir / "build" / "bin" / "Release" / "whisper-cli.exe",
        repo_dir / "build" / "whisper-cli",
        repo_dir / "main",
    ]
    for candidate in candidates:
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
    if timeout <= 0:
        raise ValueError(f"{name} must be positive")
    return timeout


def _failure(
    status: str,
    message: str,
    process: Optional[SafeProcessResult] = None,
    executable_path: str = "",
    model_path: str = "",
    data: Optional[Dict[str, Any]] = None,
) -> WhisperCppInstallResult:
    payload = dict(data or {})
    if process is not None:
        payload["process"] = _safe_process_data(process)
    return WhisperCppInstallResult(
        success=False,
        status=status,
        message=message,
        executable_path=executable_path,
        model_path=model_path,
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
