from pathlib import Path

from core import SingleTurnVoiceResultV1
from core.Intent import Intent
from scripts import manual_verify_single_turn_voice as manual


class StubPipeline:
    def __init__(self, result):
        self.result = result
        self.requests = []
        self.stop_calls = []

    def run_once(self, request):
        self.requests.append(request)
        return self.result

    def stop(self, request):
        self.stop_calls.append(request)


def _success_result():
    return SingleTurnVoiceResultV1(
        success=True,
        status="completed",
        recognized_text="what time is it",
        detected_intent="time_date",
        routed_skill="time_date",
        brain_text_response="The local time is 10:00.",
        resolved_voice_profile="en_US-hfc_male-medium",
        playback_status="playback_disabled",
        total_processing_time_seconds=1.25,
    )


def test_manual_script_import_is_safe_and_defaults_are_hardware_specific_only_as_config():
    parser = manual.build_parser()
    args = parser.parse_args([])

    assert callable(manual.run_manual_verification)
    assert args.microphone_device == "hw:2,0"
    assert args.speaker_device == "plughw:CARD=Device,DEV=0"
    assert args.whisper_model == "models/whisper/ggml-tiny.en.bin"
    assert args.playback is False


def test_manual_text_simulation_builds_request_and_prints_concise_summary():
    outputs = []
    pipeline = StubPipeline(_success_result())

    exit_code = manual.run_manual_verification(
        ["--text-input", "what time is it"],
        output_func=outputs.append,
        pipeline=pipeline,
    )

    assert exit_code == 0
    assert pipeline.requests[0].text_input == "what time is it"
    assert pipeline.requests[0].playback_enabled is False
    assert outputs[0] == manual.WARNING
    assert "Speaker playback is disabled" in outputs[1]
    assert "Recognized text: what time is it" in outputs
    assert "Detected intent/skill: time_date" in outputs
    assert "ARES response: The local time is 10:00." in outputs
    assert "Final status: completed" in outputs
    assert not any("{'" in line for line in outputs)


def test_manual_real_arguments_are_forwarded_to_versioned_request():
    pipeline = StubPipeline(_success_result())

    exit_code = manual.run_manual_verification(
        [
            "--record-seconds", "5",
            "--microphone-device", "hw:2,0",
            "--speaker-device", "plughw:CARD=Device,DEV=0",
            "--language", "en",
            "--whisper-model", "models/whisper/ggml-tiny.en.bin",
            "--voice-profile", "en_US-hfc_male-medium",
            "--min-rms", "25",
            "--playback",
            "--keep-audio",
            "--timeout", "180",
        ],
        output_func=lambda text: None,
        pipeline=pipeline,
    )

    request = pipeline.requests[0]
    assert exit_code == 0
    assert request.recording_duration_seconds == 5
    assert request.microphone_device == "hw:2,0"
    assert request.speaker_device == "plughw:CARD=Device,DEV=0"
    assert request.language == "en"
    assert request.whisper_model_profile.endswith("models\\whisper\\ggml-tiny.en.bin") or request.whisper_model_profile.endswith("models/whisper/ggml-tiny.en.bin")
    assert request.tts_voice_profile == "en_US-hfc_male-medium"
    assert request.minimum_rms == 25
    assert request.playback_enabled is True
    assert request.cleanup_policy == "keep"
    assert request.timeout_seconds == 180


def test_manual_auto_stop_arguments_are_forwarded_without_changing_intent_safety():
    pipeline = StubPipeline(_success_result())

    exit_code = manual.run_manual_verification(
        [
            "--auto-stop",
            "--speech-start-rms", "220",
            "--silence-rms", "110",
            "--silence-seconds", "0.8",
            "--speech-wait-timeout", "9",
            "--max-utterance-seconds", "14",
            "--pre-roll-seconds", "0.3",
            "--frame-ms", "30",
            "--required-speech-frames", "4",
        ],
        output_func=lambda text: None,
        pipeline=pipeline,
    )

    request = pipeline.requests[0]
    assert exit_code == 0
    assert request.capture_mode == "auto_stop"
    assert request.speech_start_rms == 220
    assert request.silence_rms == 110
    assert request.silence_duration_seconds == 0.8
    assert request.speech_wait_timeout_seconds == 9
    assert request.maximum_utterance_seconds == 14
    assert request.pre_roll_seconds == 0.3
    assert request.frame_duration_ms == 30
    assert request.required_speech_frames == 4


def test_manual_failure_returns_nonzero_and_prints_stage_reason():
    outputs = []
    pipeline = StubPipeline(
        SingleTurnVoiceResultV1(
            success=False,
            status="transcription_failed",
            error_stage="transcription",
            error_reason="whisper_fail",
        )
    )

    exit_code = manual.run_manual_verification(
        ["--text-input", "hello"],
        output_func=outputs.append,
        pipeline=pipeline,
    )

    assert exit_code == 2
    assert "Failure stage: transcription" in outputs
    assert "Failure reason: whisper_fail" in outputs


def test_verbose_mode_is_the_only_mode_that_prints_full_json():
    outputs = []
    pipeline = StubPipeline(_success_result())

    manual.run_manual_verification(
        ["--text-input", "hello", "--verbose"],
        output_func=outputs.append,
        pipeline=pipeline,
    )

    assert any('"contract_name": "voice.single_turn.result"' in line for line in outputs)


def test_manual_script_has_no_subprocess_or_hardware_command_execution_logic():
    source = Path(manual.__file__).read_text(encoding="utf-8")

    assert "import subprocess" not in source
    assert "subprocess." not in source
    assert "shell=True" not in source
    assert "arecord" not in source
    assert "aplay" not in source
    assert "record_conversation_turn" not in source


def test_existing_brain_handler_returns_safe_local_unknown_response():
    class UnknownManager:
        def parse_intent(self, text):
            return Intent("unknown", 0.0, {}, text)

        def handle(self, text, run_before_intents=True):
            return None

    response = manual.build_existing_brain_handler(UnknownManager())("unsupported request")

    assert response.text == "I cannot handle that request yet."
    assert response.skill == "unknown"
    assert response.metadata["detected_intent"] == "unknown"


def test_brain_and_core_service_do_not_import_audio_runtime_implementations():
    repo_root = Path(manual.__file__).resolve().parent.parent
    brain_boundary_sources = [
        (repo_root / "core" / "IntentParser.py").read_text(encoding="utf-8"),
        (repo_root / "core" / "Planner.py").read_text(encoding="utf-8"),
        (repo_root / "skills" / "manager.py").read_text(encoding="utf-8"),
    ]
    core_service_source = (repo_root / "core" / "CoreService.py").read_text(encoding="utf-8")

    forbidden = ("LinuxAlsa", "LinuxWhisper", "LinuxPiper", "arecord", "aplay", "whisper-cli")
    assert all(token not in source for source in brain_boundary_sources for token in forbidden)
    assert all(token not in core_service_source for token in forbidden)
