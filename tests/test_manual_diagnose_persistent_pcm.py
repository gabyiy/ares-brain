from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from core import AudioChunk
from core.PcmIntegrity import (
    CANONICAL_PCM_FRAME_BYTES,
    CANONICAL_PCM_FRAME_DURATION_MS,
    CANONICAL_PCM_SAMPLE_FORMAT,
    CANONICAL_PCM_SAMPLES_PER_FRAME,
    canonical_pcm_contract,
)
from core.WavAudio import (
    CANONICAL_CHANNELS,
    CANONICAL_SAMPLE_RATE_HZ,
    CANONICAL_SAMPLE_WIDTH_BYTES,
    validate_canonical_wav,
    write_audio_chunk_wav,
)
from scripts import manual_diagnose_persistent_pcm as manual


ONE_SECOND_BYTES = (
    CANONICAL_SAMPLE_RATE_HZ
    * CANONICAL_CHANNELS
    * CANONICAL_SAMPLE_WIDTH_BYTES
)


def _alternating_pcm(amplitude: int, sample_count: int) -> bytes:
    values = (amplitude, -amplitude)
    return b"".join(
        int(values[index % 2]).to_bytes(2, "little", signed=True)
        for index in range(sample_count)
    )


def _diagnostic_pcm(
    record_seconds: int = 2,
    *,
    silence_amplitude: int = 4,
    spoken_amplitude: int = 1200,
) -> bytes:
    silence = _varying_hash_pcm_with_fixed_rms(silence_amplitude, 1)
    spoken = _varying_hash_pcm_with_fixed_rms(
        spoken_amplitude,
        record_seconds - 1,
    )
    return silence + spoken


