import json
from pathlib import Path

from core import SafeProcessResult, VoiceProfile, VoiceProfileRegistry
from scripts import install_piper_raspberry_pi as installer
from scripts import manual_verify_linux_tts as manual_tts


class FakeInstallRunner:
    def __init__(self, missing=None, fail_url=""):
        self.missing = set(missing or [])
        self.fail_url = fail_url
        self.calls = []

    def which(self, executable):
        return None if executable in self.missing else f"/usr/bin/{executable}"

    def run(self, args, timeout_seconds):
        safe_args = list(args)
        self.calls.append({"args": safe_args, "timeout_seconds": timeout_seconds})
        if safe_args[0] == "curl" and self.fail_url and self.fail_url in safe_args[-1]:
            return SafeProcessResult(args=safe_args, returncode=2, stderr="download failed")
        if safe_args[0] == "curl" and "-o" in safe_args:
            output_path = Path(safe_args[safe_args.index("-o") + 1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.name.endswith(".onnx.json"):
                sample_rate = 16000 if "amy" in safe_args[-1] else 22050
                output_path.write_text(
                    json.dumps(
                        {
                            "audio": {"sample_rate": sample_rate},
                            "language": {"code": "en_US"},
                        }
                    ),
                    encoding="utf-8",
                )
            elif output_path.suffix == ".onnx":
                output_path.write_bytes(b"valid model content")
            else:
                output_path.write_bytes(b"downloaded archive")
        if safe_args[0] == "tar" and "-C" in safe_args:
            piper_dir = Path(safe_args[safe_args.index("-C") + 1])
            executable = piper_dir / "piper" / "piper"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"piper")
        return SafeProcessResult(args=safe_args, returncode=0)


def _profile(
    profile_id,
    model_name,
    default=False,
    sample_rate=22050,
    source_name="voice",
):
    return VoiceProfile(
        profile_id=profile_id,
        display_name=profile_id,
        engine="piper",
        language="en",
        locale="en_US",
        gender="male" if "male" in profile_id else "female",
        quality="medium" if sample_rate == 22050 else "low",
        model_path=f"models/piper/{model_name}.onnx",
        config_path=f"models/piper/{model_name}.onnx.json",
        expected_sample_rate_hz=sample_rate,
        enabled=True,
        is_default=default,
        source_model_url=f"https://voices.example/{source_name}.onnx",
        source_config_url=f"https://voices.example/{source_name}.onnx.json",
        minimum_model_size_bytes=4,
    )


def _registry(tmp_path):
    return VoiceProfileRegistry(
        [
            _profile("en_US-hfc_male-medium", "male", default=True, source_name="male"),
            _profile(
                "en_US-amy-low",
                "amy",
                sample_rate=16000,
                source_name="amy",
            ),
        ],
        project_root=tmp_path,
    )


def _write_installed_voice(registry, profile_id):
    profile = registry.resolve(profile_id)
    model = registry.model_path(profile)
    config = registry.config_path(profile)
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"valid model content")
    config.write_text(
        json.dumps(
            {
                "audio": {"sample_rate": profile.expected_sample_rate_hz},
                "language": {"code": profile.locale},
            }
        ),
        encoding="utf-8",
    )


def _write_runtime(tmp_path):
    executable = tmp_path / "external" / "piper" / "piper" / "piper"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"piper")


def test_install_piper_installs_runtime_and_default_male_profile(tmp_path):
    runner = FakeInstallRunner()
    messages = []

    result = installer.install_piper(
        piper_dir=tmp_path / "external" / "piper",
        voice_registry=_registry(tmp_path),
        project_root=tmp_path,
        output_func=messages.append,
        runner=runner,
    )

    assert result.success is True
    assert result.status == "installed"
    assert result.data["voice_profile"]["profile_id"] == "en_US-hfc_male-medium"
    assert result.model_path.endswith("male.onnx")
    assert result.config_path.endswith("male.onnx.json")
    assert any("Selected voice profile: en_US-hfc_male-medium" in item for item in messages)
    assert [call["args"][0] for call in runner.calls].count("curl") == 3
    assert [call["args"][0] for call in runner.calls].count("tar") == 1


