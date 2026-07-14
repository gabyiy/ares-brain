from pathlib import Path
from types import SimpleNamespace

import pytest

from core import CAPTURE_MODE_AUTO_STOP, CAPTURE_MODE_FIXED_DURATION, SingleTurnVoiceResultV1
from scripts import run_ares_voice as launcher


def _lifecycle_result(
    success=True,
    status="healthy",
    error_message="",
    data=None,
):
    return SimpleNamespace(
        success=success,
        status=status,
        error_message=error_message,
        data=dict(data or {}),
    )


def _health_result(success=True, failures=None):
    components = {
        "brain": {"success": True, "status": "healthy"},
        "microphone": {"success": True, "status": "healthy"},
        "speech_to_text": {"success": True, "status": "healthy"},
        "text_to_speech": {"success": True, "status": "healthy"},
        "speaker": {"success": True, "status": "healthy"},
    }
    for component, status, reason in failures or []:
        components[component] = {
            "success": False,
            "status": status,
            "error_message": reason,
        }
    return _lifecycle_result(
        success=success,
        status="healthy" if success else "unhealthy_components",
        error_message="" if success else "unhealthy_components",
        data={
            "external_result": {
                "success": success,
                "status": "healthy" if success else "unhealthy_components",
                "components": components,
            }
        },
    )


def _success_result():
    return SingleTurnVoiceResultV1(
        success=True,
        status="completed",
        recognized_text="How much is two plus two?",
        cleaned_transcript="How much is two plus two",
        normalized_command="calculate 2 + 2",
        extracted_calculator_expression="two plus two",
        detected_intent="calculate",
        routed_skill="calculator",
        planner_decision="1 step(s): calculator",
        execution_result="success",
        brain_text_response="Result: 4",
        playback_status="played",
        total_processing_time_seconds=1.5,
    )


class FakePipeline:
    def __init__(self, result=None, health=None, order=None):
        self.result = result or _success_result()
        self.health = health or _health_result()
        self.order = order if order is not None else []
        self.requests = []
        self.start_count = 0
        self.health_count = 0
        self.stop_count = 0
        self.run_count = 0
        self.speaker_adapter = None

    def start(self, request):
        self.start_count += 1
        self.order.append("preflight.start")
        return _lifecycle_result(True, "ready")

    def health_check(self, request):
        self.health_count += 1
        self.order.append("preflight.health")
        return self.health

    def stop(self, request):
        self.stop_count += 1
        self.order.append("preflight.stop")
        return _lifecycle_result(True, "stopped")

    def run_once(self, request):
        self.run_count += 1
        self.requests.append(request)
        self.order.append("pipeline.run_once")
        return self.result


def test_default_cli_values_match_verified_raspberry_pi_configuration():
    args = launcher.build_parser().parse_args([])

    assert args.microphone_device == "plughw:2,0"
    assert args.speaker_device == "plughw:CARD=Device,DEV=0"
    assert args.language == "en"
    assert args.whisper_command == "external/whisper.cpp/build/bin/whisper-cli"
    assert args.whisper_model == "models/whisper/ggml-base.en.bin"
    assert args.voice_profile == "en_US-hfc_male-medium"
    assert args.timeout == 300.0
    assert args.fixed_duration is False
    assert args.record_seconds == 5
    assert args.diagnostic_routing is False
    assert args.retain_diagnostic_audio is False
    assert args.play_diagnostic_audio is False
    assert args.no_playback is False


