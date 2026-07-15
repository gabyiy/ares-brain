from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core import SingleTurnVoiceRequestV1
from scripts import manual_verify_standby_wake_hardware
from scripts import manual_verify_standby_wake_runtime
from scripts import run_ares_standby_voice


class FakePipeline:
    def __init__(self, *, healthy: bool = True):
        self.healthy = healthy
        self.started = 0
        self.stopped = 0

    def start(self, request):
        self.started += 1
        return {"success": True, "status": "started", "data": {}}

    def health_check(self, request):
        return {
            "success": self.healthy,
            "status": "healthy" if self.healthy else "unhealthy",
            "error_message": "" if self.healthy else "injected health failure",
            "data": {"external_result": {"components": {}}},
        }

    def stop(self, request):
        self.stopped += 1
        return {"success": True, "status": "stopped", "data": {}}


class FakeWakeListener:
    def __init__(self, *, healthy: bool = True):
        self.healthy = healthy
        self.start_count = 0
        self.stop_count = 0

    def start(self, runtime_id=""):
        self.start_count += 1
        return SimpleNamespace(success=True, status="started", error_code="", error_message="")

    def health(self, runtime_id=""):
        return SimpleNamespace(
            success=self.healthy,
            status="healthy" if self.healthy else "unhealthy",
            error_code="" if self.healthy else "wake_unhealthy",
            error_message="" if self.healthy else "wake unhealthy",
        )

    def stop(self, reason=""):
        self.stop_count += 1
        return SimpleNamespace(success=True, status="stopped")


class FakeRuntime:
    def __init__(self, *, loop_success: bool = True, wake_healthy: bool = True):
        self.runtime_id = "fake-runtime"
        self.standby_wake_listener = FakeWakeListener(healthy=wake_healthy)
        self.loop_success = loop_success
        self.shutdown_count = 0

    def run(self):
        return SimpleNamespace(
            success=self.loop_success,
            status="stopped" if self.loop_success else "wake_listener_failed",
            error_code="" if self.loop_success else "injected_failure",
            stop_reason="shutdown_requested",
        )

    def shutdown(self, reason=""):
        self.shutdown_count += 1


def _factory(*, pipeline_healthy=True, wake_healthy=True, loop_success=True):
    pipeline = FakePipeline(healthy=pipeline_healthy)
    runtime = FakeRuntime(loop_success=loop_success, wake_healthy=wake_healthy)

    def create(args, output_func=print):
        return runtime, pipeline, SingleTurnVoiceRequestV1()

    return create, runtime, pipeline


def test_production_standby_voice_cli_defaults_match_verified_raspberry_pi_stack():
    args = run_ares_standby_voice.build_parser().parse_args([])
    assert args.microphone_device == "plughw:2,0"
    assert args.speaker_device == "plughw:CARD=Device,DEV=0"
    assert args.wake_whisper_model == "models/whisper/ggml-tiny.en.bin"
    assert args.command_whisper_model == "models/whisper/ggml-base.en.bin"
    assert args.voice_profile == "en_US-hfc_male-medium"
    assert args.inactivity_seconds == 30
    assert args.timeout == 300
    assert args.diagnostic_routing is False
    assert args.retain_diagnostic_audio is False


def test_production_standby_voice_cli_supports_required_overrides():
    args = run_ares_standby_voice.build_parser().parse_args(
        [
            "--microphone-device", "plughw:9,1",
            "--speaker-device", "plughw:CARD=Other,DEV=0",
            "--wake-whisper-command", "wake-cli",
            "--wake-whisper-model", "wake.bin",
            "--command-whisper-command", "command-cli",
            "--command-whisper-model", "command.bin",
            "--voice-profile", "alternate",
            "--inactivity-seconds", "45",
            "--diagnostic-routing",
            "--retain-diagnostic-audio",
        ]
    )
    assert args.microphone_device == "plughw:9,1"
    assert args.speaker_device == "plughw:CARD=Other,DEV=0"
    assert args.wake_whisper_command == "wake-cli"
    assert args.command_whisper_model == "command.bin"
    assert args.voice_profile == "alternate"
    assert args.inactivity_seconds == 45
    assert args.diagnostic_routing and args.retain_diagnostic_audio


