from core import (
    CAPTURE_MODE_AUTO_STOP,
    CoreService,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockTextToSpeechAdapter,
    MultiTurnVoiceSession,
    MultiTurnVoiceSessionRequestV1,
    MicrophoneResult,
    RmsVoiceActivityCapture,
    SingleTurnVoiceRequestV1,
    SingleTurnVoicePipeline,
    SpeakerPlaybackResult,
    VoiceActivityCaptureRequestV1,
    VAD_STATUS_COMPLETED_AFTER_SILENCE,
)
from events import EventBus
from scripts.manual_verify_single_turn_voice import build_existing_brain_handler
from skills import SkillManager
from skills.builtin.calculator import CalculatorSkill


class NoAudioSpeaker:
    playing = False

    def __init__(self):
        self.play_count = 0
        self.stop_count = 0

    def health_check(self):
        return SpeakerPlaybackResult(True, "healthy", "No-audio speaker is healthy.")

    def start(self):
        return SpeakerPlaybackResult(True, "started", "No-audio speaker started.")

    def stop(self):
        self.stop_count += 1
        return SpeakerPlaybackResult(True, "stopped", "No-audio speaker stopped.")

    def cancel_current(self):
        return None

    def play_wav(self, *args, **kwargs):
        self.play_count += 1
        return SpeakerPlaybackResult(True, "played", "No-audio playback completed.")


class SyntheticFrameSource:
    def __init__(self, frames):
        self.frames = list(frames)

    def read_frame(self, frame_bytes, timeout_seconds):
        if not self.frames:
            raise EOFError("synthetic_stream_complete")
        return self.frames.pop(0)

    def close(self):
        return None


class SyntheticVadMicrophone:
    def __init__(self, frames):
        self.frames = list(frames)
        self.detector = RmsVoiceActivityCapture()
        self.active = False

    def health_check(self):
        return MicrophoneResult(True, "healthy")

    def start(self):
        self.active = True
        self.detector.start()
        return MicrophoneResult(True, "started")

    def stop(self):
        self.active = False
        self.detector.stop()
        return MicrophoneResult(True, "stopped")

    def cancel_current(self):
        return None

    def record_until_silence(self, output_path, **kwargs):
        return self.detector.execute(
            VoiceActivityCaptureRequestV1(
                output_wav_path=str(output_path),
                microphone_device=kwargs.get("device") or "synthetic",
                frame_duration_ms=kwargs["frame_duration_ms"],
                calibration_enabled=kwargs["calibration_enabled"],
                calibration_duration_seconds=kwargs["calibration_duration_seconds"],
                speech_start_rms=kwargs["speech_start_rms"],
                speech_continue_rms=kwargs["speech_continue_rms"],
                silence_rms=kwargs["silence_rms"],
                required_speech_frames=kwargs["required_speech_frames"],
                required_continue_frames=kwargs["required_continue_frames"],
                required_silence_frames=kwargs["required_silence_frames"],
                silence_duration_seconds=kwargs["silence_seconds"],
                speech_wait_timeout_seconds=kwargs["speech_wait_timeout_seconds"],
                maximum_utterance_seconds=kwargs["maximum_utterance_seconds"],
                pre_roll_seconds=kwargs["pre_roll_seconds"],
                minimum_speech_start_rms=kwargs["minimum_speech_start_rms"],
                maximum_speech_start_rms=kwargs["maximum_speech_start_rms"],
                minimum_speech_continue_rms=kwargs["minimum_speech_continue_rms"],
                maximum_speech_continue_rms=kwargs["maximum_speech_continue_rms"],
                minimum_silence_rms=kwargs["minimum_silence_rms"],
                maximum_silence_rms=kwargs["maximum_silence_rms"],
                frame_debug_enabled=kwargs["frame_debug_enabled"],
                correlation_id=kwargs["correlation_id"],
                session_id=kwargs["session_id"],
            ),
            SyntheticFrameSource(self.frames),
            cancel_requested=kwargs.get("cancel_requested"),
        )


def _pcm_frame(amplitude):
    return int(amplitude).to_bytes(2, "little", signed=True) * 320


