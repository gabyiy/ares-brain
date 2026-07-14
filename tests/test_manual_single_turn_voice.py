from pathlib import Path

import pytest

from core import SingleTurnVoiceResultV1, SpeakerPlaybackResult
from core.Intent import Intent
from scripts import manual_verify_single_turn_voice as manual


class StubPipeline:
    def __init__(self, result, speaker_adapter=None):
        self.result = result
        self.requests = []
        self.stop_calls = []
        self.speaker_adapter = speaker_adapter

    def run_once(self, request):
        self.requests.append(request)
        return self.result

    def stop(self, request):
        self.stop_calls.append(request)


class FakeSpeaker:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")
        return SpeakerPlaybackResult(True, "started")

    def play_wav(self, wav_path, device=None, timeout_seconds=None):
        self.calls.append(("play", str(wav_path), device, timeout_seconds))
        return SpeakerPlaybackResult(True, "played", wav_path=str(wav_path))

    def stop(self):
        self.calls.append("stop")
        return SpeakerPlaybackResult(True, "stopped")


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


def _diagnostic_result(tmp_path, success=True, status="completed"):
    raw = tmp_path / "raw_capture.wav"
    assembled = tmp_path / "assembled_utterance.wav"
    normalized = tmp_path / "normalized_whisper_input.wav"
    for path in (raw, assembled, normalized):
        path.write_bytes(b"diagnostic-audio")
    result = SingleTurnVoiceResultV1(
        success=success,
        status=status,
        raw_transcript="Hello Ares, what is two plus two?",
        recorded_wav_path=str(normalized),
        data={
            "recording": {
                "success": True,
                "status": "completed_after_silence",
                "data": {
                    "raw_wav_path": str(raw),
                    "assembled_wav_path": str(assembled),
                    "normalized_wav_path": str(normalized),
                    "final_whisper_input_path": str(normalized),
                },
            }
        },
    )
    return result, raw, assembled, normalized


def test_manual_script_import_is_safe_and_defaults_are_hardware_specific_only_as_config():
    parser = manual.build_parser()
    args = parser.parse_args([])

    assert callable(manual.run_manual_verification)
    assert args.microphone_device == "plughw:2,0"
    assert args.speaker_device == "plughw:CARD=Device,DEV=0"
    assert args.whisper_model == "models/whisper/ggml-base.en.bin"
    assert args.playback is False
    assert args.preserve_diagnostic_audio is False
    assert args.diagnostic_audio is False
    assert args.play_diagnostic_capture is False
    assert args.play_diagnostic_assembled is False
    assert args.play_diagnostic_normalized is False
    assert args.play_diagnostic_audio is False
    assert args.playback_debug_stages is False
    assert args.diagnostic_routing is False
    assert args.duration_loss_tolerance == 0.05


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


def test_diagnostic_routing_flag_prints_bounded_structured_report():
    outputs = []
    pipeline = StubPipeline(
        SingleTurnVoiceResultV1(
            success=True,
            status="completed",
            raw_transcript="Calculate 2 plus 2.",
            cleaned_transcript="Calculate 2 plus 2",
            normalized_command="calculate 2 + 2",
            extracted_calculator_expression="2 plus 2",
            transcript_cleanup_rule="calculator_natural_language_wrapper",
            detected_intent="calculate",
            candidate_skills=[
                {
                    "skill": "calculator",
                    "considered": True,
                    "eligible": True,
                    "selected": True,
                    "confidence": 1.0,
                    "reason": "structured intent match: calculate",
                }
            ],
            routed_skill="calculator",
            planner_decision="1 step(s): calculator",
            execution_result="success",
            brain_text_response="Result: 4",
        )
    )

    exit_code = manual.run_manual_verification(
        ["--text-input", "Calculate 2 plus 2.", "--diagnostic-routing"],
        output_func=outputs.append,
        pipeline=pipeline,
    )

    assert exit_code == 0
    assert "Routing diagnostics" in outputs
    assert "Raw transcript: Calculate 2 plus 2." in outputs
    assert "Cleaned transcript: Calculate 2 plus 2" in outputs
    assert "Normalized command: calculate 2 + 2" in outputs
    assert "Extracted calculator expression: 2 plus 2" in outputs
    assert (
        "Transcript cleanup rule: calculator_natural_language_wrapper" in outputs
    )
    assert "Parsed intent: calculate" in outputs
    assert "Candidate skills: calculator (confidence=1.000, selected)" in outputs
    assert "Selected skill: calculator" in outputs
    assert "Planner decision: 1 step(s): calculator" in outputs
    assert "Execution result: success" in outputs
    assert "Rejection reason: (none)" in outputs


