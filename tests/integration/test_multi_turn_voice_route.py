from core import (
    CoreService,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockTextToSpeechAdapter,
    MultiTurnVoiceSession,
    MultiTurnVoiceSessionRequestV1,
    SingleTurnVoicePipeline,
    SpeakerPlaybackResult,
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
