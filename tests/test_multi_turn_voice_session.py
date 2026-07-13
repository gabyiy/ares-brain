from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core import (
    DEFAULT_CONVERSATION_STOP_PHRASES,
    EVENT_CLEANUP_COMPLETED,
    EVENT_SESSION_COMPLETED,
    EVENT_SESSION_FAILED,
    EVENT_STOP_PHRASE,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    LIFECYCLE_STOPPED,
    CancellationToken,
    LifecycleResult,
    ModuleLifecycleManager,
    MultiTurnVoiceSession,
    MultiTurnVoiceSessionRequestV1,
    ResourceManager,
    SESSION_CANCELLED,
    SESSION_CHECKING_STOP_PHRASE,
    SESSION_COMPLETED,
    SESSION_CREATED,
    SESSION_FAILED,
    SESSION_PROCESSING,
    SESSION_STARTING,
    SESSION_STOPPING,
    SessionStateMachine,
    SessionStateTransitionError,
    SingleTurnVoiceResultV1,
    build_multi_turn_voice_session_manifest,
    normalize_stop_phrase,
)
from core.EventBus import EventBus
from events import EventHistoryStore


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class FakeSingleTurnPipeline:
    def __init__(self, results=None, clock=None, turn_advance=0.0):
        self.lifecycle_manager = ModuleLifecycleManager()
        self.resource_manager = ResourceManager()
        self.event_bus = EventBus()
        self.event_history_store = None
        self.clock = clock or FakeClock()
        self.turn_advance = turn_advance
        self.results = list(results or [])
        self.requests = []
        self.local_outputs = []
        self.brain_inputs = []
        self.stop_count = 0
        self._stage_observers = []
        self.force_busy = False
        self.force_busy_after_calls = None
        self.local_output_failure = None
        self.active_call = False
        self.concurrent_violation = False

    def add_stage_observer(self, observer):
        self._stage_observers.append(observer)

        def unsubscribe():
            if observer in self._stage_observers:
                self._stage_observers.remove(observer)

        return unsubscribe

    def coordination_status(self):
        busy_after = (
            self.force_busy_after_calls is not None
            and len(self.requests) >= self.force_busy_after_calls
        )
        return {
            "idle": not self.force_busy and not busy_after and not self.active_call,
            "capture_active": False,
            "playback_active": False,
            "heavy_stage": "",
            "speaker_playing": False,
        }

    def run_once(self, request, cancellation_token=None, pre_brain_hook=None):
        if self.active_call:
            self.concurrent_violation = True
        self.active_call = True
        self.requests.append(request)
        try:
            self._stage(2, "Recording", "skipped" if request.text_input else "running")
            self._stage(3, "Transcribing", "skipped" if request.text_input else "running")
            recognized = request.text_input or "calculate 2 + 2"
            decision = pre_brain_hook(recognized) if pre_brain_hook else None
            if decision is not None and decision.handled:
                return SingleTurnVoiceResultV1(
                    success=True,
                    status=decision.status,
                    correlation_id=request.correlation_id,
                    session_id=request.session_id,
                    recognized_text=recognized,
                    brain_execution_status=decision.status,
                    simulated_input=bool(request.text_input),
                )
            self._stage(4, "Processing through ARES Brain", "running")
            self.brain_inputs.append(recognized)
            self._stage(5, "Synthesizing response", "running" if request.playback_enabled else "skipped")
            self._stage(6, "Playing response", "running" if request.playback_enabled else "skipped")
            self.clock.advance(self.turn_advance)
            if self.results:
                scripted = self.results.pop(0)
                if isinstance(scripted, BaseException):
                    raise scripted
                result = scripted(request) if callable(scripted) else scripted
                return replace(
                    result,
                    correlation_id=result.correlation_id or request.correlation_id,
                    session_id=result.session_id or request.session_id,
                    recognized_text=result.recognized_text or recognized,
                    simulated_input=bool(request.text_input),
                )
            response = (
                "Result: 4"
                if normalize_stop_phrase(recognized) == "calculate 2 2"
                or "calculate" in recognized.casefold()
                else "I cannot handle that request yet."
            )
            return SingleTurnVoiceResultV1(
                success=True,
                status="completed",
                correlation_id=request.correlation_id,
                session_id=request.session_id,
                recognized_text=recognized,
                brain_execution_status="routed",
                detected_intent="calculate" if "calculate" in recognized.casefold() else "unknown",
                routed_skill="calculator" if "calculate" in recognized.casefold() else "unknown",
                brain_text_response=response,
                tts_status="skipped_simulated_input",
                playback_status="playback_disabled",
                simulated_input=bool(request.text_input),
            )
        finally:
            self.active_call = False

    def run_local_output(self, request, text, cancellation_token=None):
        if self.active_call:
            self.concurrent_violation = True
        self.local_outputs.append((text, request))
        if self.local_output_failure is not None:
            return self.local_output_failure
        return SingleTurnVoiceResultV1(
            success=True,
            status="completed_local_output",
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            brain_execution_status="local_output",
            brain_text_response=text,
            tts_status="synthesized",
            playback_status="played",
            simulated_input=True,
        )

    def stop(self):
        self.stop_count += 1
        self.active_call = False
        return LifecycleResult(True, "stopped", LIFECYCLE_STOPPED)

    def _stage(self, index, label, status):
        for observer in list(self._stage_observers):
            observer(index, 6, label, status)