def _varying_hash_pcm_with_fixed_rms(amplitude: int, seconds: int) -> bytes:
    frames = []
    for frame_index in range(manual._frame_count(seconds)):
        samples = [
            amplitude if sample_index % 2 == 0 else -amplitude
            for sample_index in range(CANONICAL_PCM_SAMPLES_PER_FRAME)
        ]
        pair_index = frame_index % (CANONICAL_PCM_SAMPLES_PER_FRAME // 2)
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


def _varying_near_silent_pcm(record_seconds: int = 2) -> bytes:
    frames = []
    for frame_index in range(manual._frame_count(record_seconds)):
        amplitude = 4 + (frame_index % 5)
        frames.append(
            _alternating_pcm(amplitude, CANONICAL_PCM_SAMPLES_PER_FRAME)
        )
    return b"".join(frames)


def _write_canonical_wav(path: Path, pcm: bytes) -> None:
    write_audio_chunk_wav(
        AudioChunk(
            data=pcm,
            sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
            channels=CANONICAL_CHANNELS,
            sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
            source="persistent_pcm_diagnostic_test",
        ),
        path,
    )


def _statistics_report(pcm: bytes, record_seconds: int = 2) -> dict:
    stages = {
        name: statistics.to_dict()
        for name, statistics in manual._stage_statistics(
            pcm,
            record_seconds,
        ).items()
    }
    frame_count = stages["all"]["frame_count"]
    return {
        "direct": stages,
        "persistent": stages,
        "comparison_device": "plughw:2,0",
        "direct_arecord_commands": [
            [
                "/usr/bin/arecord",
                "-f",
                "S16_LE",
                "-c",
                "1",
                "-r",
                "16000",
                "-D",
                "plughw:2,0",
                "-t",
                "wav",
                "direct-silence.wav",
            ],
            [
                "/usr/bin/arecord",
                "-f",
                "S16_LE",
                "-c",
                "1",
                "-r",
                "16000",
                "-D",
                "plughw:2,0",
                "-t",
                "wav",
                "direct-spoken.wav",
            ],
        ],
        "persistent_arecord_command": [
            "/usr/bin/arecord",
            "-f",
            "S16_LE",
            "-c",
            "1",
            "-r",
            "16000",
            "-D",
            "plughw:2,0",
            "-t",
            "raw",
            "-",
        ],
        "direct_wav": {
            "success": True,
            "sample_rate_hz": 16000,
            "channels": 1,
            "sample_width_bytes": 2,
        },
        "direct_arecord_raw_wav_headers": [
            {
                "success": True,
                "sample_rate_hz": 16000,
                "channels": 1,
                "sample_width_bytes": 2,
            },
            {
                "success": True,
                "sample_rate_hz": 16000,
                "channels": 1,
                "sample_width_bytes": 2,
            },
        ],
        "persistent_wav": {
            "success": True,
            "sample_rate_hz": 16000,
            "channels": 1,
            "sample_width_bytes": 2,
        },
        "resolved_persistent_device": "plughw:2,0",
        "persistent_stream_counters": {
            "total_low_level_reads": frame_count + 3,
            "valid_full_pcm_frames": frame_count,
            "partial_reads": 3,
            "empty_reads": 0,
            "read_errors": 0,
            "discarded_bytes": 0,
            "zero_filled_bytes": 0,
            "repeated_frame_hashes": 0,
            "mutable_buffer_reuse_detected": 0,
            "valid_microphone_bytes_delivered_to_vad": (
                frame_count * CANONICAL_PCM_FRAME_BYTES
            ),
            "fresh_microphone_bytes_delivered_to_vad": (
                frame_count * CANONICAL_PCM_FRAME_BYTES
            ),
            "pending_partial_bytes": 0,
        },
        "files": {
            "direct_arecord_wav": "direct_arecord.wav",
            "direct_raw_room_silence_wav": "direct-room-silence.raw.wav",
            "direct_raw_spoken_wav": "direct-spoken.raw.wav",
            "persistent_stream_wav": "persistent_stream.wav",
        },
    }


class FakePersistentFrameSource:
    def __init__(self, pcm: bytes, *, fail_after_frames: int | None = None):
        self.frames = [
            pcm[offset : offset + CANONICAL_PCM_FRAME_BYTES]
            for offset in range(0, len(pcm), CANONICAL_PCM_FRAME_BYTES)
        ]
        self.fail_after_frames = fail_after_frames
        self.read_count = 0
        self.read_error_count = 0

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
            "empty_reads": 0,
            "read_errors": self.read_error_count,
            "discarded_bytes": 0,
            "zero_filled_bytes": 0,
            "repeated_frame_hashes": 0,
            "mutable_buffer_reuse_detected": 0,
            "valid_microphone_bytes_delivered_to_vad": delivered,
            "fresh_microphone_bytes_delivered_to_vad": delivered,
            "pending_partial_bytes": 0,
        }


