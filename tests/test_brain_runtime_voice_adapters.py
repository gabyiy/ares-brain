from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import (
    RuntimeOutputMessage,
    SingleTurnPipelineRuntimeInputAdapter,
    SingleTurnPipelineRuntimeOutputAdapter,
    SingleTurnVoiceRequestV1,
    VoiceRuntimeGate,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakePipeline:
    def __init__(self) -> None:
        self.input_text = "calculate 2 plus 2"
        self.input_status = "runtime_transport_captured"
        self.input_success = True
        self.input_error_stage = ""
        self.input_error_reason = ""
        self.requests = []
        self.local_outputs = []
        self.stop_count = 0
        self.output_success = True
        self.stage_observers = []
        self.capture_ready_observers = []
        self.before_capture_ready = None
        self.on_transcribing = None
        self.raw_decisions = []
        self.pre_brain_decisions = []

    def add_stage_observer(self, observer):
        self.stage_observers.append(observer)

        def unsubscribe():
            if observer in self.stage_observers:
                self.stage_observers.remove(observer)

        return unsubscribe

    def _stage(self, index, label, status):
        for observer in list(self.stage_observers):
            observer(index, 6, label, status)

    def add_capture_ready_observer(self, observer):
        self.capture_ready_observers.append(observer)

        def unsubscribe():
            if observer in self.capture_ready_observers:
                self.capture_ready_observers.remove(observer)

        return unsubscribe

    def run_once(
        self,
        request,
        cancellation_token=None,
        pre_brain_hook=None,
        raw_transcript_hook=None,
    ):
        self.requests.append(request)
        self._stage(2, "Recording", "running")
        if self.before_capture_ready is not None:
            self.before_capture_ready()
        for observer in list(self.capture_ready_observers):
            observer(
                {
                    "capture_start_reason": "calibration_completed_stream_ready",
                    "pre_roll_frames": 25,
                    "pre_roll_seconds": 0.5,
                }
            )
        if self.input_text:
            self._stage(2, "Recording", "completed")
            self._stage(3, "Transcribing", "running")
            if self.on_transcribing is not None:
                self.on_transcribing()
        raw_decision = None
        if raw_transcript_hook is not None and self.input_text:
            raw_decision = raw_transcript_hook(self.input_text)
            self.raw_decisions.append(raw_decision)
        if (
            self.input_text
            and (raw_decision is None or not raw_decision.handled)
            and pre_brain_hook is not None
        ):
            decision = pre_brain_hook(self.input_text)
            self.pre_brain_decisions.append(decision)
            assert decision.handled is True
            assert decision.continue_to_output is False
        elif raw_decision is not None:
            assert raw_decision.continue_to_output is False
        return SimpleNamespace(
            success=self.input_success,
            status=self.input_status,
            recognized_text=self.input_text,
            error_stage=self.input_error_stage,
            error_reason=self.input_error_reason,
            recording_status="completed_after_silence",
            raw_transcript=self.input_text,
            cleaned_transcript=self.input_text,
            transcription_processing_time_seconds=0.42,
            recording_duration_seconds=1.1,
            data={
                "recording": {
                    "stop_reason": "completed_after_silence",
                    "raw_duration_seconds": 2.8,
                    "assembled_duration_seconds": 1.1,
                    "normalized_duration_seconds": 1.1,
                },
                "audio_finalization": {
                    "started_at": "2026-07-16T10:00:00Z",
                    "completed_at": "2026-07-16T10:00:00.010000Z",
                    "wav_path": str(request.recording_output_path),
                    "wav_byte_size": 35244,
                    "sample_rate_hz": 16000,
                    "channels": 1,
                    "sample_width_bytes": 2,
                },
                "transcription_boundary": {
                    "started_at": "2026-07-16T10:00:00.020000Z",
                    "completed_at": "2026-07-16T10:00:00.440000Z",
                    "timeout_seconds": request.transcription_timeout_seconds or 30.0,
                    "microphone_capture_released": True,
                },
                "transcription": {
                    "status": "transcribed" if self.input_success else self.input_status,
                    "data": {
                        "transcription_backend": "whisper.cpp",
                        "transcription_started_at": "2026-07-16T10:00:00.020000Z",
                        "transcription_completed_at": "2026-07-16T10:00:00.440000Z",
                        "transcription_timeout_seconds": (
                            request.transcription_timeout_seconds or 30.0
                        ),
                        "process": {
                            "returncode": 0,
                            "metadata": {
                                "pid": 4102,
                                "pgid": 4102,
                                "elapsed_seconds": 0.42,
                                "terminated": False,
                                "killed": False,
                                "reaped": True,
                                "cleanup_completed": True,
                                "output_handles_closed": True,
                            },
                        },
                        "transcript_parsing_status": (
                            "completed" if self.input_success else "not_started"
                        ),
                    },
                },
                "cleanup": {
                    "removed": [str(request.recording_output_path)],
                    "preserved": [],
                },
            },
        )

    def run_local_output(self, request, text, cancellation_token=None):
        self.local_outputs.append((request, text))
        return SimpleNamespace(
            success=self.output_success,
            status="completed_local_output" if self.output_success else "playback_failed",
            error_reason="" if self.output_success else "speaker_failed",
        )

    def stop(self, request=None):
        self.stop_count += 1
        return SimpleNamespace(success=True, status="stopped")


def _request(tmp_path: Path) -> SingleTurnVoiceRequestV1:
    return SingleTurnVoiceRequestV1(
        recording_output_path=str(tmp_path / "base.wav"),
        capture_mode="auto_stop",
        playback_enabled=True,
        cleanup_policy="delete_on_success",
        correlation_id="base-correlation",
    )


def test_active_voice_input_delegates_capture_and_transcription_to_single_turn_pipeline(tmp_path):
    pipeline = FakePipeline()
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
    )
    result = adapter.wait_for_input(1.0)
    assert result.status == "input"
    assert result.text == "calculate 2 plus 2"
    assert len(pipeline.requests) == 1
    request = pipeline.requests[0]
    assert request.session_id == "session-1"
    assert request.playback_enabled is False
    assert request.text_input == ""
    assert request.recording_output_path != str(tmp_path / "base.wav")
    assert request.pre_roll_seconds == pytest.approx(0.5)
    assert request.silence_duration_seconds == pytest.approx(0.9)
    assert request.metadata["capture_profile"] == "active_command_v1"
    assert request.metadata["canonical_pcm"] == "16000_hz_mono_s16_le"
    assert result.metadata["capture_stop_reason"] == "completed_after_silence"
    assert result.metadata["raw_capture_duration_seconds"] == pytest.approx(2.8)
    assert result.metadata["finalized_candidate_duration_seconds"] == pytest.approx(1.1)