def test_repo_relative_path_resolution_is_independent_of_current_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resolved = run_ares_standby_voice._repo_path("models/whisper/wake.bin")
    assert resolved == run_ares_standby_voice.REPO_ROOT / "models/whisper/wake.bin"
    command = run_ares_standby_voice._repo_path_or_command("whisper-cli")
    assert command == "whisper-cli"


def test_static_dependency_validation_fails_before_runtime_construction_for_missing_model(tmp_path):
    args = run_ares_standby_voice.build_parser().parse_args(
        ["--wake-whisper-model", str(tmp_path / "missing.bin")]
    )
    error = run_ares_standby_voice._validate_static_dependencies(args)
    assert "wake Whisper" in error or "Whisper command" in error


def test_foreground_launcher_preflights_then_runs_and_stops_cleanly():
    factory, runtime, pipeline = _factory()
    output = []
    code = run_ares_standby_voice.run_standby_voice(
        [], output_func=output.append, runtime_factory=factory
    )
    assert code == 0
    assert pipeline.started == 1 and pipeline.stopped == 1
    assert runtime.standby_wake_listener.start_count == 1
    assert runtime.standby_wake_listener.stop_count == 1
    assert any("foreground standby" in line.casefold() for line in output)
    assert output[-1] == "ARES standby voice runtime stopped cleanly."


def test_foreground_launcher_returns_dependency_failure_before_runtime_loop():
    factory, _, pipeline = _factory(pipeline_healthy=False)
    output = []
    code = run_ares_standby_voice.run_standby_voice(
        [], output_func=output.append, runtime_factory=factory
    )
    assert code == 3
    assert pipeline.stopped == 1
    assert any("dependency check failed" in line.casefold() for line in output)


def test_foreground_launcher_returns_wake_health_and_loop_failures():
    wake_factory, _, _ = _factory(wake_healthy=False)
    assert run_ares_standby_voice.run_standby_voice([], runtime_factory=wake_factory) == 3
    loop_factory, _, _ = _factory(loop_success=False)
    assert run_ares_standby_voice.run_standby_voice([], runtime_factory=loop_factory) == 1


def test_deterministic_manual_wake_runtime_script_passes_and_is_hardware_free():
    output = []
    code = manual_verify_standby_wake_runtime.run_verification(output.append)
    assert code == 0
    text = "\n".join(output)
    assert "No speech: STANDBY" in text
    assert "Unrelated speech: rejected silently" in text
    assert "Result: 4" in text
    assert "Inactivity 30.000s: STANDBY" in text
    assert "Standby wake runtime verification passed." in text


def test_hardware_helper_is_bounded_and_uses_production_runtime_parser():
    defaults = manual_verify_standby_wake_hardware.build_parser().parse_args([])
    assert defaults.maximum_listen_cycles == 80
    assert defaults.microphone_device == "plughw:2,0"
    assert defaults.wake_whisper_model.endswith("ggml-tiny.en.bin")
    output = []
    code = manual_verify_standby_wake_hardware.run_hardware_verification(
        ["--maximum-listen-cycles", "2"], output_func=output.append
    )
    assert code == 2
    assert "between 7 and 500" in output[0]


def test_production_and_hardware_scripts_never_replay_capture_or_daemonize():
    production = Path("scripts/run_ares_standby_voice.py").read_text(encoding="utf-8").casefold()
    hardware = Path("scripts/manual_verify_standby_wake_hardware.py").read_text(encoding="utf-8").casefold()
    combined = production + hardware
    for forbidden in ("shell=true", "systemd", "daemonize", "play-diagnostic-audio", "play_diagnostic_audio"):
        assert forbidden not in combined
    assert "run_ares_voice.py" not in production
    assert "run_once(" not in production
    assert "record_until_silence(" not in production
    assert "transcribe_wav(" not in production
