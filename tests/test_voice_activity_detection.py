from __future__ import annotations

from pathlib import Path
import wave

import pytest

from core import (
    CancellationToken,
    RmsVoiceActivityCapture,
    VAD_STATUS_CANCELLED,
    VAD_STATUS_COMPLETED_AFTER_SILENCE,
    VAD_STATUS_DEVICE_ERROR,
    VAD_STATUS_INVALID_AUDIO,
    VAD_STATUS_MAXIMUM_DURATION,
    VAD_STATUS_NO_SPEECH_TIMEOUT,
    VAD_STATUS_TIMEOUT,
    VoiceActivityCaptureRequestV1,
)


FRAME_MS = 20
SAMPLES_PER_FRAME = 320
FRAME_BYTES = SAMPLES_PER_FRAME * 2


def pcm_frame(amplitude: int) -> bytes:
    return int(amplitude).to_bytes(2, "little", signed=True) * SAMPLES_PER_FRAME


class SyntheticFrameSource:
    def __init__(self, frames=None, failure=None):
        self.frames = list(frames or [])
        self.failure = failure
        self.read_count = 0
        self.closed = False

    def read_frame(self, frame_bytes, timeout_seconds):
        self.read_count += 1
        if self.failure:
            raise self.failure
        if not self.frames:
            raise EOFError("synthetic_frames_exhausted")
        frame = self.frames.pop(0)
        assert frame_bytes == FRAME_BYTES
        return frame

    def close(self):
        self.closed = True


def request(tmp_path, **overrides):
    values = {
        "output_wav_path": str(tmp_path / "capture.wav"),
        "microphone_device": "hw:2,0",
        "frame_duration_ms": FRAME_MS,
        "speech_start_rms": 200,
        "silence_rms": 120,
        "required_speech_frames": 2,
        "silence_duration_seconds": 0.1,
        "speech_wait_timeout_seconds": 0.1,
        "maximum_utterance_seconds": 0.2,
        "pre_roll_seconds": 0.04,
    }
    values.update(overrides)
    return VoiceActivityCaptureRequestV1(**values)


def execute(tmp_path, frames, **overrides):
    detector = RmsVoiceActivityCapture()
    detector.start()
    return detector.execute(request(tmp_path, **overrides), SyntheticFrameSource(frames))


def read_pcm(path):
    with wave.open(str(path), "rb") as wav_file:
        return wav_file.readframes(wav_file.getnframes())


def test_no_speech_returns_timeout_without_wav(tmp_path):
    result = execute(tmp_path, [pcm_frame(40)] * 5)

    assert result.success is False
    assert result.status == VAD_STATUS_NO_SPEECH_TIMEOUT
    assert result.speech_detected is False
    assert result.ambient_rms == pytest.approx(40.0)
    assert not Path(request(tmp_path).output_wav_path).exists()


def test_immediate_speech_completes_after_silence_and_trims_tail(tmp_path):
    result = execute(
        tmp_path,
        [pcm_frame(500), pcm_frame(500), *([pcm_frame(20)] * 5)],
        pre_roll_seconds=0.0,
    )

    assert result.success is True
    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.stop_reason == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.silence_duration_at_stop_seconds == pytest.approx(0.1)
    assert result.duration_seconds == pytest.approx(0.04)
    assert len(read_pcm(result.wav_path)) == FRAME_BYTES * 2
    assert result.data["terminal_silence_trimmed"] is True
    assert result.data["speech_start_status"] == "speech_detected"


