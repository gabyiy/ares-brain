from __future__ import annotations

from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from core import (
    ActiveCommandLocalDiagnostics,
    SingleTurnVoiceRequestV1,
    StandbyListenResultV1,
    WakeAttemptResult,
    WakeLocalDiagnostics,
)
from events import EventHistoryStore
from memory.schema_migrations import StoreWriteLock, store_lock_path
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
    assert args.wake_min_confidence == 0.55
    assert args.wake_medium_confidence == 0.40
    assert args.wake_medium_confirmations == 2
    assert args.wake_recalibration_seconds == 300.0
    assert args.command_whisper_model == "models/whisper/ggml-base.en.bin"
    assert args.voice_profile == "en_US-hfc_male-medium"
    assert args.inactivity_seconds == 30
    assert args.timeout == 300
    assert args.active_transcription_timeout == 30
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
            "--wake-medium-confidence", "0.75",
            "--wake-medium-confirmations", "3",
            "--wake-recalibration-seconds", "600",
            "--command-whisper-command", "command-cli",
            "--command-whisper-model", "command.bin",
            "--voice-profile", "alternate",
            "--inactivity-seconds", "45",
            "--active-transcription-timeout", "22",
            "--diagnostic-routing",
            "--diagnostic-wake",
            "--retain-diagnostic-audio",
        ]
    )
    assert args.microphone_device == "plughw:9,1"
    assert args.speaker_device == "plughw:CARD=Other,DEV=0"
    assert args.vosk_model == "models/vosk/custom"
    assert args.wake_min_confidence == 0.9
    assert args.wake_medium_confidence == 0.75
    assert args.wake_medium_confirmations == 3
    assert args.wake_recalibration_seconds == 600
    assert args.command_whisper_model == "command.bin"
    assert args.voice_profile == "alternate"
    assert args.inactivity_seconds == 45
    assert args.active_transcription_timeout == 22
    assert args.diagnostic_routing and args.diagnostic_wake and args.retain_diagnostic_audio


def test_active_command_pipeline_uses_separate_hard_whisper_timeout():
    args = run_ares_standby_voice.build_parser().parse_args([])

    command_args = run_ares_standby_voice._command_pipeline_args(args)
    request = run_ares_standby_voice.single_turn.request_from_args(command_args)

    assert command_args.timeout == 300
    assert command_args.transcription_timeout == 30
    assert request.timeout_seconds == 300
    assert request.transcription_timeout_seconds == 30


@pytest.mark.parametrize("value", ["0", "nan", "301"])
def test_active_transcription_timeout_fails_closed_before_runtime_creation(
    value,
    tmp_path,
):
    factory, runtime, _ = _factory()
    output = []

    code = run_ares_standby_voice.run_standby_voice(
        [
            "--active-transcription-timeout",
            value,
            "--runtime-lock-path",
            str(tmp_path / "runtime"),
        ],
        output_func=output.append,
        runtime_factory=factory,
    )

    assert code == 2
    assert "active transcription timeout" in output[0]
    assert runtime.standby_wake_listener.start_count == 0


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


def test_foreground_launcher_preflights_then_runs_and_stops_cleanly(tmp_path):
    factory, runtime, pipeline = _factory()
    output = []
    lock_target = tmp_path / "runtime"
    code = run_ares_standby_voice.run_standby_voice(
        ["--runtime-lock-path", str(lock_target)],
        output_func=output.append,
        runtime_factory=factory,
    )
    assert code == 0
    assert pipeline.started == 1 and pipeline.stopped == 1
    assert runtime.standby_wake_listener.start_count == 1
    assert runtime.standby_wake_listener.stop_count == 1
    assert any("foreground standby" in line.casefold() for line in output)
    assert output[-1] == "ARES standby voice runtime stopped cleanly."
    assert not store_lock_path(lock_target).exists()


def test_foreground_launcher_returns_dependency_failure_before_runtime_loop(tmp_path):
    factory, _, pipeline = _factory(pipeline_healthy=False)
    output = []
    code = run_ares_standby_voice.run_standby_voice(
        ["--runtime-lock-path", str(tmp_path / "runtime")],
        output_func=output.append,
        runtime_factory=factory,
    )
    assert code == 3
    assert pipeline.stopped == 1
    assert any("dependency check failed" in line.casefold() for line in output)


