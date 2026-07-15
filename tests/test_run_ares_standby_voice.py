from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core import SingleTurnVoiceRequestV1, StandbyListenResultV1, WakeLocalDiagnostics
from scripts import manual_diagnose_wake_word
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
        self.standby_wake_listener.stop("runtime_complete")
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
    assert args.vosk_model == "models/vosk/vosk-model-small-en-us-0.15"
    assert args.wake_min_confidence == 0.8
    assert args.command_whisper_model == "models/whisper/ggml-base.en.bin"
    assert args.voice_profile == "en_US-hfc_male-medium"
    assert args.inactivity_seconds == 30
    assert args.timeout == 300
    assert args.diagnostic_routing is False
    assert args.diagnostic_wake is False
    assert args.retain_diagnostic_audio is False


def test_production_standby_voice_cli_supports_required_overrides():
    args = run_ares_standby_voice.build_parser().parse_args(
        [
            "--microphone-device", "plughw:9,1",
            "--speaker-device", "plughw:CARD=Other,DEV=0",
            "--vosk-model", "models/vosk/custom",
            "--wake-min-confidence", "0.9",
            "--command-whisper-command", "command-cli",
            "--command-whisper-model", "command.bin",
            "--voice-profile", "alternate",
            "--inactivity-seconds", "45",
            "--diagnostic-routing",
            "--diagnostic-wake",
            "--retain-diagnostic-audio",
        ]
    )
    assert args.microphone_device == "plughw:9,1"
    assert args.speaker_device == "plughw:CARD=Other,DEV=0"
    assert args.vosk_model == "models/vosk/custom"
    assert args.wake_min_confidence == 0.9
    assert args.command_whisper_model == "command.bin"
    assert args.voice_profile == "alternate"
    assert args.inactivity_seconds == 45
    assert args.diagnostic_routing and args.diagnostic_wake and args.retain_diagnostic_audio


def test_repo_relative_path_resolution_is_independent_of_current_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resolved = run_ares_standby_voice._repo_path("models/whisper/wake.bin")
    assert resolved == run_ares_standby_voice.REPO_ROOT / "models/whisper/wake.bin"
    command = run_ares_standby_voice._repo_path_or_command("whisper-cli")
    assert command == "whisper-cli"


def test_static_dependency_validation_fails_before_runtime_construction_for_missing_model(tmp_path, monkeypatch):
    monkeypatch.setattr(run_ares_standby_voice.importlib.util, "find_spec", lambda name: object())
    args = run_ares_standby_voice.build_parser().parse_args(
        ["--vosk-model", str(tmp_path / "missing-model")]
    )
    error = run_ares_standby_voice._validate_static_dependencies(args)
    assert "Vosk wake model is missing" in error
    assert "vosk-model-small-en-us-0.15" in error


def test_static_dependency_validation_reports_missing_vosk_package(monkeypatch):
    monkeypatch.setattr(run_ares_standby_voice.importlib.util, "find_spec", lambda name: None)
    args = run_ares_standby_voice.build_parser().parse_args([])
    error = run_ares_standby_voice._validate_static_dependencies(args)
    assert error == "Vosk is not installed. Run: python -m pip install -r requirements.txt"


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
    assert defaults.attempts_per_test == 3
    assert defaults.microphone_device == "plughw:2,0"
    assert defaults.vosk_model.endswith("vosk-model-small-en-us-0.15")
    output = []
    code = manual_verify_standby_wake_hardware.run_hardware_verification(
        ["--attempts-per-test", "0"], output_func=output.append
    )
    assert code == 2
    assert "between 1 and 5" in output[0]


