from __future__ import annotations

from pathlib import Path

import pytest

from core import (
    LinuxAlsaMicrophoneAdapter,
    SafeProcessResult,
    VAD_STATUS_COMPLETED_AFTER_SILENCE,
    VAD_STATUS_DEVICE_ERROR,
    VAD_STATUS_NO_SPEECH_TIMEOUT,
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
        self.closed = False
        self.stderr = ""

    def read_frame(self, frame_bytes, timeout_seconds):
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


def test_linux_alsa_capabilities_advertise_activity_capture():
    adapter = LinuxAlsaMicrophoneAdapter(runner=DeviceRunner())

    capabilities = adapter.get_capabilities()

    assert "arecord_pcm_rms_auto_stop" in capabilities.data["supported_modes"]
    assert capabilities.data["automatic_end_of_speech"] is True
    assert capabilities.data["background_listening"] == "disabled"