def test_foreground_launcher_returns_wake_health_and_loop_failures(tmp_path):
    wake_factory, _, _ = _factory(wake_healthy=False)
    assert run_ares_standby_voice.run_standby_voice(
        ["--runtime-lock-path", str(tmp_path / "wake-runtime")],
        runtime_factory=wake_factory,
    ) == 3
    loop_factory, _, _ = _factory(loop_success=False)
    assert run_ares_standby_voice.run_standby_voice(
        ["--runtime-lock-path", str(tmp_path / "loop-runtime")],
        runtime_factory=loop_factory,
    ) == 1


def test_standby_calibration_renderer_reports_quality_without_hiding_cleanup():
    lines = run_ares_standby_voice.render_standby_calibration_diagnostics(
        {
            "wake_model_healthy": True,
            "microphone_adapter_healthy": True,
            "alsa_device_open": False,
            "alsa_device_open_attempt_succeeded": True,
            "alsa_device_closed_during_cleanup": True,
            "valid_pcm_received": True,
            "standby_listener_healthy": False,
            "failing_subsystem": "standby_calibration",
            "calibration_thresholds": {},
            "calibration_diagnostics": {
                "frame_count": 150,
                "frame_duration_seconds": 0.02,
                "minimum_rms": 210.0,
                "median_rms": 260.0,
                "percentile_20_rms": 230.0,
                "percentile_80_rms": 310.0,
                "maximum_rms": 1800.0,
                "speech_frame_count": 8,
                "non_speech_frame_count": 142,
                "longest_non_speech_sequence": 71,
                "bootstrap_threshold_rms": 780.0,
                "selected_noise_floor_rms": 225.0,
                "quiet_sample_count": 38,
                "quiet_sample_fraction": 0.253333,
                "clipped_frame_count": 0,
                "zero_frame_count": 0,
                "quality_passed": False,
                "quality_reason": "calibration_noise_floor_unusable",
                "rms_summary": [
                    {
                        "first_frame": 1,
                        "last_frame": 10,
                        "minimum_rms": 210.0,
                        "mean_rms": 250.0,
                        "maximum_rms": 300.0,
                    }
                ],
            },
        },
        include_energy_summary=True,
    )

    text = "\n".join(lines)
    assert "yes / no / yes" in text
    assert "RMS min / median / p20 / p80 / max" in text
    assert "calibration_noise_floor_unusable" in text
    assert "RMS frames 1-10" in text
    assert "standby_calibration" in text


def test_second_production_runtime_is_rejected_without_starting_capture(tmp_path):
    lock_target = tmp_path / "ares_standby_voice.runtime"
    factory, runtime, _ = _factory()
    output = []

    with StoreWriteLock(lock_target, owner_kind="ares_standby_voice_runtime"):
        code = run_ares_standby_voice.run_standby_voice(
            ["--runtime-lock-path", str(lock_target)],
            output_func=output.append,
            runtime_factory=factory,
        )

    assert code == 4
    assert len(output) == 1
    assert output[0].startswith("ARES is already running (PID ")
    assert runtime.standby_wake_listener.start_count == 0


def test_keyboard_interrupt_releases_production_runtime_lock(tmp_path):
    lock_target = tmp_path / "ares_standby_voice.runtime"
    pipeline = FakePipeline()

    class InterruptedRuntime(FakeRuntime):
        def run(self):
            raise KeyboardInterrupt

    runtime = InterruptedRuntime()

    def factory(args, output_func=print):
        return runtime, pipeline, SingleTurnVoiceRequestV1()

    code = run_ares_standby_voice.run_standby_voice(
        ["--runtime-lock-path", str(lock_target)],
        output_func=lambda _: None,
        runtime_factory=factory,
    )

    assert code == 130
    assert runtime.shutdown_count == 1
    assert not store_lock_path(lock_target).exists()


def test_runtime_error_releases_production_lock_and_resources(tmp_path):
    lock_target = tmp_path / "ares_standby_voice.runtime"
    pipeline = FakePipeline()

    class FailedRuntime(FakeRuntime):
        def run(self):
            raise RuntimeError("injected runtime failure")

    runtime = FailedRuntime()

    def factory(args, output_func=print):
        return runtime, pipeline, SingleTurnVoiceRequestV1()

    output = []
    code = run_ares_standby_voice.run_standby_voice(
        ["--runtime-lock-path", str(lock_target)],
        output_func=output.append,
        runtime_factory=factory,
    )

    assert code == 1
    assert runtime.shutdown_count == 1
    assert not store_lock_path(lock_target).exists()
    assert any("failed and cleaned up" in line for line in output)


