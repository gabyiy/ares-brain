from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import (
    BRAIN_ACTIVE,
    BRAIN_STANDBY,
    BRAIN_STOPPED,
    BrainRuntime,
    BrainRuntimeConfig,
    BrainSessionManager,
    CollectingRuntimeOutputAdapter,
    CoreService,
    QueuedRuntimeInputAdapter,
    QueuedStandbyWakeListener,
    RuntimeInputResult,
    StandbyListenResultV1,
    WakeAttemptResult,
    WakeLocalDiagnostics,
    WakeListenerConfig,
)
from scripts import manual_verify_standby_wake_runtime as manual_wake


def _runtime(tmp_path):
    return manual_wake._build_runtime(tmp_path, manual_wake.FakeClock())


def test_standby_no_speech_keeps_runtime_alive_without_session_or_output(tmp_path):
    runtime, _, output, wake = _runtime(tmp_path)
    runtime.start()
    wake.push(None)
    result = runtime.poll_once()
    assert result.success
    assert result.status == "standby_listening"
    assert runtime.session_manager.state == BRAIN_STANDBY
    assert runtime.session_manager.session_id == ""
    assert output.texts == []


def test_unrelated_standby_speech_is_silently_rejected_without_core_route(tmp_path):
    runtime, _, output, wake = _runtime(tmp_path)
    runtime.start()
    wake.push("I read about Ares yesterday")
    result = runtime.poll_once()
    assert result.status == "standby_listening"
    assert result.data["speech_detected"] is True
    assert runtime.snapshot().command_count == 0
    assert runtime.session_manager.session_id == ""
    assert output.texts == []


@pytest.mark.parametrize(
    "phrase",
    [
        "Ares",
        "Aris",
        "ARES.",
        "Hey, Ares",
        "Hey Aris",
        "Hello Aries",
        "Wake up, Aris",
    ],
)
def test_verified_wake_phrase_activates_and_acknowledges_exactly_once(phrase, tmp_path):
    runtime, _, output, wake = _runtime(tmp_path)
    runtime.start()
    wake.push(phrase)
    result = runtime.poll_once()
    assert result.status == "activated"
    assert runtime.session_manager.state == BRAIN_ACTIVE
    assert runtime.session_manager.session_id
    assert output.texts == ["Yes Gabi."]
    assert runtime.snapshot().activation_count == 1


def test_guarded_duplicate_wake_result_creates_only_one_session(tmp_path):
    runtime, _, output, wake = _runtime(tmp_path)
    runtime.start()
    wake.push(
        StandbyListenResultV1(
            success=True,
            status="wake_detected",
            speech_detected=True,
            wake_detected=True,
            command_category="activation",
            normalized_wake_phrase="ares",
            matched_phrase="ares",
            selected_alias="ares",
            selected_wake_phrase="ares",
            canonical_wake_phrase="ares",
            classification_path="vosk_constrained_grammar_duplicate_collapse",
            classification_reason="accepted_canonical_duplicate_wake",
            duplicate_collapse_used=True,
        )
    )

    result = runtime.poll_once()

    assert result.status == "activated"
    assert runtime.session_manager.session_id
    assert runtime.snapshot().activation_count == 1
    assert output.texts == ["Yes Gabi."]