def test_wake_diagnostic_rendering_is_explicit_and_prints_manual_playback_only():
    diagnostics = WakeLocalDiagnostics(
        raw_transcript="Okay, Aries.",
        normalized_transcript="okay aries",
        selected_alias="aries",
        selected_wake_phrase="okay aries",
        canonical_wake_phrase="ares",
        classification_path="vosk_constrained_grammar",
        classification_reason="accepted_vosk_constrained_grammar",
        classification="accepted",
        capture_duration_seconds=0.8,
        raw_capture_duration_seconds=2.1,
        assembled_duration_seconds=0.8,
        normalized_duration_seconds=0.8,
        whisper_input_duration_seconds=0.8,
        capture_stop_reason="completed_after_silence",
        recognizer_name="vosk_constrained_grammar",
        raw_recognition_result='[{"text": "okay aries"}]',
        recognition_status="wake_detected",
        recognition_confidence=0.94,
        recognition_confidence_available=True,
        recognition_processing_time_seconds=0.04,
        recognizer_model_path="models/vosk/vosk-model-small-en-us-0.15",
        retained_audio_path="/tmp/wake candidate.wav",
    )
    lines = run_ares_standby_voice.render_wake_diagnostics(
        diagnostics,
        speaker_device="plughw:CARD=Device,DEV=0",
    )
    text = "\n".join(lines)
    assert "Recognizer used: vosk_constrained_grammar" in text
    assert 'Raw recognition result: [{"text": "okay aries"}]' in text
    assert "Normalized phrase: okay aries" in text
    assert "Confidence: 0.940" in text
    assert "Selected alias: aries" in text
    assert "Wake classification: accepted" in text
    assert "Classification path: vosk_constrained_grammar" in text
    assert "aplay -D" in text
    assert not any(line.strip().startswith("Playing") for line in lines)


def test_retention_requires_explicit_wake_diagnostics_before_runtime_creation():
    factory, runtime, _ = _factory()
    output = []
    code = run_ares_standby_voice.run_standby_voice(
        ["--retain-diagnostic-audio"],
        output_func=output.append,
        runtime_factory=factory,
    )
    assert code == 2
    assert runtime.standby_wake_listener.start_count == 0
    assert "requires --diagnostic-wake" in output[0]


def test_one_attempt_wake_diagnostic_cli_defaults_and_validation():
    args = manual_diagnose_wake_word.build_parser().parse_args([])
    assert args.microphone_device == "plughw:2,0"
    assert args.speaker_device == "plughw:CARD=Device,DEV=0"
    assert args.vosk_model.endswith("vosk-model-small-en-us-0.15")
    output = []
    code = manual_diagnose_wake_word.run_diagnostic(
        ["--retain-diagnostic-audio"],
        output_func=output.append,
        listener_factory=lambda **_: None,
    )
    assert code == 2
    assert "requires --diagnostic-wake" in output[0]


def test_one_attempt_wake_diagnostic_accepts_aries_and_exits_after_one_capture():
    instances = []

    class DiagnosticListener:
        def __init__(self, *, diagnostic_callback=None, **kwargs):
            self.callback = diagnostic_callback
            self.listen_count = 0
            self.stop_count = 0
            instances.append(self)

        def start(self, runtime_id=""):
            return SimpleNamespace(success=True, status="started", error_code="")

        def health(self, runtime_id=""):
            return SimpleNamespace(success=True, status="healthy", error_code="")

        def listen_once(self, request):
            self.listen_count += 1
            self.callback(
                WakeLocalDiagnostics(
                    raw_transcript="Aries.",
                    normalized_transcript="aries",
                    selected_alias="aries",
                    selected_wake_phrase="aries",
                    canonical_wake_phrase="ares",
                    classification_path="vosk_constrained_grammar",
                    classification_reason="accepted_vosk_constrained_grammar",
                    classification="accepted",
                    recognizer_name="vosk_constrained_grammar",
                    raw_recognition_result='{"text":"aries"}',
                    recognition_status="wake_detected",
                    recognition_confidence=0.95,
                    recognition_confidence_available=True,
                    recognizer_model_path="vosk-model",
                )
            )
            return StandbyListenResultV1(
                success=True,
                status="wake_detected",
                wake_detected=True,
                speech_detected=True,
                selected_alias="aries",
                selected_wake_phrase="aries",
                canonical_wake_phrase="ares",
            )

        def stop(self, reason=""):
            self.stop_count += 1
            return SimpleNamespace(success=True, status="stopped")

    output = []
    code = manual_diagnose_wake_word.run_diagnostic(
        ["--diagnostic-wake"],
        output_func=output.append,
        listener_factory=DiagnosticListener,
    )
    assert code == 0
    assert instances[0].listen_count == 1
    assert instances[0].stop_count == 1
    assert any("Say Ares once, then remain silent." in line for line in output)
    assert any("Raw recognized phrase: Aries." in line for line in output)
    assert output[-1] == "Wake result: accepted (aries -> ares)."