def test_sigterm_request_releases_production_lock_and_resources(tmp_path):
    lock_target = tmp_path / "ares_standby_voice.runtime"
    pipeline = FakePipeline()

    class TerminatedRuntime(FakeRuntime):
        def run(self):
            raise run_ares_standby_voice.RuntimeTerminationRequested(signal.SIGTERM)

    runtime = TerminatedRuntime()

    def factory(args, output_func=print):
        return runtime, pipeline, SingleTurnVoiceRequestV1()

    output = []
    code = run_ares_standby_voice.run_standby_voice(
        ["--runtime-lock-path", str(lock_target)],
        output_func=output.append,
        runtime_factory=factory,
    )
    assert code == 128 + signal.SIGTERM
    assert runtime.shutdown_count == 1
    assert not store_lock_path(lock_target).exists()
    assert any("cleaned up" in line for line in output)


@pytest.mark.parametrize(
    ("interruption", "expected_code"),
    [
        (KeyboardInterrupt(), 130),
        (
            run_ares_standby_voice.RuntimeTerminationRequested(signal.SIGTERM),
            128 + signal.SIGTERM,
        ),
    ],
)
def test_preflight_interruption_releases_runtime_and_process_lock(
    interruption,
    expected_code,
    tmp_path,
):
    lock_target = tmp_path / "ares_standby_voice.runtime"
    pipeline = FakePipeline()
    runtime = FakeRuntime()

    def interrupted_start(runtime_id=""):
        raise interruption

    runtime.standby_wake_listener.start = interrupted_start

    def factory(args, output_func=print):
        return runtime, pipeline, SingleTurnVoiceRequestV1()

    code = run_ares_standby_voice.run_standby_voice(
        ["--runtime-lock-path", str(lock_target)],
        output_func=lambda _line: None,
        runtime_factory=factory,
    )

    assert code == expected_code
    assert runtime.shutdown_count == 1
    assert not store_lock_path(lock_target).exists()


def test_production_composition_reuses_one_event_history_store(tmp_path):
    history = EventHistoryStore(path=tmp_path / "events.json")
    captured = {}

    class CompositionPipeline:
        def __init__(self, store):
            self.event_history_store = store

        def run_once(self, request, cancellation_token=None, pre_brain_hook=None):
            return SimpleNamespace(success=True, status="runtime_transport_captured")

        def run_local_output(self, request, text, cancellation_token=None):
            return SimpleNamespace(success=True, status="completed_local_output")

        def stop(self, request=None):
            return SimpleNamespace(success=True, status="stopped")

    def pipeline_factory(args, **kwargs):
        captured.update(kwargs)
        return CompositionPipeline(kwargs["event_history_store"])

    class CompositionWakeListener:
        def __init__(self, *, config, **kwargs):
            self.config = config

        def start(self, *args, **kwargs):
            return SimpleNamespace(success=True, status="started")

        enter_standby = start
        leave_standby = start
        listen_once = start
        cancel = start
        stop = start
        snapshot = start
        health = start

    args = run_ares_standby_voice.build_parser().parse_args([])
    runtime, pipeline, _ = run_ares_standby_voice.create_runtime(
        args,
        pipeline_factory=pipeline_factory,
        wake_listener_factory=CompositionWakeListener,
        event_history_store=history,
    )

    assert pipeline.event_history_store is history
    assert captured["event_history_store"] is history
    assert captured["skill_manager"].event_history_store is history
    assert runtime._event_history_store is history
    assert runtime.session_manager._event_history_store is history
    assert runtime.core_service._event_history_store is history
    assert runtime.core_service.resource_manager.event_history_store is history


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
    assert defaults.wake_reliability_attempts == 0
    assert defaults.verification_mode == "all"
    assert defaults.microphone_device == "plughw:2,0"
    assert defaults.vosk_model.endswith("vosk-model-small-en-us-0.15")
    output = []
    code = manual_verify_standby_wake_hardware.run_hardware_verification(
        ["--attempts-per-test", "0"], output_func=output.append
    )
    assert code == 2
    assert "between 1 and 5" in output[0]