def _request(tmp_path, **overrides):
    values = {
        "recording_output_directory": str(tmp_path),
        "recording_duration_seconds": 1,
        "maximum_turns": 5,
        "maximum_session_duration_seconds": 180,
        "total_session_timeout_seconds": 180,
        "maximum_consecutive_failures": 3,
        "inter_turn_delay_seconds": 0,
        "greeting_enabled": False,
        "closing_phrase_enabled": False,
        "simulated_text_turns": ["calculate 2 + 2", "goodbye Ares"],
        "correlation_id": "session-corr",
        "session_id": "session-id",
    }
    values.update(overrides)
    return MultiTurnVoiceSessionRequestV1(**values)


def _run(tmp_path, pipeline=None, clock=None, **request_overrides):
    active_clock = clock or FakeClock()
    active_pipeline = pipeline or FakeSingleTurnPipeline(clock=active_clock)
    manager = MultiTurnVoiceSession(
        active_pipeline,
        clock=active_clock,
        sleeper=active_clock.advance,
    )
    result = manager.run_session(_request(tmp_path, **request_overrides))
    return result, manager, active_pipeline, active_clock


def _failure(status, stage, reason="failed"):
    return SingleTurnVoiceResultV1(
        success=False,
        status=status,
        error_stage=stage,
        error_reason=reason,
    )


def test_successful_two_turn_session_stops_before_brain_on_stop_phrase(tmp_path):
    result, _, pipeline, _ = _run(tmp_path)

    assert result.success is True
    assert result.attempted_turns == 2
    assert result.successful_turns == 1
    assert result.stop_reason == "owner_stop_phrase"
    assert result.recognized_stop_phrase == "goodbye Ares"
    assert pipeline.brain_inputs == ["calculate 2 + 2"]
    assert result.turn_summaries[0]["brain_text_response"] == "Result: 4"
    assert result.turn_summaries[1]["brain_text_response"] == ""


def test_maximum_turn_session_is_bounded(tmp_path):
    texts = [f"calculate {index} + 1" for index in range(7)]
    result, _, pipeline, _ = _run(
        tmp_path,
        simulated_text_turns=texts,
        maximum_turns=5,
    )

    assert result.success is True
    assert result.attempted_turns == 5
    assert result.maximum_turns_reached is True
    assert result.stop_reason == "maximum_turns"
    assert len(pipeline.requests) == 5


@pytest.mark.parametrize(
    "spoken",
    ["goodbye Ares", "GOODBYE ARES", "  Goodbye,   Ares!!!  "],
)
def test_stop_phrase_is_case_and_punctuation_normalized(tmp_path, spoken):
    result, _, pipeline, _ = _run(tmp_path, simulated_text_turns=[spoken])

    assert result.stop_reason == "owner_stop_phrase"
    assert pipeline.brain_inputs == []


def test_unrelated_phrase_does_not_trigger_stop(tmp_path):
    result, _, pipeline, _ = _run(
        tmp_path,
        simulated_text_turns=["goodbye messages are useful", "goodbye"],
    )

    assert result.attempted_turns == 2
    assert pipeline.brain_inputs == ["goodbye messages are useful"]


def test_default_stop_phrases_are_bounded_exact_phrases():
    assert "stop listening" in DEFAULT_CONVERSATION_STOP_PHRASES
    assert "exit conversation" in DEFAULT_CONVERSATION_STOP_PHRASES
    assert normalize_stop_phrase(" Goodbye, Ares! ") == "goodbye ares"


