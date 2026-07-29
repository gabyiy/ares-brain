from __future__ import annotations

from pathlib import Path
import wave

import pytest

from core import (
    LinuxAlsaMicrophoneAdapter,
    RmsVoiceActivityCapture,
    RollingPcmFrameSource,
    SafeProcessResult,
    VAD_STATUS_COMPLETED_AFTER_SILENCE,
    VAD_STATUS_DEVICE_ERROR,
    VAD_STATUS_NO_SPEECH_TIMEOUT,
    VAD_STATUS_TIMEOUT,
    VoiceActivityCaptureRequestV1,
)


DEVICE_LIST = """**** List of CAPTURE Hardware Devices ****
card 2: Device [USB Audio Device], device 0: USB Audio [USB Audio]
"""
SAMPLES_PER_FRAME = 320


def frame(amplitude):
    return int(amplitude).to_bytes(2, "little", signed=True) * SAMPLES_PER_FRAME


class DeviceRunner:
    def which(self, executable):
        return "/usr/bin/arecord"

    def run(self, args, timeout_seconds):
        return SafeProcessResult(args=list(args), returncode=0, stdout=DEVICE_LIST)


class FrameSource:
    def __init__(self, frames):
        self.frames = list(frames)
        self.read_count = 0
        self.closed = False
        self.stderr = ""

    def read_frame(self, frame_bytes, timeout_seconds):
        self.read_count += 1
        if not self.frames:
            raise EOFError("done")
        return self.frames.pop(0)

    def close(self):
        self.closed = True


class BufferedFrameSource(FrameSource):
    def __init__(self, frames, *, buffered_bytes=0):
        super().__init__(frames)
        self.buffered_bytes = buffered_bytes
        self.discard_calls = 0

    def discard_available(self, maximum_bytes):
        self.discard_calls += 1
        discarded = min(self.buffered_bytes, maximum_bytes)
        self.buffered_bytes -= discarded
        return discarded


class FailureAfterFramesSource(FrameSource):
    def __init__(self, frames, failure):
        super().__init__(frames)
        self.failure = failure

    def read_frame(self, frame_bytes, timeout_seconds):
        if self.frames:
            return super().read_frame(frame_bytes, timeout_seconds)
        raise self.failure


class RetryCloseFrameSource(FrameSource):
    def __init__(self, frames):
        super().__init__(frames)
        self.close_attempts = 0

    def close(self):
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise RuntimeError("injected close failure")
        super().close()


class StreamRunner:
    def __init__(self, source=None, failure=None):
        self.source = source
        self.failure = failure
        self.calls = []

    def start(self, args):
        self.calls.append(list(args))
        if self.failure:
            raise self.failure
        return self.source


class PreflightFrameSource:
    def __init__(self, events, *, payload=None, failure=None, stderr=""):
        self.events = events
        self.payload = frame(0) if payload is None else payload
        self.failure = failure
        self.stderr = stderr
        self.closed = False

    def read_frame(self, frame_bytes, timeout_seconds):
        self.events.append(("read", frame_bytes, timeout_seconds))
        if self.failure is not None:
            raise self.failure
        return self.payload

    def close(self):
        self.events.append(("close",))
        self.closed = True

    def snapshot(self):
        return {
            "transport_argv": ["/usr/bin/arecord", "-t", "raw", "-"],
            "stdout_transport_mode": "raw_pcm_pipe_continuous_pump",
            "process_pid": 2468,
            "process_exit_status": 1 if self.failure is not None else 0,
            "process_liveness_observable": True,
            "stderr_preview": self.stderr,
            "stream_ended": self.failure is not None,
            "terminal_reason": (
                "arecord_process_exited" if self.failure is not None else ""
            ),
            "closed": self.closed,
        }


class PreflightStreamRunner:
    def __init__(self, source, events):
        self.source = source
        self.events = events
        self.calls = []

    def start(self, args):
        self.calls.append(list(args))
        self.events.append(("open", list(args)))
        return self.source


class StructuredStopFrameSource(FrameSource):
    """Frame source exposing the production controlled-stop result contract."""

    def __init__(self, frames, *, unexpected_failure=False):
        super().__init__(frames)
        self.unexpected_failure = bool(unexpected_failure)

    def snapshot(self):
        status = "unexpected_failure" if self.unexpected_failure else "controlled_stop"
        return {
            "process_pid": 2468,
            "process_exit_status": 1 if self.closed else None,
            "closed": self.closed,
            "controlled_stop": (
                {
                    "stop_requested": True,
                    "valid_pcm_received": self.read_count > 0,
                    "valid_full_pcm_frames": self.read_count,
                    "child_exit_code": 1,
                    "child_signal": None,
                    "stderr": (
                        "arecord: pcm_read:2272: read error: Input/output error"
                        if self.unexpected_failure
                        else "arecord: pcm_read:2272: read error: Interrupted system call"
                    ),
                    "process_reaped": True,
                    "cleanup_completed": True,
                    "unexpected_failure": self.unexpected_failure,
                    "status": status,
                    "final_health_effect": (
                        "unhealthy" if self.unexpected_failure else "none"
                    ),
                }
                if self.closed
                else {}
            ),
        }