def test_hardware_helper_stage_e_requires_bypass_and_does_not_retry_unknown_fallback():
    snapshot = SimpleNamespace(current_lifecycle_state="STANDBY", session_id="")
    passed, _ = manual_verify_standby_wake_hardware._stage_passed(
        "E",
        SimpleNamespace(
            response_text="",
            data={"core_service_bypassed": True},
        ),
        snapshot,
        None,
        "session-1",
    )
    fallback, _ = manual_verify_standby_wake_hardware._stage_passed(
        "E",
        SimpleNamespace(
            response_text="I cannot handle that request yet.",
            data={"core_service_bypassed": False},
        ),
        snapshot,
        None,
        "session-1",
    )

    assert passed is True
    assert fallback is False
    assert manual_verify_standby_wake_hardware._retry_allowed(
        "E", SimpleNamespace(status="input_timeout")
    )
    assert not manual_verify_standby_wake_hardware._retry_allowed(
        "E", SimpleNamespace(status="command_completed")
    )


class ReliabilityListener:
    def __init__(self, accepted, *, reopen_on_attempt=0):
        self.accepted = list(accepted)
        self.index = 0
        self.reopen_on_attempt = reopen_on_attempt
        self.open_count = 1
        self.calibration_count = 1
        self.stream_id = "reliability-stream-1"
        self.last_result = None
        self.last_diagnostics = None

    def snapshot(self, runtime_id=""):
        return SimpleNamespace(
            stream_active=True,
            stream_open_count=self.open_count,
            calibration_count=self.calibration_count,
            stream_instance_id=self.stream_id,
            stream_open_reasons=["listener_start"],
            stream_close_reasons=[],
            calibration_reasons=["listener_start:initial_calibration"],
            ownership_handoffs=[],
        )

    def listen_once(self, request):
        accepted = self.accepted[self.index]
        self.index += 1
        if self.reopen_on_attempt == self.index:
            self.open_count += 1
            self.calibration_count += 1
            self.stream_id = f"reliability-stream-{self.open_count}"
        self.last_result = StandbyListenResultV1(
            success=True,
            status="wake_detected" if accepted else "non_wake_speech",
            speech_detected=True,
            wake_detected=accepted,
            recognition_confidence=0.83 if accepted else 0.6,
            recognition_confidence_available=True,
            classification_reason=(
                "accepted_vosk_constrained_grammar"
                if accepted
                else "exact_constrained_phrase_not_matched"
            ),
            duration_seconds=0.8,
            stream_open_count=self.open_count,
            calibration_count=self.calibration_count,
            candidate_number=self.index,
            stream_instance_id=self.stream_id,
            alsa_handle_id=f"{self.stream_id}-handle",
        )
        self.last_diagnostics = WakeLocalDiagnostics(
            raw_transcript="ares" if accepted else "go to",
            beginning_clipped=False,
        )
        return self.last_result


class ReliabilityRuntime:
    def __init__(self, accepted, *, reopen_on_attempt=0):
        self.runtime_id = "reliability-runtime"
        self.state = "STANDBY"
        self.session_id = ""
        self.standby_wake_listener = ReliabilityListener(
            accepted,
            reopen_on_attempt=reopen_on_attempt,
        )

    def snapshot(self):
        return SimpleNamespace(
            current_lifecycle_state=self.state,
            session_id=self.session_id,
        )

    def build_standby_wake_request(self):
        return SimpleNamespace(correlation_id="reliability-correlation")


def test_hardware_reliability_mode_reports_nine_of_ten_without_hiding_misses():
    output = []
    runtime = ReliabilityRuntime([True] * 9 + [False])
    assert manual_verify_standby_wake_hardware._run_wake_reliability(
        runtime,
        10,
        output_func=output.append,
        diagnostic_enabled=True,
        wake_transcripts=[],
    )
    assert any("9/10 accepted" in line for line in output)
    assert any("rejected" in line for line in output)
    assert any("opens=1; calibrations=1" in line for line in output)
    assert any("exact_phrase_mismatch=1" in line for line in output)


def test_hardware_reliability_mode_pauses_between_candidates_and_prints_vad_metrics():
    output = []
    pauses = []
    runtime = ReliabilityRuntime([True, True, True])
    assert manual_verify_standby_wake_hardware._run_wake_reliability(
        runtime,
        3,
        output_func=output.append,
        diagnostic_enabled=True,
        wake_transcripts=[],
        pause_seconds=0.75,
        sleeper=pauses.append,
    )
    assert pauses == [0.75, 0.75]
    text = "\n".join(output)
    assert "Wake VAD: noise_floor=" in text
    assert "Wake audio: raw=" in text
    assert "Vosk decision: raw_tokens=" in text
    assert "duplicate_collapse=" in text


