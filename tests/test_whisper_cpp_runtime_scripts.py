from pathlib import Path

from core import SafeProcessResult, TranscriptionResult
from scripts import install_whisper_cpp_raspberry_pi as installer
from scripts import verify_whisper_cpp_runtime as verifier


class FakeSetupRunner:
    def __init__(self, missing=None, fail_command=None, timeout_command=None):
        self.missing = set(missing or [])
        self.fail_command = fail_command
        self.timeout_command = timeout_command
        self.calls = []

    def which(self, executable):
        if executable in self.missing:
            return None
        if "/" in str(executable) or "\\" in str(executable):
            path = Path(str(executable))
            return str(path) if path.exists() else None
        return f"/usr/bin/{executable}"

    def run(self, args, timeout_seconds):
        safe_args = [str(arg) for arg in args]
        self.calls.append({"args": safe_args, "timeout_seconds": timeout_seconds})
        command_key = " ".join(safe_args[:2])
        if self.timeout_command and self.timeout_command in command_key:
            return SafeProcessResult(
                args=safe_args,
                returncode=-1,
                timed_out=True,
                error_message="process_timeout",
            )
        if self.fail_command and self.fail_command in command_key:
            return SafeProcessResult(args=safe_args, returncode=2, stderr="forced failure")

        if safe_args[:3] == ["git", "clone", "--depth"]:
            repo_dir = Path(safe_args[-1])
            (repo_dir / "models").mkdir(parents=True, exist_ok=True)
            (repo_dir / "models" / "download-ggml-model.sh").write_text(
                "#!/bin/sh\n",
                encoding="utf-8",
            )
        elif safe_args[:2] == ["cmake", "--build"]:
            build_bin = Path(safe_args[2]) / "bin"
            build_bin.mkdir(parents=True, exist_ok=True)
            (build_bin / "whisper-cli").write_bytes(b"fake executable")
        elif safe_args and safe_args[0] == "bash":
            model_name = safe_args[2]
            output_dir = Path(safe_args[3])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / f"ggml-{model_name}.bin").write_bytes(b"fake model")

        return SafeProcessResult(args=safe_args, returncode=0, stdout="ok")


class FakeVerifyRunner:
    def __init__(self, available=True):
        self.available = available

    def which(self, executable):
        return str(executable) if self.available else None


class FakeSuccessfulStt:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def transcribe_wav(self, wav_path, timeout_seconds=None):
        return TranscriptionResult(
            success=True,
            status="transcribed",
            text="hello from pi",
            confidence=1.0,
            data={
                "processing_time_seconds": 0.25,
                "language": "en",
                "wav_path": str(wav_path),
                "timeout_seconds": timeout_seconds,
            },
        )


class FakeFailingStt:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def transcribe_wav(self, wav_path, timeout_seconds=None):
        return TranscriptionResult(
            success=False,
            status="transcription_failed",
            text="",
            confidence=0.0,
            error_message="forced_failure",
            data={"wav_path": str(wav_path)},
        )


class FakeEmptyStt:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def transcribe_wav(self, wav_path, timeout_seconds=None):
        return TranscriptionResult(
            success=True,
            status="no_transcription",
            text="",
            confidence=0.0,
            data={"wav_path": str(wav_path)},
        )


def test_install_whisper_cpp_clones_builds_downloads_and_verifies(tmp_path):
    outputs = []
    repo_dir = tmp_path / "external" / "whisper.cpp"
    model_path = tmp_path / "models" / "whisper" / "ggml-tiny.en.bin"
    runner = FakeSetupRunner()

    result = installer.install_whisper_cpp(
        repo_dir=repo_dir,
        model_output_path=model_path,
        output_func=outputs.append,
        runner=runner,
    )

    assert result.success is True
    assert result.status == "installed"
    assert Path(result.executable_path).exists()
    assert model_path.exists()
    assert any(call["args"][:3] == ["git", "clone", "--depth"] for call in runner.calls)
    assert any(call["args"][:2] == ["cmake", "-S"] for call in runner.calls)
    assert any(call["args"][:2] == ["cmake", "--build"] for call in runner.calls)
    assert any(call["args"][0] == "bash" for call in runner.calls)
    assert any("does not start wake word" in line for line in outputs)


def test_install_whisper_cpp_reuses_existing_checkout_and_model(tmp_path):
    repo_dir = tmp_path / "external" / "whisper.cpp"
    (repo_dir / "models").mkdir(parents=True)
    (repo_dir / "models" / "download-ggml-model.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    build_bin = repo_dir / "build" / "bin"
    build_bin.mkdir(parents=True)
    (build_bin / "whisper-cli").write_bytes(b"fake executable")
    model_path = tmp_path / "models" / "whisper" / "ggml-tiny.en.bin"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"existing model")
    runner = FakeSetupRunner()

    result = installer.install_whisper_cpp(
        repo_dir=repo_dir,
        model_output_path=model_path,
        output_func=lambda _line: None,
        runner=runner,
    )

    assert result.success is True
    assert result.executable_path == str(build_bin / "whisper-cli")
    assert result.model_path == str(model_path)
    assert not any(call["args"][:3] == ["git", "clone", "--depth"] for call in runner.calls)
    assert not any(call["args"][0] == "bash" for call in runner.calls)