def test_persistent_stream_opens_once_across_rejected_and_accepted_candidates(tmp_path):
    source = FrameSource(
        [
            *([frame(20)] * 5),
            frame(400),
            frame(450),
            *([frame(20)] * 5),
        ]
    )
    stream_runner = StreamRunner(source)
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=stream_runner,
    )
    assert adapter.start().success
    handle = adapter.open_persistent_stream(owner="standby_wake_listener")

    silent = adapter.record_persistent_until_silence(
        handle,
        tmp_path / "silent.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.0,
    )
    speech = adapter.record_persistent_until_silence(
        handle,
        tmp_path / "speech.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.0,
    )

    assert silent.status == VAD_STATUS_NO_SPEECH_TIMEOUT
    assert speech.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert len(stream_runner.calls) == 1
    assert adapter.persistent_stream_snapshot()["open_count"] == 1
    assert adapter.persistent_stream_snapshot()["stream_id"] == "alsa-pcm-stream-1"
    assert adapter.persistent_stream_snapshot()["alsa_handle_id"] == (
        "alsa-pcm-stream-1-handle"
    )
    assert source.closed is False
    assert adapter.close_persistent_stream(
        handle,
        owner="standby_wake_listener",
    ).success
    assert source.closed is True
    assert adapter.persistent_stream_snapshot()["close_count"] == 1


def test_failed_persistent_close_retains_ownership_for_cleanup_retry():
    source = RetryCloseFrameSource([])
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success
    handle = adapter.open_persistent_stream(owner="standby_wake_listener")

    first = adapter.close_persistent_stream(
        handle,
        owner="standby_wake_listener",
    )

    assert first.success is False
    assert first.status == "stream_close_failed"
    assert handle.closed is False
    assert adapter.persistent_stream_snapshot()["active"] is True
    assert adapter.persistent_stream_snapshot()["close_count"] == 0

    second = adapter.close_persistent_stream(
        handle,
        owner="standby_wake_listener",
    )

    assert second.success is True
    assert source.close_attempts == 2
    assert source.closed is True
    assert handle.closed is True
    assert adapter.persistent_stream_snapshot()["active"] is False
    assert adapter.persistent_stream_snapshot()["close_count"] == 1


def test_persistent_close_reports_structured_unhealthy_stop_after_releasing_owner():
    source = StructuredStopFrameSource(
        [frame(400)],
        unexpected_failure=True,
    )
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success is True
    handle = adapter.open_persistent_stream(owner="standby_wake_listener")
    assert handle.frame_source.read_frame(640, 1.0) == frame(400)

    result = adapter.close_persistent_stream(
        handle,
        owner="standby_wake_listener",
    )

    assert result.success is False
    assert result.status == "stream_close_unhealthy"
    assert result.data["controlled_stop"]["unexpected_failure"] is True
    assert result.data["microphone_ownership_released"] is True
    assert adapter.persistent_stream_snapshot()["active"] is False
    assert adapter.persistent_stream_snapshot()["close_count"] == 1


def test_failed_adapter_stop_reports_lock_risk_and_remains_retryable():
    source = RetryCloseFrameSource([])
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success
    adapter.open_persistent_stream(owner="standby_wake_listener")

    first = adapter.stop()

    assert first.success is False
    assert first.status == "stop_failed"
    assert adapter.started is True
    assert adapter.persistent_stream_snapshot()["active"] is True

    second = adapter.stop()

    assert second.success is True
    assert source.close_attempts == 2
    assert source.closed is True
    assert adapter.started is False
    assert adapter.persistent_stream_snapshot()["active"] is False


def test_persistent_stream_calibrates_without_reopening_or_closing_source(tmp_path):
    source = FrameSource([*([frame(40)] * 5), *([frame(20)] * 5)])
    stream_runner = StreamRunner(source)
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=stream_runner,
    )
    assert adapter.start().success
    handle = adapter.open_persistent_stream(owner="standby_wake_listener")
    calibration = adapter.calibrate_persistent_stream(
        handle,
        VoiceActivityCaptureRequestV1(
            output_wav_path=str(tmp_path / "unused.wav"),
            microphone_device="hw:2,0",
            calibration_duration_seconds=0.1,
            frame_duration_ms=20,
        ),
    )
    assert calibration.success
    assert calibration.frame_count == 5
    assert calibration.thresholds.speech_start_rms > calibration.thresholds.speech_continue_rms
    assert len(stream_runner.calls) == 1
    assert source.closed is False
    adapter.close_persistent_stream(handle, owner="standby_wake_listener")