def test_active_voice_input_reports_visible_bounded_progress_in_order(tmp_path):
    pipeline = FakePipeline()
    statuses = []
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
        status_callback=statuses.append,
    )

    result = adapter.wait_for_input(1.0)

    assert result.status == "input"
    assert statuses == [
        "ARES is waiting for your command...",
        "Active microphone capture started",
        "Speech detected",
        "Command captured",
        "Transcribing command",
        "Processing command",
    ]
    assert pipeline.stage_observers == []
    assert pipeline.capture_ready_observers == []


def test_active_prompt_waits_for_frame_safe_post_calibration_boundary(tmp_path):
    pipeline = FakePipeline()
    statuses = []
    statuses_before_ready = []
    pipeline.before_capture_ready = lambda: statuses_before_ready.extend(statuses)
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
        status_callback=statuses.append,
    )

    result = adapter.wait_for_input(1.0)

    assert result.status == "input"
    assert statuses_before_ready == []
    assert statuses.count("ARES is waiting for your command...") == 1
    assert statuses.count("Active microphone capture started") == 1
    assert adapter.last_diagnostics.audio_capture_start_reason == (
        "calibration_completed_stream_ready"
    )


@pytest.mark.parametrize(
    ("transcript", "action"),
    [
        ("Ares", "attention_only"),
        ("Goodbye", "standby"),
        ("Bye Ares", "standby"),
        ("Ares shut down", "shutdown"),
        ("Shutdown RS", "shutdown"),
    ],
)
def test_active_voice_transport_uses_authoritative_active_lifecycle_normalizer(
    transcript,
    action,
    tmp_path,
):
    pipeline = FakePipeline()
    pipeline.input_text = transcript
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
    )

    result = adapter.wait_for_input(1.0)

    assert result.status == "input"
    assert result.text == transcript
    assert len(pipeline.raw_decisions) == 1
    assert pipeline.raw_decisions[0].handled is True
    assert pipeline.raw_decisions[0].data["lifecycle_action"] == action
    assert pipeline.pre_brain_decisions == []


