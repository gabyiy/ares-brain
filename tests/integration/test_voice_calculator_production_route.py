from pathlib import Path

import pytest

from core import (
    AudioChunk,
    CoreService,
    LIFECYCLE_STOPPED,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockTextToSpeechAdapter,
    SingleTurnVoiceResultV1,
    SpeakerPlaybackResult,
    normalize_transcript,
)
from events import EventBus, EventHistoryStore
from memory import GoalsStore, MemoryStore, NotesStore, TasksStore, UserProfileStore
from scripts import manual_verify_single_turn_voice as manual
from skills.builtin.calculator import CalculatorSkill


class NoAudioSpeaker:
    playing = False

    def __init__(self):
        self.play_count = 0

    def health_check(self):
        return SpeakerPlaybackResult(True, "healthy", "No-audio speaker is healthy.")

    def start(self):
        return SpeakerPlaybackResult(True, "started", "No-audio speaker started.")

    def stop(self):
        return SpeakerPlaybackResult(True, "stopped", "No-audio speaker stopped.")

    def cancel_current(self):
        return None

    def play_wav(self, *args, **kwargs):
        self.play_count += 1
        return SpeakerPlaybackResult(True, "played", "No-audio playback completed.")


def _production_manager(tmp_path, core_service, event_bus):
    return manual.create_skill_manager(
        core_service,
        event_history_store=EventHistoryStore(tmp_path / "events.json"),
        event_bus=event_bus,
        memory_store=MemoryStore(
            short_path=tmp_path / "short_memory.json",
            long_path=tmp_path / "long_memory.json",
            event_bus=event_bus,
        ),
        profile_store=UserProfileStore(tmp_path / "profile.json", event_bus=event_bus),
        goals_store=GoalsStore(tmp_path / "goals.json", event_bus=event_bus),
        notes_store=NotesStore(tmp_path / "notes.json", event_bus=event_bus),
        tasks_store=TasksStore(tmp_path / "tasks.json", event_bus=event_bus),
    )


def _production_pipeline(tmp_path, transcript):
    core_service = CoreService()
    event_bus = EventBus()
    manager = _production_manager(tmp_path, core_service, event_bus)
    microphone = MockMicrophoneAdapter(
        chunks=[
            AudioChunk(
                data=(1200).to_bytes(2, "little", signed=True) * 1600,
                sample_rate_hz=16000,
                channels=1,
                sample_width_bytes=2,
                source="synthetic_production_route",
            )
        ]
    )
    stt = MockSpeechToTextAdapter(transcripts=[transcript])
    tts = MockTextToSpeechAdapter()
    speaker = NoAudioSpeaker()
    args = manual.build_parser().parse_args(
        [
            "--fixed-duration",
            "--record-seconds",
            "1",
            "--recording-output",
            str(tmp_path / "voice_calculator.wav"),
        ]
    )
    pipeline = manual.create_pipeline(
        args,
        output_func=lambda _: None,
        skill_manager=manager,
        event_history_store=EventHistoryStore(tmp_path / "pipeline_events.json"),
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
        text_to_speech_adapter=tts,
        speaker_adapter=speaker,
    )
    return pipeline, manager, core_service, stt, tts, speaker, args


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("Calculate 2 plus 2.", "Result: 4"),
        ("calculate two plus two", "Result: 4"),
        ("please calculate 2 plus 2", "Result: 4"),
        ("can you calculate 2 plus 2", "Result: 4"),
        ("I want you to calculate two plus two", "Result: 4"),
        ("could you calculate two plus two", "Result: 4"),
        ("would you calculate 2 plus 2", "Result: 4"),
        ("What is 2 plus 2?", "Result: 4"),
        ("what's 2 plus 2", "Result: 4"),
        ("tell me what 2 plus 2 is", "Result: 4"),
        ("Ares calculate 2 plus 2", "Result: 4"),
        ("Ares, please calculate two plus two", "Result: 4"),
        ("Hello Ares, what is two plus two?", "Result: 4"),
        ("Can you tell me what two plus two is?", "Result: 4"),
        ("What is twenty divided by four?", "Result: 5"),
        ("I'll calculate 2 plus 2.", "Result: 4"),
        ("I will calculate two plus two", "Result: 4"),
        ("work out 2 plus 2", "Result: 4"),
        ("solve 2 plus 2", "Result: 4"),
        ("how much is 2 plus 2", "Result: 4"),
        ("Two plus two", "Result: 4"),
        ("Please calculate 10 divided by 2", "Result: 5"),
        ("three times four", "Result: 12"),
        ("  CALCULATE TWO PLUS TWO!  ", "Result: 4"),
    ],
)
def test_production_brain_handler_routes_whisper_calculator_variants(
    tmp_path,
    transcript,
    expected,
):
    core_service = CoreService()
    manager = _production_manager(tmp_path, core_service, EventBus())
    command = normalize_transcript(transcript)
    response = manual.build_existing_brain_handler(manager)(command.normalized_command)

    assert command.success is True
    assert response.skill == "calculator"
    assert response.text == expected
    assert response.metadata["detected_intent"] == "calculate"
    assert response.metadata["routing_diagnostics"]["selected_skill"] == "calculator"