def test_install_piper_installs_explicit_amy_profile(tmp_path):
    runner = FakeInstallRunner()

    result = installer.install_piper(
        piper_dir=tmp_path / "external" / "piper",
        voice_profile_id="en_US-amy-low",
        voice_registry=_registry(tmp_path),
        project_root=tmp_path,
        output_func=lambda _: None,
        runner=runner,
    )

    assert result.success is True
    assert result.data["voice_profile"]["profile_id"] == "en_US-amy-low"
    assert result.model_path.endswith("amy.onnx")


def test_install_piper_is_safe_to_rerun_without_download(tmp_path):
    registry = _registry(tmp_path)
    _write_runtime(tmp_path)
    _write_installed_voice(registry, "en_US-hfc_male-medium")
    runner = FakeInstallRunner()

    result = installer.install_piper(
        piper_dir=tmp_path / "external" / "piper",
        voice_registry=registry,
        project_root=tmp_path,
        output_func=lambda _: None,
        runner=runner,
    )

    assert result.success is True
    assert runner.calls == []


def test_install_piper_reports_missing_dependencies(tmp_path):
    runner = FakeInstallRunner(missing={"curl"})

    result = installer.install_piper(
        piper_dir=tmp_path / "external" / "piper",
        voice_registry=_registry(tmp_path),
        project_root=tmp_path,
        output_func=lambda _: None,
        runner=runner,
    )

    assert result.success is False
    assert result.status == "missing_install_dependency"
    assert result.data["missing"] == ["curl"]
    assert runner.calls == []


def test_install_piper_rejects_unknown_profile_before_download(tmp_path):
    runner = FakeInstallRunner()

    result = installer.install_piper(
        voice_profile_id="missing",
        voice_registry=_registry(tmp_path),
        project_root=tmp_path,
        output_func=lambda _: None,
        runner=runner,
    )

    assert result.success is False
    assert result.status == "invalid_voice_profile"
    assert result.data["voice_profile_error"] == "unknown_profile"
    assert runner.calls == []


def test_install_piper_failed_voice_download_is_structured(tmp_path):
    registry = _registry(tmp_path)
    _write_runtime(tmp_path)
    runner = FakeInstallRunner(fail_url="male.onnx")

    result = installer.install_piper(
        piper_dir=tmp_path / "external" / "piper",
        voice_registry=registry,
        project_root=tmp_path,
        output_func=lambda _: None,
        runner=runner,
    )

    assert result.success is False
    assert result.status == "voice_model_download_failed"
    assert not registry.model_path(registry.default_profile()).exists()


def test_install_piper_replaces_partial_model_without_redownloading_config(tmp_path):
    registry = _registry(tmp_path)
    profile = registry.default_profile()
    _write_runtime(tmp_path)
    _write_installed_voice(registry, profile.profile_id)
    registry.model_path(profile).write_bytes(b"x")
    runner = FakeInstallRunner()

    result = installer.install_piper(
        piper_dir=tmp_path / "external" / "piper",
        voice_registry=registry,
        project_root=tmp_path,
        output_func=lambda _: None,
        runner=runner,
    )

    assert result.success is True
    curl_urls = [call["args"][-1] for call in runner.calls if call["args"][0] == "curl"]
    assert curl_urls == [profile.source_model_url]
    assert registry.validate_installed(profile).success is True


def test_manual_tts_script_import_is_safe_and_playback_is_explicit():
    parser = manual_tts.build_parser()

    args = parser.parse_args(["--text", "hello"])

    assert args.text == "hello"
    assert args.voice_profile == ""
    assert args.playback is False


def test_manual_tts_default_piper_command_prefers_path_only_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_tts, "REPO_ROOT", tmp_path)

    assert manual_tts._default_piper_command("") == "piper"

    executable = tmp_path / "external" / "piper" / "piper" / "piper"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"piper")

    assert manual_tts._default_piper_command("") == str(executable)
    assert manual_tts._default_piper_command("/opt/piper") == "/opt/piper"