def test_active_microphone_gate_is_released_before_whisper_inference(tmp_path):
    pipeline = FakePipeline()
    gate = VoiceRuntimeGate(settle_delay_seconds=0)
    pipeline.on_transcribing = lambda: (
        pytest.fail("microphone gate remained active during Whisper")
        if gate.snapshot()["capture_active"]
        else None
    )
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
        voice_io_gate=gate,
    )

    result = adapter.wait_for_input(1.0)

    assert result.status == "input"
    assert gate.snapshot()["capture_active"] is False


def test_active_transcription_timeout_is_visible_bounded_and_keeps_session_input_open(
    tmp_path,
):
    pipeline = FakePipeline()
    pipeline.input_text = ""
    pipeline.input_success = False
    pipeline.input_status = "transcription_timeout"
    pipeline.input_error_stage = "transcription"
    pipeline.input_error_reason = "whisper_transcription_timeout"
    statuses = []
    diagnostics = []
    request = replace(
        _request(tmp_path),
        cleanup_policy="delete_always",
        transcription_timeout_seconds=15.0,
    )
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=request,
        session_id_provider=lambda: "session-1",
        status_callback=statuses.append,
        diagnostic_callback=diagnostics.append,
    )

    result = adapter.wait_for_input(1.0)

    assert result.status == "timeout"
    assert result.metadata["transcription_failure_type"] == "transcription_timeout"
    assert result.metadata["retryable"] is True
    assert result.metadata["microphone_gate_released"] is True
    assert (
        "Command transcription timeout handled after 15 seconds; "
        "ARES remains active"
    ) in statuses
    assert "Microphone gate released: yes" in statuses
    assert "Temporary audio cleanup: removed" in statuses
    assert "ARES: I could not transcribe that. Please try again." in statuses
    assert statuses[-1] == "No command heard; still active"
    assert len(diagnostics) == 1
    pipeline.input_text = "calculate two plus two"
    pipeline.input_success = True
    pipeline.input_status = "runtime_transport_captured"
    pipeline.input_error_stage = ""
    pipeline.input_error_reason = ""
    retry = adapter.wait_for_input(1.0)
    assert retry.status == "input"
    assert retry.text == "calculate two plus two"


@pytest.mark.parametrize("captured_text", ["goodbye ares", "shutdown ares"])
def test_failed_transcription_never_emits_lifecycle_control_input(
    captured_text,
    tmp_path,
):
    pipeline = FakePipeline()
    pipeline.input_text = captured_text
    pipeline.input_success = False
    pipeline.input_status = "transcription_timeout"
    pipeline.input_error_stage = "transcription"
    pipeline.input_error_reason = "whisper_transcription_timeout"
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
    )

    result = adapter.wait_for_input(1.0)

    assert result.status == "timeout"
    assert result.text == ""
    assert result.metadata["transcription_failure_type"] == "transcription_timeout"