def test_multi_turn_session_reuses_real_brain_route_for_each_non_stop_turn(tmp_path):
    core_service = CoreService()
    manager = SkillManager(event_bus=EventBus(), core_service=core_service)
    manager.register(CalculatorSkill())
    speaker = NoAudioSpeaker()
    pipeline = SingleTurnVoicePipeline(
        microphone_adapter=MockMicrophoneAdapter(),
        speech_to_text_adapter=MockSpeechToTextAdapter(),
        text_to_speech_adapter=MockTextToSpeechAdapter(),
        speaker_adapter=speaker,
        command_handler=build_existing_brain_handler(manager),
        core_service=core_service,
    )
    session = MultiTurnVoiceSession(pipeline, sleeper=lambda _: None)
    request = MultiTurnVoiceSessionRequestV1(
        recording_output_directory=str(tmp_path),
        recording_duration_seconds=1,
        simulated_text_turns=[
            "calculate 2 + 2",
            "calculate 3 * 5",
            "goodbye Ares",
        ],
        maximum_turns=5,
        inter_turn_delay_seconds=0,
        greeting_enabled=False,
        closing_phrase_enabled=False,
        correlation_id="integration-session-corr",
        session_id="integration-session",
    )

    result = session.run_session(request)

    assert result.success is True
    assert result.attempted_turns == 3
    assert result.successful_turns == 2
    assert [turn["brain_text_response"] for turn in result.turn_summaries] == [
        "Result: 4",
        "Result: 15",
        "",
    ]
    assert [turn["detected_intent"] for turn in result.turn_summaries[:2]] == [
        "calculate",
        "calculate",
    ]
    assert [turn["routed_skill"] for turn in result.turn_summaries[:2]] == [
        "calculator",
        "calculator",
    ]
    assert manager.last_plan is not None
    assert manager.last_execution is not None
    assert result.recognized_stop_phrase == "goodbye Ares"
    assert speaker.play_count == 0
    usage = pipeline.resource_manager.current_usage()
    assert usage["active_task_count"] == 0
    assert "multi_turn_voice_session" not in usage["reservation_names"]
    assert "single_turn_voice_pipeline" not in usage["reservation_names"]
    assert "voice" in usage["reservation_names"]


def test_stop_phrase_never_reaches_real_skill_manager(monkeypatch, tmp_path):
    core_service = CoreService()
    manager = SkillManager(event_bus=EventBus(), core_service=core_service)
    manager.register(CalculatorSkill())
    handled = []
    original_handle = manager.handle

    def recording_handle(text, *args, **kwargs):
        handled.append(str(text))
        return original_handle(text, *args, **kwargs)

    monkeypatch.setattr(manager, "handle", recording_handle)
    pipeline = SingleTurnVoicePipeline(
        MockMicrophoneAdapter(),
        MockSpeechToTextAdapter(),
        MockTextToSpeechAdapter(),
        NoAudioSpeaker(),
        build_existing_brain_handler(manager),
        core_service=core_service,
    )
    session = MultiTurnVoiceSession(pipeline, sleeper=lambda _: None)

    result = session.run_session(
        MultiTurnVoiceSessionRequestV1(
            recording_output_directory=str(tmp_path),
            recording_duration_seconds=1,
            simulated_text_turns=["calculate 5 + 5", "goodbye Ares"],
            inter_turn_delay_seconds=0,
            greeting_enabled=False,
            closing_phrase_enabled=False,
        )
    )

    assert result.success is True
    assert handled == ["calculate 5 + 5"]


def test_synthetic_pcm_vad_routes_trimmed_utterance_through_real_brain(tmp_path):
    core_service = CoreService()
    manager = SkillManager(event_bus=EventBus(), core_service=core_service)
    manager.register(CalculatorSkill())
    microphone = SyntheticVadMicrophone(
        [_pcm_frame(40), _pcm_frame(450), _pcm_frame(500), *([_pcm_frame(20)] * 5)]
    )
    stt = MockSpeechToTextAdapter(transcripts=["calculate 2 + 2"])
    speaker = NoAudioSpeaker()
    pipeline = SingleTurnVoicePipeline(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
        text_to_speech_adapter=MockTextToSpeechAdapter(),
        speaker_adapter=speaker,
        command_handler=build_existing_brain_handler(manager),
        core_service=core_service,
    )

    result = pipeline.run_once(
        SingleTurnVoiceRequestV1(
            recording_output_path=str(tmp_path / "detected.wav"),
            recording_duration_seconds=1,
            capture_mode=CAPTURE_MODE_AUTO_STOP,
            calibration_enabled=False,
            speech_start_rms=200,
            silence_rms=120,
            required_speech_frames=2,
            silence_duration_seconds=0.1,
            speech_wait_timeout_seconds=0.1,
            maximum_utterance_seconds=0.2,
            pre_roll_seconds=0.02,
            playback_enabled=False,
            cleanup_policy="keep",
        )
    )

    assert result.success is True
    assert result.recording_status == "completed_after_silence"
    assert result.recognized_text == "calculate 2 + 2"
    assert result.detected_intent == "calculate"
    assert result.routed_skill == "calculator"
    assert result.brain_text_response == "Result: 4"
    assert stt.transcription_count == 1
    assert speaker.play_count == 0
    assert microphone.active is False