def test_maximum_duration_stops_after_completed_turn(tmp_path):
    clock = FakeClock()
    pipeline = FakeSingleTurnPipeline(clock=clock, turn_advance=1.0)
    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        clock=clock,
        simulated_text_turns=["calculate 2 + 2", "calculate 3 + 3"],
        maximum_session_duration_seconds=1,
        total_session_timeout_seconds=1,
    )

    assert result.attempted_turns == 1
    assert result.maximum_duration_reached is True
    assert result.stop_reason == "maximum_session_duration"


def test_insufficient_remaining_time_prevents_real_recording(tmp_path):
    result, _, pipeline, _ = _run(
        tmp_path,
        simulated_text_turns=[],
        recording_duration_seconds=5,
        maximum_session_duration_seconds=4,
        total_session_timeout_seconds=4,
    )

    assert result.attempted_turns == 0
    assert result.stop_reason == "insufficient_remaining_session_time"
    assert pipeline.requests == []


@pytest.mark.parametrize("enabled,expected", [(True, 2), (False, 0)])
def test_configured_greeting_and_closing_are_pipeline_owned(tmp_path, enabled, expected):
    result, _, pipeline, _ = _run(
        tmp_path,
        playback_enabled=enabled,
        greeting_enabled=enabled,
        closing_phrase_enabled=enabled,
    )

    assert result.success is True
    assert len(pipeline.local_outputs) == expected
    if enabled:
        assert pipeline.local_outputs[0][0] == "Hello Gabriel. I am listening."
        assert pipeline.local_outputs[1][0] == "Goodbye Gabriel."


def test_fatal_greeting_output_failure_stops_before_first_turn(tmp_path):
    pipeline = FakeSingleTurnPipeline()
    pipeline.local_output_failure = _failure("tts_failed", "synthesis", "piper_missing")

    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        playback_enabled=True,
        greeting_enabled=True,
        closing_phrase_enabled=False,
    )

    assert result.success is False
    assert result.attempted_turns == 0
    assert result.stop_reason == "fatal_component_failure"
    assert result.error_reason == "piper_missing"


def test_playback_is_disabled_by_default(tmp_path):
    result, _, pipeline, _ = _run(tmp_path)

    assert result.success is True
    assert all(request.playback_enabled is False for request in pipeline.requests)
    assert pipeline.local_outputs == []


def test_voice_profile_selection_is_forwarded_without_model_paths(tmp_path):
    result, _, pipeline, _ = _run(
        tmp_path,
        tts_voice_profile="en_US-amy-low",
    )

    assert result.success is True
    assert all(request.tts_voice_profile == "en_US-amy-low" for request in pipeline.requests)


def test_empty_voice_profile_delegates_to_configured_default(tmp_path):
    result, _, pipeline, _ = _run(tmp_path)

    assert result.success is True
    assert all(request.tts_voice_profile == "" for request in pipeline.requests)


def test_single_turn_pipeline_calls_are_strictly_sequential(tmp_path):
    result, _, pipeline, _ = _run(
        tmp_path,
        simulated_text_turns=["calculate 1 + 1", "calculate 2 + 2", "goodbye"],
    )

    assert result.success is True
    assert pipeline.concurrent_violation is False
    assert len(pipeline.requests) == 3


def test_silence_retries_then_stops_at_failure_threshold(tmp_path):
    pipeline = FakeSingleTurnPipeline(
        results=[
            _failure("silent_audio", "recording_validation", "audio_below_rms_threshold"),
            _failure("silent_audio", "recording_validation", "audio_below_rms_threshold"),
        ]
    )
    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        simulated_text_turns=["one", "two", "three"],
        maximum_consecutive_failures=2,
    )

    assert result.success is False
    assert result.silent_turns == 2
    assert result.stop_reason == "maximum_consecutive_failures"


def test_silence_retry_can_be_disabled(tmp_path):
    pipeline = FakeSingleTurnPipeline(
        results=[_failure("silent_audio", "recording_validation")]
    )
    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        simulated_text_turns=["one", "two"],
        silence_retry_enabled=False,
    )

    assert result.attempted_turns == 1
    assert result.stop_reason == "silence_retry_disabled"