def test_hardware_reliability_mode_fails_below_nine_of_ten():
    output = []
    runtime = ReliabilityRuntime([True] * 8 + [False] * 2)
    assert not manual_verify_standby_wake_hardware._run_wake_reliability(
        runtime,
        10,
        output_func=output.append,
        diagnostic_enabled=True,
        wake_transcripts=[],
    )
    assert any("8/10 accepted" in line for line in output)


def test_hardware_reliability_mode_fails_on_unexpected_stream_reopen():
    output = []
    runtime = ReliabilityRuntime([True] * 10, reopen_on_attempt=4)
    assert not manual_verify_standby_wake_hardware._run_wake_reliability(
        runtime,
        10,
        output_func=output.append,
        diagnostic_enabled=True,
        wake_transcripts=[],
    )
    assert any("stream changed" in line for line in output)


def test_hardware_reliability_excludes_bounded_infrastructure_failure_from_denominator():
    class RecoveringListener(ReliabilityListener):
        def __init__(self):
            super().__init__([True] * 10)
            self.infrastructure_emitted = False

        def listen_once(self, request):
            if not self.infrastructure_emitted:
                self.infrastructure_emitted = True
                self.last_result = StandbyListenResultV1(
                    success=False,
                    status="failed",
                    infrastructure_failure=True,
                    error_code="calibration_failed",
                    stop_reason="calibration_failed",
                    stream_open_count=1,
                    calibration_count=1,
                    stream_instance_id=self.stream_id,
                )
                self.last_diagnostics = WakeLocalDiagnostics(
                    infrastructure_failure=True,
                )
                return self.last_result
            return super().listen_once(request)

    output = []
    runtime = ReliabilityRuntime([])
    runtime.standby_wake_listener = RecoveringListener()
    assert manual_verify_standby_wake_hardware._run_wake_reliability(
        runtime,
        10,
        output_func=output.append,
        diagnostic_enabled=True,
        wake_transcripts=[],
    )
    assert any("excluded from recognition denominator" in line for line in output)
    assert any("10/10 accepted" in line for line in output)
    assert any("infrastructure_failures=1" in line for line in output)


def test_hardware_reliability_mode_prompts_and_consumes_second_confirmation_candidate():
    class ConfirmingListener(ReliabilityListener):
        def __init__(self):
            super().__init__([False, True])

        def listen_once(self, request):
            self.index += 1
            confirmation_required = self.index == 1
            accepted = self.index == 2
            self.last_result = StandbyListenResultV1(
                success=True,
                status="non_wake_speech" if confirmation_required else "wake_detected",
                speech_detected=True,
                wake_detected=accepted,
                confirmation_required=confirmation_required,
                confirmation_count=self.index,
                confirmation_required_count=2,
                recognition_confidence=0.464,
                recognition_confidence_available=True,
                classification_reason=(
                    "medium_confidence_confirmation_required"
                    if confirmation_required
                    else "accepted_medium_confidence_repetition"
                ),
                duration_seconds=0.8,
                stream_open_count=1,
                calibration_count=1,
                candidate_number=self.index,
                stream_instance_id=self.stream_id,
                alsa_handle_id=f"{self.stream_id}-handle",
            )
            self.last_diagnostics = WakeLocalDiagnostics(
                raw_transcript="aris",
                beginning_clipped=False,
            )
            return self.last_result

    output = []
    runtime = ReliabilityRuntime([])
    runtime.standby_wake_listener = ConfirmingListener()

    assert manual_verify_standby_wake_hardware._run_wake_reliability(
        runtime,
        1,
        output_func=output.append,
        diagnostic_enabled=True,
        wake_transcripts=[],
    )
    assert runtime.standby_wake_listener.index == 2
    assert any(
        line.strip() == "Low-confidence wake detected. Say Ares once more."
        for line in output
    )
    assert any("Confirmation result: accepted; count=2/2" in line for line in output)
    assert any("1/1 accepted" in line for line in output)