def test_short_utterance_writes_valid_mono_pcm_wav(tmp_path):
    result = execute(
        tmp_path,
        [pcm_frame(30), pcm_frame(450), pcm_frame(500), pcm_frame(420), *([pcm_frame(0)] * 5)],
    )

    assert result.success is True
    with wave.open(result.wav_path, "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getnframes() > 0


def test_long_utterance_stops_at_maximum_duration(tmp_path):
    result = execute(
        tmp_path,
        [pcm_frame(500)] * 20,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.0,
    )

    assert result.success is True
    assert result.status == VAD_STATUS_MAXIMUM_DURATION
    assert result.duration_seconds <= 0.22


def test_pre_roll_preserves_audio_before_detection(tmp_path):
    pre_roll = [pcm_frame(50), pcm_frame(75)]
    result = execute(
        tmp_path,
        [*pre_roll, pcm_frame(450), pcm_frame(500), *([pcm_frame(0)] * 5)],
        pre_roll_seconds=0.04,
    )

    pcm = read_pcm(result.wav_path)
    assert pcm.startswith(b"".join(pre_roll))
    assert result.data["pre_roll_frames"] == 2


def test_first_syllable_candidate_frames_are_preserved(tmp_path):
    first = pcm_frame(300)
    second = pcm_frame(500)
    result = execute(
        tmp_path,
        [first, second, *([pcm_frame(0)] * 5)],
        pre_roll_seconds=0.0,
    )

    assert read_pcm(result.wav_path).startswith(first + second)


def test_pause_shorter_than_silence_threshold_is_preserved(tmp_path):
    pause = [pcm_frame(20), pcm_frame(20)]
    result = execute(
        tmp_path,
        [pcm_frame(500), pcm_frame(500), *pause, pcm_frame(350), *([pcm_frame(0)] * 5)],
        pre_roll_seconds=0.0,
    )

    pcm = read_pcm(result.wav_path)
    assert pause[0] + pause[1] in pcm
    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE


def test_threshold_boundary_starts_speech_and_silence_boundary_stops(tmp_path):
    result = execute(
        tmp_path,
        [pcm_frame(200), pcm_frame(200), *([pcm_frame(120)] * 5)],
        pre_roll_seconds=0.0,
    )

    assert result.success is True
    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE


def test_hysteresis_keeps_mid_level_frame_active(tmp_path):
    middle = pcm_frame(150)
    result = execute(
        tmp_path,
        [pcm_frame(250), pcm_frame(250), middle, *([pcm_frame(100)] * 5)],
        pre_roll_seconds=0.0,
    )

    assert middle in read_pcm(result.wav_path)


def test_cancellation_returns_structured_result_without_wav(tmp_path):
    detector = RmsVoiceActivityCapture()
    detector.start()

    result = detector.execute(
        request(tmp_path),
        SyntheticFrameSource([pcm_frame(500)]),
        cancel_requested=lambda: True,
    )

    assert result.success is False
    assert result.status == VAD_STATUS_CANCELLED
    assert not Path(request(tmp_path).output_wav_path).exists()


def test_unrequested_cooperative_token_does_not_cancel_capture(tmp_path):
    token = CancellationToken("vad-task")
    result = execute(
        tmp_path,
        [pcm_frame(500), pcm_frame(500), *([pcm_frame(0)] * 5)],
        pre_roll_seconds=0.0,
    )
    detector = RmsVoiceActivityCapture()
    detector.start()
    result_with_token = detector.execute(
        request(tmp_path, output_wav_path=str(tmp_path / "token.wav"), pre_roll_seconds=0.0),
        SyntheticFrameSource([pcm_frame(500), pcm_frame(500), *([pcm_frame(0)] * 5)]),
        cancel_requested=token,
    )

    assert result.success is True
    assert result_with_token.success is True


def test_device_failure_is_structured(tmp_path):
    detector = RmsVoiceActivityCapture()
    detector.start()
    result = detector.execute(
        request(tmp_path),
        SyntheticFrameSource(failure=OSError("device failed")),
    )

    assert result.success is False
    assert result.status == VAD_STATUS_DEVICE_ERROR
    assert "OSError" in result.error_message


def test_frame_timeout_is_structured(tmp_path):
    detector = RmsVoiceActivityCapture()
    detector.start()
    result = detector.execute(
        request(tmp_path),
        SyntheticFrameSource(failure=TimeoutError("timeout")),
    )

    assert result.success is False
    assert result.status == VAD_STATUS_TIMEOUT


def test_invalid_pcm_frame_is_rejected(tmp_path):
    result = execute(tmp_path, [b"short"])

    assert result.success is False
    assert result.status == VAD_STATUS_INVALID_AUDIO
    assert result.error_message == "invalid_pcm_frame_size"


def test_invalid_threshold_hysteresis_is_rejected(tmp_path):
    result = execute(tmp_path, [pcm_frame(500)], speech_start_rms=100, silence_rms=120)

    assert result.success is False
    assert result.status == VAD_STATUS_INVALID_AUDIO
    assert result.error_message == "speech_start_rms_must_exceed_silence_rms"


def test_execute_before_start_is_rejected_and_lifecycle_is_idempotent(tmp_path):
    detector = RmsVoiceActivityCapture()
    result = detector.execute(request(tmp_path), SyntheticFrameSource([pcm_frame(500)]))

    assert result.status == VAD_STATUS_DEVICE_ERROR
    assert detector.start().success is True
    assert detector.start().success is True
    assert detector.health_check().status == "healthy"
    assert detector.stop().success is True
    assert detector.stop().success is True


def test_vad_hardware_logic_does_not_enter_brain_or_skill_boundaries():
    repo_root = Path(__file__).resolve().parent.parent
    boundaries = [
        repo_root / "core" / "IntentParser.py",
        repo_root / "core" / "Planner.py",
        repo_root / "skills" / "manager.py",
        repo_root / "skills" / "builtin" / "calculator.py",
    ]
    forbidden = ("VoiceActivityDetection", "RmsVoiceActivityCapture", "arecord", "pcm_frame_rms")

    for path in boundaries:
        source = path.read_text(encoding="utf-8")
        assert all(token not in source for token in forbidden)