def test_blank_transcription_retries_then_stops(tmp_path):
    pipeline = FakeSingleTurnPipeline(
        results=[
            _failure("blank_transcription", "transcription"),
            _failure("blank_transcription", "transcription"),
        ]
    )
    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        simulated_text_turns=["one", "two", "three"],
        maximum_consecutive_failures=2,
    )

    assert result.blank_transcription_turns == 2
    assert result.stop_reason == "maximum_consecutive_failures"


def test_blank_transcription_retry_can_be_disabled(tmp_path):
    pipeline = FakeSingleTurnPipeline(
        results=[_failure("blank_transcription", "transcription")]
    )
    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        simulated_text_turns=["one", "two"],
        blank_transcription_retry_enabled=False,
    )

    assert result.attempted_turns == 1
    assert result.stop_reason == "blank_transcription_retry_disabled"


def test_recoverable_whisper_failure_does_not_corrupt_next_turn(tmp_path):
    pipeline = FakeSingleTurnPipeline(
        results=[_failure("transcription_failed", "transcription", "whisper_failed")]
    )
    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        simulated_text_turns=["first", "calculate 2 + 2", "goodbye"],
    )

    assert result.success is True
    assert result.failed_turns == 1
    assert result.successful_turns == 1
    assert result.status == "completed_with_partial_failures"


@pytest.mark.parametrize(
    "reason",
    ["microphone_unavailable", "whisper_model_missing"],
)
def test_fatal_component_health_failure_stops_session(tmp_path, reason):
    pipeline = FakeSingleTurnPipeline(
        results=[_failure("health_check_failed", "health_check", reason)]
    )
    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        simulated_text_turns=["first", "second"],
    )

    assert result.success is False
    assert result.stop_reason == "fatal_component_failure"
    assert result.error_reason == reason
    assert len(pipeline.requests) == 1
    assert EVENT_SESSION_FAILED in {event["type"] for event in result.events}


def test_playback_failure_is_recoverable_within_limit(tmp_path):
    pipeline = FakeSingleTurnPipeline(
        results=[_failure("playback_failed", "playback", "aplay_failed")]
    )
    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        playback_enabled=True,
        greeting_enabled=False,
        closing_phrase_enabled=False,
        simulated_text_turns=["first", "calculate 2 + 2", "goodbye"],
    )

    assert result.success is True
    assert result.failed_turns == 1
    assert result.successful_turns == 1


def test_tts_failure_is_fatal_when_playback_is_required(tmp_path):
    pipeline = FakeSingleTurnPipeline(
        results=[_failure("tts_failed", "synthesis", "piper_failed")]
    )
    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        playback_enabled=True,
        greeting_enabled=False,
        closing_phrase_enabled=False,
        simulated_text_turns=["calculate 2 + 2", "second"],
    )

    assert result.success is False
    assert result.stop_reason == "fatal_component_failure"
    assert result.attempted_turns == 1


def test_resource_lock_failure_is_fatal_and_does_not_start_next_turn(tmp_path):
    pipeline = FakeSingleTurnPipeline()
    pipeline.force_busy_after_calls = 1
    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        simulated_text_turns=["calculate 1 + 1", "calculate 2 + 2"],
    )

    assert result.success is False
    assert result.stop_reason == "fatal_resource_lock_failure"
    assert len(pipeline.requests) == 1


def test_unsupported_brain_response_is_not_a_fatal_session_error(tmp_path):
    result, _, pipeline, _ = _run(
        tmp_path,
        simulated_text_turns=["unsupported request", "goodbye"],
    )

    assert result.success is True
    assert result.successful_turns == 1
    assert pipeline.brain_inputs == ["unsupported request"]
    assert result.turn_summaries[0]["brain_text_response"] == "I cannot handle that request yet."


def test_pre_requested_cancellation_cleans_lifecycle_and_resources(tmp_path):
    pipeline = FakeSingleTurnPipeline()
    manager = MultiTurnVoiceSession(pipeline, clock=pipeline.clock, sleeper=pipeline.clock.advance)
    token = CancellationToken("cancel-session")
    token.cancel("keyboard_interrupt")

    result = manager.run_session(_request(tmp_path), cancellation_token=token)

    assert result.cancelled is True
    assert result.final_state == SESSION_CANCELLED
    assert result.resource_cleanup_status == "completed"
    usage = pipeline.resource_manager.current_usage()
    assert usage["active_task_count"] == 0
    assert usage["reservation_names"] == []
    assert pipeline.stop_count >= 1