def test_runtime_carries_exact_immutable_wake_attempt_through_activation(tmp_path):
    class AttemptListener(QueuedStandbyWakeListener):
        def __init__(self):
            super().__init__()
            result = StandbyListenResultV1(
                success=True,
                status="wake_detected",
                attempt_id="wake-attempt-runtime",
                candidate_id="wake-candidate-runtime",
                    stream_generation=4,
                    candidate_number=9,
                    stream_instance_id="stream-four",
                    capture_valid=True,
                    recognizer_invoked=True,
                speech_detected=True,
                wake_detected=True,
                command_category="activation",
                normalized_wake_phrase="ares",
                matched_phrase="ares",
                duration_seconds=0.6,
                    sample_rate_hz=16000,
                    channels=1,
                    sample_width_bytes=2,
                    cleanup_status="removed",
                )
            diagnostics = WakeLocalDiagnostics(
                attempt_id="wake-attempt-runtime",
                candidate_id="wake-candidate-runtime",
                stream_generation=4,
                capture_valid=True,
                recognizer_invoked=True,
                raw_transcript="Ares",
                normalized_transcript="ares",
            )
            self.attempt = WakeAttemptResult(
                attempt_id="wake-attempt-runtime",
                candidate_id="wake-candidate-runtime",
                stream_instance_id="stream-four",
                stream_generation=4,
                candidate_number=9,
                capture_valid=True,
                recognizer_invoked=True,
                infrastructure_failure=False,
                lifecycle_state_before="STANDBY",
                lifecycle_state_after="STANDBY",
                cleanup_status="removed",
                result=result,
                diagnostics=diagnostics,
            )

        def listen_attempt(self, request):
            return self.attempt

        def completed_attempt(self, attempt_id):
            return self.attempt if attempt_id == self.attempt.attempt_id else None

        def complete_attempt_lifecycle(self, attempt_id, state):
            if attempt_id != self.attempt.attempt_id:
                return None
            self.attempt = replace(self.attempt, lifecycle_state_after=state)
            return self.attempt

    manager = BrainSessionManager()
    service = CoreService(brain_session_manager=manager)
    output = CollectingRuntimeOutputAdapter()
    listener = AttemptListener()
    runtime = BrainRuntime(
        core_service=service,
        command_handler=lambda _: SimpleNamespace(text="unused", skill="none"),
        input_adapter=QueuedRuntimeInputAdapter(),
        output_adapter=output,
        standby_wake_listener=listener,
    )
    assert runtime.start().success
    activated = runtime.poll_once()
    assert activated.status == "activated"
    assert activated.data["wake_attempt_id"] == "wake-attempt-runtime"
    assert activated.data["wake_candidate_id"] == "wake-candidate-runtime"
    assert activated.data["wake_stream_generation"] == 4
    assert listener.attempt.lifecycle_state_after == "ACTIVE"
    assert output.texts == ["Yes Gabi."]


def test_constrained_aris_alias_activates_once_without_core_routing(tmp_path):
    runtime, active, output, wake = _runtime(tmp_path)
    runtime.start()
    wake.push("Aris.")
    activated = runtime.poll_once()
    first_session = runtime.session_manager.session_id
    assert activated.status == "activated"
    assert first_session
    assert runtime.snapshot().activation_count == 1
    assert runtime.snapshot().command_count == 0
    assert output.texts == ["Yes Gabi."]
    assert wake.snapshot().stream_active is False

    active.push("calculate 2 plus 2")
    command = runtime.poll_once()
    assert command.response_text == "Result: 4"
    assert runtime.session_manager.session_id == first_session

    active.push("goodbye Aris")
    standby = runtime.poll_once()
    assert standby.status == "standby_entered"
    assert standby.data["core_service_bypassed"] is True
    assert runtime.session_manager.session_id == ""
    assert wake.snapshot().listener_state == "ready"
    assert wake.snapshot().stream_active is True
    wake.push("hello ares")
    assert runtime.poll_once().status == "activated"
    assert runtime.session_manager.session_id != first_session
    active.push("shutdown Aris")
    assert runtime.poll_once().status == "stopped"


def test_persistent_runtime_end_to_end_lifecycle_sequence(tmp_path):
    (
        runtime,
        _microphone,
        whisper,
        piper,
        _speaker,
        _spoken,
        wake,
    ) = manual_wake._build_voice_runtime(tmp_path, manual_wake.FakeClock())
    wake.push("Ares")
    wake.push("Ares")
    whisper.push("calculate two plus two")
    whisper.push("goodbye Ares")
    whisper.push("shutdown Ares")

    loop = runtime.run()

    assert loop.success is True
    assert loop.status == "stopped"
    assert loop.stop_reason == "explicit_shutdown_command"
    assert loop.iteration_count == 5
    assert runtime.session_manager.state == BRAIN_STOPPED
    assert runtime.session_manager.session_id == ""
    assert runtime.snapshot().activation_count == 2
    assert runtime.snapshot().standby_return_count == 1
    assert "Result: 4" in piper.texts
    assert piper.texts.count("Yes Gabi.") == 2
    assert wake.snapshot().listener_state == "stopped"