def test_install_whisper_cpp_missing_dependency_fails_safely(tmp_path):
    result = installer.install_whisper_cpp(
        repo_dir=tmp_path / "whisper.cpp",
        model_output_path=tmp_path / "ggml-tiny.en.bin",
        output_func=lambda _line: None,
        runner=FakeSetupRunner(missing={"cmake"}),
    )

    assert result.success is False
    assert result.status == "missing_install_dependency"
    assert result.data["missing"] == ["cmake"]


def test_install_whisper_cpp_build_failure_returns_clear_result(tmp_path):
    result = installer.install_whisper_cpp(
        repo_dir=tmp_path / "whisper.cpp",
        model_output_path=tmp_path / "ggml-tiny.en.bin",
        output_func=lambda _line: None,
        runner=FakeSetupRunner(fail_command="cmake --build"),
    )

    assert result.success is False
    assert result.status == "cmake_build_failed"
    assert result.data["process"]["returncode"] == 2


def test_install_script_entrypoint_is_import_safe():
    assert callable(installer.run_installation)
    assert installer.DEFAULT_MODEL_NAME == "tiny.en"


def test_verify_whisper_runtime_transcribes_existing_sample(tmp_path):
    outputs = []
    command = tmp_path / "whisper-cli"
    command.write_bytes(b"fake executable")
    model = tmp_path / "ggml-tiny.en.bin"
    model.write_bytes(b"fake model")
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"fake wav")

    result = verifier.verify_whisper_runtime(
        whisper_command=str(command),
        model_path=model,
        wav_path=wav,
        output_func=outputs.append,
        runner=FakeVerifyRunner(),
        stt_factory=FakeSuccessfulStt,
    )

    assert result.success is True
    assert result.status == "transcribed"
    assert result.transcription_text == "hello from pi"
    assert result.whisper_command == str(command)
    assert result.model_path == str(model)
    assert result.wav_path == str(wav)
    assert any("does not record audio" in line for line in outputs)


def test_verify_whisper_runtime_missing_command_fails_safely(tmp_path):
    model = tmp_path / "ggml-tiny.en.bin"
    model.write_bytes(b"fake model")
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"fake wav")

    result = verifier.verify_whisper_runtime(
        whisper_command="missing-whisper-cli",
        model_path=model,
        wav_path=wav,
        output_func=lambda _line: None,
        runner=FakeVerifyRunner(available=False),
        stt_factory=FakeSuccessfulStt,
    )

    assert result.success is False
    assert result.status == "whisper_cli_missing"


def test_verify_whisper_runtime_missing_model_fails_safely(tmp_path):
    command = tmp_path / "whisper-cli"
    command.write_bytes(b"fake executable")
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"fake wav")

    result = verifier.verify_whisper_runtime(
        whisper_command=str(command),
        model_path=tmp_path / "missing.bin",
        wav_path=wav,
        output_func=lambda _line: None,
        runner=FakeVerifyRunner(),
        stt_factory=FakeSuccessfulStt,
    )

    assert result.success is False
    assert result.status == "model_missing"
    assert result.whisper_command == str(command)


def test_verify_whisper_runtime_missing_wav_fails_safely(tmp_path):
    command = tmp_path / "whisper-cli"
    command.write_bytes(b"fake executable")
    model = tmp_path / "ggml-tiny.en.bin"
    model.write_bytes(b"fake model")

    result = verifier.verify_whisper_runtime(
        whisper_command=str(command),
        model_path=model,
        wav_path=tmp_path / "missing.wav",
        output_func=lambda _line: None,
        runner=FakeVerifyRunner(),
        stt_factory=FakeSuccessfulStt,
    )

    assert result.success is False
    assert result.status == "wav_sample_missing"
    assert result.model_path == str(model)


def test_verify_whisper_runtime_transcription_failure_is_reported(tmp_path):
    command = tmp_path / "whisper-cli"
    command.write_bytes(b"fake executable")
    model = tmp_path / "ggml-tiny.en.bin"
    model.write_bytes(b"fake model")
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"fake wav")

    result = verifier.verify_whisper_runtime(
        whisper_command=str(command),
        model_path=model,
        wav_path=wav,
        output_func=lambda _line: None,
        runner=FakeVerifyRunner(),
        stt_factory=FakeFailingStt,
    )

    assert result.success is False
    assert result.status == "transcription_failed"
    assert result.data["transcription"]["error_message"] == "forced_failure"


def test_verify_whisper_runtime_empty_transcription_is_failure(tmp_path):
    command = tmp_path / "whisper-cli"
    command.write_bytes(b"fake executable")
    model = tmp_path / "ggml-tiny.en.bin"
    model.write_bytes(b"fake model")
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"fake wav")

    result = verifier.verify_whisper_runtime(
        whisper_command=str(command),
        model_path=model,
        wav_path=wav,
        output_func=lambda _line: None,
        runner=FakeVerifyRunner(),
        stt_factory=FakeEmptyStt,
    )

    assert result.success is False
    assert result.status == "empty_transcription"


def test_verify_script_entrypoint_is_import_safe():
    assert callable(verifier.run_runtime_verification)
    assert verifier.DEFAULT_WHISPER_CPP_DIR.name == "whisper.cpp"