def test_keyboard_interrupt_from_execute_returns_concise_cancelled_result(monkeypatch, tmp_path):
    pipeline = FakeSingleTurnPipeline()
    manager = MultiTurnVoiceSession(pipeline)
    monkeypatch.setattr(manager, "execute", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    result = manager.run_session(_request(tmp_path))

    assert result.cancelled is True
    assert result.status == "cancelled"
    assert result.error_reason == "keyboard_interrupt"


def test_keyboard_interrupt_from_interactive_input_uses_normal_cleanup_path(tmp_path):
    pipeline = FakeSingleTurnPipeline()

    def interrupt_input(turn):
        raise KeyboardInterrupt

    manager = MultiTurnVoiceSession(
        pipeline,
        text_input_provider=interrupt_input,
        clock=pipeline.clock,
        sleeper=pipeline.clock.advance,
    )

    result = manager.run_session(
        _request(tmp_path, simulated_text_turns=[], interactive_text=True)
    )

    assert result.cancelled is True
    assert result.stop_reason == "keyboard_interrupt"
    assert result.resource_cleanup_status == "completed"
    assert manager.lifecycle_status()["state"] == LIFECYCLE_STOPPED
    usage = pipeline.resource_manager.current_usage()
    assert usage["active_task_count"] == 0
    assert usage["reservation_names"] == []


def test_parent_session_and_turn_correlation_ids_are_preserved(tmp_path):
    result, _, pipeline, _ = _run(tmp_path)

    assert result.correlation_id == "session-corr"
    assert result.session_id == "session-id"
    assert pipeline.requests[0].correlation_id == "session-corr:turn-001"
    assert pipeline.requests[1].correlation_id == "session-corr:turn-002"
    assert all(request.session_id == "session-id" for request in pipeline.requests)


def test_turn_events_carry_turn_correlation_and_parent_session_ids(tmp_path):
    result, _, _, _ = _run(tmp_path)
    turn_events = [event for event in result.events if event["type"] == EVENT_TURN_COMPLETED]

    assert turn_events[0]["correlation_id"] == "session-corr:turn-001"
    assert turn_events[0]["session_id"] == "session-id"
    assert turn_events[0]["payload"]["turn_number"] == 1


def test_expected_events_are_emitted_without_raw_transcript(tmp_path):
    secret_text = "calculate 2 + 2 private transcript"
    result, _, _, _ = _run(
        tmp_path,
        simulated_text_turns=[secret_text, "goodbye"],
    )
    event_types = {event["type"] for event in result.events}
    serialized_events = repr(result.events)

    assert EVENT_TURN_COMPLETED in event_types
    assert EVENT_STOP_PHRASE in event_types
    assert EVENT_SESSION_COMPLETED in event_types
    assert EVENT_CLEANUP_COMPLETED in event_types
    assert secret_text not in serialized_events


def test_failure_events_are_recorded_in_local_history_without_transcript(tmp_path):
    history = EventHistoryStore(path=tmp_path / "events.json")
    pipeline = FakeSingleTurnPipeline(
        results=[_failure("transcription_failed", "transcription", "private transcript")]
    )
    manager = MultiTurnVoiceSession(
        pipeline,
        event_history_store=history,
        clock=pipeline.clock,
        sleeper=pipeline.clock.advance,
    )

    result = manager.run_session(
        _request(
            tmp_path,
            simulated_text_turns=["secret user words"],
            maximum_consecutive_failures=1,
        )
    )

    assert result.success is False
    assert EVENT_TURN_FAILED in {record.type for record in history.list()}
    assert "secret user words" not in repr([record.to_dict() for record in history.list()])


def test_keep_audio_policy_is_forwarded_per_turn(tmp_path):
    result, _, pipeline, _ = _run(tmp_path, cleanup_policy="keep")

    assert result.success is True
    assert all(request.cleanup_policy == "keep" for request in pipeline.requests)
    assert result.data["raw_audio_persisted"] is True


def test_default_cleanup_policy_does_not_persist_raw_audio(tmp_path):
    result, _, pipeline, _ = _run(tmp_path)

    assert result.data["raw_audio_persisted"] is False
    assert all(request.cleanup_policy == "delete_on_success" for request in pipeline.requests)


def test_session_state_history_contains_required_ordered_states(tmp_path):
    result, _, _, _ = _run(tmp_path)
    states = [entry["to_state"] for entry in result.state_history]

    assert states[0] == SESSION_CREATED
    assert SESSION_STARTING in states
    assert SESSION_CHECKING_STOP_PHRASE in states
    assert SESSION_PROCESSING in states
    assert states[-2:] == [SESSION_STOPPING, SESSION_COMPLETED]


def test_illegal_session_state_transition_is_rejected():
    clock = FakeClock()
    machine = SessionStateMachine(clock=clock)

    with pytest.raises(SessionStateTransitionError, match="illegal_session_transition"):
        machine.transition(SESSION_COMPLETED, "invalid")


def test_session_request_and_result_contracts_round_trip(tmp_path):
    request = _request(tmp_path)
    restored = MultiTurnVoiceSessionRequestV1.from_dict(request.to_dict())
    result, _, _, _ = _run(tmp_path)
    restored_result = type(result).from_dict(result.to_dict())

    assert restored == request
    assert restored_result == result


def test_invalid_unbounded_session_request_is_rejected_before_pipeline(tmp_path):
    pipeline = FakeSingleTurnPipeline()
    manager = MultiTurnVoiceSession(pipeline)

    result = manager.run_session(_request(tmp_path, maximum_turns=0))

    assert result.success is False
    assert result.status == "contract_rejected"
    assert result.error_reason == "maximum_turns_out_of_range"
    assert pipeline.requests == []


def test_session_manifest_is_light_bounded_and_depends_on_single_turn():
    manifest = build_multi_turn_voice_session_manifest()

    assert manifest.capabilities == ["voice.conversation_session"]
    assert manifest.dependencies.required_capabilities == ["voice.single_turn"]
    assert manifest.resources.heavy_module is False
    assert manifest.resources.maximum_concurrent_tasks == 1
    assert manifest.metadata["owner_triggered_only"] is True


def test_session_manager_contains_no_hardware_or_model_subprocess_logic():
    source = "\n".join(
        Path(path).read_text(encoding="utf-8").lower()
        for path in (
            "core/MultiTurnVoiceSession.py",
            "core/MultiTurnVoiceExecution.py",
            "core/MultiTurnVoiceRuntime.py",
        )
    )

    assert "subprocess" not in source
    assert "arecord" not in source
    assert "aplay" not in source
    assert "whisper-cli" not in source
    assert ".onnx" not in source


def test_interactive_text_provider_is_bounded_and_uses_stop_handling(tmp_path):
    provided = iter(["calculate 2 + 2", "goodbye Ares"])
    pipeline = FakeSingleTurnPipeline()
    manager = MultiTurnVoiceSession(
        pipeline,
        text_input_provider=lambda turn: next(provided),
        clock=pipeline.clock,
        sleeper=pipeline.clock.advance,
    )

    result = manager.run_session(
        _request(tmp_path, simulated_text_turns=[], interactive_text=True)
    )

    assert result.success is True
    assert result.attempted_turns == 2
    assert result.stop_reason == "owner_stop_phrase"
    assert pipeline.brain_inputs == ["calculate 2 + 2"]


def test_interactive_input_exhaustion_stops_without_real_microphone_fallback(tmp_path):
    pipeline = FakeSingleTurnPipeline()
    manager = MultiTurnVoiceSession(
        pipeline,
        text_input_provider=lambda turn: None,
        clock=pipeline.clock,
        sleeper=pipeline.clock.advance,
    )

    result = manager.run_session(
        _request(tmp_path, simulated_text_turns=[], interactive_text=True)
    )

    assert result.success is True
    assert result.stop_reason == "input_exhausted"
    assert result.attempted_turns == 0
    assert pipeline.requests == []


def test_partial_failure_returns_safe_final_result_and_no_resource_leaks(tmp_path):
    pipeline = FakeSingleTurnPipeline(
        results=[_failure("playback_failed", "playback", "speaker_failed")]
    )
    result, _, _, _ = _run(
        tmp_path,
        pipeline=pipeline,
        simulated_text_turns=["first", "goodbye"],
    )

    assert result.success is True
    assert result.status == "completed_with_partial_failures"
    assert result.failed_turns == 1
    usage = pipeline.resource_manager.current_usage()
    assert usage["active_task_count"] == 0
    assert usage["reservation_names"] == []
    assert pipeline.concurrent_violation is False
