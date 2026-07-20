from __future__ import annotations

from pathlib import Path

import pytest

from core import (
    RmsVoiceActivityCapture,
    RollingPcmFrameSource,
    VAD_STATUS_COMPLETED_AFTER_SILENCE,
    VAD_STATUS_NO_SPEECH_TIMEOUT,
    VoiceActivityCaptureRequestV1,
    WakeListenerConfig,
)
from core.VoiceActivityCalibration import (
    AmbientStatistics,
    derive_wake_thresholds,
)


FRAME_MS = 20
SAMPLES_PER_FRAME = 320
FRAME_BYTES = SAMPLES_PER_FRAME * 2


def pcm_frame(amplitude: int) -> bytes:
    return int(amplitude).to_bytes(2, "little", signed=True) * SAMPLES_PER_FRAME


class SyntheticFrameSource:
    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False

    def read_frame(self, frame_bytes, timeout_seconds):
        assert frame_bytes == FRAME_BYTES
        if not self.frames:
            raise EOFError("synthetic_frames_exhausted")
        return self.frames.pop(0)

    def close(self):
        self.closed = True


def ambient_statistics(value: float = 400.0) -> AmbientStatistics:
    return AmbientStatistics(
        mean_rms=value,
        median_rms=value,
        percentile_rms=value + 20.0,
        peak_rms=value + 40.0,
        noise_floor_rms=value - 200.0,
        sample_count=150,
    )


def wake_request(tmp_path: Path, **overrides) -> VoiceActivityCaptureRequestV1:
    values = {
        "output_wav_path": str(tmp_path / "wake.wav"),
        "microphone_device": "plughw:2,0",
        "frame_duration_ms": FRAME_MS,
        "calibration_enabled": False,
        "calibration_duration_seconds": 0.0,
        "speech_start_rms": 472.0,
        "speech_continue_rms": 432.0,
        "silence_rms": 416.0,
        "required_speech_frames": 2,
        "required_continue_frames": 3,
        "required_silence_frames": 5,
        "silence_duration_seconds": 0.1,
        "speech_wait_timeout_seconds": 0.2,
        "maximum_utterance_seconds": 1.0,
        "pre_roll_seconds": 0.08,
        "speech_end_padding_seconds": 0.02,
        "frame_debug_enabled": True,
        "metadata": {
            "vad_profile": "standby_wake_short_v1",
            "minimum_speech_duration_seconds": 0.08,
            "diagnostic_rms_interval_frames": 1,
        },
    }
    values.update(overrides)
    return VoiceActivityCaptureRequestV1(**values)


def execute_wake(tmp_path: Path, frames, **overrides):
    raw = SyntheticFrameSource(frames)
    source = RollingPcmFrameSource(raw)
    detector = RmsVoiceActivityCapture()
    detector.start()
    result = detector.execute(wake_request(tmp_path, **overrides), source)
    return result, source


def test_wake_sensitivity_profiles_are_ordered_and_above_measured_ambient(tmp_path):
    request = wake_request(tmp_path)
    conservative = derive_wake_thresholds(
        ambient_statistics(), request, "conservative"
    )
    normal = derive_wake_thresholds(ambient_statistics(), request, "normal")
    sensitive = derive_wake_thresholds(ambient_statistics(), request, "sensitive")

    assert conservative.speech_start_rms > normal.speech_start_rms
    assert normal.speech_start_rms > sensitive.speech_start_rms > 400.0
    for thresholds in (conservative, normal, sensitive):
        assert (
            thresholds.speech_start_rms
            > thresholds.speech_continue_rms
            > thresholds.silence_rms
            > 400.0
        )


@pytest.mark.parametrize("value", ["", "aggressive", "NORMAL "])
def test_wake_sensitivity_configuration_is_bounded(value):
    if value == "NORMAL ":
        config = WakeListenerConfig(wake_vad_sensitivity=value)
        assert config.wake_vad_sensitivity == "normal"
    else:
        with pytest.raises(ValueError, match="wake_vad_sensitivity"):
            WakeListenerConfig(wake_vad_sensitivity=value)


def test_short_wake_envelope_starts_on_two_frames_and_preserves_pre_roll(tmp_path):
    frames = (
        [pcm_frame(400)] * 4
        + [pcm_frame(560)] * 2
        + [pcm_frame(500)] * 4
        + [pcm_frame(400)] * 5
    )
    result, source = execute_wake(tmp_path, frames)

    assert result.success is True
    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.speech_frame_count >= 6
    assert result.pre_roll_frames_retained == 4
    assert result.duration_seconds > 0
    assert result.data["source_frames_read_delta"] == result.total_frames_read
    assert result.data["source_live_frames_read_delta"] == result.total_frames_read
    assert result.data["source_bytes_read_delta"] == result.total_frames_read * FRAME_BYTES
    assert result.data["maximum_consecutive_speech_evidence"] == 2
    assert result.data["speech_start_threshold_crossing_count"] >= 2
    assert source.snapshot()["closed"] is False

    transition = next(
        item for item in result.data["transitions"] if item["to"] == "SPEECH"
    )
    trace = result.data["frame_trace"]
    transition_trace = next(item for item in trace if item["frame"] == transition["frame"])
    assert transition_trace["vad_state_transition"] == "WAITING->SPEECH"
    assert transition_trace["consecutive_speech_evidence"] == 2
    assert all(item["bytes_read"] == FRAME_BYTES for item in trace)
    assert all(item["source_read_sequence"] > 0 for item in trace)


def test_normal_room_noise_reaches_vad_but_creates_no_candidate(tmp_path):
    result, source = execute_wake(tmp_path, [pcm_frame(410)] * 10)

    assert result.status == VAD_STATUS_NO_SPEECH_TIMEOUT
    assert result.speech_detected is False
    assert result.duration_seconds == 0
    assert result.data["capture_failure_stage"] == "speech_threshold_not_crossed"
    assert result.data["source_frames_read_delta"] == 10
    assert result.data["source_live_frames_read_delta"] == 10
    assert result.data["source_bytes_read_delta"] == 10 * FRAME_BYTES
    assert result.data["listening_duration_seconds"] == pytest.approx(0.2)
    assert result.data["speech_start_threshold_crossing_count"] == 0
    assert result.data["maximum_observed_rms"] == pytest.approx(410.0)
    assert len(result.data["frame_trace"]) == 10
    assert source.snapshot()["closed"] is False


def test_brief_impulse_does_not_satisfy_two_frame_speech_evidence(tmp_path):
    frames = [pcm_frame(410)] * 2 + [pcm_frame(900)] + [pcm_frame(410)] * 7
    result, _ = execute_wake(tmp_path, frames)

    assert result.status == VAD_STATUS_NO_SPEECH_TIMEOUT
    assert result.speech_detected is False
    assert result.data["capture_failure_stage"] == "speech_start_evidence_incomplete"
    assert result.data["speech_start_threshold_crossing_count"] == 1
    assert result.data["maximum_consecutive_speech_evidence"] == 1


def test_sensitive_profile_is_still_above_ambient_and_below_normal(tmp_path):
    request = wake_request(tmp_path)
    normal = derive_wake_thresholds(ambient_statistics(430.0), request, "normal")
    sensitive = derive_wake_thresholds(
        ambient_statistics(430.0), request, "sensitive"
    )

    assert 430.0 < sensitive.speech_start_rms < normal.speech_start_rms
    assert sensitive.speech_continue_rms > 430.0
    assert sensitive.silence_rms > 430.0