def test_diagnostic_routing_prints_bounded_owner_memory_operation_fields(tmp_path):
    outputs = []
    profile_path = tmp_path / "owner_profile.json"
    pipeline = StubPipeline(
        SingleTurnVoiceResultV1(
            success=True,
            status="completed",
            raw_transcript="Remember that modified white color is blue.",
            cleaned_transcript="Remember that modified white color is blue",
            normalized_command="remember that my favorite color is blue",
            transcript_cleanup_rule="owner_memory_whisper_alias_v1",
            detected_intent="owner_memory",
            candidate_skills=[
                {
                    "skill": "owner_memory",
                    "considered": True,
                    "eligible": True,
                    "selected": True,
                    "confidence": 1.0,
                    "reason": "structured intent match: owner_memory",
                },
                {
                    "skill": "tasks",
                    "considered": True,
                    "eligible": False,
                    "selected": False,
                    "confidence": 0.0,
                    "reason": "intent mismatch",
                },
            ],
            routed_skill="owner_memory",
            planner_decision="1 step(s): owner_memory",
            execution_result="success",
            brain_text_response="I will remember that your favorite color is blue.",
            routing_diagnostics={
                "owner_memory": {
                    "action": "save",
                    "normalized_key": "favorite_color",
                    "extracted_value": "blue",
                    "profile_path": str(profile_path),
                    "file_existed_before": False,
                    "operation_result": "created",
                }
            },
        )
    )

    exit_code = manual.run_manual_verification(
        [
            "--text-input",
            "Remember that modified white color is blue.",
            "--diagnostic-routing",
        ],
        output_func=outputs.append,
        pipeline=pipeline,
    )

    assert exit_code == 0
    assert "Parsed owner-memory action: save" in outputs
    assert "Extracted owner-memory key: favorite_color" in outputs
    assert "Extracted owner-memory value: blue" in outputs
    assert f"Resolved owner-profile path: {profile_path}" in outputs
    assert "Owner-profile file existed before operation: False" in outputs
    assert "Owner-memory operation result: created" in outputs
    assert "Selected skill: owner_memory" in outputs


def test_diagnostic_audio_prints_requested_actual_and_normalized_formats():
    outputs = []
    result = SingleTurnVoiceResultV1(
        success=True,
        status="completed",
        recorded_wav_path="normalized.wav",
        data={
            "recording": {
                "success": True,
                "status": "recorded",
                "data": {
                    "requested_device": "hw:2,0",
                    "resolved_capture_device": "plughw:2,0",
                    "requested_sample_rate_hz": 16000,
                    "actual_sample_rate_hz": 44100,
                    "actual_channels": 1,
                    "actual_sample_width_bytes": 2,
                    "normalized_sample_rate_hz": 16000,
                    "normalized_channels": 1,
                    "normalized_sample_width_bytes": 2,
                    "raw_wav_path": "raw.wav",
                    "normalized_wav_path": "normalized.wav",
                    "raw_duration_seconds": 1.0,
                    "normalized_duration_seconds": 1.0,
                    "final_whisper_input_path": "normalized.wav",
                },
            }
        },
    )

    code = manual.run_manual_verification(
        ["--text-input", "status", "--diagnostic-audio"],
        output_func=outputs.append,
        pipeline=StubPipeline(result),
    )

    assert code == 0
    assert "Requested microphone device: hw:2,0" in outputs
    assert "Resolved capture device: plughw:2,0" in outputs
    assert "Actual captured sample rate: 44100 Hz" in outputs
    assert "Normalized sample rate: 16000 Hz" in outputs
    assert "Raw WAV path: raw.wav" in outputs
    assert "Final Whisper input path: normalized.wav" in outputs


def test_legacy_debug_stage_alias_implies_diagnostic_preservation():
    args = manual.build_parser().parse_args(["--playback-debug-stages"])

    assert manual._diagnostic_audio_requested(args) is True
    assert manual._selected_diagnostic_playback_stages(args) == (
        "raw capture",
        "assembled utterance",
        "normalized Whisper input",
    )


def test_diagnostic_audio_preserves_transcript_and_plays_stages_in_order(tmp_path):
    result, raw, assembled, normalized = _diagnostic_result(tmp_path)
    speaker = FakeSpeaker()
    pipeline = StubPipeline(result, speaker_adapter=speaker)
    outputs = []

    code = manual.run_manual_verification(
        [
            "--text-input",
            "status",
            "--preserve-diagnostic-audio",
            "--play-diagnostic-audio",
            "--speaker-device",
            "plughw:CARD=Device,DEV=0",
            "--timeout",
            "12",
        ],
        output_func=outputs.append,
        pipeline=pipeline,
    )

    assert code == 0
    assert speaker.calls == [
        "start",
        ("play", str(raw), "plughw:CARD=Device,DEV=0", 12.0),
        ("play", str(assembled), "plughw:CARD=Device,DEV=0", 12.0),
        ("play", str(normalized), "plughw:CARD=Device,DEV=0", 12.0),
        "stop",
    ]
    transcript_path = normalized.parent / "whisper_transcript.txt"
    assert transcript_path.read_text(encoding="utf-8") == (
        "Hello Ares, what is two plus two?\n"
    )
    assert "Whisper transcript output path: " + str(transcript_path) in outputs