def test_runtime_serializes_standby_and_active_microphone_ownership(tmp_path):
    runtime, active, _, wake = _runtime(tmp_path)
    runtime.start()
    initial = wake.snapshot()
    assert initial.stream_active
    assert initial.stream_open_count == 1
    wake.push("Aries")
    assert runtime.poll_once().status == "activated"
    assert not wake.snapshot().stream_active
    active.push("goodbye Aries")
    assert runtime.poll_once().status == "standby_entered"
    resumed = wake.snapshot()
    assert resumed.stream_active
    assert resumed.stream_open_count == 2
    assert resumed.stream_close_count == 1
    assert resumed.stream_open_reason == "runtime_return_to_standby"
    assert resumed.calibration_reason == (
        "runtime_return_to_standby:initial_calibration"
    )
    assert resumed.ownership_handoff_source == "active_command"
    assert resumed.ownership_handoff_destination == "queued_standby"


def test_runtime_exposes_one_privacy_safe_wake_request_builder(tmp_path):
    runtime, _, _, wake = _runtime(tmp_path)
    runtime.start()
    request = runtime.build_standby_wake_request(
        correlation_id="wake-probe-correlation"
    )
    assert request.runtime_id == runtime.runtime_id
    assert request.lifecycle_state == BRAIN_STANDBY
    assert request.correlation_id == "wake-probe-correlation"
    assert request.wake_phrase_aliases == ["ares", "aris", "aries"]
    assert request.metadata["contains_transcript"] is False
    assert wake.snapshot().stream_open_count == 1


def test_active_repeated_activation_keeps_session_and_uses_active_acknowledgement(tmp_path):
    runtime, active, output, wake = _runtime(tmp_path)
    runtime.start()
    wake.push("Ares")
    runtime.poll_once()
    session_id = runtime.session_manager.session_id
    active.push("Ares")
    result = runtime.poll_once()
    assert result.status == "already_active"
    assert runtime.session_manager.session_id == session_id
    assert output.texts == ["Yes Gabi.", "Yes Gabi, I am listening."]
    assert runtime.snapshot().activation_count == 1


def test_active_calculator_uses_real_core_route_and_same_session(tmp_path):
    runtime, active, output, wake = _runtime(tmp_path)
    runtime.start()
    wake.push("Ares")
    runtime.poll_once()
    session_id = runtime.session_manager.session_id
    active.push("calculate 2 plus 2")
    result = runtime.poll_once()
    assert result.success
    assert result.response_text == "Result: 4"
    assert result.data["selected_skill"] == "calculator"
    assert runtime.session_manager.session_id == session_id
    assert output.texts[-1] == "Result: 4"


def test_active_owner_memory_create_recall_and_delete_confirmation_use_real_route(tmp_path):
    runtime, active, _, wake = _runtime(tmp_path)
    runtime.start()
    wake.push("Ares")
    runtime.poll_once()
    for command in (
        "Remember that my favorite color is blue.",
        "What is my favorite color?",
        "Forget my favorite color.",
        "Yes, delete it.",
        "What is my favorite color?",
    ):
        active.push(command)
        result = runtime.poll_once()
        assert result.success
    assert "do not know" in result.response_text.casefold()
    assert runtime.session_manager.state == BRAIN_ACTIVE