class FakeMicrophoneAdapter:
    def __init__(
        self,
        *,
        direct_pcm: bytes,
        persistent_pcm: bytes,
        fail_after_frames: int | None = None,
        close_failure: bool = False,
        stop_failure: bool = False,
        **kwargs,
    ):
        self.direct_pcm = direct_pcm
        self.frame_source = FakePersistentFrameSource(
            persistent_pcm,
            fail_after_frames=fail_after_frames,
        )
        self.constructor_kwargs = dict(kwargs)
        self.close_failure = close_failure
        self.stop_failure = stop_failure
        self.calls: list[object] = []
        self.handle = None
        self.direct_offset = 0

    def start(self):
        self.calls.append("start")
        return SimpleNamespace(success=True, status="started")

    def record_wav(self, output_path, **kwargs):
        self.calls.append(("record_wav", Path(output_path), dict(kwargs)))
        byte_count = int(kwargs["seconds"]) * ONE_SECOND_BYTES
        stage_pcm = self.direct_pcm[
            self.direct_offset : self.direct_offset + byte_count
        ]
        self.direct_offset += byte_count
        _write_canonical_wav(Path(output_path), stage_pcm)
        raw_path = Path(output_path).with_name(
            f"{Path(output_path).stem}.raw.wav"
        )
        _write_canonical_wav(raw_path, stage_pcm)
        return SimpleNamespace(
            success=True,
            status="recorded",
            data={
                "process": {
                    "args": [
                        "/usr/bin/arecord",
                        "-f",
                        CANONICAL_PCM_SAMPLE_FORMAT,
                        "-c",
                        "1",
                        "-r",
                        str(CANONICAL_SAMPLE_RATE_HZ),
                        "-D",
                        str(kwargs.get("device") or ""),
                        "-t",
                        "wav",
                        str(output_path),
                    ]
                },
                "raw_wav_path": str(raw_path),
                "raw_wav": {
                    "success": True,
                    "sample_rate_hz": CANONICAL_SAMPLE_RATE_HZ,
                    "channels": CANONICAL_CHANNELS,
                    "sample_width_bytes": CANONICAL_SAMPLE_WIDTH_BYTES,
                    "frames": len(stage_pcm) // CANONICAL_SAMPLE_WIDTH_BYTES,
                },
            },
        )

    def open_persistent_stream(self, *, owner, device):
        self.calls.append(("open_persistent_stream", owner, device))
        self.handle = SimpleNamespace(
            frame_source=self.frame_source,
            command=(
                "/usr/bin/arecord",
                "-f",
                CANONICAL_PCM_SAMPLE_FORMAT,
                "-c",
                "1",
                "-r",
                str(CANONICAL_SAMPLE_RATE_HZ),
                "-t",
                "raw",
                "-D",
                device,
                "-",
            ),
            resolved_device=device,
            closed=False,
        )
        return self.handle

    def close_persistent_stream(self, handle, *, owner):
        self.calls.append(("close_persistent_stream", owner))
        assert handle is self.handle
        handle.closed = True
        return SimpleNamespace(
            success=not self.close_failure,
            status="close_failed" if self.close_failure else "closed",
        )

    def stop(self):
        self.calls.append("stop")
        return SimpleNamespace(
            success=not self.stop_failure,
            status="stop_failed" if self.stop_failure else "stopped",
        )


def _run_with_fake_microphone(
    tmp_path: Path,
    *,
    direct_pcm: bytes,
    persistent_pcm: bytes,
    fail_after_frames: int | None = None,
    close_failure: bool = False,
    stop_failure: bool = False,
    playback: bool = False,
    playback_success: bool = True,
) -> tuple[int, FakeMicrophoneAdapter, list[str], Path]:
    adapters: list[FakeMicrophoneAdapter] = []

    def microphone_factory(**kwargs):
        adapter = FakeMicrophoneAdapter(
            direct_pcm=direct_pcm,
            persistent_pcm=persistent_pcm,
            fail_after_frames=fail_after_frames,
            close_failure=close_failure,
            stop_failure=stop_failure,
            **kwargs,
        )
        adapters.append(adapter)
        return adapter

    output: list[str] = []
    output_root = tmp_path / "diagnostics"
    argv = [
        "--record-seconds",
        "2",
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
        input_func=lambda _prompt: "",
        microphone_factory=microphone_factory,
        speaker_factory=(
            (lambda **_kwargs: FakeSpeakerAdapter(play_success=playback_success))
            if playback
            else lambda **_kwargs: pytest.fail(
                "speaker factory must remain unused without --playback"
            )
        ),
    )
    assert len(adapters) == 1
    return code, adapters[0], output, output_root