def test_preserving_diagnostic_audio_never_implies_playback(tmp_path):
    result, _, _, normalized = _diagnostic_result(tmp_path)
    speaker = FakeSpeaker()
    pipeline = StubPipeline(result, speaker_adapter=speaker)

    code = manual.run_manual_verification(
        ["--text-input", "status", "--preserve-diagnostic-audio"],
        output_func=lambda _: None,
        pipeline=pipeline,
    )

    assert code == 0
    assert pipeline.requests[0].diagnostic_audio is True
    assert pipeline.requests[0].cleanup_policy == "keep"
    assert speaker.calls == []
    assert (normalized.parent / "whisper_transcript.txt").exists()


@pytest.mark.parametrize(
    ("flag", "label", "path_index"),
    [
        ("--play-diagnostic-capture", "raw capture", 0),
        ("--play-diagnostic-assembled", "assembled utterance", 1),
        ("--play-diagnostic-normalized", "normalized Whisper input", 2),
    ],
)
def test_individual_diagnostic_playback_flags_play_only_selected_stage(
    tmp_path,
    flag,
    label,
    path_index,
):
    result, raw, assembled, normalized = _diagnostic_result(tmp_path)
    paths = (raw, assembled, normalized)
    speaker = FakeSpeaker()
    outputs = []

    code = manual.run_manual_verification(
        ["--text-input", "status", flag],
        output_func=outputs.append,
        pipeline=StubPipeline(result, speaker_adapter=speaker),
    )

    assert code == 0
    assert speaker.calls == [
        "start",
        ("play", str(paths[path_index]), manual.DEFAULT_SPEAKER_DEVICE, 300.0),
        "stop",
    ]
    assert f"Playing diagnostic {label}: {paths[path_index]}" in outputs


def test_all_diagnostic_playback_deduplicates_individual_flags(tmp_path):
    result, raw, assembled, normalized = _diagnostic_result(tmp_path)
    speaker = FakeSpeaker()

    code = manual.run_manual_verification(
        [
            "--text-input",
            "status",
            "--play-diagnostic-audio",
            "--play-diagnostic-capture",
        ],
        output_func=lambda _: None,
        pipeline=StubPipeline(result, speaker_adapter=speaker),
    )

    assert code == 0
    assert [call[1] for call in speaker.calls if isinstance(call, tuple)] == [
        str(raw),
        str(assembled),
        str(normalized),
    ]


def test_response_playback_flag_does_not_replay_microphone_diagnostics(tmp_path):
    result, _, _, _ = _diagnostic_result(tmp_path)
    speaker = FakeSpeaker()
    pipeline = StubPipeline(result, speaker_adapter=speaker)

    code = manual.run_manual_verification(
        ["--text-input", "status", "--playback"],
        output_func=lambda _: None,
        pipeline=pipeline,
    )

    assert code == 0
    assert pipeline.requests[0].playback_enabled is True
    assert pipeline.requests[0].diagnostic_audio is False
    assert speaker.calls == []


def test_failure_does_not_replay_capture_without_explicit_diagnostic_flag(tmp_path):
    result, _, _, _ = _diagnostic_result(
        tmp_path,
        success=False,
        status="transcript_rejected",
    )
    speaker = FakeSpeaker()

    code = manual.run_manual_verification(
        ["--text-input", "unsupported"],
        output_func=lambda _: None,
        pipeline=StubPipeline(result, speaker_adapter=speaker),
    )

    assert code == 2
    assert speaker.calls == []


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
            "--calibration-seconds", "0.6",
            "--speech-start-rms", "220",
            "--speech-continue-rms", "165",
            "--silence-rms", "110",
            "--silence-seconds", "0.8",
            "--speech-wait-timeout", "9",
            "--max-utterance-seconds", "14",
            "--pre-roll-seconds", "0.3",
            "--frame-ms", "30",
            "--required-speech-frames", "4",
            "--required-continue-frames", "3",
            "--required-silence-frames", "6",
            "--duration-loss-tolerance", "0.08",
            "--frame-debug",
            "--diagnostic-audio",
        ],
        output_func=lambda text: None,
        pipeline=pipeline,
    )

    request = pipeline.requests[0]
    assert exit_code == 0
    assert request.capture_mode == "auto_stop"
    assert request.diagnostic_audio is True
    assert request.cleanup_policy == "keep"
    assert request.calibration_enabled is True
    assert request.calibration_duration_seconds == 0.6
    assert request.speech_start_rms == 220
    assert request.speech_continue_rms == 165
    assert request.silence_rms == 110
    assert request.silence_duration_seconds == 0.8
    assert request.speech_wait_timeout_seconds == 9
    assert request.maximum_utterance_seconds == 14
    assert request.pre_roll_seconds == 0.3
    assert request.frame_duration_ms == 30
    assert request.required_speech_frames == 4
    assert request.required_continue_frames == 3
    assert request.required_silence_frames == 6
    assert request.duration_loss_tolerance_seconds == 0.08
    assert request.frame_debug_enabled is True


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
        last_plan = None

        def parse_intent(self, text):
            return Intent("unknown", 0.0, {}, text)

        def candidate_diagnostics(self, text, run_before_intents=True):
            return []

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
