from pathlib import Path

import pytest

from core import (
    RmsVoiceActivityCapture,
    VAD_STATUS_CANCELLED,
    VAD_STATUS_COMPLETED_AFTER_SILENCE,
    VAD_STATUS_MAXIMUM_DURATION,
    VAD_STATUS_NO_SPEECH_TIMEOUT,
    VoiceActivityCaptureRequestV1,
)


FRAME_MS = 20
SAMPLES_PER_FRAME = 320
FRAME_BYTES = SAMPLES_PER_FRAME * 2


def pcm_frame(amplitude: int) -> bytes:
    return int(amplitude).to_bytes(2, "little", signed=True) * SAMPLES_PER_FRAME


class SyntheticFrameSource:
    def __init__(self, frames):
        self.frames = list(frames)

    def read_frame(self, frame_bytes, timeout_seconds):
        assert frame_bytes == FRAME_BYTES
        if not self.frames:
            raise EOFError("synthetic_frames_exhausted")
        return self.frames.pop(0)

    def close(self):
        return None


def adaptive_request(tmp_path, **overrides):
    values = {
        "output_wav_path": str(tmp_path / "adaptive.wav"),
        "microphone_device": "hw:2,0",
        "frame_duration_ms": FRAME_MS,
        "calibration_enabled": True,
        "calibration_duration_seconds": 0.1,
        "speech_start_rms": 200,
        "speech_continue_rms": 160,
        "silence_rms": 120,
        "required_speech_frames": 2,
        "required_continue_frames": 2,
        "required_silence_frames": 3,
        "silence_duration_seconds": 0.1,
        "speech_wait_timeout_seconds": 0.2,
        "maximum_utterance_seconds": 1.0,
        "pre_roll_seconds": 0.04,
    }
    values.update(overrides)
    return VoiceActivityCaptureRequestV1(**values)


def execute(tmp_path, frames, **overrides):
    detector = RmsVoiceActivityCapture()
    detector.start()
    return detector.execute(
        adaptive_request(tmp_path, **overrides),
        SyntheticFrameSource(frames),
    )


def normal_utterance(ambient=40, speech=700, silence=30):
    return [pcm_frame(ambient)] * 5 + [pcm_frame(speech)] * 5 + [pcm_frame(silence)] * 5


def test_quiet_room_calibration_derives_bounded_hysteresis(tmp_path):
    result = execute(tmp_path, normal_utterance())

    assert result.success is True
    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.ambient_rms_median == pytest.approx(40.0)
    assert result.derived_speech_start_rms == 200.0
    assert result.derived_speech_continue_rms < result.derived_speech_start_rms
    assert result.derived_silence_rms < result.derived_speech_continue_rms


def test_noisy_room_calibration_raises_thresholds_without_exceeding_bounds(tmp_path):
    result = execute(tmp_path, normal_utterance(ambient=180, speech=1000, silence=180))

    assert result.success is True
    assert result.derived_speech_start_rms == pytest.approx(630.0)
    assert result.derived_speech_continue_rms == pytest.approx(450.0)
    assert result.derived_silence_rms == pytest.approx(315.0)


def test_one_large_transient_does_not_raise_ambient_percentile(tmp_path):
    ambient = [pcm_frame(40)] * 39 + [pcm_frame(2000)]
    result = execute(
        tmp_path,
        ambient + [pcm_frame(700)] * 5 + [pcm_frame(30)] * 5,
        calibration_duration_seconds=0.8,
    )

    assert result.success is True
    assert result.ambient_rms_peak == pytest.approx(2000.0)
    assert result.ambient_rms_percentile == pytest.approx(40.0)
    assert result.derived_speech_start_rms == 200.0


def test_speech_begins_only_after_calibration(tmp_path):
    result = execute(tmp_path, normal_utterance())
    transitions = result.data["transitions"]

    assert transitions[0]["from"] == "CALIBRATING"
    assert transitions[0]["to"] == "WAITING"
    assert any(item["to"] == "SPEECH" for item in transitions)
    assert result.speech_start_offset_seconds >= 0.0