def test_parser_defaults_and_canonical_contract_are_explicit():
    args = manual.build_parser().parse_args([])

    assert args.microphone_device == "plughw:2,0"
    assert args.speaker_device == "plughw:CARD=Device,DEV=0"
    assert args.record_seconds == 4
    assert args.playback is False
    assert args.output_root == str(manual.DEFAULT_OUTPUT_ROOT)
    assert args.runtime_lock_path == str(manual.DEFAULT_RUNTIME_LOCK_PATH)
    assert canonical_pcm_contract() == {
        "sample_rate_hz": 16000,
        "channels": 1,
        "sample_format": "S16_LE",
        "sample_width_bytes": 2,
        "byte_order": "little",
        "signed": True,
        "frame_duration_ms": 20,
        "samples_per_frame": 320,
        "frame_bytes": 640,
    }
    assert CANONICAL_PCM_SAMPLES_PER_FRAME == 320
    assert CANONICAL_PCM_FRAME_BYTES == 640
    assert manual._frame_count(args.record_seconds) == 200


def test_stage_statistics_split_first_second_from_spoken_remainder():
    pcm = _diagnostic_pcm(record_seconds=3, silence_amplitude=4, spoken_amplitude=900)

    stages = manual._stage_statistics(pcm, 3)

    assert stages["room_silence"].byte_count == ONE_SECOND_BYTES
    assert stages["room_silence"].sample_count == CANONICAL_SAMPLE_RATE_HZ
    assert stages["room_silence"].rms == pytest.approx(4.0)
    assert stages["room_silence"].peak == 4
    assert stages["spoken"].byte_count == 2 * ONE_SECOND_BYTES
    assert stages["spoken"].sample_count == 2 * CANONICAL_SAMPLE_RATE_HZ
    assert stages["spoken"].rms == pytest.approx(900.0)
    assert stages["spoken"].peak == 900
    assert stages["all"].byte_count == 3 * ONE_SECOND_BYTES
    assert stages["all"].frame_count == manual._frame_count(3)
    assert manual._signal_changed(stages) is True


def test_stage_statistics_reject_duration_mismatch():
    with pytest.raises(ValueError, match="diagnostic_pcm_duration_mismatch"):
        manual._stage_statistics(_diagnostic_pcm(2)[:-2], 2)


def test_structural_report_accepts_complete_frames_despite_partial_syscall_reads():
    report = _statistics_report(_diagnostic_pcm(2))
    output: list[str] = []

    success = manual._render_report(report, output_func=output.append)

    assert success is True
    assert "  Structural PCM integrity: yes" in output
    assert "  partial_reads: 3" in output
    assert "  zero_filled_bytes: 0" in output
    assert "  Mutable source buffer reused across a boundary: no" in output


@pytest.mark.parametrize(
    ("counter", "value"),
    [
        ("valid_full_pcm_frames", 99),
        ("empty_reads", 1),
        ("read_errors", 1),
        ("zero_filled_bytes", 2),
        ("pending_partial_bytes", 1),
    ],
)
def test_structural_report_fails_closed_on_counter_invariant_violation(counter, value):
    report = _statistics_report(_diagnostic_pcm(2))
    report["persistent_stream_counters"][counter] = value

    assert manual._render_report(report, output_func=lambda _line: None) is False


def test_report_identifies_direct_valid_persistent_integrity_failure():
    report = _statistics_report(_diagnostic_pcm(2))
    report["persistent_stream_counters"]["empty_reads"] = 1
    output: list[str] = []

    assert manual._render_report(report, output_func=output.append) is False

    assert "  Direct path integrity: yes" in output
    assert "  Persistent path integrity: no" in output
    assert "  Direct valid while persistent is not: yes" in output


def test_structural_report_rejects_wrong_device_or_noncanonical_header():
    wrong_device = _statistics_report(_diagnostic_pcm(2))
    wrong_device["resolved_persistent_device"] = "plughw:3,0"
    bad_header = _statistics_report(_diagnostic_pcm(2))
    bad_header["direct_arecord_raw_wav_headers"][0]["sample_rate_hz"] = 8000

    assert manual._render_report(
        wrong_device,
        output_func=lambda _line: None,
    ) is False
    assert manual._render_report(
        bad_header,
        output_func=lambda _line: None,
    ) is False