def test_repository_relative_paths_resolve_from_script_root_not_working_directory(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    args = launcher.build_parser().parse_args([])
    manual_args = launcher.single_turn.build_parser().parse_args(
        launcher._single_turn_arguments(args)
    )

    request = launcher.single_turn.request_from_args(manual_args)

    assert Path(request.whisper_executable_path) == (
        launcher.REPO_ROOT / launcher.DEFAULT_WHISPER_COMMAND
    ).resolve()
    assert Path(request.whisper_model_profile) == (
        launcher.REPO_ROOT / launcher.DEFAULT_WHISPER_MODEL
    ).resolve()
    assert Path(request.recording_output_path).is_absolute()
    assert Path(request.recording_output_path).is_relative_to(launcher.REPO_ROOT)


def test_launcher_uses_existing_factory_and_single_turn_pipeline(monkeypatch):
    order = []
    pipeline = FakePipeline(order=order)
    factory_calls = []

    def factory(args, output_func=print):
        factory_calls.append(args)
        return pipeline

    monkeypatch.setattr(launcher.single_turn, "create_pipeline", factory)

    code = launcher.run_ares_voice([], output_func=lambda text: order.append(f"out:{text}"))

    assert code == launcher.EXIT_SUCCESS
    assert len(factory_calls) == 1
    assert pipeline.run_count == 1
    assert order.index("out:ARES is listening...") < order.index("pipeline.run_once")
    assert pipeline.start_count == 1
    assert pipeline.health_count == 1
    assert pipeline.stop_count == 1


def test_default_request_enables_auto_stop_and_response_playback_only():
    pipeline = FakePipeline()

    code = launcher.run_ares_voice([], output_func=lambda _: None, pipeline=pipeline)

    request = pipeline.requests[0]
    assert code == launcher.EXIT_SUCCESS
    assert request.capture_mode == CAPTURE_MODE_AUTO_STOP
    assert request.playback_enabled is True
    assert request.diagnostic_audio is False
    assert request.cleanup_policy == "delete_on_success"
    assert request.tts_voice_profile == "en_US-hfc_male-medium"
    assert request.metadata["source"] == "run_ares_voice"
    assert request.metadata["owner_triggered"] is True
    assert request.metadata["interaction_mode"] == "single_turn"


def test_pipeline_factory_failure_is_reported_before_capture():
    output = []

    def failing_factory(args, output_func=print):
        raise ValueError("invalid local voice configuration")

    code = launcher.run_ares_voice(
        [],
        output_func=output.append,
        pipeline_factory=failing_factory,
    )

    assert code == launcher.EXIT_DEPENDENCY_FAILURE
    assert "before microphone capture" in output[0]
    assert "construction_failed (ValueError)" in output[1]
    assert "voice profile configuration" in output[1]
    assert "ARES is listening..." not in output


def test_no_playback_override_disables_only_response_playback():
    pipeline = FakePipeline()

    code = launcher.run_ares_voice(
        ["--no-playback"],
        output_func=lambda _: None,
        pipeline=pipeline,
    )

    assert code == launcher.EXIT_SUCCESS
    assert pipeline.requests[0].playback_enabled is False
    assert pipeline.requests[0].diagnostic_audio is False


def test_fixed_duration_and_record_seconds_are_forwarded():
    pipeline = FakePipeline()

    code = launcher.run_ares_voice(
        ["--fixed-duration", "--record-seconds", "8"],
        output_func=lambda _: None,
        pipeline=pipeline,
    )

    assert code == launcher.EXIT_SUCCESS
    assert pipeline.requests[0].capture_mode == CAPTURE_MODE_FIXED_DURATION
    assert pipeline.requests[0].recording_duration_seconds == 8


def test_major_cli_overrides_reach_the_versioned_request(tmp_path):
    pipeline = FakePipeline()
    whisper_command = tmp_path / "whisper-cli"
    whisper_model = tmp_path / "model.bin"

    code = launcher.run_ares_voice(
        [
            "--microphone-device",
            "plughw:4,1",
            "--speaker-device",
            "plughw:CARD=Other,DEV=1",
            "--language",
            "ro",
            "--whisper-command",
            str(whisper_command),
            "--whisper-model",
            str(whisper_model),
            "--voice-profile",
            "en_US-amy-low",
            "--timeout",
            "120",
            "--diagnostic-routing",
            "--retain-diagnostic-audio",
            "--no-playback",
        ],
        output_func=lambda _: None,
        pipeline=pipeline,
    )

    request = pipeline.requests[0]
    assert code == launcher.EXIT_SUCCESS
    assert request.microphone_device == "plughw:4,1"
    assert request.speaker_device == "plughw:CARD=Other,DEV=1"
    assert request.language == "ro"
    assert Path(request.whisper_executable_path) == whisper_command
    assert Path(request.whisper_model_profile) == whisper_model
    assert request.tts_voice_profile == "en_US-amy-low"
    assert request.timeout_seconds == 120
    assert request.playback_enabled is False
    assert request.diagnostic_audio is True
    assert request.cleanup_policy == "keep"


def test_play_diagnostic_audio_is_opt_in_and_implies_retention():
    args = launcher.build_parser().parse_args(["--play-diagnostic-audio"])
    manual_args = launcher.single_turn.build_parser().parse_args(
        launcher._single_turn_arguments(args)
    )
    request = launcher.single_turn.request_from_args(manual_args)

    assert manual_args.play_diagnostic_audio is True
    assert request.diagnostic_audio is True
    assert request.cleanup_policy == "keep"


@pytest.mark.parametrize(
    ("component", "status", "reason", "expected_action"),
    [
        (
            "speech_to_text",
            "whisper_binary_missing",
            "whisper_binary_missing",
            "--whisper-command",
        ),
        (
            "speech_to_text",
            "whisper_model_missing",
            "whisper_model_missing",
            "--whisper-model",
        ),
        (
            "text_to_speech",
            "piper_executable_missing",
            "piper_executable_missing",
            "install_piper_raspberry_pi.py",
        ),
        (
            "text_to_speech",
            "model_missing",
            "voice_model_missing",
            "en_US-hfc_male-medium",
        ),
        (
            "text_to_speech",
            "profile_config_invalid",
            "config_missing",
            "config/voice_profiles.json",
        ),
    ],
)
def test_missing_runtime_dependency_fails_before_pipeline_capture(
    component,
    status,
    reason,
    expected_action,
):
    pipeline = FakePipeline(
        health=_health_result(False, [(component, status, reason)])
    )
    output = []

    code = launcher.run_ares_voice([], output_func=output.append, pipeline=pipeline)

    assert code == launcher.EXIT_DEPENDENCY_FAILURE
    assert pipeline.run_count == 0
    assert pipeline.stop_count == 1
    assert "before microphone capture" in output[0]
    assert expected_action in "\n".join(output)
    assert "ARES is listening..." not in output


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_success_result(), launcher.EXIT_SUCCESS),
        (
            SingleTurnVoiceResultV1(
                success=False,
                status="transcript_rejected",
                error_stage="transcript_normalization",
                error_reason="unsupported_arithmetic_word",
            ),
            launcher.EXIT_INPUT_REJECTED,
        ),
        (
            SingleTurnVoiceResultV1(
                success=False,
                status="tts_failed",
                error_stage="synthesis",
                error_reason="piper_failed",
            ),
            launcher.EXIT_PIPELINE_FAILURE,
        ),
    ],
)
def test_launcher_exit_codes_for_pipeline_results(result, expected):
    pipeline = FakePipeline(result=result)

    code = launcher.run_ares_voice([], output_func=lambda _: None, pipeline=pipeline)

    assert code == expected
    assert pipeline.run_count == 1


def test_launcher_source_does_not_enable_monitoring_or_duplicate_pipeline_stages():
    source = Path(launcher.__file__).read_text(encoding="utf-8")

    forbidden = (
        "import subprocess",
        "subprocess.",
        "shell=True",
        "amixer",
        "configure_linux_alsa_monitoring",
        ".record(",
        ".transcribe(",
        ".synthesize(",
        ".play_wav(",
    )
    assert all(token not in source for token in forbidden)
    assert "single_turn.create_pipeline" in source
    assert "active_pipeline.run_once(request)" in source