def test_post_calibration_frames_continue_through_same_persistent_stream(tmp_path):
    source = FrameSource([*([frame(400)] * 5), *([frame(410)] * 5)])
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success
    handle = adapter.open_persistent_stream(owner="standby_wake_listener")
    calibration = adapter.calibrate_persistent_stream(
        handle,
        VoiceActivityCaptureRequestV1(
            output_wav_path=str(tmp_path / "unused.wav"),
            microphone_device="hw:2,0",
            calibration_duration_seconds=0.1,
            frame_duration_ms=20,
            metadata={
                "calibration_confirm_non_speech": True,
                "calibration_maximum_seconds": 0.1,
                "calibration_quiet_sample_fraction": 0.25,
                "calibration_minimum_quiet_frame_fraction": 0.2,
                "calibration_maximum_speech_frame_fraction": 0.75,
                "calibration_maximum_noise_floor_rms": 600,
                "calibration_maximum_clipped_frame_fraction": 0.1,
                "calibration_bootstrap_speech_multiplier": 3,
                "calibration_bootstrap_speech_margin_rms": 180,
                "vad_profile": "standby_wake_short_v1",
                "wake_vad_sensitivity": "normal",
            },
        ),
    )
    assert calibration.success
    assert calibration.thresholds.speech_start_rms == pytest.approx(472.0)
    handle.frame_source.clear_history()
    after_calibration = adapter.persistent_stream_snapshot()
    assert after_calibration["rolling_pre_roll"]["live_frame_count"] == 5
    assert after_calibration["rolling_pre_roll"]["read_sequence"] == 5

    result = adapter.record_persistent_until_silence(
        handle,
        tmp_path / "post-calibration.wav",
        calibration_enabled=False,
        speech_start_rms=calibration.thresholds.speech_start_rms,
        speech_continue_rms=calibration.thresholds.speech_continue_rms,
        silence_rms=calibration.thresholds.silence_rms,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0,
        capture_profile="standby_wake_short_v1",
        minimum_speech_duration_seconds=0.08,
        frame_debug_enabled=True,
        diagnostic_rms_interval_frames=1,
    )

    assert result.status == VAD_STATUS_NO_SPEECH_TIMEOUT
    assert result.data["source_read_sequence_start"] == 5
    assert result.data["source_read_sequence_end"] == 10
    assert result.data["source_live_frame_sequence_start"] == 5
    assert result.data["source_live_frame_sequence_end"] == 10
    assert result.data["source_live_frames_read_delta"] == 5
    assert result.data["source_live_bytes_read_delta"] == 5 * 640
    assert result.data["capture_failure_stage"] == "speech_threshold_not_crossed"
    assert len(result.data["frame_trace"]) == 5
    assert adapter.persistent_stream_snapshot()["open_count"] == 1
    assert source.closed is False
    adapter.close_persistent_stream(handle, owner="standby_wake_listener")


def test_persistent_stream_rejects_second_capture_owner():
    source = FrameSource([frame(20)] * 5)
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success
    handle = adapter.open_persistent_stream(owner="standby_wake_listener")
    with pytest.raises(RuntimeError, match="microphone_capture_already_owned"):
        adapter.open_persistent_stream(owner="active_command")
    snapshot = adapter.persistent_stream_snapshot()
    assert snapshot["owner"] == "standby_wake_listener"
    assert snapshot["open_count"] == 1
    adapter.close_persistent_stream(handle, owner="standby_wake_listener")


def test_rolling_pre_roll_recovers_speech_split_across_poll_boundary(tmp_path):
    source = FrameSource(
        [
            frame(20),
            frame(20),
            frame(20),
            frame(20),
            frame(500),
            frame(550),
            *([frame(20)] * 5),
        ]
    )
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success
    handle = adapter.open_persistent_stream(owner="standby_wake_listener")
    first = adapter.record_persistent_until_silence(
        handle,
        tmp_path / "boundary-one.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.4,
    )
    second = adapter.record_persistent_until_silence(
        handle,
        tmp_path / "boundary-two.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.2,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.4,
    )
    assert first.status == VAD_STATUS_NO_SPEECH_TIMEOUT
    assert second.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert second.pre_roll_frames_retained >= 4
    rolling = adapter.persistent_stream_snapshot()["rolling_pre_roll"]
    assert rolling["replayed_frame_count"] >= 4
    adapter.close_persistent_stream(handle, owner="standby_wake_listener")


def test_persistent_candidate_reset_clears_history_and_bounded_buffer_without_reopen():
    source = BufferedFrameSource([frame(20)] * 10, buffered_bytes=3 * 640)
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success
    handle = adapter.open_persistent_stream(owner="standby_wake_listener")
    handle.frame_source.read_frame(640, 1.0)
    assert adapter.persistent_stream_snapshot()["rolling_pre_roll"]["history_frame_count"] == 1
    reset = adapter.reset_persistent_candidate(
        handle,
        frame_duration_ms=20,
        maximum_discard_seconds=0.2,
    )
    rolling = adapter.persistent_stream_snapshot()["rolling_pre_roll"]
    assert reset["stale_pcm_frames_discarded"] == 3
    assert reset["stream_remained_open"] is True
    assert rolling["history_frame_count"] == 0
    assert rolling["candidate_reset_count"] == 1
    assert source.discard_calls == 1
    assert adapter.persistent_stream_snapshot()["open_count"] == 1
    adapter.close_persistent_stream(handle, owner="standby_wake_listener")