@pytest.mark.parametrize("phrase", ["goodbye Ares", "go to sleep Ares", "standby Ares", "sleep Ares"])
def test_goodbye_controls_return_to_standby_without_stopping_listener(phrase, tmp_path):
    runtime, active, _, wake = _runtime(tmp_path)
    runtime.start()
    wake.push("Ares")
    runtime.poll_once()
    active.push(phrase)
    result = runtime.poll_once()
    assert result.status == "standby_entered"
    assert runtime.session_manager.state == BRAIN_STANDBY
    assert runtime.session_manager.session_id == ""
    assert wake.snapshot().listener_state == "ready"


def test_shutdown_control_is_recognized_during_standby_and_stops_all_resources(tmp_path):
    runtime, active, output, wake = _runtime(tmp_path)
    runtime.start()
    wake.push("shutdown Ares")
    result = runtime.poll_once()
    assert result.success
    assert runtime.session_manager.state == BRAIN_STOPPED
    assert wake.snapshot().listener_state == "stopped"
    assert active.closed and output.closed


def test_shutdown_control_during_active_uses_active_voice_input(tmp_path):
    runtime, active, _, wake = _runtime(tmp_path)
    runtime.start()
    wake.push("Ares")
    runtime.poll_once()
    active.push("shutdown Ares")
    result = runtime.poll_once()
    assert result.success
    assert runtime.session_manager.state == BRAIN_STOPPED
    assert wake.snapshot().listener_state == "stopped"


def test_inactivity_boundary_returns_to_standby_and_next_wake_creates_new_session(tmp_path):
    clock = manual_wake.FakeClock()
    runtime, active, _, wake = manual_wake._build_runtime(tmp_path, clock)
    runtime.start()
    wake.push("Ares")
    runtime.poll_once()
    first = runtime.session_manager.session_id
    clock.advance(29.999)
    active.push(RuntimeInputResult.timeout())
    assert runtime.poll_once().status == "input_timeout"
    assert runtime.session_manager.state == BRAIN_ACTIVE
    clock.advance(0.001)
    active.push(RuntimeInputResult.timeout())
    assert runtime.poll_once().status == "standby_entered"
    assert runtime.session_manager.session_id == ""
    wake.push("Ares")
    runtime.poll_once()
    assert runtime.session_manager.session_id != first


def test_non_wake_speech_does_not_count_as_command_or_brain_failure(tmp_path):
    runtime, _, _, wake = _runtime(tmp_path)
    runtime.start()
    for text in ("nearest shop", "address this issue", "where is Ares located"):
        wake.push(text)
        assert runtime.poll_once().success
    snapshot = runtime.snapshot()
    assert snapshot.command_count == 0
    assert snapshot.failure_count == 0
    assert runtime.session_manager.snapshot().consecutive_failure_count == 0


def test_wake_listener_failure_is_structured_and_recovers_until_bounded_limit(tmp_path):
    runtime, _, _, wake = _runtime(tmp_path)
    runtime.start()
    failure = StandbyListenResultV1(
        success=False,
        status="failed",
        error_code="injected_wake_failure",
        error_message="injected",
    )
    wake.push(failure)
    first = runtime.poll_once()
    assert first.status == "wake_listener_failed"
    assert runtime.session_manager.state == BRAIN_STANDBY
    wake.push(None)
    assert runtime.poll_once().success
    wake.push(failure)
    assert runtime.poll_once().status == "wake_listener_failed"
    assert runtime.session_manager.state == BRAIN_STANDBY


def test_consecutive_wake_listener_failures_stop_at_configured_limit(tmp_path):
    runtime, _, _, wake = _runtime(tmp_path)
    runtime.start()
    for index in range(3):
        wake.push(
            StandbyListenResultV1(
                success=False,
                status="failed",
                error_code="injected_wake_failure",
                error_message="injected",
            )
        )
        result = runtime.poll_once()
    assert result.status == "maximum_failures_reached"
    assert runtime.session_manager.state == BRAIN_STOPPED


