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


class ObservableFailureSource(SyntheticFrameSource):
    def snapshot(self):
        return {
            "transport_argv": [
                "/usr/bin/arecord",
                "-q",
                "-f",
                "S16_LE",
                "-c",
                "1",
                "-r",
                "16000",
                "-t",
                "raw",
                "-D",
                "plughw:2,0",
                "-",
            ],
            "process_pid": 4321,
            "process_exit_status": 1,
            "process_liveness_observable": True,
            "stream_ended": True,
            "terminal_reason": "arecord_process_exited",
            "stderr_preview": "arecord: audio open error: Device or resource busy",
            "closed": False,
        }


def request(tmp_path, **overrides):
    values = {
        "output_wav_path": str(tmp_path / "capture.wav"),
        "microphone_device": "hw:2,0",
        "frame_duration_ms": FRAME_MS,
        "calibration_enabled": False,
        "speech_start_rms": 200,
        "speech_continue_rms": 150,
        "silence_rms": 120,
        "required_speech_frames": 2,
        "required_continue_frames": 1,
        "required_silence_frames": 1,
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


def test_pcm_read_failure_preserves_exact_message_and_structured_source_context(
    tmp_path,
):
    detector = RmsVoiceActivityCapture()
    detector.start()
    source = ObservableFailureSource(
        failure=RuntimeError("arecord_process_exited:1")
    )

    result = detector.execute(request(tmp_path), source)

    assert result.success is False
    assert result.status == VAD_STATUS_DEVICE_ERROR
    assert result.error_message == (
        "pcm_stream_error:RuntimeError:arecord_process_exited:1"
    )
    failure = result.data["pcm_exception"]
    assert failure["exception_class"] == "RuntimeError"
    assert failure["exception_message"] == "arecord_process_exited:1"
    assert failure["failing_method"] == "read_frame"
    assert failure["microphone_device"] == "hw:2,0"
    assert failure["requested_pcm_format"]["expected_frame_bytes"] == FRAME_BYTES
    assert failure["alsa_child_process_id"] == 4321
    assert failure["alsa_process_exit_status"] == 1
    assert failure["alsa_stderr"].endswith("Device or resource busy")
    assert failure["stream_lifecycle_state"] == "ended"
    assert "traceback" not in failure


def test_pcm_read_traceback_is_retained_only_with_explicit_diagnostic_opt_in(
    tmp_path,
):
    detector = RmsVoiceActivityCapture()
    detector.start()
    source = ObservableFailureSource(
        failure=RuntimeError("arecord_stdout_closed_while_process_alive")
    )

    result = detector.execute(
        request(
            tmp_path,
            metadata={"diagnostic_exception_traceback": True},
        ),
        source,
    )

    failure = result.data["pcm_exception"]
    assert failure["exception_message"] == (
        "arecord_stdout_closed_while_process_alive"
    )
    assert "RuntimeError: arecord_stdout_closed_while_process_alive" in failure[
        "traceback"
    ]


def test_invalid_pcm_read_preserves_exact_type_message_and_diagnostic_context(
    tmp_path,
):
    detector = RmsVoiceActivityCapture()
    detector.start()
    source = ObservableFailureSource(
        failure=ValueError("odd_byte_pcm_payload:639")
    )

    result = detector.execute(
        request(
            tmp_path,
            metadata={"diagnostic_exception_traceback": True},
        ),
        source,
    )

    assert result.success is False
    assert result.status == VAD_STATUS_INVALID_AUDIO
    assert result.error_message == (
        "invalid_pcm_stream:ValueError:odd_byte_pcm_payload:639"
    )
    failure = result.data["pcm_exception"]
    assert failure["exception_class"] == "ValueError"
    assert failure["exception_message"] == "odd_byte_pcm_payload:639"
    assert failure["failing_method"] == "read_frame"
    assert "ValueError: odd_byte_pcm_payload:639" in failure["traceback"]


def test_stream_calibration_preserves_exact_pcm_failure_without_normal_traceback(
    tmp_path,
):
    detector = RmsVoiceActivityCapture()
    detector.start()
    source = ObservableFailureSource(
        failure=RuntimeError("arecord_process_exited:7")
    )
    calibration_request = request(
        tmp_path,
        calibration_enabled=True,
        calibration_duration_seconds=0.1,
    )

    result = detector.calibrate_stream(calibration_request, source)

    assert result.success is False
    assert result.error_message == (
        "pcm_stream_error:RuntimeError:arecord_process_exited:7"
    )
    assert result.data["pcm_exception"]["exception_message"] == (
        "arecord_process_exited:7"
    )
    assert "traceback" not in result.data["pcm_exception"]


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


def test_capture_ready_boundary_occurs_after_calibration_before_waiting(tmp_path):
    detector = RmsVoiceActivityCapture()
    detector.start()
    source = SyntheticFrameSource([pcm_frame(40)] * 5)
    notifications = []

    def ready(details):
        notifications.append((source.read_count, dict(details)))
        # Model the real owner contract: the utterance begins only in response
        # to the post-calibration ready prompt, never while calibration runs.
        source.frames.extend(
            [
                *([pcm_frame(50)] * 25),
                pcm_frame(400),
                pcm_frame(500),
                *([pcm_frame(20)] * 5),
            ]
        )

    result = detector.execute(
        request(
            tmp_path,
            calibration_enabled=True,
            calibration_duration_seconds=0.1,
            speech_wait_timeout_seconds=1.0,
            maximum_utterance_seconds=1.0,
            pre_roll_seconds=0.5,
        ),
        source,
        capture_ready_callback=ready,
    )

    assert result.success is True
    assert len(notifications) == 1
    frames_consumed, details = notifications[0]
    assert frames_consumed == 5
    assert details["capture_start_reason"] == "calibration_completed_stream_ready"
    assert details["calibration_frames_consumed"] == 5
    assert details["expected_frame_bytes"] == FRAME_BYTES
    assert details["pre_roll_frames"] == 25
    assert result.data["capture_ready_notified"] is True
    assert result.data["capture_start_reason"] == (
        "calibration_completed_stream_ready"
    )
    pcm = read_pcm(result.wav_path)
    assert pcm.startswith(pcm_frame(50))
    assert pcm_frame(40) not in pcm
    assert pcm_frame(400) + pcm_frame(500) in pcm


def test_half_second_pre_roll_preserves_subthreshold_first_consonant(tmp_path):
    quiet = [pcm_frame(30)] * 25
    first_consonant = pcm_frame(180)
    first_confirmed = pcm_frame(350)
    second_confirmed = pcm_frame(500)
    result = execute(
        tmp_path,
        [
            *quiet,
            first_consonant,
            first_confirmed,
            second_confirmed,
            *([pcm_frame(20)] * 5),
        ],
        pre_roll_seconds=0.5,
        speech_wait_timeout_seconds=1.0,
        maximum_utterance_seconds=1.0,
    )

    pcm = read_pcm(result.wav_path)
    consonant_offset = pcm.index(first_consonant)
    speech_offset = pcm.index(first_confirmed)
    assert result.success is True
    assert result.pre_roll_frames_retained == 25
    assert consonant_offset < speech_offset
    assert pcm[speech_offset : speech_offset + FRAME_BYTES * 2] == (
        first_confirmed + second_confirmed
    )
    assert result.data["expected_pre_roll_frames"] == 25
    assert result.data["beginning_clipped"] == "no"
    assert result.data["leading_audio_trimmed_seconds"] == 0.0


@pytest.mark.parametrize("phrase", ["goodbye Ares", "shutdown Ares"])
def test_active_lifecycle_candidate_keeps_leading_verb_audio_before_ares(
    tmp_path,
    phrase,
):
    # Distinct synthetic envelopes stand in for the leading lifecycle verb and
    # trailing assistant address. The leading onset begins below the start gate
    # and must still precede the complete trailing segment in Whisper's WAV.
    leading_verb = pcm_frame(180) + pcm_frame(360) + pcm_frame(440)
    trailing_ares = pcm_frame(620) + pcm_frame(700)
    result = execute(
        tmp_path,
        [
            *([pcm_frame(30)] * 25),
            leading_verb[:FRAME_BYTES],
            leading_verb[FRAME_BYTES : FRAME_BYTES * 2],
            leading_verb[FRAME_BYTES * 2 :],
            trailing_ares[:FRAME_BYTES],
            trailing_ares[FRAME_BYTES:],
            *([pcm_frame(20)] * 45),
        ],
        pre_roll_seconds=0.5,
        silence_duration_seconds=0.9,
        speech_wait_timeout_seconds=1.0,
        maximum_utterance_seconds=3.0,
    )

    pcm = read_pcm(result.wav_path)
    assert result.success is True, phrase
    assert leading_verb in pcm, phrase
    assert trailing_ares in pcm, phrase
    assert pcm.index(leading_verb) < pcm.index(trailing_ares), phrase
    assert result.data["beginning_clipped"] == "no"


def test_speech_onset_inside_pcm_read_frame_is_preserved_exactly(tmp_path):
    quiet_half = pcm_frame(30)[: FRAME_BYTES // 2]
    speech_half = pcm_frame(400)[FRAME_BYTES // 2 :]
    boundary_frame = quiet_half + speech_half
    result = execute(
        tmp_path,
        [
            *([pcm_frame(30)] * 25),
            boundary_frame,
            pcm_frame(500),
            *([pcm_frame(20)] * 5),
        ],
        pre_roll_seconds=0.5,
        speech_wait_timeout_seconds=1.0,
        maximum_utterance_seconds=1.0,
    )

    pcm = read_pcm(result.wav_path)
    assert result.success is True
    assert boundary_frame in pcm
    assert pcm.index(boundary_frame) < pcm.index(pcm_frame(500))
    assert result.data["captured_frame_indexes_unique"] is True


def test_active_terminal_quiet_window_keeps_internal_phrase_pause(tmp_path):
    internal_pause = [pcm_frame(40)] * 20
    result = execute(
        tmp_path,
        [
            pcm_frame(500),
            pcm_frame(500),
            *internal_pause,
            pcm_frame(450),
            pcm_frame(450),
            pcm_frame(450),
            *([pcm_frame(20)] * 45),
        ],
        required_continue_frames=3,
        required_silence_frames=5,
        silence_duration_seconds=0.9,
        speech_wait_timeout_seconds=1.0,
        maximum_utterance_seconds=3.0,
        pre_roll_seconds=0.5,
    )

    assert result.success is True
    assert result.terminal_silence_confirmed is True
    assert result.silence_duration_at_stop_seconds == pytest.approx(0.9)
    assert result.terminal_silence_reset_count == 1
    assert b"".join(internal_pause) in read_pcm(result.wav_path)


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
    assert result.data["pcm_exception"]["exception_class"] == "TimeoutError"
    assert result.data["pcm_exception"]["exception_message"] == "timeout"
    assert "traceback" not in result.data["pcm_exception"]


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