def test_candidate_reset_prevents_stale_wake_pcm_from_replaying_next_candidate(tmp_path):
    source = FrameSource(
        [
            frame(500),
            frame(550),
            *([frame(20)] * 5),
            *([frame(20)] * 3),
            frame(600),
            frame(650),
            *([frame(20)] * 5),
        ]
    )
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success
    handle = adapter.open_persistent_stream(owner="standby_wake_listener")
    common = {
        "calibration_enabled": False,
        "required_speech_frames": 2,
        "silence_seconds": 0.1,
        "speech_wait_timeout_seconds": 0.2,
        "maximum_utterance_seconds": 0.3,
        "pre_roll_seconds": 0.1,
        "capture_profile": "standby_wake_short_v1",
        "minimum_speech_duration_seconds": 0.04,
    }
    first = adapter.record_persistent_until_silence(
        handle,
        tmp_path / "first.wav",
        **common,
    )
    assert first.success
    adapter.reset_persistent_candidate(handle, maximum_discard_seconds=0)
    second = adapter.record_persistent_until_silence(
        handle,
        tmp_path / "second.wav",
        **common,
    )
    assert second.success
    assert second.data["duplicate_frame_append_count"] == 0
    assert second.data["captured_frame_indexes_unique"] is True
    assert second.pre_roll_frames_retained <= 3
    adapter.close_persistent_stream(handle, owner="standby_wake_listener")


def test_linux_alsa_auto_stop_streams_raw_pcm_with_argument_list(tmp_path):
    source = FrameSource([frame(400), frame(450), *([frame(20)] * 5)])
    stream_runner = StreamRunner(source)
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=stream_runner,
    )

    assert adapter.start().success is True
    result = adapter.record_until_silence(
        tmp_path / "vad.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.0,
    )

    assert result.success is True
    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert source.closed is True
    command = stream_runner.calls[0]
    assert command == [
        "/usr/bin/arecord", "-q", "-f", "S16_LE", "-c", "1",
        "-r", "16000", "-t", "raw", "-D", "plughw:2,0", "-",
    ]
    assert result.requested_device == "hw:2,0"
    assert result.resolved_capture_device == "plughw:2,0"
    assert result.final_whisper_input_path != str(tmp_path / "vad.wav")
    assert Path(result.final_whisper_input_path).exists()
    assert Path(result.final_whisper_input_path).name == "normalized_whisper_input.wav"
    assert result.duration_invariant_status == "duration_consistent"
    assert result.data["process"]["shell"] is False
    assert result.metadata["subprocess_shell"] is False


def test_auto_stop_accepts_structured_controlled_interrupted_read_cleanup(tmp_path):
    source = StructuredStopFrameSource(
        [frame(400), frame(450), *([frame(20)] * 5)]
    )
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success is True

    result = adapter.record_until_silence(
        tmp_path / "controlled-stop.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.0,
    )

    assert result.success is True
    assert result.data["pcm_stream_cleanup"]["controlled_stop"]["status"] == (
        "controlled_stop"
    )
    assert adapter.health_check().success is True


def test_auto_stop_rejects_unrelated_exit_one_even_after_valid_candidate(tmp_path):
    source = StructuredStopFrameSource(
        [frame(400), frame(450), *([frame(20)] * 5)],
        unexpected_failure=True,
    )
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success is True

    result = adapter.record_until_silence(
        tmp_path / "unexpected-stop.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.0,
    )

    assert result.success is False
    assert result.status == VAD_STATUS_DEVICE_ERROR
    assert result.error_message == "pcm_stream_stop_unhealthy:unexpected_failure"
    controlled = result.data["pcm_stream_cleanup"]["controlled_stop"]
    assert controlled["unexpected_failure"] is True
    assert controlled["final_health_effect"] == "unhealthy"


def test_linux_alsa_ready_boundary_waits_for_live_stream_and_calibration(tmp_path):
    source = FrameSource([frame(40)] * 5)
    stream_runner = StreamRunner(source)
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=stream_runner,
    )
    ready_events = []

    def ready(details):
        ready_events.append(
            {
                **dict(details),
                "read_count": source.read_count,
                "stream_open_count": len(stream_runner.calls),
            }
        )
        source.frames.extend(
            [
                *([frame(50)] * 25),
                frame(400),
                frame(500),
                *([frame(20)] * 5),
            ]
        )

    assert adapter.start().success is True
    result = adapter.record_until_silence(
        tmp_path / "active-ready.wav",
        calibration_enabled=True,
        calibration_duration_seconds=0.1,
        required_speech_frames=2,
        required_continue_frames=1,
        required_silence_frames=1,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=1.0,
        maximum_utterance_seconds=1.0,
        pre_roll_seconds=0.5,
        capture_profile="active_command_v1",
        capture_ready_callback=ready,
    )

    assert result.success is True
    assert len(ready_events) == 1
    assert ready_events[0]["read_count"] == 5
    assert ready_events[0]["stream_open_count"] == 1
    assert ready_events[0]["capture_start_reason"] == (
        "calibration_completed_stream_ready"
    )
    assert result.pre_roll_frames_retained == 25
    assert result.data["beginning_clipped"] == "no"
    assert result.actual_sample_rate_hz == 16000
    assert result.actual_channels == 1
    assert result.actual_sample_width_bytes == 2
    assert source.closed is True