def test_structural_report_rejects_one_repeated_persistent_stage():
    report = _statistics_report(_diagnostic_pcm(2))
    report["persistent"]["spoken"]["unique_frame_hash_count"] = 1
    report["persistent"]["spoken"]["repeated_frame_percentage"] = 100.0

    assert manual._render_report(report, output_func=lambda _line: None) is False


@pytest.mark.parametrize("stage_name", ["room_silence", "spoken"])
def test_structural_report_rejects_nearly_all_zero_persistent_stage(stage_name):
    report = _statistics_report(_diagnostic_pcm(2))
    report["persistent"][stage_name]["zero_sample_percentage"] = 99.99

    assert manual._render_report(report, output_func=lambda _line: None) is False


@pytest.mark.parametrize(
    "mutation",
    ["executable", "direct_type", "persistent_type", "persistent_stdout"],
)
def test_structural_report_rejects_mismatched_arecord_process_contract(mutation):
    report = _statistics_report(_diagnostic_pcm(2))
    if mutation == "executable":
        report["direct_arecord_commands"][0][0] = "/different/arecord"
    elif mutation == "direct_type":
        command = report["direct_arecord_commands"][0]
        command[command.index("-t") + 1] = "raw"
    elif mutation == "persistent_type":
        command = report["persistent_arecord_command"]
        command[command.index("-t") + 1] = "wav"
    else:
        report["persistent_arecord_command"][-1] = "persistent.wav"

    assert manual._render_report(report, output_func=lambda _line: None) is False


def test_signal_change_helpers_reject_unchanged_spoken_stage():
    unchanged = _diagnostic_pcm(
        record_seconds=2,
        silence_amplitude=4,
        spoken_amplitude=4,
    )
    stages = manual._stage_statistics(unchanged, 2)
    serialized = {name: value.to_dict() for name, value in stages.items()}

    assert manual._signal_changed(stages) is False
    assert manual._signal_changed_from_dict(serialized) is False
    assert manual._raw_bytes_nonzero(serialized) is True


def test_signal_change_helpers_reject_near_four_rms_as_unrealistic_speech():
    near_silent = _diagnostic_pcm(
        record_seconds=2,
        silence_amplitude=4,
        spoken_amplitude=5,
    )
    stages = manual._stage_statistics(near_silent, 2)
    serialized = {name: value.to_dict() for name, value in stages.items()}

    assert manual._signal_changed(stages) is False
    assert manual._signal_changed_from_dict(serialized) is False


def test_saved_wav_helper_preserves_exact_canonical_pcm(tmp_path):
    pcm = _diagnostic_pcm(2)
    path = tmp_path / "persistent.wav"
    _write_canonical_wav(path, pcm)

    loaded, validation = manual._read_canonical_wav_pcm(path)

    assert loaded == pcm
    assert validation["success"] is True
    assert validation["sample_rate_hz"] == CANONICAL_SAMPLE_RATE_HZ
    assert validation["channels"] == CANONICAL_CHANNELS
    assert validation["sample_width_bytes"] == CANONICAL_SAMPLE_WIDTH_BYTES
    assert validation["frames"] == 2 * CANONICAL_SAMPLE_RATE_HZ
    assert validation["duration_seconds"] == pytest.approx(2.0)


def test_saved_wav_helper_rejects_noncanonical_header(tmp_path):
    path = tmp_path / "noncanonical.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(_alternating_pcm(100, 8000))

    with pytest.raises(ValueError):
        manual._read_canonical_wav_pcm(path)