def test_active_voice_no_speech_timeout_is_visible_and_keeps_adapter_open(tmp_path):
    pipeline = FakePipeline()
    pipeline.input_text = ""
    pipeline.input_status = "no_speech_timeout"
    pipeline.input_success = False
    pipeline.input_error_stage = "recording_validation"
    statuses = []
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
        status_callback=statuses.append,
    )

    first = adapter.wait_for_input(1.0)
    second = adapter.wait_for_input(1.0)

    assert first.status == second.status == "timeout"
    assert statuses.count("No command heard; still active") == 2
    assert statuses.count("Active microphone capture started") == 2
    assert adapter.capture_count == 2


def test_successful_pipeline_with_empty_transcript_is_nonterminal_and_retries(tmp_path):
    pipeline = FakePipeline()
    pipeline.input_text = ""
    pipeline.input_success = True
    pipeline.input_status = "runtime_transport_captured"
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
    )

    first = adapter.wait_for_input(1.0)
    second = adapter.wait_for_input(1.0)

    assert first.status == second.status == "timeout"
    assert adapter.capture_count == 2
    assert adapter.last_diagnostics.pipeline_status == "runtime_transport_captured"
    assert adapter.last_diagnostics.runtime_terminal is False


def test_source_local_end_of_input_is_nonterminal_and_does_not_close_adapter(tmp_path):
    pipeline = FakePipeline()
    pipeline.input_text = ""
    pipeline.input_success = False
    pipeline.input_status = "end_of_input"
    pipeline.input_error_stage = "recording"
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
    )

    first = adapter.wait_for_input(1.0)
    pipeline.input_text = "calculate two plus two"
    pipeline.input_success = True
    pipeline.input_status = "runtime_transport_captured"
    pipeline.input_error_stage = ""
    second = adapter.wait_for_input(1.0)

    assert first.status == "timeout"
    assert first.metadata["runtime_terminal"] is False
    assert second.status == "input"
    assert adapter.capture_count == 2


