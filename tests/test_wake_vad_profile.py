from __future__ import annotations

from dataclasses import replace
import math

import pytest

from core import (
    RmsVoiceActivityCapture,
    VAD_STATUS_COMPLETED_AFTER_SILENCE,
    VAD_STATUS_MAXIMUM_DURATION,
    VoiceActivityCaptureRequestV1,
    WakeListenerConfig,
)


FRAME_MS = 20
SAMPLES_PER_FRAME = 320
FRAME_BYTES = SAMPLES_PER_FRAME * 2


def pcm_frame(amplitude: int) -> bytes:
    return int(amplitude).to_bytes(2, "little", signed=True) * SAMPLES_PER_FRAME


class SyntheticFrameSource:
    def __init__(self, frames):
        self.frames = list(frames)
        self.read_count = 0

    def read_frame(self, frame_bytes, timeout_seconds):
        assert frame_bytes == FRAME_BYTES
        self.read_count += 1
        if not self.frames:
            raise EOFError("synthetic_frames_exhausted")
        return self.frames.pop(0)

    def close(self):
        return None


def wake_request(tmp_path, config: WakeListenerConfig) -> VoiceActivityCaptureRequestV1:
    return VoiceActivityCaptureRequestV1(
        output_wav_path=str(tmp_path / "wake.wav"),
        microphone_device=config.microphone_device,
        frame_duration_ms=config.frame_duration_ms,
        calibration_enabled=config.calibration_enabled,
        calibration_duration_seconds=config.calibration_duration_seconds,
        speech_start_rms=config.speech_start_rms,
        speech_continue_rms=config.speech_continue_rms,
        silence_rms=config.silence_rms,
        minimum_speech_start_rms=config.minimum_speech_start_rms,
        maximum_speech_start_rms=config.maximum_speech_start_rms,
        minimum_speech_continue_rms=config.minimum_speech_continue_rms,
        maximum_speech_continue_rms=config.maximum_speech_continue_rms,
        minimum_silence_rms=config.minimum_silence_rms,
        maximum_silence_rms=config.maximum_silence_rms,
        required_speech_frames=config.required_speech_frames,
        required_continue_frames=config.required_continue_frames,
        required_silence_frames=config.required_silence_frames,
        silence_duration_seconds=config.silence_duration_seconds,
        speech_wait_timeout_seconds=config.speech_wait_timeout_seconds,
        maximum_utterance_seconds=config.maximum_utterance_seconds,
        pre_roll_seconds=config.pre_roll_seconds,
        speech_end_padding_seconds=config.speech_end_padding_seconds,
        frame_read_timeout_seconds=config.frame_read_timeout_seconds,
        frame_debug_enabled=True,
        metadata={
            "vad_profile": "standby_wake_short_v1",
            "minimum_speech_duration_seconds": (
                config.minimum_speech_duration_seconds
            ),
            "diagnostic_rms_interval_frames": (
                config.diagnostic_rms_interval_frames
            ),
        },
    )


def execute_wake(tmp_path, frames, config=None):
    config = config or WakeListenerConfig()
    source = SyntheticFrameSource(frames)
    detector = RmsVoiceActivityCapture()
    detector.start()
    return detector.execute(wake_request(tmp_path, config), source), source


def test_one_word_wake_completes_after_wake_mode_terminal_silence(tmp_path):
    config = WakeListenerConfig()
    calibration_frames = math.ceil(
        config.calibration_duration_seconds / (config.frame_duration_ms / 1000.0)
    )
    silence_frames = math.ceil(
        config.silence_duration_seconds / (config.frame_duration_ms / 1000.0)
    )
    frames = (
        [pcm_frame(40)] * calibration_frames
        + [pcm_frame(40)] * 4
        + [pcm_frame(900)] * 8
        + [pcm_frame(40)] * silence_frames
    )
    result, _ = execute_wake(tmp_path, frames, config)
    assert result.success
    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.maximum_duration_reached is False
    assert result.silence_duration_at_stop_seconds == pytest.approx(
        config.silence_duration_seconds,
        abs=0.02,
    )
    assert result.duration_seconds < config.maximum_utterance_seconds


def test_wake_mode_noise_cannot_extend_capture_beyond_hard_active_bound(tmp_path):
    config = WakeListenerConfig()
    calibration_frames = math.ceil(config.calibration_duration_seconds / 0.02)
    maximum_frames = math.ceil(config.maximum_utterance_seconds / 0.02)
    frames = (
        [pcm_frame(40)] * calibration_frames
        + [pcm_frame(900)] * 8
        + [pcm_frame(135)] * (maximum_frames + 20)
    )
    result, source = execute_wake(tmp_path, frames, config)
    assert result.success
    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.duration_seconds <= (
        config.maximum_utterance_seconds + config.pre_roll_seconds + 0.04
    )
    assert source.read_count <= calibration_frames + maximum_frames + 2