def test_factory_driven_diagnostic_saves_report_and_releases_microphone(tmp_path):
    pcm = _diagnostic_pcm(2)

    code, adapter, output, output_root = _run_with_fake_microphone(
        tmp_path,
        direct_pcm=pcm,
        persistent_pcm=pcm,
    )

    assert code == 0
    assert adapter.constructor_kwargs == {
        "device": "plughw:2,0",
        "record_seconds": 2,
        "sample_rate_hz": CANONICAL_SAMPLE_RATE_HZ,
        "channels": CANONICAL_CHANNELS,
        "sample_format": CANONICAL_PCM_SAMPLE_FORMAT,
        "timeout_seconds": 7.0,
    }
    assert adapter.calls[0] == "start"
    record_calls = [call for call in adapter.calls if isinstance(call, tuple) and call[0] == "record_wav"]
    assert [call[2]["seconds"] for call in record_calls] == [1, 1]
    assert all(call[2]["diagnostic_audio"] is True for call in record_calls)
    assert adapter.calls[-2:] == [
        ("close_persistent_stream", "persistent_pcm_diagnostic"),
        "stop",
    ]
    assert adapter.handle.closed is True
    run_directories = list(output_root.iterdir())
    assert len(run_directories) == 1
    run_directory = run_directories[0]
    persistent_wav = validate_canonical_wav(run_directory / "persistent_stream.wav")
    assert persistent_wav["success"] is True
    assert persistent_wav["duration_seconds"] == pytest.approx(2.0)
    report = json.loads(
        (run_directory / "pcm_integrity_report.json").read_text(encoding="utf-8")
    )
    assert report["success"] is True
    assert report["persistent_speech_signal_changed"] is True
    assert report["direct_speech_signal_changed"] is True
    assert len(report["direct_arecord_commands"]) == 2
    assert Path(report["files"]["direct_raw_room_silence_wav"]).exists()
    assert Path(report["files"]["direct_raw_spoken_wav"]).exists()
    assert report["canonical_pcm_contract"] == canonical_pcm_contract()
    assert report["persistent_stream_counters"]["valid_full_pcm_frames"] == 100
    assert any(line.startswith("PASS: persistent PCM") for line in output)
    assert not list(tmp_path.glob("*.lock"))


def test_factory_driven_unchanged_signal_returns_five_and_cleans_up(
    tmp_path,
):
    direct_pcm = _diagnostic_pcm(2)
    unchanged_persistent = _diagnostic_pcm(
        2,
        silence_amplitude=4,
        spoken_amplitude=4,
    )

    code, adapter, output, output_root = _run_with_fake_microphone(
        tmp_path,
        direct_pcm=direct_pcm,
        persistent_pcm=unchanged_persistent,
    )

    assert code == 5
    assert adapter.calls[-2:] == [
        ("close_persistent_stream", "persistent_pcm_diagnostic"),
        "stop",
    ]
    report_path = next(output_root.iterdir()) / "pcm_integrity_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["success"] is False
    assert report["persistent_speech_signal_changed"] is False
    assert any("Do not run wake reliability yet" in line for line in output)
    assert not list(tmp_path.glob("*.lock"))