def test_active_command_diagnostics_use_current_command_capture_and_runtime_result(tmp_path):
    pipeline = FakePipeline()
    pipeline.input_text = "goodbye aris"
    emitted = []
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
        diagnostic_callback=emitted.append,
    )

    captured = adapter.wait_for_input(1.0)
    adapter.record_runtime_result(
        runtime_result=SimpleNamespace(
            command_category="standby",
            normalized_input="goodbye ares",
            current_lifecycle_state="STANDBY",
            session_id="",
            data={
                "core_service_bypassed": True,
                "lifecycle_command": {
                    "cleaned_transcript": "goodbye aris",
                    "normalized_transcript": "goodbye",
                    "canonicalized_transcript": "goodbye",
                    "canonical_name": "ares",
                    "matched_alias": "aris",
                    "alias_type": "pronunciation_alias",
                    "assistant_alias_removed": "aris",
                    "alias_position": "suffix",
                    "action": "standby",
                    "matched_phrase": "goodbye",
                    "negation_detected": False,
                    "rejection_reason": "",
                },
            },
        ),
        lifecycle_state_before="ACTIVE",
        session_id_before="session-1",
    )

    assert captured.text == "goodbye aris"
    assert len(emitted) == 1
    diagnostics = emitted[0]
    assert diagnostics.raw_transcript == "goodbye aris"
    assert diagnostics.cleaned_transcript == "goodbye aris"
    assert diagnostics.alias_canonicalized_transcript == "goodbye"
    assert diagnostics.lifecycle_normalized_transcript == "goodbye"
    assert diagnostics.matched_assistant_alias == "aris"
    assert diagnostics.assistant_alias_type == "pronunciation_alias"
    assert diagnostics.assistant_alias_removed == "aris"
    assert diagnostics.alias_position == "suffix"
    assert diagnostics.canonical_name == "ares"
    assert diagnostics.negation_detected is False
    assert diagnostics.selected_lifecycle_action == "standby"
    assert diagnostics.matched_lifecycle_phrase == "goodbye"
    assert diagnostics.core_service_bypassed is True
    assert diagnostics.activation_handler_called is False
    assert diagnostics.lifecycle_state_before == "ACTIVE"
    assert diagnostics.lifecycle_state_after == "STANDBY"
    assert diagnostics.session_id_before == "session-1"
    assert diagnostics.session_id_after == ""
    assert diagnostics.capture_stop_reason == "completed_after_silence"
    assert diagnostics.raw_capture_duration_seconds == pytest.approx(2.8)
    assert diagnostics.finalized_candidate_duration_seconds == pytest.approx(1.1)
    assert diagnostics.whisper_processing_duration_seconds == pytest.approx(0.42)
    assert diagnostics.terminal_silence_status == "confirmed_terminal_silence"
    assert diagnostics.wav_byte_size == 35244
    assert diagnostics.wav_sample_rate_hz == 16000
    assert diagnostics.wav_channels == 1
    assert diagnostics.wav_sample_width_bytes == 2
    assert diagnostics.transcription_backend == "whisper.cpp"
    assert diagnostics.transcription_status == "transcribed"
    assert diagnostics.transcription_timeout_seconds == 30.0
    assert diagnostics.whisper_process_pid == 4102
    assert diagnostics.whisper_process_group_id == 4102
    assert diagnostics.whisper_process_exit_code == 0
    assert diagnostics.whisper_process_elapsed_seconds == pytest.approx(0.42)
    assert diagnostics.whisper_process_reaped is True
    assert diagnostics.whisper_process_cleanup_completed is True
    assert diagnostics.whisper_output_handles_closed is True
    assert diagnostics.transcript_parsing_status == "completed"
    assert diagnostics.microphone_gate_released_before_inference is True
    assert diagnostics.temporary_audio_cleanup_status == "removed"
    assert diagnostics.routing_started_at
    assert diagnostics.routing_completed_at
    assert diagnostics.pipeline_status == "runtime_transport_captured"
    assert diagnostics.runtime_terminal is False
    assert diagnostics.runtime_terminal_reason == "not_terminal"


def test_active_rs_shutdown_uses_central_lifecycle_parser_and_diagnostics(tmp_path):
    pipeline = FakePipeline()
    pipeline.input_text = "Shut down RS"
    emitted = []
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
        diagnostic_callback=emitted.append,
    )

    captured = adapter.wait_for_input(1.0)
    adapter.record_runtime_result(
        runtime_result=SimpleNamespace(
            command_category="shutdown",
            normalized_input="shutdown ares",
            current_lifecycle_state="STOPPED",
            session_id="",
            stop_reason="explicit_shutdown_command",
            data={
                "core_service_bypassed": True,
                "lifecycle_command": {
                    "cleaned_transcript": "shutdown rs",
                    "normalized_transcript": "shutdown",
                    "canonicalized_transcript": "shutdown",
                    "canonical_name": "ares",
                    "matched_alias": "rs",
                    "alias_type": "acoustic_alias",
                    "assistant_alias_removed": "rs",
                    "alias_position": "suffix",
                    "action": "shutdown",
                    "matched_phrase": "shutdown",
                    "negation_detected": False,
                    "rejection_reason": "",
                },
            },
        ),
        lifecycle_state_before="ACTIVE",
        session_id_before="session-1",
    )

    assert captured.status == "input"
    assert captured.text == "Shut down RS"
    assert len(emitted) == 1
    diagnostics = emitted[0]
    assert diagnostics.raw_transcript == "Shut down RS"
    assert diagnostics.cleaned_transcript == "shutdown rs"
    assert diagnostics.lifecycle_normalized_transcript == "shutdown"
    assert diagnostics.alias_canonicalized_transcript == "shutdown"
    assert diagnostics.matched_assistant_alias == "rs"
    assert diagnostics.assistant_alias_type == "acoustic_alias"
    assert diagnostics.assistant_alias_removed == "rs"
    assert diagnostics.alias_position == "suffix"
    assert diagnostics.canonical_name == "ares"
    assert diagnostics.selected_lifecycle_action == "shutdown"
    assert diagnostics.matched_lifecycle_phrase == "shutdown"
    assert diagnostics.core_service_bypassed is True
    assert diagnostics.runtime_terminal is True
    assert diagnostics.runtime_terminal_reason == "explicit_shutdown_command"


