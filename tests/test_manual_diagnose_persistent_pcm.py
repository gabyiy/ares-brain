from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from core.PcmIntegrity import (
    CANONICAL_PCM_FRAME_BYTES,
    CANONICAL_PCM_FRAME_DURATION_MS,
    CANONICAL_PCM_SAMPLE_FORMAT,
    canonical_pcm_contract,
)
from core.WavAudio import (
    CANONICAL_CHANNELS,
    CANONICAL_SAMPLE_RATE_HZ,
    CANONICAL_SAMPLE_WIDTH_BYTES,
    analyze_wav_audio,
    validate_canonical_wav,
)
from scripts import manual_diagnose_persistent_pcm as manual


def _varying_pcm(
    amplitude: int,
    seconds: int,
    *,
    sample_rate_hz: int,
) -> bytes:
    samples_per_frame = (
        sample_rate_hz * CANONICAL_PCM_FRAME_DURATION_MS // 1000
    )
    frames: list[bytes] = []
    for frame_index in range(seconds * 50):
        samples = [
            amplitude if sample_index % 2 == 0 else -amplitude
            for sample_index in range(samples_per_frame)
        ]
        pair_index = frame_index % (samples_per_frame // 2)
        first_index = pair_index * 2
        samples[first_index], samples[first_index + 1] = (
            samples[first_index + 1],
            samples[first_index],
        )
        frames.append(
            b"".join(
                int(sample).to_bytes(2, "little", signed=True)
                for sample in samples
            )
        )
    return b"".join(frames)


def _diagnostic_pcm(
    phase_seconds: int = 2,
    *,
    sample_rate_hz: int = CANONICAL_SAMPLE_RATE_HZ,
    quiet_amplitude: int = 4,
    spoken_amplitude: int = 1200,
) -> bytes:
    return _varying_pcm(
        quiet_amplitude,
        phase_seconds,
        sample_rate_hz=sample_rate_hz,
    ) + _varying_pcm(
        spoken_amplitude,
        phase_seconds,
        sample_rate_hz=sample_rate_hz,
    )


def _write_wav(path: Path, pcm: bytes, *, sample_rate_hz: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(CANONICAL_CHANNELS)
        output.setsampwidth(CANONICAL_SAMPLE_WIDTH_BYTES)
        output.setframerate(sample_rate_hz)
        output.writeframes(pcm)


def _split_phases(
    pcm: bytes,
    *,
    phase_seconds: int,
    sample_rate_hz: int,
) -> tuple[bytes, bytes]:
    split = (
        phase_seconds
        * sample_rate_hz
        * CANONICAL_CHANNELS
        * CANONICAL_SAMPLE_WIDTH_BYTES
    )
    return pcm[:split], pcm[split:]


def _header(
    *,
    sample_rate_hz: int,
    phase_seconds: int,
    path: str,
) -> dict:
    return {
        "success": True,
        "path": path,
        "sample_rate_hz": sample_rate_hz,
        "channels": CANONICAL_CHANNELS,
        "sample_width_bytes": CANONICAL_SAMPLE_WIDTH_BYTES,
        "frames": sample_rate_hz * phase_seconds,
        "duration_seconds": float(phase_seconds),
    }


def _stage_reports(
    pcm: bytes,
    *,
    phase_seconds: int,
    sample_rate_hz: int,
    frame_bytes: int,
    path_prefix: str,
) -> dict:
    quiet, spoken = _split_phases(
        pcm,
        phase_seconds=phase_seconds,
        sample_rate_hz=sample_rate_hz,
    )
    return {
        stage: manual._stage_report(
            stage_pcm,
            header=_header(
                sample_rate_hz=sample_rate_hz,
                phase_seconds=phase_seconds,
                path=f"{path_prefix}_{stage}.wav",
            ),
            sample_rate_hz=sample_rate_hz,
            frame_bytes=frame_bytes,
        )
        for stage, stage_pcm in (("quiet", quiet), ("spoken", spoken))
    }


def _statistics_report(
    *,
    phase_seconds: int = 2,
    direct_quiet_amplitude: int = 4,
    direct_spoken_amplitude: int = 1200,
    persistent_quiet_amplitude: int = 4,
    persistent_spoken_amplitude: int = 1200,
    production_metadata_required: bool = False,
) -> dict:
    direct_pcm = _diagnostic_pcm(
        phase_seconds,
        sample_rate_hz=manual.DIRECT_NATIVE_SAMPLE_RATE_HZ,
        quiet_amplitude=direct_quiet_amplitude,
        spoken_amplitude=direct_spoken_amplitude,
    )
    persistent_pcm = _diagnostic_pcm(
        phase_seconds,
        quiet_amplitude=persistent_quiet_amplitude,
        spoken_amplitude=persistent_spoken_amplitude,
    )
    persistent_frame_count = 2 * manual._frame_count(phase_seconds)
    persistent_bytes = persistent_frame_count * CANONICAL_PCM_FRAME_BYTES
    files = {
        "direct_quiet_wav": "direct_quiet.wav",
        "direct_spoken_wav": "direct_spoken.wav",
        "persistent_quiet_wav": "persistent_quiet.wav",
        "persistent_spoken_wav": "persistent_spoken.wav",
        "json_report": "pcm_integrity_report.json",
    }
    direct_processes = []
    for index, stage in enumerate(("quiet", "spoken"), start=1):
        output_path = files[f"direct_{stage}_wav"]
        direct_processes.append(
            {
                "success": True,
                "stage": stage,
                "process_pid": 1000 + index,
                "process_exit_status": 0,
                "timed_out": False,
                "command": [
                    "/usr/bin/arecord",
                    "-D",
                    "hw:2,0",
                    "-f",
                    "S16_LE",
                    "-r",
                    "44100",
                    "-c",
                    "1",
                    "-d",
                    str(phase_seconds),
                    "-t",
                    "wav",
                    output_path,
                ],
            }
        )
    counters = {
        "total_low_level_reads": persistent_frame_count,
        "valid_full_pcm_frames": persistent_frame_count,
        "partial_reads": 3,
        "accumulated_partial_bytes": 91,
        "empty_reads": 0,
        "eof_count": 0,
        "unexpected_eof_count": 0,
        "read_errors": 0,
        "discarded_bytes": 0,
        "zero_filled_bytes": 0,
        "repeated_frame_hashes": 0,
        "maximum_consecutive_repeated_non_silent_frames": 0,
        "pathological_duplicate_frame_detected": False,
        "maximum_consecutive_tiny_rms_frames": phase_seconds * 50,
        "mutable_buffer_reuse_detected": 0,
        "valid_microphone_bytes_delivered_to_vad": persistent_bytes,
        "fresh_microphone_bytes_delivered_to_vad": persistent_bytes,
        "pending_partial_bytes": 0,
        "pending_discard_alignment_bytes": 0,
        "replayed_frame_count": 0,
        "queue_overflow_dropped_frames": 0,
        "queue_overflow_dropped_bytes": 0,
        "candidate_reset_discarded_frames": 0,
        "candidate_reset_discarded_bytes": 0,
        "process_pid": 4321,
        "process_exit_status": None,
        "process_alive": True,
        "dead_process_detected": False,
        "low_level_read_size_counts": {"640": persistent_frame_count},
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
        "stdout_transport_mode": "raw_pcm_pipe_continuous_pump",
        "stderr_transport_mode": "separate_bounded_pipe",
        "expected_frame_bytes": CANONICAL_PCM_FRAME_BYTES,
    }
    post_close = {
        **counters,
        "process_exit_status": -15,
        "process_alive": False,
    }
    report = {
        "success": False,
        "protocol": {
            "quiet_seconds": phase_seconds,
            "spoken_seconds": phase_seconds,
        },
        "requested_device": "plughw:2,0",
        "direct_native_device": "hw:2,0",
        "persistent_conversion_device": "plughw:2,0",
        "resolved_persistent_device": "plughw:2,0",
        "canonical_pcm_contract": canonical_pcm_contract(),
        "production_process_metadata_required": production_metadata_required,
        "persistent_stream_id": "test-stream-1",
        "persistent_alsa_handle_id": "arecord-pid-4321",
        "direct_processes": direct_processes,
        "persistent_arecord_command": [
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
        "persistent_process": {
            "process_pid": 4321,
            "alive_before_close": True,
            "exit_status_before_close": None,
            "alive_after_close": False,
            "exit_status_after_close": -15,
            "metadata_available_before_close": True,
            "metadata_available_after_close": True,
        },
        "direct": _stage_reports(
            direct_pcm,
            phase_seconds=phase_seconds,
            sample_rate_hz=manual.DIRECT_NATIVE_SAMPLE_RATE_HZ,
            frame_bytes=manual.DIRECT_NATIVE_FRAME_BYTES,
            path_prefix="direct",
        ),
        "persistent": _stage_reports(
            persistent_pcm,
            phase_seconds=phase_seconds,
            sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
            frame_bytes=CANONICAL_PCM_FRAME_BYTES,
            path_prefix="persistent",
        ),
        "persistent_stream_counters": counters,
        "persistent_post_close_counters": post_close,
        "cleanup_failures": [],
        "files": files,
    }
    manual._populate_signal_comparison(report)
    return report


class FakePersistentFrameSource:
    def __init__(self, pcm: bytes, *, fail_after_frames: int | None = None):
        self.frames = [
            pcm[offset : offset + CANONICAL_PCM_FRAME_BYTES]
            for offset in range(0, len(pcm), CANONICAL_PCM_FRAME_BYTES)
        ]
        self.fail_after_frames = fail_after_frames
        self.read_count = 0
        self.read_error_count = 0
        self.closed = False
        self.process = SimpleNamespace(pid=4321, returncode=None)

    def read_frame(self, frame_bytes: int, timeout_seconds: float) -> bytes:
        assert frame_bytes == CANONICAL_PCM_FRAME_BYTES
        assert timeout_seconds == 1.0
        if (
            self.fail_after_frames is not None
            and self.read_count >= self.fail_after_frames
        ):
            self.read_error_count += 1
            raise RuntimeError("injected_persistent_read_failure")
        if not self.frames:
            raise EOFError("test_pcm_exhausted")
        self.read_count += 1
        return self.frames.pop(0)

    def snapshot(self) -> dict:
        delivered = self.read_count * CANONICAL_PCM_FRAME_BYTES
        return {
            "total_low_level_reads": self.read_count,
            "valid_full_pcm_frames": self.read_count,
            "partial_reads": 0,
            "accumulated_partial_bytes": 0,
            "empty_reads": 0,
            "eof_count": 0,
            "unexpected_eof_count": 0,
            "read_errors": self.read_error_count,
            "discarded_bytes": 0,
            "zero_filled_bytes": 0,
            "repeated_frame_hashes": 0,
            "maximum_consecutive_repeated_non_silent_frames": 0,
            "pathological_duplicate_frame_detected": False,
            "maximum_consecutive_tiny_rms_frames": 0,
            "mutable_buffer_reuse_detected": 0,
            "valid_microphone_bytes_delivered_to_vad": delivered,
            "fresh_microphone_bytes_delivered_to_vad": delivered,
            "pending_partial_bytes": 0,
            "pending_discard_alignment_bytes": 0,
            "replayed_frame_count": 0,
            "queue_overflow_dropped_frames": 0,
            "queue_overflow_dropped_bytes": 0,
            "candidate_reset_discarded_frames": 0,
            "candidate_reset_discarded_bytes": 0,
            "process_pid": self.process.pid,
            "process_exit_status": self.process.returncode,
            "process_alive": not self.closed,
            "dead_process_detected": False,
            "low_level_read_size_counts": {
                "640": self.read_count,
            },
            "stdout_transport_mode": "raw_pcm_pipe",
            "stderr_transport_mode": "separate_bounded_pipe",
            "expected_frame_bytes": CANONICAL_PCM_FRAME_BYTES,
        }


class FakeMicrophoneAdapter:
    def __init__(
        self,
        *,
        persistent_pcm: bytes,
        fail_after_frames: int | None = None,
        close_failure: bool = False,
        stop_failure: bool = False,
        **kwargs,
    ):
        self.frame_source = FakePersistentFrameSource(
            persistent_pcm,
            fail_after_frames=fail_after_frames,
        )
        self.constructor_kwargs = dict(kwargs)
        self.close_failure = close_failure
        self.stop_failure = stop_failure
        self.calls: list[object] = []
        self.handle = None

    def start(self):
        self.calls.append("start")
        return SimpleNamespace(success=True, status="started")

    def open_persistent_stream(self, *, owner, device):
        self.calls.append(("open_persistent_stream", owner, device))
        self.handle = SimpleNamespace(
            frame_source=self.frame_source,
            command=(
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
                device,
                "-",
            ),
            requested_device=device,
            resolved_device=device,
            stream_id="test-stream-1",
            alsa_handle_id="arecord-pid-4321",
            closed=False,
        )
        return self.handle

    def close_persistent_stream(self, handle, *, owner):
        self.calls.append(("close_persistent_stream", owner))
        assert handle is self.handle
        if self.close_failure:
            return SimpleNamespace(success=False, status="close_failed")
        handle.closed = True
        self.frame_source.closed = True
        self.frame_source.process.returncode = -15
        return SimpleNamespace(success=True, status="closed")

    def stop(self):
        self.calls.append("stop")
        return SimpleNamespace(
            success=not self.stop_failure,
            status="stop_failed" if self.stop_failure else "stopped",
        )


class FakeSpeakerAdapter:
    def __init__(self, *, play_success: bool = True):
        self.play_success = play_success
        self.calls: list[object] = []

    def start(self):
        self.calls.append("start")
        return SimpleNamespace(success=True, status="started")

    def play_wav(self, path, **_kwargs):
        self.calls.append(("play_wav", Path(path)))
        return SimpleNamespace(
            success=self.play_success,
            status="played" if self.play_success else "play_failed",
        )

    def stop(self):
        self.calls.append("stop")
        return SimpleNamespace(success=True, status="stopped")


def _run_with_fakes(
    tmp_path: Path,
    *,
    direct_pcm: bytes | None = None,
    persistent_pcm: bytes | None = None,
    phase_seconds: int = 2,
    fail_after_frames: int | None = None,
    direct_capture_failure: bool = False,
    close_failure: bool = False,
    stop_failure: bool = False,
    playback: bool = True,
    playback_success: bool = True,
    direct_audible: bool = True,
    persistent_audible: bool = True,
) -> tuple[int, FakeMicrophoneAdapter, FakeSpeakerAdapter, list[str], Path]:
    direct_pcm = direct_pcm or _diagnostic_pcm(
        phase_seconds,
        sample_rate_hz=manual.DIRECT_NATIVE_SAMPLE_RATE_HZ,
    )
    persistent_pcm = persistent_pcm or _diagnostic_pcm(phase_seconds)
    adapters: list[FakeMicrophoneAdapter] = []
    speakers: list[FakeSpeakerAdapter] = []

    def microphone_factory(**kwargs):
        adapter = FakeMicrophoneAdapter(
            persistent_pcm=persistent_pcm,
            fail_after_frames=fail_after_frames,
            close_failure=close_failure,
            stop_failure=stop_failure,
            **kwargs,
        )
        adapters.append(adapter)
        return adapter

    direct_offset = 0
    direct_calls = 0

    def direct_capture_func(**kwargs):
        nonlocal direct_offset, direct_calls
        direct_calls += 1
        byte_count = (
            phase_seconds
            * manual.DIRECT_NATIVE_SAMPLE_RATE_HZ
            * CANONICAL_SAMPLE_WIDTH_BYTES
        )
        stage_pcm = direct_pcm[direct_offset : direct_offset + byte_count]
        direct_offset += byte_count
        output_path = Path(kwargs["output_path"])
        if not direct_capture_failure:
            _write_wav(
                output_path,
                stage_pcm,
                sample_rate_hz=manual.DIRECT_NATIVE_SAMPLE_RATE_HZ,
            )
        command = [
            "/usr/bin/arecord",
            "-D",
            kwargs["device"],
            "-f",
            kwargs["sample_format"],
            "-r",
            str(kwargs["sample_rate_hz"]),
            "-c",
            str(kwargs["channels"]),
            "-d",
            str(kwargs["seconds"]),
            "-t",
            "wav",
            str(output_path),
        ]
        return {
            "success": not direct_capture_failure,
            "command": command,
            "process_pid": 7000 + direct_calls,
            "process_exit_status": 0 if not direct_capture_failure else 1,
            "timed_out": False,
            "error": "" if not direct_capture_failure else "injected_failure",
        }

    speaker = FakeSpeakerAdapter(play_success=playback_success)

    def speaker_factory(**_kwargs):
        speakers.append(speaker)
        return speaker

    def input_func(prompt: str) -> str:
        lowered = prompt.lower()
        if "direct spoken wav" in lowered:
            return "yes" if direct_audible else "no"
        if "persistent spoken wav" in lowered:
            return "yes" if persistent_audible else "no"
        return ""

    output: list[str] = []
    output_root = tmp_path / "diagnostics"
    argv = [
        "--record-seconds",
        str(phase_seconds),
        "--output-root",
        str(output_root),
        "--runtime-lock-path",
        str(tmp_path / "standby.runtime"),
    ]
    if playback:
        argv.append("--playback")
    code = manual.run_pcm_diagnostic(
        argv,
        output_func=output.append,
        input_func=input_func,
        microphone_factory=microphone_factory,
        speaker_factory=speaker_factory,
        direct_capture_func=direct_capture_func,
    )
    assert len(adapters) == 1
    if (
        playback
        and not direct_capture_failure
        and fail_after_frames is None
        and not close_failure
        and not stop_failure
    ):
        assert speakers == [speaker]
    return code, adapters[0], speaker, output, output_root


def test_parser_defaults_and_pcm_contracts_are_explicit():
    args = manual.build_parser().parse_args([])

    assert args.microphone_device == "plughw:2,0"
    assert args.speaker_device == "plughw:CARD=Device,DEV=0"
    assert args.record_seconds == 3
    assert manual.DIRECT_NATIVE_SAMPLE_RATE_HZ == 44_100
    assert manual.DIRECT_NATIVE_FRAME_BYTES == 1764
    assert canonical_pcm_contract()["frame_bytes"] == 640
    assert manual._frame_count(args.record_seconds) == 150


def test_audibility_confirmation_prompts_once():
    prompts: list[str] = []

    confirmed = manual._ask_owner_yes_no(
        lambda prompt: prompts.append(prompt) or "yes",
        "Was the spoken WAV audible? [yes/no]",
    )

    assert confirmed is True
    assert len(prompts) == 1


@pytest.mark.parametrize(
    ("configured", "native"),
    [("plughw:2,0", "hw:2,0"), ("hw:2,0", "hw:2,0")],
)
def test_native_direct_device_is_derived_without_changing_card(configured, native):
    assert manual._native_direct_capture_device(configured) == native


def test_non_numeric_device_is_rejected_for_native_comparison():
    args = manual.build_parser().parse_args(
        ["--microphone-device", "default"]
    )
    assert "numeric hw" in manual._validate_args(args)


def test_stage_statistics_split_equal_quiet_and_spoken_phases():
    pcm = _diagnostic_pcm(
        phase_seconds=3,
        quiet_amplitude=4,
        spoken_amplitude=900,
    )

    stages = manual._stage_statistics(pcm, 3)

    assert stages["quiet"].duration_seconds == pytest.approx(3.0)
    assert stages["quiet"].rms == pytest.approx(4.0)
    assert stages["spoken"].duration_seconds == pytest.approx(3.0)
    assert stages["spoken"].rms == pytest.approx(900.0)
    assert stages["all"].duration_seconds == pytest.approx(6.0)
    assert manual._signal_changed(stages) is True


def test_stage_statistics_reject_duration_mismatch():
    with pytest.raises(ValueError, match="diagnostic_pcm_duration_mismatch"):
        manual._stage_statistics(_diagnostic_pcm(2)[:-2], 2)


def test_structural_report_accepts_native_and_production_contracts():
    report = _statistics_report()
    output: list[str] = []

    assert manual._render_report(report, output_func=output.append) is True
    assert report["direct_structural_integrity"] is True
    assert report["persistent_structural_integrity"] is True
    assert "  Structural PCM integrity: yes" in output


@pytest.mark.parametrize(
    ("counter", "value"),
    [
        ("valid_full_pcm_frames", 1),
        ("read_errors", 1),
        ("unexpected_eof_count", 1),
        ("dead_process_detected", True),
        ("zero_filled_bytes", 2),
        ("pending_partial_bytes", 1),
        ("discarded_bytes", 2),
        ("pathological_duplicate_frame_detected", True),
        ("queue_overflow_dropped_frames", 1),
        ("candidate_reset_discarded_bytes", 640),
    ],
)
def test_structural_report_fails_closed_on_transport_invariant(counter, value):
    report = _statistics_report()
    report["persistent_stream_counters"][counter] = value

    assert manual._render_report(report, output_func=lambda _line: None) is False


def test_structural_report_rejects_wrong_persistent_device():
    report = _statistics_report()
    report["resolved_persistent_device"] = "plughw:3,0"

    assert manual._render_report(report, output_func=lambda _line: None) is False


def test_structural_report_rejects_non_native_direct_header():
    report = _statistics_report()
    report["direct"]["quiet"]["wav_header"]["sample_rate_hz"] = 16_000

    assert manual._render_report(report, output_func=lambda _line: None) is False


@pytest.mark.parametrize(
    "mutation",
    ["executable", "direct_rate", "persistent_type", "persistent_stdout"],
)
def test_structural_report_rejects_command_contract_mutation(mutation):
    report = _statistics_report()
    if mutation == "executable":
        report["direct_processes"][0]["command"][0] = "/other/arecord"
    elif mutation == "direct_rate":
        command = report["direct_processes"][0]["command"]
        command[command.index("-r") + 1] = "16000"
    elif mutation == "persistent_type":
        command = report["persistent_arecord_command"]
        command[command.index("-t") + 1] = "wav"
    else:
        report["persistent_arecord_command"][-1] = "capture.raw"

    assert manual._render_report(report, output_func=lambda _line: None) is False


def test_report_contains_required_metrics_and_ratios():
    report = _statistics_report()
    stage = report["persistent"]["spoken"]
    comparison = report["signal_comparison"]

    assert stage["duration_seconds"] == pytest.approx(2.0)
    assert stage["nonzero_sample_count"] > 0
    assert stage["distinct_sample_count"] > 1
    assert stage["absolute_sample_percentage_above_100"] > 99.0
    assert stage["absolute_sample_percentage_above_300"] > 99.0
    assert stage["absolute_sample_percentage_above_1000"] > 99.0
    assert stage["absolute_sample_percentage_above_3000"] == 0.0
    assert comparison["direct_spoken_quiet_rms_ratio"] == pytest.approx(300.0)
    assert comparison["persistent_spoken_quiet_rms_ratio"] == pytest.approx(300.0)
    assert comparison["persistent_to_direct_spoken_rms_ratio"] == pytest.approx(1.0)
    assert comparison["cross_path_comparable"] is True


def test_cross_path_gate_rejects_severely_attenuated_persistent_speech():
    report = _statistics_report(
        direct_spoken_amplitude=4300,
        persistent_spoken_amplitude=300,
    )

    assert report["signal_comparison"]["persistent_signal_changed"] is True
    assert report["signal_comparison"]["cross_path_comparable"] is False


def test_signal_change_rejects_unchanged_and_near_four_rms():
    unchanged = _statistics_report(
        persistent_quiet_amplitude=4,
        persistent_spoken_amplitude=4,
    )
    near_four = _statistics_report(
        persistent_quiet_amplitude=4,
        persistent_spoken_amplitude=5,
    )

    assert unchanged["signal_comparison"]["persistent_signal_changed"] is False
    assert near_four["signal_comparison"]["persistent_signal_changed"] is False
    assert near_four["signal_comparison"]["cross_path_comparable"] is False


def test_native_wav_reader_preserves_real_44100_header_and_duration(tmp_path):
    path = tmp_path / "native.wav"
    pcm = _varying_pcm(
        900,
        3,
        sample_rate_hz=manual.DIRECT_NATIVE_SAMPLE_RATE_HZ,
    )
    _write_wav(
        path,
        pcm,
        sample_rate_hz=manual.DIRECT_NATIVE_SAMPLE_RATE_HZ,
    )

    loaded, header = manual._read_s16_wav_pcm(
        path,
        expected_sample_rate_hz=manual.DIRECT_NATIVE_SAMPLE_RATE_HZ,
        expected_channels=1,
        expected_sample_width_bytes=2,
        expected_duration_seconds=3,
    )

    assert loaded == pcm
    assert header["sample_rate_hz"] == 44_100
    assert header["duration_seconds"] == pytest.approx(3.0)


def test_factory_probe_saves_four_stage_wavs_json_and_releases_stream(tmp_path):
    code, adapter, speaker, output, output_root = _run_with_fakes(tmp_path)

    assert code == 0
    assert adapter.constructor_kwargs == {
        "device": "plughw:2,0",
        "record_seconds": 2,
        "sample_rate_hz": 16_000,
        "channels": 1,
        "sample_format": "S16_LE",
        "timeout_seconds": 7.0,
    }
    assert adapter.calls == [
        "start",
        ("open_persistent_stream", "persistent_pcm_diagnostic", "plughw:2,0"),
        ("close_persistent_stream", "persistent_pcm_diagnostic"),
        "stop",
    ]
    run_directory = next(output_root.iterdir())
    report = json.loads(
        (run_directory / "pcm_integrity_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["success"] is True
    assert report["direct_processes"][0]["command"][1:3] == ["-D", "hw:2,0"]
    assert report["persistent_arecord_command"][-1] == "-"
    assert report["persistent_process"]["process_pid"] == 4321
    assert report["persistent_process"]["exit_status_after_close"] == -15
    assert report["persistent_stream_counters"]["accumulated_partial_bytes"] == 0
    assert report["persistent_transport"] == {
        "short_read_count": 0,
        "accumulated_partial_bytes": 0,
        "pending_partial_bytes": 0,
        "consecutive_duplicate_frame_count": 0,
        "process_pid": 4321,
        "process_exit_status": -15,
        "exact_capture_command": report["persistent_arecord_command"],
    }
    assert report["audibility_confirmed"] is True
    for file_key in (
        "direct_quiet_wav",
        "direct_spoken_wav",
        "persistent_quiet_wav",
        "persistent_spoken_wav",
    ):
        path = Path(report["files"][file_key])
        assert path.exists()
        header = analyze_wav_audio(path)
        assert header["success"] is True
        assert header["duration_seconds"] == pytest.approx(2.0)
    assert analyze_wav_audio(
        Path(report["files"]["direct_spoken_wav"])
    )["sample_rate_hz"] == 44_100
    assert validate_canonical_wav(
        Path(report["files"]["persistent_spoken_wav"])
    )["success"] is True
    assert [call[0] for call in speaker.calls if isinstance(call, tuple)] == [
        "play_wav",
        "play_wav",
    ]
    assert any(line.startswith("PASS:") for line in output)


def test_factory_probe_rejects_persistent_rms_near_four(tmp_path):
    persistent = _diagnostic_pcm(
        2,
        quiet_amplitude=4,
        spoken_amplitude=5,
    )
    code, adapter, _speaker, _output, output_root = _run_with_fakes(
        tmp_path,
        persistent_pcm=persistent,
    )

    assert code == 5
    assert adapter.calls[-1] == "stop"
    report = json.loads(
        (next(output_root.iterdir()) / "pcm_integrity_report.json").read_text()
    )
    assert report["persistent_speech_signal_changed"] is False
    assert report["success"] is False


def test_factory_probe_rejects_incomparable_attenuation(tmp_path):
    direct = _diagnostic_pcm(
        2,
        sample_rate_hz=manual.DIRECT_NATIVE_SAMPLE_RATE_HZ,
        spoken_amplitude=4300,
    )
    persistent = _diagnostic_pcm(2, spoken_amplitude=300)

    code, _adapter, _speaker, output, _root = _run_with_fakes(
        tmp_path,
        direct_pcm=direct,
        persistent_pcm=persistent,
    )

    assert code == 5
    assert any("reasonably comparable" in line for line in output)


def test_probe_without_playback_cannot_claim_audibility(tmp_path):
    code, _adapter, _speaker, _output, output_root = _run_with_fakes(
        tmp_path,
        playback=False,
    )

    assert code == 6
    report = json.loads(
        (next(output_root.iterdir()) / "pcm_integrity_report.json").read_text()
    )
    assert report["playback_requested"] is False
    assert report["audibility_confirmed"] is False
    assert report["success"] is False


def test_playback_subprocess_success_without_owner_confirmation_fails(tmp_path):
    code, _adapter, _speaker, _output, output_root = _run_with_fakes(
        tmp_path,
        persistent_audible=False,
    )

    assert code == 6
    report = json.loads(
        (next(output_root.iterdir()) / "pcm_integrity_report.json").read_text()
    )
    assert report["playback_success"] is True
    assert report["persistent_spoken_audible_confirmed"] is False
    assert report["success"] is False


def test_playback_failure_blocks_probe_pass(tmp_path):
    code, _adapter, _speaker, output, output_root = _run_with_fakes(
        tmp_path,
        playback_success=False,
    )

    assert code == 6
    report = json.loads(
        (next(output_root.iterdir()) / "pcm_integrity_report.json").read_text()
    )
    assert report["playback_success"] is False
    assert report["audibility_confirmed"] is False
    assert any("audibility" in line for line in output)


@pytest.mark.parametrize("failure", ["close", "stop"])
def test_cleanup_failure_retains_failure_json_and_stage_wavs(tmp_path, failure):
    code, adapter, _speaker, _output, output_root = _run_with_fakes(
        tmp_path,
        close_failure=failure == "close",
        stop_failure=failure == "stop",
    )

    assert code == 3
    assert adapter.calls[-1] == "stop"
    run_directory = next(output_root.iterdir())
    report_path = run_directory / "pcm_integrity_report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert report["success"] is False
    assert report["cleanup_failures"]
    assert (run_directory / "direct_spoken.wav").exists()
    assert (run_directory / "persistent_spoken.wav").exists()


def test_read_failure_retains_json_partial_wav_counters_and_cleanup(tmp_path):
    code, adapter, _speaker, output, output_root = _run_with_fakes(
        tmp_path,
        fail_after_frames=3,
    )

    assert code == 3
    assert adapter.calls[-2:] == [
        ("close_persistent_stream", "persistent_pcm_diagnostic"),
        "stop",
    ]
    run_directory = next(output_root.iterdir())
    report = json.loads(
        (run_directory / "pcm_integrity_report.json").read_text()
    )
    assert report["failure_stage"] == "persistent_stream_capture"
    assert report["persistent"]["quiet"]["complete"] is False
    assert report["persistent"]["quiet"]["frame_count"] == 3
    assert report["persistent_stream_counters"]["read_errors"] == 1
    assert (run_directory / "persistent_quiet.wav").exists()
    assert any("failure JSON" in line for line in output)


def test_direct_capture_failure_still_writes_one_failure_json(tmp_path):
    code, adapter, _speaker, _output, output_root = _run_with_fakes(
        tmp_path,
        direct_capture_failure=True,
    )

    assert code == 3
    assert adapter.calls == []
    run_directory = next(output_root.iterdir())
    reports = list(run_directory.glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["failure_stage"] == "direct_native_capture"
    assert "direct_quiet_capture_failed" in report["capture_error"]


def test_persistent_phase_copies_reused_mutable_source_buffers():
    mutable = bytearray(_varying_pcm(500, 1, sample_rate_hz=16_000)[:640])

    class ReusedSource:
        def __init__(self):
            self.calls = 0

        def read_frame(self, _frame_bytes, _timeout):
            self.calls += 1
            mutable[0:2] = self.calls.to_bytes(2, "little", signed=True)
            return memoryview(mutable)

    destination: list[bytes] = []
    manual._capture_persistent_phase(
        ReusedSource(),
        seconds=1,
        destination=destination,
    )
    first = destination[0]
    mutable[:] = b"\x00" * len(mutable)

    assert len(destination) == 50
    assert isinstance(first, bytes)
    assert int.from_bytes(first[:2], "little", signed=True) == 1
    assert first != bytes(mutable)


def test_production_report_requires_process_and_transport_metadata():
    report = _statistics_report(production_metadata_required=True)
    del report["persistent_stream_counters"]["stdout_transport_mode"]

    assert manual._render_report(report, output_func=lambda _line: None) is False


def test_production_report_accepts_complete_process_lifecycle_metadata():
    report = _statistics_report(production_metadata_required=True)

    assert manual._render_report(report, output_func=lambda _line: None) is True


def test_pathological_duplicate_spoken_stage_cannot_pass():
    report = _statistics_report()
    report["persistent"]["spoken"]["unique_frame_hash_count"] = 1
    report["persistent"]["spoken"]["repeated_frame_percentage"] = 100.0

    assert manual._render_report(report, output_func=lambda _line: None) is False


def test_extensive_but_not_total_duplicate_stage_cannot_pass():
    report = _statistics_report()
    report["persistent"]["spoken"]["unique_frame_hash_count"] = 2
    report["persistent"]["spoken"]["repeated_frame_percentage"] = 75.0

    assert manual._render_report(report, output_func=lambda _line: None) is False
