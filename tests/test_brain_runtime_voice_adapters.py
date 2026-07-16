from __future__ import annotations

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

    def run_once(self, request, cancellation_token=None, pre_brain_hook=None):
        self.requests.append(request)
        if pre_brain_hook is not None and self.input_text:
            decision = pre_brain_hook(self.input_text)
            assert decision.handled is True
            assert decision.continue_to_output is False
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
                }
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
    assert result.metadata["capture_stop_reason"] == "completed_after_silence"
    assert result.metadata["raw_capture_duration_seconds"] == pytest.approx(2.8)
    assert result.metadata["finalized_candidate_duration_seconds"] == pytest.approx(1.1)


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
            data={"core_service_bypassed": True},
        ),
        lifecycle_state_before="ACTIVE",
        session_id_before="session-1",
    )

    assert captured.text == "goodbye aris"
    assert len(emitted) == 1
    diagnostics = emitted[0]
    assert diagnostics.raw_transcript == "goodbye aris"
    assert diagnostics.alias_canonicalized_transcript == "goodbye ares"
    assert diagnostics.selected_lifecycle_action == "standby"
    assert diagnostics.core_service_bypassed is True
    assert diagnostics.lifecycle_state_before == "ACTIVE"
    assert diagnostics.lifecycle_state_after == "STANDBY"
    assert diagnostics.session_id_before == "session-1"
    assert diagnostics.session_id_after == ""
    assert diagnostics.capture_stop_reason == "completed_after_silence"
    assert diagnostics.raw_capture_duration_seconds == pytest.approx(2.8)
    assert diagnostics.finalized_candidate_duration_seconds == pytest.approx(1.1)
    assert diagnostics.whisper_processing_duration_seconds == pytest.approx(0.42)
    assert diagnostics.terminal_silence_status == "confirmed_terminal_silence"


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
    )
    assert output.write(RuntimeOutputMessage(category="acknowledgement", text="Yes Gabi.")).success
    first = active_input.wait_for_input(0.25)
    assert first.status == "timeout"
    assert pipeline.requests == []
    second = active_input.wait_for_input(0.25)
    assert second.status == "input"
    assert len(pipeline.requests) == 1


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
    assert active_input.wait_for_input(0.1).status == "end_of_input"


def test_voice_runtime_adapters_do_not_duplicate_hardware_or_brain_implementations():
    source = Path("core/BrainRuntimeVoiceAdapters.py").read_text(encoding="utf-8").casefold()
    for forbidden in ("arecord", "aplay", "whisper-cli", "--model", "shell=true", "skillmanager", "json.dump"):
        assert forbidden not in source
    assert ".run_once(" in source
    assert ".run_local_output(" in source