def test_factory_driven_direct_signal_failure_also_blocks_success(tmp_path):
    unchanged_direct = _diagnostic_pcm(
        2,
        silence_amplitude=4,
        spoken_amplitude=5,
    )
    persistent_pcm = _diagnostic_pcm(2)

    code, adapter, output, output_root = _run_with_fake_microphone(
        tmp_path,
        direct_pcm=unchanged_direct,
        persistent_pcm=persistent_pcm,
    )

    assert code == 5
    assert adapter.calls[-1] == "stop"
    report = json.loads(
        (next(output_root.iterdir()) / "pcm_integrity_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["direct_speech_signal_changed"] is False
    assert report["persistent_speech_signal_changed"] is True
    assert report["success"] is False
    assert any("direct arecord spoken audio" in line for line in output)


def test_varied_but_near_silent_persistent_audio_cannot_pass(tmp_path):
    direct_pcm = _diagnostic_pcm(2)
    near_silent_persistent = _varying_near_silent_pcm(2)

    code, adapter, output, output_root = _run_with_fake_microphone(
        tmp_path,
        direct_pcm=direct_pcm,
        persistent_pcm=near_silent_persistent,
    )

    assert code == 5
    assert adapter.calls[-1] == "stop"
    report = json.loads(
        (next(output_root.iterdir()) / "pcm_integrity_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["persistent_speech_signal_changed"] is False
    assert report["success"] is False
    assert any("realistic diagnostic amplitude" in line for line in output)


@pytest.mark.parametrize("failure", ["close", "stop"])
def test_cleanup_failure_prevents_pass_and_still_attempts_adapter_stop(
    tmp_path,
    failure,
):
    pcm = _diagnostic_pcm(2)

    code, adapter, output, output_root = _run_with_fake_microphone(
        tmp_path,
        direct_pcm=pcm,
        persistent_pcm=pcm,
        close_failure=failure == "close",
        stop_failure=failure == "stop",
    )

    assert code == 3
    assert adapter.calls[-1] == "stop"
    assert not (next(output_root.iterdir()) / "pcm_integrity_report.json").exists()
    assert any("cleanup" in line or "stop_failed" in line for line in output)


class FakeSpeakerAdapter:
    def __init__(self, *, play_success: bool = True, **_kwargs):
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


def test_requested_playback_failure_is_reported_and_returns_false(tmp_path):
    speaker = FakeSpeakerAdapter(play_success=False)
    paths = (tmp_path / "direct.wav", tmp_path / "persistent.wav")

    success = manual._play_recordings(
        speaker_factory=lambda **_kwargs: speaker,
        speaker_device="plughw:CARD=Device,DEV=0",
        paths=paths,
        output_func=lambda _line: None,
    )

    assert success is False
    assert speaker.calls == [
        "start",
        ("play_wav", paths[0]),
        ("play_wav", paths[1]),
        "stop",
    ]


def test_requested_playback_failure_blocks_diagnostic_pass(tmp_path):
    pcm = _diagnostic_pcm(2)

    code, adapter, output, output_root = _run_with_fake_microphone(
        tmp_path,
        direct_pcm=pcm,
        persistent_pcm=pcm,
        playback=True,
        playback_success=False,
    )

    assert code == 6
    assert adapter.calls[-1] == "stop"
    report = json.loads(
        (next(output_root.iterdir()) / "pcm_integrity_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["playback_requested"] is True
    assert report["playback_success"] is False
    assert report["success"] is False
    assert any("audible comparison remains unconfirmed" in line for line in output)


def test_factory_read_error_preserves_counters_partial_wav_and_cleanup(tmp_path):
    pcm = _diagnostic_pcm(2)

    code, adapter, output, output_root = _run_with_fake_microphone(
        tmp_path,
        direct_pcm=pcm,
        persistent_pcm=pcm,
        fail_after_frames=3,
    )

    assert code == 3
    assert adapter.calls[-2:] == [
        ("close_persistent_stream", "persistent_pcm_diagnostic"),
        "stop",
    ]
    assert adapter.handle.closed is True
    assert any("injected_persistent_read_failure" in line for line in output)
    run_directory = next(output_root.iterdir())
    report = json.loads(
        (run_directory / "pcm_integrity_report.json").read_text(encoding="utf-8")
    )
    assert report["success"] is False
    assert report["failure_stage"] == "persistent_stream_capture"
    assert report["captured_persistent_frames"] == 3
    assert report["captured_persistent_bytes"] == 3 * CANONICAL_PCM_FRAME_BYTES
    assert report["persistent_stream_counters"]["read_errors"] == 1
    assert report["persistent_stream_counters"]["valid_full_pcm_frames"] == 3
    partial_path = Path(report["files"]["persistent_partial_wav"])
    assert partial_path.exists()
    assert validate_canonical_wav(partial_path)["duration_seconds"] == pytest.approx(
        0.06
    )
    assert "  read_errors: 1" in output
    assert not list(tmp_path.glob("*.lock"))