def test_hardware_reliability_excludes_confirmation_infrastructure_failure():
    class ConfirmationRecoveryListener(ReliabilityListener):
        def __init__(self):
            super().__init__([])

        def listen_once(self, request):
            self.index += 1
            if self.index == 1:
                self.last_result = StandbyListenResultV1(
                    success=True,
                    status="non_wake_speech",
                    speech_detected=True,
                    confirmation_required=True,
                    confirmation_count=1,
                    confirmation_required_count=2,
                    recognition_confidence=0.464,
                    classification_reason="medium_confidence_confirmation_required",
                    duration_seconds=0.8,
                    stream_open_count=1,
                    calibration_count=1,
                    stream_instance_id=self.stream_id,
                )
            elif self.index == 2:
                self.last_result = StandbyListenResultV1(
                    success=False,
                    status="failed",
                    infrastructure_failure=True,
                    error_code="device_error",
                    stop_reason="device_error",
                    stream_open_count=1,
                    calibration_count=1,
                    stream_instance_id=self.stream_id,
                )
            else:
                self.last_result = StandbyListenResultV1(
                    success=True,
                    status="wake_detected",
                    speech_detected=True,
                    wake_detected=True,
                    recognition_confidence=0.8,
                    classification_reason="accepted_vosk_constrained_grammar",
                    duration_seconds=0.8,
                    stream_open_count=1,
                    calibration_count=1,
                    stream_instance_id=self.stream_id,
                )
            self.last_diagnostics = WakeLocalDiagnostics(raw_transcript="ares")
            return self.last_result

    output = []
    runtime = ReliabilityRuntime([])
    runtime.standby_wake_listener = ConfirmationRecoveryListener()

    assert manual_verify_standby_wake_hardware._run_wake_reliability(
        runtime,
        1,
        output_func=output.append,
        diagnostic_enabled=True,
        wake_transcripts=[],
    )
    assert runtime.standby_wake_listener.index == 3
    assert any("Confirmation infrastructure failure" in line for line in output)
    assert any("infrastructure_failures=1" in line for line in output)
    assert any("1/1 accepted" in line for line in output)


def test_hardware_helper_active_summary_uses_active_diagnostics_not_stale_wake_result():
    output = []
    diagnostics = ActiveCommandLocalDiagnostics(
        raw_transcript="goodbye aris",
        alias_canonicalized_transcript="goodbye ares",
        raw_capture_duration_seconds=2.4,
        finalized_candidate_duration_seconds=1.1,
        wav_path="/tmp/runtime-command.wav",
        wav_byte_size=35244,
        wav_sample_rate_hz=16000,
        wav_channels=1,
        wav_sample_width_bytes=2,
        transcription_backend="whisper.cpp",
        transcription_started_at="2026-07-16T10:00:00Z",
        transcription_completed_at="2026-07-16T10:00:00.5Z",
        transcription_status="transcribed",
        whisper_processing_duration_seconds=0.5,
        temporary_audio_cleanup_status="removed",
    )
    manual_verify_standby_wake_hardware._print_recognition_summary(
        output.append,
        diagnostics=diagnostics,
        wake_result=None,
        result=SimpleNamespace(command_category="standby", error_code=""),
        before_state="ACTIVE",
        diagnostic_enabled=True,
    )
    text = "\n".join(output)
    assert "Recognizer used: whisper_active_command" in text
    assert "Raw recognition result: goodbye aris" in text
    assert "Normalized phrase: goodbye ares" in text
    assert "active_command_or_none" not in text
    assert "Active audio: capture=2.400s; finalized=1.100s" in text
    assert "format=16000Hz/1ch/2B" in text
    assert "Active transcription: backend=whisper.cpp" in text
    assert "cleanup=removed" in text

    summary = []
    manual_verify_standby_wake_hardware._print_transcript_summary(
        summary.append,
        ["ares"],
        ["goodbye aris"],
    )
    assert summary == [
        "Wake transcripts: ares",
        "Active-command transcripts: goodbye aris",
    ]


