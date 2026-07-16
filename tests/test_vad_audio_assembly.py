from __future__ import annotations

from pathlib import Path
import wave

import pytest

from core import (
    RmsVoiceActivityCapture,
    VAD_STATUS_COMPLETED_AFTER_SILENCE,
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


def execute(tmp_path: Path, frames, **overrides):
    values = {
        "output_wav_path": str(tmp_path / "assembled.wav"),
        "microphone_device": "plughw:2,0",
        "frame_duration_ms": FRAME_MS,
        "calibration_enabled": True,
        "calibration_duration_seconds": 0.75,
        "speech_start_rms": 200,
        "speech_continue_rms": 160,
        "silence_rms": 120,
        "required_speech_frames": 3,
        "required_continue_frames": 3,
        "required_silence_frames": 5,
        "silence_duration_seconds": 0.9,
        "speech_wait_timeout_seconds": 10.0,
        "maximum_utterance_seconds": 15.0,
        "pre_roll_seconds": 0.25,
    }
    values.update(overrides)
    detector = RmsVoiceActivityCapture()
    detector.start()
    return detector.execute(
        VoiceActivityCaptureRequestV1(**values),
        SyntheticFrameSource(frames),
    )


def read_pcm(path: str) -> bytes:
    with wave.open(path, "rb") as wav_file:
        return wav_file.readframes(wav_file.getnframes())


def raspberry_pi_342_second_sequence():
    calibration = [pcm_frame(40)] * 38
    waiting = [pcm_frame(40)] * 10
    speech_start = [pcm_frame(700)] * 3
    first_phrase = [pcm_frame(700)] * 20
    first_pause = [pcm_frame(139)] * 8
    first_resume = [pcm_frame(700)] * 3
    second_phrase = [pcm_frame(700)] * 10
    second_pause = [pcm_frame(139)] * 6
    second_resume = [pcm_frame(700)] * 3
    final_phrase = [pcm_frame(700)] * 15
    low_energy_final_words = [pcm_frame(139)] * 10
    terminal_silence = [pcm_frame(20)] * 45
    raw = (
        calibration
        + waiting
        + speech_start
        + first_phrase
        + first_pause
        + first_resume
        + second_phrase
        + second_pause
        + second_resume
        + final_phrase
        + low_energy_final_words
        + terminal_silence
    )
    assembled = (
        waiting
        + speech_start
        + first_phrase
        + first_pause
        + first_resume
        + second_phrase
        + second_pause
        + second_resume
        + final_phrase
        + low_energy_final_words
    )
    return raw, assembled, first_pause, second_pause, low_energy_final_words


def test_342_second_capture_preserves_complete_detected_utterance(tmp_path):
    raw, assembled, first_pause, second_pause, final_words = (
        raspberry_pi_342_second_sequence()
    )

    result = execute(tmp_path, raw)

    assert len(raw) == 171
    assert result.success is True
    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.total_frames_read == 171
    assert result.raw_duration_seconds == pytest.approx(3.42)
    assert result.final_assembled_frame_count == len(assembled) == 88
    assert result.final_assembled_sample_count == 88 * SAMPLES_PER_FRAME
    assert result.final_assembled_byte_count == 88 * FRAME_BYTES
    assert result.assembled_duration_seconds == pytest.approx(1.76)
    assert result.duration_seconds == pytest.approx(1.76)
    assert result.trailing_silence_frame_count == 45
    assert result.trailing_silence_trimmed_seconds == pytest.approx(0.9)
    assert result.possible_silence_frames_retained == 24
    pcm = read_pcm(result.wav_path)
    assert pcm == b"".join(assembled)
    assert b"".join(first_pause) in pcm
    assert b"".join(second_pause) in pcm
    assert pcm.endswith(b"".join(final_words))


def test_detected_342_second_utterance_is_not_replaced_by_one_second_segment(
    tmp_path,
):
    calibration = [pcm_frame(40)] * 38
    pre_roll = [pcm_frame(40)] * 10
    utterance = (
        [pcm_frame(700)] * 3
        + [pcm_frame(700)] * 50
        + [pcm_frame(139)] * 8
        + [pcm_frame(700)] * 3
        + [pcm_frame(700)] * 40
        + [pcm_frame(139)] * 6
        + [pcm_frame(700)] * 3
        + [pcm_frame(700)] * 48
    )
    trailing = [pcm_frame(20)] * 45

    result = execute(tmp_path, calibration + pre_roll + utterance + trailing)

    expected = pre_roll + utterance
    assert len(expected) == 171
    assert result.success is True
    assert result.assembled_duration_seconds == pytest.approx(3.42)
    assert result.final_assembled_sample_count == 54720
    assert result.normalized_sample_count == 54720
    assert result.duration_seconds != pytest.approx(1.0)
    assert read_pcm(result.wav_path) == b"".join(expected)


def test_repeated_possible_silence_transitions_do_not_reset_assembly(tmp_path):
    raw, assembled, _, _, _ = raspberry_pi_342_second_sequence()

    result = execute(tmp_path, raw)
    transitions = [
        (item["from"], item["to"])
        for item in result.data["transitions"]
    ]

    assert transitions.count(("SPEECH", "POSSIBLE_SILENCE")) == 3
    assert transitions.count(("POSSIBLE_SILENCE", "SPEECH")) == 2
    assert transitions[-1] == ("POSSIBLE_SILENCE", "COMPLETE")
    assert read_pcm(result.wav_path) == b"".join(assembled)


def test_only_confirmed_terminal_silence_is_trimmed(tmp_path):
    speech = [pcm_frame(700)] * 2
    low_energy_words = [pcm_frame(159)] * 4
    trailing = [pcm_frame(20)] * 5

    result = execute(
        tmp_path,
        speech + low_energy_words + trailing,
        calibration_enabled=False,
        required_speech_frames=2,
        required_continue_frames=2,
        required_silence_frames=1,
        silence_duration_seconds=0.1,
        speech_wait_timeout_seconds=0.2,
        maximum_utterance_seconds=1.0,
        pre_roll_seconds=0.0,
    )

    assert result.success is True
    assert result.trailing_silence_frame_count == 5
    assert result.trailing_silence_trimmed_seconds == pytest.approx(0.1)
    assert result.untrimmed_duration_seconds == pytest.approx(0.22)
    assert result.assembled_duration_seconds == pytest.approx(0.12)
    assert read_pcm(result.wav_path) == b"".join(speech + low_energy_words)


def test_short_lifecycle_phrase_uses_command_profile_and_finishes_after_terminal_silence(
    tmp_path,
):
    calibration = [pcm_frame(40)] * 38
    waiting = [pcm_frame(40)] * 8
    short_phrase = [pcm_frame(700)] * 18
    terminal_room_noise = [pcm_frame(70)] * 45

    result = execute(
        tmp_path,
        calibration + waiting + short_phrase + terminal_room_noise,
    )

    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.stop_reason == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.maximum_duration_reached is False
    assert result.raw_duration_seconds == pytest.approx(2.18)
    assert result.trailing_silence_trimmed_seconds == pytest.approx(0.9)
    assert result.data["transitions"][-1]["to"] == "COMPLETE"


def test_command_profile_preserves_natural_pause_and_does_not_cut_longer_command(
    tmp_path,
):
    calibration = [pcm_frame(40)] * 38
    first_words = [pcm_frame(700)] * 40
    internal_pause = [pcm_frame(100)] * 20
    resumed_words = [pcm_frame(700)] * 45
    terminal_silence = [pcm_frame(40)] * 45

    result = execute(
        tmp_path,
        calibration + first_words + internal_pause + resumed_words + terminal_silence,
    )

    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    pcm = read_pcm(result.wav_path)
    assert b"".join(internal_pause) in pcm
    assert result.maximum_duration_reached is False
    assert result.assembled_duration_seconds > 2.0