def test_linux_alsa_auto_stop_diagnostics_keep_distinct_raw_and_trimmed_wavs(tmp_path):
    source = FrameSource([frame(400), frame(450), *([frame(20)] * 5)])
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success is True

    result = adapter.record_until_silence(
        tmp_path / "vad.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.0,
        diagnostic_audio=True,
    )

    assert result.success is True
    assert result.raw_wav_path
    assert result.assembled_wav_path
    assert len(
        {result.raw_wav_path, result.assembled_wav_path, result.normalized_wav_path}
    ) == 3
    assert all(
        Path(path).exists()
        for path in (
            result.raw_wav_path,
            result.assembled_wav_path,
            result.normalized_wav_path,
        )
    )
    assert result.final_whisper_input_path == result.normalized_wav_path
    assert result.actual_sample_rate_hz == 16000
    assert result.normalized_sample_rate_hz == 16000
    assert result.assembled_duration_seconds == result.normalized_duration_seconds
    assert result.normalized_sample_count == result.final_assembled_sample_count


def test_auto_stop_uses_unique_current_turn_paths(tmp_path):
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(
            FrameSource([frame(400), frame(450), *([frame(20)] * 5)])
        ),
    )
    adapter.start()
    first = adapter.record_until_silence(
        tmp_path / "vad.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.0,
        diagnostic_audio=True,
    )
    adapter.stream_runner = StreamRunner(
        FrameSource([frame(500), frame(550), *([frame(20)] * 5)])
    )
    second = adapter.record_until_silence(
        tmp_path / "vad.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.0,
        diagnostic_audio=True,
    )

    assert first.success is True
    assert second.success is True
    assert first.final_whisper_input_path != second.final_whisper_input_path
    assert Path(first.final_whisper_input_path).exists()
    assert Path(second.final_whisper_input_path).exists()


def test_auto_stop_stream_requests_canonical_format_even_if_adapter_defaults_differ(tmp_path):
    source = FrameSource([frame(400), frame(450), *([frame(20)] * 5)])
    stream_runner = StreamRunner(source)
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        sample_rate_hz=44100,
        channels=2,
        runner=DeviceRunner(),
        stream_runner=stream_runner,
    )
    assert adapter.start().success is True

    result = adapter.record_until_silence(
        tmp_path / "canonical.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
        pre_roll_seconds=0.0,
    )

    command = stream_runner.calls[0]
    assert result.success is True
    assert command[command.index("-r") + 1] == "16000"
    assert command[command.index("-c") + 1] == "1"
    assert command[command.index("-f") + 1] == "S16_LE"
    assert result.requested_sample_rate_hz == 16000


def test_linux_alsa_auto_stop_no_speech_returns_structured_failure(tmp_path):
    source = FrameSource([frame(20)] * 5)
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    adapter.start()

    result = adapter.record_until_silence(
        tmp_path / "silent.wav",
        calibration_enabled=False,
        required_speech_frames=2,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.2,
    )

    assert result.success is False
    assert result.status == VAD_STATUS_NO_SPEECH_TIMEOUT
    assert result.wav_path == ""
    assert source.closed is True


def test_linux_alsa_auto_stop_requires_adapter_lifecycle_start(tmp_path):
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(FrameSource([])),
    )

    result = adapter.record_until_silence(tmp_path / "not_started.wav")

    assert result.success is False
    assert result.status == VAD_STATUS_DEVICE_ERROR
    assert result.error_message == "microphone_not_started"


def test_linux_alsa_auto_stop_stream_start_failure_is_safe(tmp_path):
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(failure=OSError("device busy")),
    )
    adapter.start()

    result = adapter.record_until_silence(tmp_path / "failed.wav")

    assert result.success is False
    assert result.status == VAD_STATUS_DEVICE_ERROR
    assert "device busy" in result.error_message
    assert result.data["pcm_exception"]["exception_class"] == "OSError"
    assert result.data["pcm_exception"]["exception_message"] == "device busy"
    assert result.data["pcm_exception"]["failing_method"] == (
        "SafePcmStreamRunner.start"
    )
    assert result.data["pcm_stream_cleanup"]["status"] == "not_started"
    assert "traceback" not in result.data["pcm_exception"]


def test_stream_start_failure_retains_traceback_only_for_diagnostic_capture(tmp_path):
    adapter = LinuxAlsaMicrophoneAdapter(
        device="hw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(failure=OSError("device busy exact")),
    )
    adapter.start()

    result = adapter.record_until_silence(
        tmp_path / "failed-diagnostic.wav",
        diagnostic_exception_traceback=True,
    )

    assert result.error_message == "device busy exact"
    failure = result.data["pcm_exception"]
    assert failure["exception_message"] == "device busy exact"
    assert "OSError: device busy exact" in failure["traceback"]