def test_hardware_verifier_uses_exact_completed_attempt_not_mutable_last_fields():
    no_speech = StandbyListenResultV1(
        success=True,
        status="no_speech",
        attempt_id="attempt-current",
        candidate_id="candidate-current",
        stream_generation=2,
        candidate_number=4,
        stream_instance_id="stream-two",
        capture_valid=False,
        recognizer_invoked=False,
        speech_detected=False,
        duration_seconds=0.0,
        cleanup_status="removed",
    )
    diagnostics = WakeLocalDiagnostics(
        attempt_id="attempt-current",
        candidate_id="candidate-current",
        stream_generation=2,
        capture_valid=False,
        recognizer_invoked=False,
    )
    exact = WakeAttemptResult(
        attempt_id="attempt-current",
        candidate_id="candidate-current",
        stream_instance_id="stream-two",
        stream_generation=2,
        candidate_number=4,
        capture_valid=False,
        recognizer_invoked=False,
        infrastructure_failure=False,
        lifecycle_state_before="STANDBY",
        lifecycle_state_after="STANDBY",
        cleanup_status="removed",
        result=no_speech,
        diagnostics=diagnostics,
    )

    class Listener:
        last_result = StandbyListenResultV1(
            success=True,
            status="wake_detected",
            speech_detected=True,
            wake_detected=True,
        )
        last_diagnostics = WakeLocalDiagnostics(raw_transcript="stale ares")

        def completed_attempt(self, attempt_id):
            return exact if attempt_id == exact.attempt_id else None

    attempt = manual_verify_standby_wake_hardware._completed_runtime_attempt(
        Listener(),
        SimpleNamespace(data={"wake_attempt_id": "attempt-current"}),
        "STANDBY",
        "STANDBY",
    )
    assert attempt is exact
    assert attempt.diagnostics.raw_transcript == ""
    assert manual_verify_standby_wake_hardware._wake_attempt_consistency_error(
        attempt
    ) == ""


def test_wake_attempt_contract_rejects_recognition_with_zero_duration_audio():
    result = StandbyListenResultV1(
        success=True,
        status="wake_detected",
        attempt_id="attempt-invalid",
        candidate_id="candidate-invalid",
        stream_generation=1,
        candidate_number=1,
        stream_instance_id="stream-one",
        capture_valid=True,
        recognizer_invoked=True,
        speech_detected=True,
        wake_detected=True,
        sample_rate_hz=16000,
        channels=1,
        sample_width_bytes=2,
        duration_seconds=0.0,
        cleanup_status="removed",
    )
    with pytest.raises(ValueError, match="non-empty audio"):
        WakeAttemptResult(
            attempt_id="attempt-invalid",
            candidate_id="candidate-invalid",
            stream_instance_id="stream-one",
            stream_generation=1,
            candidate_number=1,
            capture_valid=True,
            recognizer_invoked=True,
            infrastructure_failure=False,
            lifecycle_state_before="STANDBY",
            lifecycle_state_after="STANDBY",
            cleanup_status="removed",
            result=result,
        )