@pytest.mark.parametrize(
    ("status", "stage"),
    [
        ("no_speech_timeout", "recording_validation"),
        ("silent_audio", "recording_validation"),
        ("blank_transcription", "transcription"),
        ("transcript_rejected", "transcript_normalization"),
    ],
)
def test_active_voice_input_maps_no_usable_command_to_runtime_timeout(status, stage, tmp_path):
    pipeline = FakePipeline()
    pipeline.input_text = ""
    pipeline.input_status = status
    pipeline.input_success = False
    pipeline.input_error_stage = stage
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
    )
    result = adapter.wait_for_input(1.0)
    assert result.status == "timeout"


def test_active_voice_input_maps_pipeline_failure_without_hiding_it(tmp_path):
    pipeline = FakePipeline()
    pipeline.input_text = ""
    pipeline.input_status = "recording_failed"
    pipeline.input_success = False
    pipeline.input_error_stage = "recording"
    pipeline.input_error_reason = "device unavailable"
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
    )
    result = adapter.wait_for_input(1.0)
    assert result.status == "failed"
    assert result.error_code == "active_voice_pipeline_failed"
    assert "device unavailable" in result.error_message
    assert adapter.last_diagnostics is not None
    assert adapter.last_diagnostics.raw_capture_duration_seconds == pytest.approx(2.8)


def test_runtime_voice_output_uses_local_output_path_and_only_plays_response(tmp_path):
    pipeline = FakePipeline()
    spoken = []
    adapter = SingleTurnPipelineRuntimeOutputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        output_func=spoken.append,
    )
    result = adapter.write(
        RuntimeOutputMessage(
            category="brain_response",
            text="Result: 4",
            correlation_id="response-correlation",
            session_id="session-1",
        )
    )
    assert result.success
    assert spoken == ["Result: 4"]
    assert len(pipeline.local_outputs) == 1
    request, text = pipeline.local_outputs[0]
    assert text == "Result: 4"
    assert request.text_input == "Result: 4"
    assert request.playback_enabled is True
    assert request.diagnostic_audio is False
    assert request.metadata["captured_audio_playback"] is False


def test_runtime_voice_output_reports_tts_or_speaker_failure(tmp_path):
    pipeline = FakePipeline()
    pipeline.output_success = False
    adapter = SingleTurnPipelineRuntimeOutputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        output_func=None,
    )
    result = adapter.write(RuntimeOutputMessage(category="brain_response", text="Result: 4"))
    assert not result.success
    assert result.error_code == "voice_pipeline_output_failed"
    assert result.error_message == "speaker_failed"


def test_voice_gate_rejects_capture_while_playback_and_playback_while_capture():
    gate = VoiceRuntimeGate(settle_delay_seconds=0)
    gate.begin_playback("response")
    with pytest.raises(RuntimeError, match="playback_active"):
        gate.begin_capture("command")
    gate.end_playback("response")
    gate.begin_capture("command")
    with pytest.raises(RuntimeError, match="capture_active"):
        gate.begin_playback("response")
    gate.end_capture("command")