def test_malformed_wake_listener_result_is_not_treated_as_activation(tmp_path):
    class MalformedListener(QueuedStandbyWakeListener):
        def listen_once(self, request):
            return {"wake_detected": True}

    manager = BrainSessionManager()
    service = CoreService(brain_session_manager=manager)
    runtime = BrainRuntime(
        core_service=service,
        command_handler=lambda _: SimpleNamespace(text="unused", skill="none"),
        input_adapter=QueuedRuntimeInputAdapter(),
        output_adapter=CollectingRuntimeOutputAdapter(),
        config=BrainRuntimeConfig(),
        standby_wake_listener=MalformedListener(),
    )
    runtime.start()
    result = runtime.poll_once()
    assert not result.success
    assert result.error_code == "malformed_wake_listener_result"
    assert manager.state == BRAIN_STANDBY
    assert manager.session_id == ""


def test_runtime_rejects_incomplete_or_phrase_colliding_wake_listener():
    manager = BrainSessionManager()
    service = CoreService(brain_session_manager=manager)
    with pytest.raises(ValueError, match="missing methods"):
        BrainRuntime(
            core_service=service,
            command_handler=lambda _: None,
            input_adapter=QueuedRuntimeInputAdapter(),
            output_adapter=CollectingRuntimeOutputAdapter(),
            standby_wake_listener=SimpleNamespace(config=WakeListenerConfig()),
        )
    colliding = QueuedStandbyWakeListener(
        config=WakeListenerConfig(wake_phrase_prefixes=("shutdown",))
    )
    with pytest.raises(ValueError, match="overlap"):
        BrainRuntime(
            core_service=service,
            command_handler=lambda _: None,
            input_adapter=QueuedRuntimeInputAdapter(),
            output_adapter=CollectingRuntimeOutputAdapter(),
            standby_wake_listener=colliding,
        )
    mismatched = QueuedStandbyWakeListener(
        config=WakeListenerConfig(wake_phrase_aliases=("ares", "alternate"))
    )
    with pytest.raises(ValueError, match="must match BrainRuntime"):
        BrainRuntime(
            core_service=service,
            command_handler=lambda _: None,
            input_adapter=QueuedRuntimeInputAdapter(),
            output_adapter=CollectingRuntimeOutputAdapter(),
            standby_wake_listener=mismatched,
        )


def test_wake_runtime_events_are_emitted_without_transcripts_memory_values_or_audio(tmp_path):
    runtime, active, _, wake = _runtime(tmp_path)
    runtime.start()
    wake.push("private unrelated sentence about Ares yesterday")
    runtime.poll_once()
    wake.push("Aris.")
    runtime.poll_once()
    active.push("Remember that my favorite color is ultraviolet.")
    runtime.poll_once()
    active.push("goodbye Aris")
    runtime.poll_once()
    serialized = json.dumps([event.to_dict() for event in runtime.events()]).casefold()
    assert "brain_wake_candidate_detected" in serialized
    assert "brain_wake_detected" in serialized
    assert "brain_wake_rejected" in serialized
    assert "private unrelated sentence" not in serialized
    assert '"aris"' not in serialized
    assert "ultraviolet" not in serialized
    assert "goodbye aris" not in serialized
    assert "raw_transcript" not in serialized
    assert "audio_bytes" not in serialized
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in tmp_path.rglob("*.json")
    ).casefold()
    assert "aris" not in persisted


def test_no_speech_poll_does_not_emit_noisy_standby_listener_event(tmp_path):
    runtime, _, _, wake = _runtime(tmp_path)
    runtime.start()
    wake.push(None)
    runtime.poll_once()
    assert runtime.events("brain_runtime_standby_listened") == []


def test_runtime_remains_hardware_agnostic_and_only_depends_on_wake_contract():
    source = Path("core/BrainRuntime.py").read_text(encoding="utf-8").casefold()
    for forbidden in ("arecord", "aplay", "whisper", "piper", "microphone", "speaker", "shell=true"):
        assert forbidden not in source
    assert "standbywakelistener" in source.replace("_", "")
    assert "standbylistenresultv1" in source.replace("_", "")