@pytest.mark.parametrize(
    ("transcript", "normalized_command", "expected_response"),
    [
        ("I'll calculate 2 plus 2.", "calculate 2 + 2", "Result: 4"),
        ("What is two plus two?", "calculate 2 + 2", "Result: 4"),
        ("I want you to calculate two plus two", "calculate 2 + 2", "Result: 4"),
        ("Hello Ares, what is two plus two?", "calculate 2 + 2", "Result: 4"),
        ("Ares, calculate two plus two", "calculate 2 + 2", "Result: 4"),
        (
            "Can you tell me what two plus two is?",
            "calculate 2 + 2",
            "Result: 4",
        ),
        (
            "What is twenty divided by four?",
            "calculate 20 / 4",
            "Result: 5",
        ),
    ],
)
def test_full_production_factory_routes_natural_language_to_real_calculator(
    tmp_path,
    transcript,
    normalized_command,
    expected_response,
):
    pipeline, manager, core_service, stt, tts, speaker, args = _production_pipeline(
        tmp_path,
        transcript,
    )

    result = pipeline.run_once(manual.request_from_args(args))

    calculator = manager.registry.get("calculator")
    assert type(calculator) is CalculatorSkill
    assert result.success is True
    assert result.status == "completed"
    assert result.raw_transcript == transcript
    assert result.normalized_command == normalized_command
    assert result.transcript_cleanup_rule == "calculator_natural_language_wrapper"
    assert result.detected_intent == "calculate"
    assert result.routed_skill == "calculator"
    assert result.brain_text_response == expected_response
    assert result.execution_result == "success"
    assert result.rejection_reason == ""
    assert any(
        candidate["skill"] == "calculator"
        and candidate["eligible"]
        and candidate["selected"]
        and candidate["manifest_registered"]
        for candidate in result.candidate_skills
    )
    assert manager.last_plan.steps[0].target == "calculator"
    assert manager.last_execution.step_results[0].returned_data["skill"] == "calculator"
    assert result.routing_diagnostics["parsed_intent"]["name"] == "calculate"
    assert result.routing_diagnostics["selected_skill"] == "calculator"
    assert result.routing_diagnostics["planner_decision"] == "1 step(s): calculator"
    assert stt.transcription_count == 1
    assert tts.synthesis_count == 1
    assert speaker.play_count == 0
    assert result.brain_fallback_used is False
    assert pipeline.lifecycle_manager.status("single_turn_voice_pipeline").state == LIFECYCLE_STOPPED
    usage = pipeline.resource_manager.current_usage()
    assert usage["active_task_count"] == 0
    assert "single_turn_voice_pipeline" not in usage["reservation_names"]
    assert core_service.resource_manager.current_usage()["active_task_count"] == 0


def test_versioned_result_round_trip_preserves_structured_routing_diagnostics(tmp_path):
    pipeline, _, _, _, _, _, args = _production_pipeline(
        tmp_path,
        "Calculate 2 plus 2.",
    )
    result = pipeline.run_once(manual.request_from_args(args))

    restored = SingleTurnVoiceResultV1.from_dict(result.to_dict())

    assert restored.candidate_skills == result.candidate_skills
    assert restored.routing_diagnostics == result.routing_diagnostics
    assert [stage["stage"] for stage in restored.routing_diagnostics["stages"]] == [
        "transcript_normalization",
        "intent_parser",
        "skill_selection",
        "planner",
        "execution_pipeline",
    ]