def test_one_attempt_diagnostic_explains_unknown_token_rejection():
    class DiagnosticListener:
        def __init__(self, *, diagnostic_callback=None, **kwargs):
            self.callback = diagnostic_callback
            self.last_diagnostics = None

        def start(self, runtime_id=""):
            return SimpleNamespace(success=True, status="started", error_code="")

        def health(self, runtime_id=""):
            return SimpleNamespace(success=True, status="healthy", error_code="")

        def listen_once(self, request):
            self.last_diagnostics = WakeLocalDiagnostics(
                raw_transcript="[unk]",
                normalized_transcript="unk",
                classification_path="vosk_constrained_grammar",
                classification_reason="unknown_token_result",
                classification="rejected",
                rejection_reason="unknown_token_result",
                recognizer_name="vosk_constrained_grammar",
                raw_recognition_result='{"text":"[unk]"}',
                recognition_status="non_wake_speech",
            )
            self.callback(self.last_diagnostics)
            return StandbyListenResultV1(
                success=True,
                status="non_wake_speech",
                speech_detected=True,
                wake_detected=False,
                classification_path="vosk_constrained_grammar",
                classification_reason="unknown_token_result",
                rejection_reason="unknown_token_result",
            )

        def stop(self, reason=""):
            return SimpleNamespace(success=True, status="stopped")

    output = []
    code = manual_diagnose_wake_word.run_diagnostic(
        ["--diagnostic-wake"],
        output_func=output.append,
        listener_factory=DiagnosticListener,
    )
    text = "\n".join(output)
    assert code == 1
    assert "Classification path: vosk_constrained_grammar" in text
    assert "Raw recognition result" in text
    assert "[unk] and unrelated words are rejected" in text


def test_hardware_verifier_fails_nonzero_after_bounded_attempts():
    class NoSpeechRuntime:
        def __init__(self):
            self.runtime_id = "bounded-runtime"
            self.standby_wake_listener = FakeWakeListener()
            self.standby_wake_listener.last_result = StandbyListenResultV1(
                success=True,
                status="no_speech",
                speech_detected=False,
            )
            self.shutdown_count = 0

        def start(self):
            return SimpleNamespace(success=True, status="standby", error_code="")

        def poll_once(self):
            return SimpleNamespace(
                success=True,
                status="standby_listening",
                response_text="",
                data={"speech_detected": False},
            )

        def snapshot(self):
            return SimpleNamespace(current_lifecycle_state="STANDBY", session_id="")

        def shutdown(self, reason=""):
            self.shutdown_count += 1

    runtime = NoSpeechRuntime()
    pipeline = FakePipeline()

    def factory(args, output_func=print):
        return runtime, pipeline, SingleTurnVoiceRequestV1()

    output = []
    code = manual_verify_standby_wake_hardware.run_hardware_verification(
        ["--attempts-per-test", "1"],
        output_func=output.append,
        runtime_factory=factory,
    )
    assert code == 1
    assert runtime.shutdown_count == 1
    assert any("Test A: PASS" in line for line in output)
    assert any("Test B: FAIL after 1 attempts" in line for line in output)


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