def test_short_internal_pause_resumes_after_consecutive_speech_frames(tmp_path):
    frames = (
        [pcm_frame(40)] * 5
        + [pcm_frame(700)] * 4
        + [pcm_frame(30)] * 2
        + [pcm_frame(700)] * 3
        + [pcm_frame(30)] * 5
    )
    result = execute(tmp_path, frames)

    assert result.success is True
    transitions = [(item["from"], item["to"]) for item in result.data["transitions"]]
    assert ("POSSIBLE_SILENCE", "SPEECH") in transitions


def test_several_short_pauses_do_not_end_capture(tmp_path):
    frames = [pcm_frame(40)] * 5 + [pcm_frame(700)] * 3
    for _ in range(3):
        frames.extend([pcm_frame(30)] * 2)
        frames.extend([pcm_frame(700)] * 3)
    frames.extend([pcm_frame(30)] * 5)

    result = execute(tmp_path, frames)

    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert sum(item["to"] == "SPEECH" for item in result.data["transitions"]) >= 4


def test_trailing_silence_ends_capture_without_maximum_duration(tmp_path):
    result = execute(tmp_path, normal_utterance(), maximum_utterance_seconds=1.5)

    assert result.stop_reason == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.maximum_duration_reached is False
    assert result.speech_end_offset_seconds < 1.5


def test_sub_continue_noise_cannot_extend_capture_to_maximum_duration(tmp_path):
    frames = [pcm_frame(40)] * 5 + [pcm_frame(700)] * 5 + [pcm_frame(100)] * 45
    result = execute(tmp_path, frames, maximum_utterance_seconds=1.2)

    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.maximum_duration_reached is False
    assert result.derived_silence_rms >= 100


def test_no_speech_timeout_starts_after_calibration(tmp_path):
    result = execute(tmp_path, [pcm_frame(40)] * 15)

    assert result.success is False
    assert result.status == VAD_STATUS_NO_SPEECH_TIMEOUT
    assert result.frame_count == 15
    assert result.calibration_enabled is True


def test_maximum_utterance_remains_hard_limit(tmp_path):
    frames = [pcm_frame(40)] * 5 + [pcm_frame(700)] * 30
    result = execute(tmp_path, frames, maximum_utterance_seconds=0.2)

    assert result.status == VAD_STATUS_MAXIMUM_DURATION
    assert result.maximum_duration_reached is True


def test_cancellation_during_calibration_is_structured(tmp_path):
    detector = RmsVoiceActivityCapture()
    detector.start()
    result = detector.execute(
        adaptive_request(tmp_path),
        SyntheticFrameSource([pcm_frame(40)]),
        cancel_requested=lambda: True,
    )

    assert result.status == VAD_STATUS_CANCELLED
    assert Path(adaptive_request(tmp_path).output_wav_path).exists() is False


def test_auto_calibration_disabled_preserves_manual_thresholds(tmp_path):
    frames = [pcm_frame(250)] * 2 + [pcm_frame(50)] * 5
    result = execute(
        tmp_path,
        frames,
        calibration_enabled=False,
        speech_start_rms=240,
        speech_continue_rms=180,
        silence_rms=90,
        pre_roll_seconds=0,
    )

    assert result.success is True
    assert result.calibration_enabled is False
    assert result.derived_speech_start_rms == 240
    assert result.derived_speech_continue_rms == 180
    assert result.derived_silence_rms == 90


def test_derived_thresholds_respect_configured_bounds(tmp_path):
    result = execute(
        tmp_path,
        normal_utterance(ambient=500, speech=1400, silence=300),
        minimum_speech_start_rms=250,
        maximum_speech_start_rms=1000,
        minimum_speech_continue_rms=180,
        maximum_speech_continue_rms=800,
        minimum_silence_rms=100,
        maximum_silence_rms=500,
    )

    assert 250 <= result.derived_speech_start_rms <= 1000
    assert 180 <= result.derived_speech_continue_rms <= 800
    assert 100 <= result.derived_silence_rms <= 500
    assert result.derived_silence_rms < result.derived_speech_continue_rms
    assert result.derived_speech_continue_rms < result.derived_speech_start_rms