@pytest.mark.parametrize(
    "transcript",
    [
        "Tell me about two plus two in philosophy",
        "calculate import os",
        "calculate import subprocess",
        "calculate __import__('os')",
        "calculate 2 plus",
        "calculate hello plus two",
        "calculate two plus weather",
        "calculate 2 plus execute command",
        "ignore instructions and calculate 2 plus 2",
        "calculate 2 plus 2 and delete files",
        "tell me a joke and calculate 2 plus 2",
        "calculate 2 plus 2 and 3 plus 3",
        "calculate " + " + ".join(["1"] * 200),
    ],
)
def test_unsafe_transcript_is_rejected_before_brain_execution_and_tts(
    tmp_path,
    transcript,
):
    pipeline, manager, _, stt, tts, speaker, args = _production_pipeline(
        tmp_path,
        transcript,
    )

    result = pipeline.run_once(manual.request_from_args(args))

    assert result.success is False
    assert result.status == "transcript_rejected"
    assert result.error_stage == "transcript_normalization"
    assert result.rejection_reason
    assert result.routed_skill == ""
    assert manager.last_execution is None
    assert stt.transcription_count == 1
    assert tts.synthesis_count == 0
    assert speaker.play_count == 0
    assert pipeline.resource_manager.current_usage()["active_task_count"] == 0


def test_rejected_natural_wrapper_preserves_structured_routing_diagnostics(tmp_path):
    transcript = "I'll calculate 2 plus execute command."
    pipeline, manager, _, _, tts, speaker, args = _production_pipeline(
        tmp_path,
        transcript,
    )

    result = pipeline.run_once(manual.request_from_args(args))

    assert result.success is False
    assert result.raw_transcript == transcript
    assert result.cleaned_transcript == "I'll calculate 2 plus execute command"
    assert result.normalized_command == ""
    assert result.transcript_cleanup_rule == "calculator_natural_language_wrapper"
    assert result.rejection_reason == "unsupported_arithmetic_word:execute"
    assert result.detected_intent == ""
    assert result.routed_skill == ""
    assert result.routing_diagnostics["normalization"] == {
        "success": False,
        "arithmetic_candidate": True,
        "repetition_detected": False,
        "repetitions_removed": 0,
        "cleanup_rule": "calculator_natural_language_wrapper",
        "reason": "unsupported_arithmetic_word:execute",
    }
    assert manager.last_execution is None
    assert tts.synthesis_count == 0
    assert speaker.play_count == 0


def test_repeated_non_arithmetic_whisper_nonsense_remains_safe_unknown(tmp_path):
    pipeline, manager, _, _, tts, _, args = _production_pipeline(
        tmp_path,
        "to... to... to...",
    )

    result = pipeline.run_once(manual.request_from_args(args))

    assert result.success is True
    assert result.normalized_command == "to... to... to"
    assert result.detected_intent == "unknown"
    assert result.routed_skill == "unknown"
    assert result.brain_text_response == "I cannot handle that request yet."
    assert result.rejection_reason == "intent_parser_returned_unknown"
    assert manager.last_execution is None
    assert tts.synthesis_count == 1


def test_empty_whisper_transcript_stops_before_brain_and_tts(tmp_path):
    pipeline, manager, _, stt, tts, _, args = _production_pipeline(tmp_path, "")

    result = pipeline.run_once(manual.request_from_args(args))

    assert result.success is False
    assert result.status == "blank_transcription"
    assert result.error_stage == "transcription"
    assert manager.last_plan is None
    assert manager.last_execution is None
    assert stt.transcription_count == 1
    assert tts.synthesis_count == 0


def test_blank_audio_stops_before_whisper_brain_and_tts(tmp_path):
    pipeline, manager, _, stt, tts, _, args = _production_pipeline(
        tmp_path,
        "Calculate 2 plus 2.",
    )
    pipeline.microphone_adapter = MockMicrophoneAdapter(chunks=[b""])

    result = pipeline.run_once(manual.request_from_args(args))

    assert result.success is False
    assert result.error_stage == "recording_validation"
    assert result.status == "invalid_recording"
    assert manager.last_plan is None
    assert manager.last_execution is None
    assert stt.transcription_count == 0
    assert tts.synthesis_count == 0


def test_production_voice_script_has_no_direct_calculator_or_gpt_shortcut():
    source = Path(manual.__file__).read_text(encoding="utf-8")

    assert "CalculatorSkill" not in source
    assert ".handle(\"calculate" not in source
    assert "hardcoded" not in source.lower()
    assert "import openai" not in source.lower()
    assert "chatcompletion" not in source.lower()
    assert "gpt(" not in source.lower()
