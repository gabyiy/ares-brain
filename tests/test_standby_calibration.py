from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import (
    LinuxStandbyWakeListener,
    RmsVoiceActivityCapture,
    VoiceActivityCaptureRequestV1,
    WakeListenerConfig,
)


SAMPLES_PER_FRAME = 320
FRAME_BYTES = SAMPLES_PER_FRAME * 2


def pcm_frame(amplitude: int) -> bytes:
    return int(amplitude).to_bytes(2, "little", signed=True) * SAMPLES_PER_FRAME


class FrameSource:
    def __init__(self, frames):
        self.frames = list(frames)
        self.clear_count = 0

    def read_frame(self, frame_bytes, timeout_seconds):
        assert frame_bytes == FRAME_BYTES
        if not self.frames:
            raise EOFError("frames_exhausted")
        return self.frames.pop(0)

    def clear_history(self):
        self.clear_count += 1

    def close(self):
        return None


def calibration_request(tmp_path, *, frame_count=20, **metadata_overrides):
    metadata = {
        "calibration_confirm_non_speech": True,
        "calibration_maximum_seconds": frame_count * 0.02,
        "calibration_quiet_sample_fraction": 0.25,
        "calibration_minimum_quiet_frame_fraction": 0.20,
        "calibration_maximum_speech_frame_fraction": 0.75,
        "calibration_maximum_noise_floor_rms": 600.0,
        "calibration_maximum_clipped_frame_fraction": 0.10,
        "calibration_bootstrap_speech_multiplier": 3.0,
        "calibration_bootstrap_speech_margin_rms": 180.0,
        "calibration_diagnostic_interval_frames": 5,
    }
    metadata.update(metadata_overrides)
    return VoiceActivityCaptureRequestV1(
        output_wav_path=str(tmp_path / "unused.wav"),
        calibration_duration_seconds=frame_count * 0.02,
        frame_duration_ms=20,
        metadata=metadata,
    )


def calibrate(tmp_path, frames, **metadata_overrides):
    detector = RmsVoiceActivityCapture()
    detector.start()
    source = FrameSource(frames)
    result = detector.calibrate_stream(
        calibration_request(
            tmp_path,
            frame_count=len(frames),
            **metadata_overrides,
        ),
        source,
    )
    return result, source


@pytest.mark.parametrize("ambient", [250, 420])
def test_constant_realistic_noise_calibrates_without_perfect_silence(tmp_path, ambient):
    result, _ = calibrate(tmp_path, [pcm_frame(ambient)] * 50)

    assert result.success
    assert result.ambient_statistics.noise_floor_rms == pytest.approx(float(ambient))
    assert result.diagnostics.quality_reason == "calibration_quality_passed"
    assert result.diagnostics.longest_non_speech_sequence == 50


def test_one_isolated_noisy_frame_does_not_reset_calibration(tmp_path):
    frames = [pcm_frame(260)] * 49
    frames.insert(17, pcm_frame(2400))

    result, _ = calibrate(tmp_path, frames)

    assert result.success
    assert result.frame_count == 50
    assert result.rejected_speech_frames == 1
    assert result.restart_count == 0
    assert result.ambient_statistics.noise_floor_rms == pytest.approx(260.0)


def test_intermittent_noise_with_quiet_samples_calibrates(tmp_path):
    frames = []
    for index in range(60):
        frames.append(pcm_frame(1400 if index % 5 == 0 else 280))

    result, _ = calibrate(tmp_path, frames)

    assert result.success
    assert result.diagnostics.speech_frame_count == 12
    assert result.diagnostics.quiet_sample_count == 15
    assert result.ambient_statistics.noise_floor_rms == pytest.approx(280.0)


def test_brief_speech_contamination_is_excluded_from_noise_floor(tmp_path):
    frames = [pcm_frame(220)] * 40 + [pcm_frame(1800)] * 8 + [pcm_frame(220)] * 12

    result, _ = calibrate(tmp_path, frames)

    assert result.success
    assert result.diagnostics.speech_frame_count == 8
    assert result.ambient_statistics.noise_floor_rms == pytest.approx(220.0)
    assert result.thresholds.speech_start_rms > result.thresholds.speech_continue_rms
    assert result.thresholds.speech_continue_rms > result.thresholds.silence_rms
    assert result.thresholds.silence_rms >= 120.0


def test_continuous_strong_speech_fails_calibration(tmp_path):
    result, _ = calibrate(tmp_path, [pcm_frame(2400)] * 50)

    assert not result.success
    assert result.error_code == "calibration_speech_dominated"
    assert result.diagnostics.speech_frame_count == 50


def test_all_zero_pcm_fails_calibration(tmp_path):
    result, _ = calibrate(tmp_path, [pcm_frame(0)] * 20)

    assert not result.success
    assert result.error_code == "calibration_all_zero_pcm"
    assert result.diagnostics.zero_frame_count == 20