def test_voice_gate_enforces_bounded_post_playback_settle_delay():
    clock = FakeClock()
    gate = VoiceRuntimeGate(
        settle_delay_seconds=0.35,
        clock=clock,
        sleeper=clock.sleep,
    )
    gate.begin_playback("acknowledgement")
    gate.end_playback("acknowledgement")
    assert gate.wait_for_capture(timeout_seconds=0.2) is False
    assert clock.value == pytest.approx(0.2)
    assert gate.wait_for_capture(timeout_seconds=0.2) is True
    assert clock.value == pytest.approx(0.35)
    assert gate.snapshot()["settling"] is False


def test_voice_gate_prevents_acknowledgement_self_capture_until_settled(tmp_path):
    clock = FakeClock()
    gate = VoiceRuntimeGate(settle_delay_seconds=0.5, clock=clock, sleeper=clock.sleep)
    pipeline = FakePipeline()
    statuses = []
    output = SingleTurnPipelineRuntimeOutputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        voice_io_gate=gate,
        output_func=None,
    )
    active_input = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
        voice_io_gate=gate,
        status_callback=statuses.append,
    )
    assert output.write(RuntimeOutputMessage(category="acknowledgement", text="Yes Gabi.")).success
    first = active_input.wait_for_input(0.25)
    assert first.status == "timeout"
    assert pipeline.requests == []
    assert "ARES is waiting for your command..." not in statuses
    assert "Active microphone capture started" not in statuses
    second = active_input.wait_for_input(0.25)
    assert second.status == "input"
    assert len(pipeline.requests) == 1
    assert statuses.count("ARES is waiting for your command...") == 1
    assert statuses.index("ARES is waiting for your command...") < statuses.index(
        "Active microphone capture started"
    )


def test_each_real_active_capture_has_exactly_one_waiting_message(tmp_path):
    clock = FakeClock()
    gate = VoiceRuntimeGate(
        settle_delay_seconds=0.35,
        clock=clock,
        sleeper=clock.sleep,
    )
    pipeline = FakePipeline()
    statuses = []
    output = SingleTurnPipelineRuntimeOutputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        voice_io_gate=gate,
        output_func=None,
    )
    active_input = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
        voice_io_gate=gate,
        status_callback=statuses.append,
    )

    assert output.write(
        RuntimeOutputMessage(category="brain_response", text="Result: 4")
    ).success
    assert active_input.wait_for_input(0.25).status == "timeout"
    assert active_input.wait_for_input(0.25).status == "input"

    assert statuses.count("ARES is waiting for your command...") == 1
    assert statuses.count("Active microphone capture started") == 1
    assert pipeline.requests[0].playback_enabled is False
    assert pipeline.local_outputs[0][0].metadata["captured_audio_playback"] is False
    assert gate.snapshot()["playback_active"] is False
    assert gate.snapshot()["capture_active"] is False


def test_release_and_close_are_idempotent_and_leave_gate_idle(tmp_path):
    pipeline = FakePipeline()
    gate = VoiceRuntimeGate(settle_delay_seconds=0)
    active_input = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: "session-1",
        voice_io_gate=gate,
    )
    output = SingleTurnPipelineRuntimeOutputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        voice_io_gate=gate,
        output_func=None,
    )
    active_input.release_active_resources()
    output.release_active_resources()
    active_input.close()
    active_input.close()
    output.close()
    output.close()
    assert gate.snapshot()["capture_active"] is False
    assert gate.snapshot()["playback_active"] is False
    closed = active_input.wait_for_input(0.1)
    assert closed.status == "end_of_input"
    assert closed.metadata["runtime_terminal"] is False
    assert closed.metadata["input_scope"] == "active_command"


def test_voice_runtime_adapters_do_not_duplicate_hardware_or_brain_implementations():
    source = Path("core/BrainRuntimeVoiceAdapters.py").read_text(encoding="utf-8").casefold()
    for forbidden in ("arecord", "aplay", "whisper-cli", "--model", "shell=true", "skillmanager", "json.dump"):
        assert forbidden not in source
    assert ".run_once(" in source
    assert ".run_local_output(" in source