def test_wake_diagnostic_rendering_is_explicit_and_prints_manual_playback_only():
    diagnostics = WakeLocalDiagnostics(
        raw_transcript="Hello, Aries.",
        normalized_transcript="hello aries",
        selected_alias="aries",
        selected_wake_phrase="hello aries",
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
        raw_recognition_result='[{"text": "hello aries"}]',
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
    assert 'Raw recognition result: [{"text": "hello aries"}]' in text
    assert "Normalized phrase: hello aries" in text
    assert "Confidence: 0.940" in text
    assert "Selected alias: aries" in text
    assert "Wake classification: accepted" in text
    assert "Classification path: vosk_constrained_grammar" in text
    assert "aplay -D" in text
    assert not any(line.strip().startswith("Playing") for line in lines)


def test_active_command_diagnostics_separate_real_command_capture_from_wake_capture():
    diagnostics = ActiveCommandLocalDiagnostics(
        raw_transcript="Goodbye Aris.",
        cleaned_transcript="goodbye aris",
        alias_canonicalized_transcript="goodbye ares",
        lifecycle_classification="standby",
        selected_lifecycle_action="standby",
        core_service_bypassed=True,
        lifecycle_state_before="ACTIVE",
        lifecycle_state_after="STANDBY",
        session_id_before="session-1",
        session_id_after="",
        capture_stop_reason="completed_after_silence",
        raw_capture_duration_seconds=2.4,
        finalized_candidate_duration_seconds=1.2,
        whisper_processing_duration_seconds=0.6,
        terminal_silence_status="confirmed_terminal_silence",
        audio_finalization_started_at="2026-07-16T10:00:00Z",
        audio_finalization_completed_at="2026-07-16T10:00:00.010000Z",
        wav_path="/tmp/runtime-command.wav",
        wav_byte_size=38444,
        wav_sample_rate_hz=16000,
        wav_channels=1,
        wav_sample_width_bytes=2,
        transcription_backend="whisper.cpp",
        transcription_started_at="2026-07-16T10:00:00.020000Z",
        transcription_completed_at="2026-07-16T10:00:00.620000Z",
        transcription_status="transcribed",
        transcription_timeout_seconds=30.0,
        transcript_parsing_status="completed",
        routing_started_at="2026-07-16T10:00:00.630000Z",
        routing_completed_at="2026-07-16T10:00:00.640000Z",
        temporary_audio_cleanup_status="removed",
        microphone_gate_released_before_inference=True,
    )

    rendered = "\n".join(
        run_ares_standby_voice.render_active_command_diagnostics(diagnostics)
    )

    assert "Raw Whisper transcript: Goodbye Aris." in rendered
    assert "Alias-canonicalized transcript: goodbye ares" in rendered
    assert "Lifecycle classification: standby" in rendered
    assert "CoreService routing bypassed: yes" in rendered
    assert "Lifecycle state: ACTIVE -> STANDBY" in rendered
    assert "Active session: session-1 -> none" in rendered
    assert "Raw capture duration: 2.400s" in rendered
    assert "Finalized candidate duration: 1.200s" in rendered
    assert "WAV byte size: 38444" in rendered
    assert "WAV format: 16000 Hz, 1 channel(s), 2-byte samples" in rendered
    assert "Transcription hard timeout: 30.000s" in rendered
    assert "Microphone gate released before inference: yes" in rendered
    assert "Temporary audio cleanup: removed" in rendered


def test_retention_requires_explicit_wake_diagnostics_before_runtime_creation(tmp_path):
    factory, runtime, _ = _factory()
    output = []
    code = run_ares_standby_voice.run_standby_voice(
        [
            "--retain-diagnostic-audio",
            "--runtime-lock-path",
            str(tmp_path / "runtime"),
        ],
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


def test_one_attempt_wake_diagnostic_accepts_aris_and_exits_after_one_capture():
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
                    raw_transcript="Aris.",
                    normalized_transcript="aris",
                    selected_alias="aris",
                    selected_wake_phrase="aris",
                    canonical_wake_phrase="ares",
                    classification_path="vosk_constrained_grammar",
                    classification_reason="accepted_vosk_constrained_grammar",
                    classification="accepted",
                    recognizer_name="vosk_constrained_grammar",
                    raw_recognition_result='{"text":"aris"}',
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
                selected_alias="aris",
                selected_wake_phrase="aris",
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
    assert any("Raw recognized phrase: Aris." in line for line in output)
    assert output[-1] == "Wake result: accepted (aris -> ares)."


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


def test_hardware_verifier_preflight_interrupt_releases_runtime_and_lock():
    runtime = FakeRuntime()
    pipeline = FakePipeline()

    def interrupted_start(runtime_id=""):
        raise KeyboardInterrupt

    runtime.standby_wake_listener.start = interrupted_start

    def factory(args, output_func=print):
        return runtime, pipeline, SingleTurnVoiceRequestV1()

    output = []
    code = manual_verify_standby_wake_hardware.run_hardware_verification(
        [],
        output_func=output.append,
        runtime_factory=factory,
    )

    assert code == 130
    assert runtime.shutdown_count == 1
    assert any("during preflight" in line for line in output)


def test_hardware_verifier_uses_isolated_event_history_not_production_store(monkeypatch):
    class NoSpeechRuntime:
        def __init__(self):
            self.runtime_id = "isolated-history-runtime"
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
    captured = {}

    def default_factory(args, output_func=print, event_history_store=None):
        captured["history"] = event_history_store
        return runtime, pipeline, SingleTurnVoiceRequestV1()

    monkeypatch.setattr(
        manual_verify_standby_wake_hardware.standby_voice,
        "_validate_static_dependencies",
        lambda _args: "",
    )
    monkeypatch.setattr(
        manual_verify_standby_wake_hardware.standby_voice,
        "create_runtime",
        default_factory,
    )
    monkeypatch.setattr(
        manual_verify_standby_wake_hardware,
        "inspect_ares_runtime_state",
        lambda **_kwargs: SimpleNamespace(live_runtime_conflict=False),
    )
    monkeypatch.setattr(
        manual_verify_standby_wake_hardware,
        "render_runtime_state_report",
        lambda _report, _output: None,
    )

    code = manual_verify_standby_wake_hardware.run_hardware_verification(
        ["--attempts-per-test", "1"],
        output_func=lambda _line: None,
    )
    history = captured["history"]
    assert code == 1
    assert isinstance(history, EventHistoryStore)
    assert history.path.name == "event_history.json"
    assert "ares-wake-verifier-lock-" in str(history.path.parent)
    assert history.path.resolve() != EventHistoryStore().path.resolve()
    assert runtime.shutdown_count == 1


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