def test_clipped_pcm_fails_calibration(tmp_path):
    result, _ = calibrate(tmp_path, [pcm_frame(32767)] * 20)

    assert not result.success
    assert result.error_code == "calibration_pcm_clipped"
    assert result.diagnostics.clipped_frame_fraction == 1.0


def test_missing_frames_fail_calibration(tmp_path):
    detector = RmsVoiceActivityCapture()
    detector.start()
    result = detector.calibrate_stream(
        calibration_request(tmp_path, frame_count=10),
        FrameSource([pcm_frame(200)] * 5),
    )

    assert not result.success
    assert result.error_code == "device_error"


def test_invalid_pcm_frame_format_fails_calibration(tmp_path):
    detector = RmsVoiceActivityCapture()
    detector.start()
    result = detector.calibrate_stream(
        calibration_request(tmp_path, frame_count=5),
        FrameSource([b"\x00\x01"]),
    )

    assert not result.success
    assert result.error_code == "invalid_audio"


def test_percentile_noise_floor_is_deterministic_and_ignores_outlier(tmp_path):
    amplitudes = [100, 110, 120, 130, 140, 150, 160, 170, 180, 5000]
    result, _ = calibrate(tmp_path, [pcm_frame(value) for value in amplitudes])

    assert result.success
    assert result.diagnostics.percentile_20_rms == pytest.approx(118.0)
    assert result.diagnostics.percentile_80_rms == pytest.approx(172.0)
    assert result.ambient_statistics.noise_floor_rms == pytest.approx(110.0)
    assert result.diagnostics.maximum_rms == pytest.approx(5000.0)


class RetryMicrophone:
    def __init__(self):
        self.handle = None
        self.calibration_calls = 0
        self.close_count = 0
        self.reset_count = 0

    def start(self):
        return SimpleNamespace(success=True, status="started")

    def health_check(self):
        return SimpleNamespace(success=True, status="healthy")

    def stop(self):
        return SimpleNamespace(success=True, status="stopped")

    def open_persistent_stream(self, *, owner, device=None):
        source = FrameSource([])
        self.handle = SimpleNamespace(
            owner=owner,
            closed=False,
            stream_id="retry-stream",
            alsa_handle_id="retry-handle",
            frame_source=source,
        )
        return self.handle

    def calibrate_persistent_stream(self, handle, request, **kwargs):
        self.calibration_calls += 1
        if self.calibration_calls == 1:
            return SimpleNamespace(
                success=False,
                status="calibration_speech_dominated",
                error_code="calibration_speech_dominated",
                error_message="speech contaminated calibration",
                frame_count=150,
                diagnostics=None,
            )
        return SimpleNamespace(
            success=True,
            status="calibrated",
            error_code="",
            error_message="",
            frame_count=150,
            diagnostics=None,
            thresholds=SimpleNamespace(
                speech_start_rms=700.0,
                speech_continue_rms=500.0,
                silence_rms=300.0,
                to_dict=lambda: {
                    "speech_start_rms": 700.0,
                    "speech_continue_rms": 500.0,
                    "silence_rms": 300.0,
                },
            ),
            ambient_statistics=None,
        )

    def reset_persistent_candidate(self, handle, **kwargs):
        self.reset_count += 1
        return {"stale_pcm_frames_discarded": 0}

    def record_persistent_until_silence(self, handle, output_path, **kwargs):
        raise AssertionError("wake capture must not run during calibration")

    def close_persistent_stream(self, handle, *, owner):
        handle.closed = True
        self.close_count += 1
        return SimpleNamespace(success=True, status="closed")

    def cancel_current(self):
        return None


class CalibrationOnlyRecognizer:
    recognizer_name = "calibration-only"

    def __init__(self):
        self.recognize_calls = 0

    def start(self):
        return SimpleNamespace(success=True, status="started")

    def health_check(self):
        return SimpleNamespace(success=True, status="healthy")

    def stop(self):
        return SimpleNamespace(success=True, status="stopped")

    def cancel(self):
        return SimpleNamespace(success=True, status="cancelled")

    def reset_attempt_state(self, reason="", **_kwargs):
        return None

    def recognize_wav(self, request):
        self.recognize_calls += 1
        raise AssertionError("Vosk must not run during standby calibration")


def test_listener_retries_once_after_speech_contamination_without_vosk(tmp_path):
    microphone = RetryMicrophone()
    recognizer = CalibrationOnlyRecognizer()
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=recognizer,
        config=WakeListenerConfig(
            calibration_retry_count=1,
            calibration_retry_delay_seconds=0,
        ),
        project_root=tmp_path,
        sleeper=lambda _seconds: None,
    )

    started = listener.start()
    health = listener.component_health()

    assert started.success
    assert microphone.calibration_calls == 2
    assert microphone.reset_count == 1
    assert microphone.close_count == 0
    assert recognizer.recognize_calls == 0
    assert health.data["calibration_attempt_count"] == 2
    assert health.data["stream_state"] == "HEALTHY"