def test_pcm_preflight_uses_production_command_and_opens_before_bounded_read():
    events = []
    source = PreflightFrameSource(events, payload=frame(0))
    stream_runner = PreflightStreamRunner(source, events)
    adapter = LinuxAlsaMicrophoneAdapter(
        device="plughw:2,0",
        runner=DeviceRunner(),
        stream_runner=stream_runner,
    )
    assert adapter.start().success is True

    result = adapter.preflight_pcm_stream(
        frame_read_timeout_seconds=0.25,
        owner="diagnostic_active_capture",
    )

    assert result.success is True
    assert result.status == "pcm_preflight_passed"
    assert [event[0] for event in events] == ["open", "read", "close"]
    command = stream_runner.calls[0]
    assert command[command.index("-D") + 1] == "plughw:2,0"
    assert command[command.index("-r") + 1] == "16000"
    assert command[command.index("-c") + 1] == "1"
    assert command[command.index("-f") + 1] == "S16_LE"
    assert command[command.index("-t") + 1] == "raw"
    assert command[-1] == "-"
    assert result.data["open_success"] is True
    assert result.data["first_pcm_read_success"] is True
    assert result.data["first_frame_byte_count"] == 640
    assert result.data["expected_frame_byte_count"] == 640
    assert result.data["first_frame_sample_count"] == 320
    assert result.data["first_frame_nonzero"] is False
    assert result.data["first_frame_rms"] == 0.0
    assert result.data["alsa_child_process_id"] == 2468
    assert result.data["cleanup_result"] == "completed"
    assert result.data["microphone_ownership_released"] is True
    assert adapter._active_stream is None
    assert adapter._active_stream_owner == ""


def test_pcm_preflight_accepts_controlled_arecord_exit_one_after_valid_frame():
    class ControlledInterruptedSource(PreflightFrameSource):
        def snapshot(self):
            value = super().snapshot()
            if not self.closed:
                value["process_exit_status"] = None
            else:
                value.update(
                    {
                        "process_exit_status": 1,
                        "stderr_preview": (
                            "arecord: pcm_read:2272: read error: "
                            "Interrupted system call"
                        ),
                        "controlled_stop": {
                            "stop_requested": True,
                            "valid_pcm_received": True,
                            "valid_full_pcm_frames": 1,
                            "child_exit_code": 1,
                            "child_signal": None,
                            "termination_signal_requested": "SIGTERM",
                            "termination_escalated": False,
                            "stderr": (
                                "arecord: pcm_read:2272: read error: "
                                "Interrupted system call"
                            ),
                            "process_reaped": True,
                            "cleanup_completed": True,
                            "active_failure_before_stop": False,
                            "unexpected_ownership_loss": False,
                            "unexpected_failure": False,
                            "status": "controlled_stop",
                            "final_health_effect": "none",
                            "cleanup_errors": [],
                        },
                    }
                )
            return value

    events = []
    source = ControlledInterruptedSource(events, payload=frame(175))
    adapter = LinuxAlsaMicrophoneAdapter(
        device="plughw:2,0",
        runner=DeviceRunner(),
        stream_runner=PreflightStreamRunner(source, events),
    )
    assert adapter.start().success is True

    result = adapter.preflight_pcm_stream(owner="diagnostic_active_capture")

    assert result.success is True
    assert result.status == "pcm_preflight_passed"
    assert result.data["alsa_process_exit_status"] == 1
    assert "Interrupted system call" in result.data["alsa_stderr"]
    controlled = result.data["controlled_stop"]
    assert controlled["status"] == "controlled_stop"
    assert controlled["final_health_effect"] == "none"
    assert controlled["unexpected_failure"] is False
    assert result.data["microphone_ownership_released"] is True
    health = adapter.health_check()
    assert health.success is True
    assert health.data["previous_controlled_stop_result"] == controlled


def test_pcm_preflight_is_reusable_after_controlled_stop():
    class FreshControlledSource(PreflightFrameSource):
        def snapshot(self):
            value = super().snapshot()
            value["process_exit_status"] = -15 if self.closed else None
            if self.closed:
                value["controlled_stop"] = {
                    "stop_requested": True,
                    "valid_pcm_received": True,
                    "valid_full_pcm_frames": 1,
                    "child_exit_code": -15,
                    "child_signal": 15,
                    "termination_signal_requested": "SIGTERM",
                    "termination_escalated": False,
                    "stderr": "",
                    "process_reaped": True,
                    "cleanup_completed": True,
                    "active_failure_before_stop": False,
                    "unexpected_ownership_loss": False,
                    "unexpected_failure": False,
                    "status": "controlled_stop",
                    "final_health_effect": "none",
                    "cleanup_errors": [],
                }
            return value

    class FreshStreamRunner:
        def __init__(self):
            self.events = []
            self.sources = []

        def start(self, args):
            self.events.append(("open", list(args)))
            source = FreshControlledSource(self.events, payload=frame(225))
            self.sources.append(source)
            return source

    stream_runner = FreshStreamRunner()
    adapter = LinuxAlsaMicrophoneAdapter(
        device="plughw:2,0",
        runner=DeviceRunner(),
        stream_runner=stream_runner,
    )
    assert adapter.start().success is True

    first = adapter.preflight_pcm_stream(owner="diagnostic_active_capture")
    second = adapter.preflight_pcm_stream(owner="diagnostic_active_capture")

    assert first.success is True
    assert second.success is True
    assert len(stream_runner.sources) == 2
    assert stream_runner.sources[0] is not stream_runner.sources[1]
    assert all(source.closed for source in stream_runner.sources)
    assert adapter._active_stream is None
    assert adapter._active_stream_owner == ""