def test_wake_endpoint_counts_noise_between_silence_and_continuation_as_quiet(tmp_path):
    config = WakeListenerConfig()
    calibration_frames = math.ceil(config.calibration_duration_seconds / 0.02)
    quiet_frames = math.ceil(config.silence_duration_seconds / 0.02)
    frames = (
        [pcm_frame(40)] * calibration_frames
        + [pcm_frame(900)] * 8
        + [pcm_frame(135)] * quiet_frames
    )
    result, _ = execute_wake(tmp_path, frames, config)
    assert result.success
    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.data["terminal_quiet_frame_count"] == quiet_frames
    assert result.silence_duration_at_stop_seconds == pytest.approx(
        quiet_frames * 0.02
    )


def test_wake_terminal_quiet_counter_resets_on_real_continuation_evidence(tmp_path):
    config = WakeListenerConfig()
    calibration_frames = math.ceil(config.calibration_duration_seconds / 0.02)
    quiet_frames = math.ceil(config.silence_duration_seconds / 0.02)
    frames = (
        [pcm_frame(40)] * calibration_frames
        + [pcm_frame(900)] * 8
        + [pcm_frame(40)] * 10
        + [pcm_frame(190)]
        + [pcm_frame(40)] * quiet_frames
    )
    result, _ = execute_wake(tmp_path, frames, config)
    assert result.success
    assert result.data["terminal_quiet_frame_count"] == quiet_frames
    assert result.silence_duration_at_stop_seconds == pytest.approx(
        quiet_frames * 0.02
    )


def test_wake_pcm_pre_roll_post_roll_and_speech_frames_are_appended_once(tmp_path):
    config = WakeListenerConfig()
    calibration_frames = math.ceil(config.calibration_duration_seconds / 0.02)
    pre_roll_frames = math.ceil(config.pre_roll_seconds / 0.02)
    quiet_frames = math.ceil(config.silence_duration_seconds / 0.02)
    post_roll_frames = math.ceil(config.speech_end_padding_seconds / 0.02)
    frames = (
        [pcm_frame(40)] * calibration_frames
        + [pcm_frame(40)] * (pre_roll_frames + 3)
        + [pcm_frame(900)] * 8
        + [pcm_frame(40)] * quiet_frames
    )
    result, _ = execute_wake(tmp_path, frames, config)
    assert result.success
    assert result.pre_roll_frames_retained == pre_roll_frames
    assert result.data["post_roll_frames_retained"] == post_roll_frames
    assert result.data["duplicate_frame_append_count"] == 0
    assert result.data["captured_frame_indexes_unique"] is True


def test_guarded_wake_calibration_excludes_detected_speech_without_reset(tmp_path):
    config = WakeListenerConfig()
    request = replace(
        wake_request(tmp_path, config),
        calibration_duration_seconds=0.6,
        metadata={
            "calibration_confirm_non_speech": True,
            "calibration_maximum_seconds": 1.2,
        },
    )
    source = SyntheticFrameSource(
        [pcm_frame(40)] * 10
        + [pcm_frame(900)]
        + [pcm_frame(40)] * 30
    )
    detector = RmsVoiceActivityCapture()
    detector.start()
    result = detector.calibrate_stream(request, source)
    assert result.success
    assert result.rejected_speech_frames == 1
    assert result.restart_count == 0
    assert result.frame_count == 30
    assert result.ambient_statistics.sample_count == 30
    assert result.ambient_statistics.noise_floor_rms == pytest.approx(40.0)
    assert result.diagnostics.quality_reason == "calibration_quality_passed"


def test_single_frame_click_does_not_start_a_wake_candidate(tmp_path):
    config = WakeListenerConfig()
    calibration_frames = math.ceil(config.calibration_duration_seconds / 0.02)
    wait_frames = math.ceil(config.speech_wait_timeout_seconds / 0.02)
    frames = (
        [pcm_frame(40)] * calibration_frames
        + [pcm_frame(2500)]
        + [pcm_frame(40)] * wait_frames
    )
    result, _ = execute_wake(tmp_path, frames, config)
    assert not result.success
    assert result.status == "no_speech_timeout"
    assert result.speech_detected is False


def test_wake_candidate_retains_full_configured_pre_roll_and_short_speech(tmp_path):
    config = WakeListenerConfig()
    calibration_frames = math.ceil(config.calibration_duration_seconds / 0.02)
    expected_pre_roll = math.ceil(config.pre_roll_seconds / 0.02)
    silence_frames = math.ceil(config.silence_duration_seconds / 0.02)
    frames = (
        [pcm_frame(40)] * calibration_frames
        + [pcm_frame(40)] * (expected_pre_roll + 5)
        + [pcm_frame(900)] * 5
        + [pcm_frame(40)] * silence_frames
    )
    result, _ = execute_wake(tmp_path, frames, config)
    assert result.success
    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.pre_roll_frames_retained == expected_pre_roll
    assert result.speech_frame_count >= 5
    assert result.speech_duration_seconds >= 0.1


def test_wake_profile_does_not_change_full_command_vad_defaults():
    wake = WakeListenerConfig()
    command = VoiceActivityCaptureRequestV1()
    assert wake.maximum_utterance_seconds == 1.6
    assert wake.silence_duration_seconds == 0.55
    assert wake.pre_roll_seconds == 0.4
    assert wake.speech_end_padding_seconds == 0.12
    assert command.maximum_utterance_seconds == 15.0
    assert command.silence_duration_seconds == 0.9
    assert command.pre_roll_seconds == 0.25
    assert command.speech_end_padding_seconds == 0.0