def test_multi_turn_session_reuses_auto_stop_single_turn_with_cancellation_token(tmp_path):
    core_service = CoreService()
    manager = SkillManager(event_bus=EventBus(), core_service=core_service)
    manager.register(CalculatorSkill())
    microphone = SyntheticVadMicrophone(
        [_pcm_frame(40), _pcm_frame(450), _pcm_frame(500), *([_pcm_frame(20)] * 5)]
    )
    stt = MockSpeechToTextAdapter(transcripts=["calculate 3 * 5"])
    pipeline = SingleTurnVoicePipeline(
        microphone,
        stt,
        MockTextToSpeechAdapter(),
        NoAudioSpeaker(),
        build_existing_brain_handler(manager),
        core_service=core_service,
    )
    session = MultiTurnVoiceSession(pipeline, sleeper=lambda _: None)

    result = session.run_session(
        MultiTurnVoiceSessionRequestV1(
            recording_output_directory=str(tmp_path),
            recording_duration_seconds=1,
            capture_mode=CAPTURE_MODE_AUTO_STOP,
            calibration_enabled=False,
            speech_start_rms=200,
            silence_rms=120,
            required_speech_frames=2,
            silence_duration_seconds=0.1,
            speech_wait_timeout_seconds=0.1,
            maximum_utterance_seconds=0.2,
            pre_roll_seconds=0.02,
            maximum_turns=1,
            inter_turn_delay_seconds=0,
            greeting_enabled=False,
            closing_phrase_enabled=False,
            cleanup_policy="keep",
        )
    )

    assert result.success is True
    assert result.maximum_turns_reached is True
    assert result.successful_turns == 1
    assert result.turn_summaries[0]["brain_text_response"] == "Result: 15"
    assert stt.transcription_count == 1
    assert microphone.active is False


def test_adaptive_calibration_single_turn_routes_only_trimmed_speech(tmp_path):
    core_service = CoreService()
    manager = SkillManager(event_bus=EventBus(), core_service=core_service)
    manager.register(CalculatorSkill())
    microphone = SyntheticVadMicrophone(
        [*([_pcm_frame(40)] * 5), _pcm_frame(450), _pcm_frame(500), *([_pcm_frame(20)] * 5)]
    )
    pipeline = SingleTurnVoicePipeline(
        microphone,
        MockSpeechToTextAdapter(transcripts=["what is two plus two?"]),
        MockTextToSpeechAdapter(),
        NoAudioSpeaker(),
        build_existing_brain_handler(manager),
        core_service=core_service,
    )

    result = pipeline.run_once(
        SingleTurnVoiceRequestV1(
            recording_output_path=str(tmp_path / "adaptive.wav"),
            recording_duration_seconds=1,
            capture_mode=CAPTURE_MODE_AUTO_STOP,
            calibration_enabled=True,
            calibration_duration_seconds=0.1,
            required_speech_frames=2,
            required_continue_frames=2,
            required_silence_frames=3,
            silence_duration_seconds=0.1,
            speech_wait_timeout_seconds=0.1,
            maximum_utterance_seconds=0.2,
            pre_roll_seconds=0.02,
            playback_enabled=False,
            cleanup_policy="keep",
        )
    )

    assert result.success is True
    assert result.data["recording"]["calibration_enabled"] is True
    assert result.data["recording"]["stop_reason"] == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.normalized_command == "calculate 2 + 2"
    assert result.brain_text_response == "Result: 4"


def test_adaptive_calibration_is_reused_by_bounded_multi_turn_session(tmp_path):
    core_service = CoreService()
    manager = SkillManager(event_bus=EventBus(), core_service=core_service)
    manager.register(CalculatorSkill())
    microphone = SyntheticVadMicrophone(
        [*([_pcm_frame(40)] * 5), _pcm_frame(500), _pcm_frame(550), *([_pcm_frame(20)] * 5)]
    )
    pipeline = SingleTurnVoicePipeline(
        microphone,
        MockSpeechToTextAdapter(transcripts=["two plus two"]),
        MockTextToSpeechAdapter(),
        NoAudioSpeaker(),
        build_existing_brain_handler(manager),
        core_service=core_service,
    )
    session = MultiTurnVoiceSession(pipeline, sleeper=lambda _: None)

    result = session.run_session(
        MultiTurnVoiceSessionRequestV1(
            recording_output_directory=str(tmp_path),
            recording_duration_seconds=1,
            capture_mode=CAPTURE_MODE_AUTO_STOP,
            calibration_enabled=True,
            calibration_duration_seconds=0.1,
            required_speech_frames=2,
            required_continue_frames=2,
            required_silence_frames=3,
            silence_duration_seconds=0.1,
            speech_wait_timeout_seconds=0.1,
            maximum_utterance_seconds=0.2,
            pre_roll_seconds=0.02,
            maximum_turns=1,
            inter_turn_delay_seconds=0,
            greeting_enabled=False,
            closing_phrase_enabled=False,
            cleanup_policy="keep",
        )
    )

    assert result.success is True
    assert result.successful_turns == 1
    assert result.turn_summaries[0]["normalized_command"] == "calculate 2 + 2"
    assert result.turn_summaries[0]["brain_text_response"] == "Result: 4"