def test_pcm_preflight_preserves_arecord_error_stderr_and_releases_ownership():
    events = []
    source = PreflightFrameSource(
        events,
        failure=RuntimeError("arecord_process_exited:1"),
        stderr="arecord: main:831: audio open error: Device or resource busy",
    )
    adapter = LinuxAlsaMicrophoneAdapter(
        device="plughw:2,0",
        runner=DeviceRunner(),
        stream_runner=PreflightStreamRunner(source, events),
    )
    assert adapter.start().success is True

    result = adapter.preflight_pcm_stream()

    assert result.success is False
    assert result.status == "microphone_open_error"
    assert result.error_message == "RuntimeError:arecord_process_exited:1"
    assert result.data["exception_class"] == "RuntimeError"
    assert result.data["exception_message"] == "arecord_process_exited:1"
    assert result.data["failing_method"] == "read_frame"
    assert result.data["alsa_stderr"].endswith("Device or resource busy")
    assert result.data["close_success"] is True
    assert result.data["microphone_ownership_released"] is True
    assert "traceback" not in result.data
    assert source.closed is True
    assert adapter._active_stream is None


def test_pcm_preflight_traceback_requires_explicit_diagnostic_flag():
    events = []
    source = PreflightFrameSource(
        events,
        failure=RuntimeError("arecord_stdout_closed_while_process_alive"),
    )
    adapter = LinuxAlsaMicrophoneAdapter(
        device="plughw:2,0",
        runner=DeviceRunner(),
        stream_runner=PreflightStreamRunner(source, events),
    )
    assert adapter.start().success is True

    result = adapter.preflight_pcm_stream(diagnostic_traceback=True)

    assert result.success is False
    assert "RuntimeError: arecord_stdout_closed_while_process_alive" in result.data[
        "traceback"
    ]
    assert result.data["microphone_ownership_released"] is True


def test_pcm_preflight_reports_invalid_runner_source_without_losing_cleanup_state():
    class InvalidSource:
        pass

    adapter = LinuxAlsaMicrophoneAdapter(
        device="plughw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(InvalidSource()),
    )
    assert adapter.start().success is True

    result = adapter.preflight_pcm_stream(diagnostic_traceback=True)

    assert result.success is False
    assert result.status == "microphone_open_error"
    assert result.data["exception_class"] == "TypeError"
    assert result.data["exception_message"] == (
        "PCM stream runner returned an invalid frame source"
    )
    assert result.data["close_called"] is True
    assert result.data["close_success"] is False
    assert result.data["cleanup_result"] == "incomplete"
    assert result.data["microphone_ownership_released"] is True


