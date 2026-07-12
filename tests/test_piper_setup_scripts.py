from core import SafeProcessResult
from scripts import install_piper_raspberry_pi as installer
from scripts import manual_verify_linux_tts as manual_tts


class FakeInstallRunner:
    def __init__(self, missing=None, fail_command=None):
        self.missing = set(missing or [])
        self.fail_command = fail_command
        self.calls = []

    def which(self, executable):
        return None if executable in self.missing else f"/usr/bin/{executable}"

    def run(self, args, timeout_seconds):
        safe_args = list(args)
        self.calls.append({"args": safe_args, "timeout_seconds": timeout_seconds})
        if self.fail_command and safe_args[0] == self.fail_command:
            return SafeProcessResult(
                args=safe_args,
                returncode=2,
                stderr=f"{self.fail_command} failed",
            )
        if safe_args[0] == "curl" and "-o" in safe_args:
            output_path = safe_args[safe_args.index("-o") + 1]
            with open(output_path, "wb") as handle:
                handle.write(b"downloaded")
        if safe_args[0] == "tar" and "-C" in safe_args:
            piper_dir = safe_args[safe_args.index("-C") + 1]
            import os

            os.makedirs(f"{piper_dir}/piper", exist_ok=True)
            with open(f"{piper_dir}/piper/piper", "wb") as handle:
                handle.write(b"piper")
        return SafeProcessResult(args=safe_args, returncode=0)


def test_install_piper_downloads_runtime_model_and_config(tmp_path):
    runner = FakeInstallRunner()
    messages = []

    result = installer.install_piper(
        piper_dir=tmp_path / "external" / "piper",
        voice_model_output_path=tmp_path / "models" / "piper" / "voice.onnx",
        voice_config_output_path=tmp_path / "models" / "piper" / "voice.onnx.json",
        output_func=messages.append,
        runner=runner,
    )

    assert result.success is True
    assert result.status == "installed"
    assert result.executable_path.replace("\\", "/").endswith("piper/piper")
    assert result.model_path.endswith("voice.onnx")
    assert result.config_path.endswith("voice.onnx.json")
    assert any("WARNING" in message for message in messages)
    assert [call["args"][0] for call in runner.calls].count("curl") == 3
    assert [call["args"][0] for call in runner.calls].count("tar") == 1


def test_install_piper_is_safe_to_rerun_with_existing_files(tmp_path):
    piper_executable = tmp_path / "external" / "piper" / "piper" / "piper"
    piper_executable.parent.mkdir(parents=True)
    piper_executable.write_bytes(b"piper")
    model = tmp_path / "models" / "piper" / "voice.onnx"
    config = tmp_path / "models" / "piper" / "voice.onnx.json"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model")
    config.write_text("{}", encoding="utf-8")
    runner = FakeInstallRunner()

    result = installer.install_piper(
        piper_dir=tmp_path / "external" / "piper",
        voice_model_output_path=model,
        voice_config_output_path=config,
        output_func=lambda _: None,
        runner=runner,
    )

    assert result.success is True
    assert result.status == "installed"
    assert runner.calls == []


def test_install_piper_reports_missing_dependencies(tmp_path):
    runner = FakeInstallRunner(missing={"curl"})

    result = installer.install_piper(
        piper_dir=tmp_path / "external" / "piper",
        voice_model_output_path=tmp_path / "models" / "piper" / "voice.onnx",
        voice_config_output_path=tmp_path / "models" / "piper" / "voice.onnx.json",
        output_func=lambda _: None,
        runner=runner,
    )

    assert result.success is False
    assert result.status == "missing_install_dependency"
    assert result.data["missing"] == ["curl"]
    assert runner.calls == []


def test_manual_tts_script_import_is_safe_and_playback_is_explicit():
    parser = manual_tts.build_parser()

    args = parser.parse_args(["--text", "hello"])

    assert args.text == "hello"
    assert args.playback is False


def test_manual_tts_default_piper_command_prefers_path_only_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_tts, "REPO_ROOT", tmp_path)

    assert manual_tts._default_piper_command("") == "piper"

    executable = tmp_path / "external" / "piper" / "piper" / "piper"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"piper")

    assert manual_tts._default_piper_command("") == str(executable)
    assert manual_tts._default_piper_command("/opt/piper") == "/opt/piper"