def test_pcm_preflight_retries_bounded_close_and_releases_ownership():
    source = RetryCloseFrameSource([frame(0)])
    adapter = LinuxAlsaMicrophoneAdapter(
        device="plughw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success is True

    result = adapter.preflight_pcm_stream()

    assert result.success is True
    assert source.close_attempts == 2
    assert source.closed is True
    assert result.data["close_attempts"] == 2
    assert result.data["cleanup_result"] == "completed"
    assert result.data["microphone_ownership_released"] is True
    assert adapter._active_stream is None


def test_auto_stop_failure_merges_post_close_stderr_snapshot_and_cleanup(tmp_path):
    events = []
    source = PreflightFrameSource(
        events,
        failure=RuntimeError("arecord_process_exited:1"),
        stderr="arecord: audio open error: Device or resource busy",
    )
    adapter = LinuxAlsaMicrophoneAdapter(
        device="plughw:2,0",
        runner=DeviceRunner(),
        stream_runner=PreflightStreamRunner(source, events),
    )
    assert adapter.start().success is True

    result = adapter.record_until_silence(
        tmp_path / "failure.wav",
        calibration_enabled=False,
        diagnostic_exception_traceback=True,
    )

    assert result.success is False
    assert result.error_message == (
        "pcm_stream_error:RuntimeError:arecord_process_exited:1"
    )
    assert result.data["pcm_stream_cleanup"]["status"] == "completed"
    assert result.data["pcm_stream_cleanup"]["attempts"] == 1
    assert result.data["pcm_source_snapshot_after_close"]["closed"] is True
    assert result.data["process"]["pid"] == 2468
    assert result.data["process"]["returncode"] == 1
    assert result.data["process"]["stderr"].endswith("Device or resource busy")
    failure = result.data["pcm_exception"]
    assert failure["exception_message"] == "arecord_process_exited:1"
    assert failure["cleanup_result"] == "completed"
    assert failure["source_snapshot_after_close"]["closed"] is True
    assert failure["alsa_stderr"].endswith("Device or resource busy")
    assert "RuntimeError: arecord_process_exited:1" in failure["traceback"]
    assert adapter._active_stream is None
    assert adapter._active_stream_owner == ""


def test_linux_alsa_capabilities_advertise_activity_capture():
    adapter = LinuxAlsaMicrophoneAdapter(runner=DeviceRunner())

    capabilities = adapter.get_capabilities()

    assert "arecord_pcm_rms_auto_stop" in capabilities.data["supported_modes"]
    assert capabilities.data["automatic_end_of_speech"] is True
    assert capabilities.data["background_listening"] == "disabled"


def test_persistent_stream_delivers_exact_immutable_pcm_bytes_to_vad(tmp_path):
    speech_frames = [frame(400), frame(-500), frame(600)]
    source = FrameSource([*speech_frames, *([frame(20)] * 5)])
    adapter = LinuxAlsaMicrophoneAdapter(
        device="plughw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(source),
    )
    assert adapter.start().success
    handle = adapter.open_persistent_stream(owner="standby_wake_listener")

    result = adapter.record_persistent_until_silence(
        handle,
        tmp_path / "exact-pcm.wav",
        calibration_enabled=False,
        speech_start_rms=200,
        speech_continue_rms=160,
        silence_rms=120,
        required_speech_frames=2,
        required_continue_frames=1,
        required_silence_frames=5,
        silence_seconds=0.1,
        speech_wait_timeout_seconds=0.1,
        maximum_utterance_seconds=0.3,
        pre_roll_seconds=0.0,
    )

    assert result.success
    with wave.open(result.final_whisper_input_path, "rb") as wav_file:
        delivered_pcm = wav_file.readframes(wav_file.getnframes())
    assert delivered_pcm == b"".join(speech_frames)
    assert result.data["valid_full_pcm_frames"] == 8
    assert result.data["valid_microphone_bytes_delivered_to_vad"] == 8 * 640
    assert result.data["fresh_microphone_bytes_delivered_to_vad"] == 8 * 640
    assert result.data["zero_filled_bytes"] == 0
    assert result.data["partial_reads"] == 0
    assert handle.frame_source.snapshot()["read_sequence"] == 8
    adapter.close_persistent_stream(handle, owner="standby_wake_listener")


def test_direct_and_persistent_adapter_commands_share_canonical_pcm_contract(tmp_path):
    adapter = LinuxAlsaMicrophoneAdapter(
        device="plughw:2,0",
        runner=DeviceRunner(),
        stream_runner=StreamRunner(FrameSource([])),
    )
    assert adapter.start().success
    direct = adapter._record_command(
        "/usr/bin/arecord",
        tmp_path / "direct.wav",
        4,
        "plughw:2,0",
    )
    handle = adapter.open_persistent_stream(owner="standby_wake_listener")
    persistent = list(handle.command)

    for flag, expected in (("-f", "S16_LE"), ("-c", "1"), ("-r", "16000")):
        assert direct[direct.index(flag) + 1] == expected
        assert persistent[persistent.index(flag) + 1] == expected
    assert handle.sample_rate_hz == 16000
    assert handle.channels == 1
    assert handle.sample_width_bytes == 2
    assert handle.samples_per_frame == 320
    assert handle.frame_bytes == 640
    adapter.close_persistent_stream(handle, owner="standby_wake_listener")


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        (EOFError("persistent PCM exhausted"), VAD_STATUS_DEVICE_ERROR),
        (TimeoutError("persistent PCM timeout"), VAD_STATUS_TIMEOUT),
    ],
)
def test_replay_only_then_input_absent_uses_fresh_live_frame_delta(
    tmp_path,
    failure,
    expected_status,
):
    low_level = FailureAfterFramesSource([frame(20)], failure)
    rolling = RollingPcmFrameSource(low_level, maximum_history_frames=2)
    assert rolling.read_frame(640, timeout_seconds=1.0) == frame(20)
    assert rolling.begin_window(1) == 1

    detector = RmsVoiceActivityCapture()
    detector.start()
    result = detector.execute(
        VoiceActivityCaptureRequestV1(
            output_wav_path=str(tmp_path / "replay-only.wav"),
            microphone_device="plughw:2,0",
            frame_duration_ms=20,
            calibration_enabled=False,
            speech_start_rms=200,
            speech_continue_rms=150,
            silence_rms=120,
            required_speech_frames=2,
            required_continue_frames=1,
            required_silence_frames=5,
            silence_duration_seconds=0.1,
            speech_wait_timeout_seconds=0.1,
            maximum_utterance_seconds=0.2,
            pre_roll_seconds=0.0,
        ),
        rolling,
    )

    assert result.status == expected_status
    assert result.data["source_frames_read_delta"] == 1
    assert result.data["source_live_frames_read_delta"] == 0
    assert result.data["source_bytes_read_delta"] == 640
    assert result.data["source_live_bytes_read_delta"] == 0
    assert result.data["capture_failure_stage"] == "post_calibration_input_absent"
    assert result.data["read_errors"] == 0
