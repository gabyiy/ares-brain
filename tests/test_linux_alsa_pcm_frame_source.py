from __future__ import annotations

from collections import deque
import struct
import subprocess
import time

import pytest

from core import (
    ContinuousPcmFrameSource,
    RmsVoiceActivityCapture,
    RollingPcmFrameSource,
    SafePcmStreamRunner,
    SubprocessPcmFrameSource,
    VAD_STATUS_COMPLETED_AFTER_SILENCE,
    VoiceActivityCaptureRequestV1,
)


FRAME_BYTES = 8
DESCRIPTOR = 73


def pcm_frame(*samples: int) -> bytes:
    assert len(samples) * 2 == FRAME_BYTES
    return struct.pack(f"<{len(samples)}h", *samples)


class FakePipe:
    def __init__(self, descriptor: int):
        self.descriptor = descriptor
        self.closed = False

    def fileno(self):
        return self.descriptor

    def read(self, maximum_bytes):
        if self.closed:
            raise ValueError("read of closed pipe")
        return b""

    def close(self):
        self.closed = True


class PayloadPipe(FakePipe):
    def __init__(self, descriptor: int, payload: bytes):
        super().__init__(descriptor)
        self.payload = bytes(payload)

    def read(self, maximum_bytes):
        if self.closed:
            raise ValueError("read of closed pipe")
        result = self.payload[:maximum_bytes]
        self.payload = self.payload[maximum_bytes:]
        return result


class FakeProcess:
    def __init__(self):
        self.stdout = FakePipe(DESCRIPTOR)
        self.stderr = FakePipe(DESCRIPTOR + 1)
        self.pid = 4321
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout):
        return self.returncode

    def kill(self):
        self.returncode = -9


class FakeProcessFactory:
    def __init__(self):
        self.process = FakeProcess()
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), dict(kwargs)))
        return self.process


class ScriptedRawReader:
    def __init__(self, chunks=()):
        self.chunks = deque(chunks)
        self.reads = []
        self.selects = []

    def append(self, chunk):
        self.chunks.append(chunk)

    def select(self, read, write, errors, timeout):
        self.selects.append((list(read), timeout))
        return (list(read) if self.chunks else [], [], [])

    def __call__(self, descriptor, maximum_bytes):
        self.reads.append((descriptor, maximum_bytes))
        chunk = self.chunks.popleft()
        if isinstance(chunk, BaseException):
            raise chunk
        if len(chunk) <= maximum_bytes:
            return chunk
        head = chunk[:maximum_bytes]
        self.chunks.appendleft(chunk[maximum_bytes:])
        return head


def make_source(reader: ScriptedRawReader):
    factory = FakeProcessFactory()
    source = SubprocessPcmFrameSource(
        ["/usr/bin/arecord", "-t", "raw", "-"],
        process_factory=factory,
        selector=reader.select,
        raw_reader=reader,
    )
    return source, factory


def wait_until(predicate, timeout_seconds=1.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def test_partial_reads_accumulate_into_one_exact_full_frame():
    expected = pcm_frame(1, -2, 300, -400)
    reader = ScriptedRawReader(
        [expected[:1], expected[1:4], expected[4:]],
    )
    source, _ = make_source(reader)

    result = source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    snapshot = source.snapshot()

    assert result == expected
    assert isinstance(result, bytes)
    assert reader.reads == [(DESCRIPTOR, 8), (DESCRIPTOR, 7), (DESCRIPTOR, 4)]
    assert snapshot["total_low_level_reads"] == 3
    assert snapshot["partial_reads"] == 2
    assert snapshot["accumulated_partial_bytes"] == 4
    assert snapshot["low_level_read_size_counts"] == {"1": 1, "3": 1, "4": 1}
    assert snapshot["valid_full_pcm_frames"] == 1
    assert snapshot["valid_microphone_bytes_delivered_to_vad"] == FRAME_BYTES
    assert snapshot["pending_partial_bytes"] == 0
    assert snapshot["zero_filled_bytes"] == 0


def test_empty_read_is_terminal_and_never_pads_or_advances_valid_frame_sequence():
    expected = pcm_frame(10, 20, 30, 40)
    reader = ScriptedRawReader([expected[:2], b""])
    source, _ = make_source(reader)

    with pytest.raises(EOFError, match="arecord_pcm_stream_ended"):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)

    incomplete = source.snapshot()
    assert incomplete["empty_reads"] == 1
    assert incomplete["pending_partial_bytes"] == 2
    assert incomplete["valid_full_pcm_frames"] == 0
    assert incomplete["valid_microphone_bytes_delivered_to_vad"] == 0
    assert incomplete["zero_filled_bytes"] == 0
    assert incomplete["stream_ended"] is True

    reader.append(expected[2:])
    with pytest.raises(EOFError, match="arecord_pcm_stream_ended"):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    assert source.discard_available(FRAME_BYTES) == 0

    terminal = source.snapshot()
    assert terminal["stream_ended"] is True
    assert terminal["total_low_level_reads"] == 2
    assert terminal["empty_reads"] == 1
    assert terminal["low_level_read_size_counts"] == {"2": 1, "0": 1}
    assert terminal["valid_full_pcm_frames"] == 0
    assert terminal["valid_microphone_bytes_delivered_to_vad"] == 0
    assert terminal["pending_partial_bytes"] == 2
    assert terminal["zero_filled_bytes"] == 0


def test_one_low_level_read_can_supply_several_frames_without_losing_leftovers():
    frames = [
        pcm_frame(1, 2, 3, 4),
        pcm_frame(5, 6, 7, 8),
        pcm_frame(9, 10, 11, 12),
    ]

    class OversizedReader(ScriptedRawReader):
        def __call__(self, descriptor, maximum_bytes):
            self.reads.append((descriptor, maximum_bytes))
            return self.chunks.popleft()

    reader = OversizedReader([b"".join(frames)])
    source, _ = make_source(reader)

    assert [
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
        for _ in frames
    ] == frames

    snapshot = source.snapshot()
    assert snapshot["total_low_level_reads"] == 1
    assert snapshot["valid_full_pcm_frames"] == 3
    assert snapshot["pending_partial_bytes"] == 0
    assert snapshot["low_level_read_size_counts"] == {str(FRAME_BYTES * 3): 1}


def test_odd_trailing_pcm_is_corruption_and_never_becomes_a_frame():
    reader = ScriptedRawReader([b"\x01", b""])
    source, factory = make_source(reader)
    factory.process.returncode = 0

    with pytest.raises(ValueError, match="odd_trailing_pcm_corruption:1"):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)

    snapshot = source.snapshot()
    assert snapshot["odd_trailing_byte_count"] == 1
    assert snapshot["unexpected_eof_count"] == 1
    assert snapshot["valid_full_pcm_frames"] == 0
    assert snapshot["pending_partial_bytes"] == 1
    assert snapshot["zero_filled_bytes"] == 0
    source.close()
    assert source.last_stop_result.active_failure_before_stop is True
    assert source.last_stop_result.final_health_effect == "unhealthy"


def test_zero_length_read_while_process_is_alive_is_not_clean_eof():
    reader = ScriptedRawReader([b""])
    source, _ = make_source(reader)

    with pytest.raises(
        RuntimeError,
        match="arecord_stdout_closed_while_process_alive",
    ):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)

    snapshot = source.snapshot()
    assert snapshot["process_alive"] is True
    assert snapshot["process_exit_status"] is None
    assert snapshot["eof_count"] == 1
    assert snapshot["low_level_read_size_counts"] == {"0": 1}
    assert snapshot["unexpected_eof_count"] == 1
    assert snapshot["dead_process_detected"] is False
    assert snapshot["terminal_reason"] == "stdout_closed_while_process_alive"
    assert snapshot["valid_full_pcm_frames"] == 0


def test_clean_eof_is_distinct_from_a_dead_arecord_process():
    reader = ScriptedRawReader([b""])
    source, factory = make_source(reader)
    factory.process.returncode = 0

    with pytest.raises(EOFError, match="arecord_pcm_stream_ended"):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)

    snapshot = source.snapshot()
    assert snapshot["process_exit_status"] == 0
    assert snapshot["process_alive"] is False
    assert snapshot["eof_count"] == 1
    assert snapshot["unexpected_eof_count"] == 0
    assert snapshot["dead_process_detected"] is False
    assert snapshot["terminal_reason"] == "clean_eof"
    assert snapshot["low_level_read_size_counts"] == {"0": 1}


def test_nonzero_arecord_exit_is_reported_as_a_dead_process():
    reader = ScriptedRawReader([b""])
    source, factory = make_source(reader)
    factory.process.returncode = 7

    with pytest.raises(RuntimeError, match="arecord_process_exited:7"):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)

    snapshot = source.snapshot()
    assert snapshot["process_exit_status"] == 7
    assert snapshot["process_alive"] is False
    assert snapshot["dead_process_detected"] is True
    assert snapshot["unexpected_eof_count"] == 1
    assert snapshot["terminal_reason"] == "arecord_process_exited"
    assert snapshot["low_level_read_size_counts"] == {"0": 1}
    assert snapshot["valid_full_pcm_frames"] == 0


def test_discard_removes_all_stale_audio_and_preserves_sample_alignment():
    warmup = pcm_frame(1, 2, 3, 4)
    stale_stream = pcm_frame(5, 6, 7, 8) + b"\x09\x00\x0a\x00\x0b"
    fresh_frame = pcm_frame(31, 32, 33, 34)
    continuation = b"\x00" + fresh_frame
    reader = ScriptedRawReader([warmup])
    source, _ = make_source(reader)
    assert source.read_frame(FRAME_BYTES, timeout_seconds=1.0) == warmup

    assert source.discard_available(FRAME_BYTES - 1) == 0
    assert reader.reads == [(DESCRIPTOR, FRAME_BYTES)]

    reader.append(stale_stream)
    discarded = source.discard_available(FRAME_BYTES * 2)
    after_discard = source.snapshot()

    assert discarded == len(stale_stream)
    assert after_discard["discarded_bytes"] == len(stale_stream)
    assert after_discard["pending_partial_bytes"] == 0
    assert after_discard["pending_discard_alignment_bytes"] == 1
    assert after_discard["valid_full_pcm_frames"] == 1
    assert after_discard["valid_microphone_bytes_delivered_to_vad"] == FRAME_BYTES

    reader.append(continuation)
    assert source.read_frame(FRAME_BYTES, timeout_seconds=1.0) == fresh_frame
    final = source.snapshot()
    assert final["valid_full_pcm_frames"] == 2
    assert final["valid_microphone_bytes_delivered_to_vad"] == FRAME_BYTES * 2
    assert final["pending_partial_bytes"] == 0
    assert final["pending_discard_alignment_bytes"] == 0
    assert final["discarded_bytes"] == len(stale_stream) + 1


def test_reused_mutable_reader_buffer_is_copied_before_later_mutation():
    first_payload = pcm_frame(100, 200, 300, 400)
    second_payload = pcm_frame(-100, -200, -300, -400)
    shared_buffer = bytearray(first_payload)
    reader = ScriptedRawReader([shared_buffer, shared_buffer])
    source, _ = make_source(reader)

    first = source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    shared_buffer[:] = second_payload
    second = source.read_frame(FRAME_BYTES, timeout_seconds=1.0)

    assert first == first_payload
    assert second == second_payload
    assert isinstance(first, bytes)
    assert isinstance(second, bytes)
    assert source.snapshot()["mutable_buffer_reuse_detected"] == 1


def test_repeated_hash_counter_tracks_only_adjacent_returned_frames():
    first = pcm_frame(1, 1, 1, 1)
    second = pcm_frame(2, 2, 2, 2)
    frames = [first, first, second, second, second, first]
    reader = ScriptedRawReader(frames)
    source, _ = make_source(reader)

    delivered = [
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
        for _ in frames
    ]
    snapshot = source.snapshot()

    assert delivered == frames
    assert snapshot["repeated_frame_hashes"] == 3
    assert snapshot["valid_full_pcm_frames"] == len(frames)
    assert snapshot["valid_microphone_bytes_delivered_to_vad"] == len(
        b"".join(frames)
    )
    assert snapshot["zero_filled_bytes"] == 0


def test_continuous_pump_detects_pathological_repeated_non_silent_frames():
    repeated = pcm_frame(100, -100, 100, -100)
    reader = ScriptedRawReader([repeated] * 4)
    raw_source, _ = make_source(reader)
    pump = ContinuousPcmFrameSource(
        raw_source,
        expected_frame_bytes=FRAME_BYTES,
        maximum_queue_frames=8,
        pathological_duplicate_frames=3,
        capture_stderr=False,
    )
    try:
        wait_until(
            lambda: pump.snapshot()[
                "pathological_duplicate_frame_detected"
            ]
        )
        snapshot = pump.snapshot()

        assert snapshot["repeated_frame_hashes"] == 2
        assert snapshot[
            "maximum_consecutive_repeated_non_silent_frames"
        ] == 3
        assert snapshot["pathological_duplicate_frame_detected"] is True
        assert snapshot["integrity_failure_count"] == 1
        assert snapshot["integrity_guard_discarded_frames"] == 2
        assert snapshot["queue_depth_frames"] == 0
        assert snapshot["stream_ended"] is True
        assert (
            snapshot["terminal_reason"]
            == "pathological_repeated_non_silent_pcm"
        )
        assert snapshot["maximum_consecutive_tiny_rms_frames"] == 0
        with pytest.raises(
            RuntimeError,
            match="pathological_repeated_non_silent_pcm",
        ):
            pump.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    finally:
        pump.close()


def test_continuous_pump_allows_repeated_exact_silence_frames():
    silence = b"\x00" * FRAME_BYTES
    reader = ScriptedRawReader([silence] * 4)
    raw_source, _ = make_source(reader)
    pump = ContinuousPcmFrameSource(
        raw_source,
        expected_frame_bytes=FRAME_BYTES,
        maximum_queue_frames=8,
        pathological_duplicate_frames=3,
        capture_stderr=False,
    )
    try:
        wait_until(lambda: pump.snapshot()["valid_full_pcm_frames"] == 4)
        snapshot = pump.snapshot()

        assert snapshot["repeated_frame_hashes"] == 3
        assert snapshot[
            "maximum_consecutive_repeated_non_silent_frames"
        ] == 0
        assert snapshot["pathological_duplicate_frame_detected"] is False
        assert snapshot["integrity_failure_count"] == 0
        assert [
            pump.read_frame(FRAME_BYTES, timeout_seconds=1.0)
            for _ in range(4)
        ] == [silence] * 4
    finally:
        pump.close()


def test_continuous_pump_tracks_long_tiny_rms_runs_without_promoting_audio():
    tiny_frames = [
        pcm_frame(1, -1, 2, -2),
        pcm_frame(2, -2, 3, -3),
        pcm_frame(3, -3, 4, -4),
        pcm_frame(4, -4, 5, -5),
    ]
    reader = ScriptedRawReader(tiny_frames)
    raw_source, _ = make_source(reader)
    pump = ContinuousPcmFrameSource(
        raw_source,
        expected_frame_bytes=FRAME_BYTES,
        maximum_queue_frames=8,
        tiny_rms=8.0,
        capture_stderr=False,
    )
    try:
        wait_until(lambda: pump.snapshot()["valid_full_pcm_frames"] == 4)
        snapshot = pump.snapshot()

        assert snapshot["maximum_consecutive_tiny_rms_frames"] == 4
        assert snapshot["pathological_duplicate_frame_detected"] is False
        assert snapshot["zero_filled_bytes"] == 0
    finally:
        pump.close()


def test_continuous_pump_queue_owns_mutable_source_bytes():
    original = pcm_frame(100, 200, 300, 400)
    shared = bytearray(original)
    reader = ScriptedRawReader([shared])
    raw_source, _ = make_source(reader)
    pump = ContinuousPcmFrameSource(
        raw_source,
        expected_frame_bytes=FRAME_BYTES,
        maximum_queue_frames=3,
        capture_stderr=False,
    )
    try:
        wait_until(lambda: pump.snapshot()["queue_depth_frames"] == 1)
        shared[:] = pcm_frame(-1, -2, -3, -4)

        delivered = pump.read_frame(FRAME_BYTES, timeout_seconds=1.0)
        assert delivered == original
        assert isinstance(delivered, bytes)
    finally:
        pump.close()


def test_continuous_pump_drains_during_consumer_idle_and_keeps_only_recent_frames():
    frames = [
        pcm_frame(value, value, value, value)
        for value in range(1, 11)
    ]
    reader = ScriptedRawReader(frames)
    raw_source, _ = make_source(reader)
    pump = ContinuousPcmFrameSource(
        raw_source,
        expected_frame_bytes=FRAME_BYTES,
        maximum_queue_frames=3,
        capture_stderr=False,
    )
    try:
        wait_until(lambda: pump.snapshot()["valid_full_pcm_frames"] == len(frames))
        idle_snapshot = pump.snapshot()

        assert idle_snapshot["total_low_level_reads"] == len(frames)
        assert idle_snapshot["queue_depth_frames"] == 3
        assert idle_snapshot["queue_overflow_dropped_frames"] == 7
        assert idle_snapshot["queue_overflow_dropped_bytes"] == 7 * FRAME_BYTES
        assert [
            pump.read_frame(FRAME_BYTES, timeout_seconds=1.0)
            for _ in range(3)
        ] == frames[-3:]
        assert pump.snapshot()["valid_pcm_frames_delivered_to_vad"] == 3
    finally:
        pump.close()


def test_candidate_reset_removes_idle_backlog_before_new_live_frame():
    stale_frames = [
        pcm_frame(value, value, value, value)
        for value in (10, 20, 30)
    ]
    fresh_frame = pcm_frame(900, -900, 700, -700)
    reader = ScriptedRawReader(stale_frames)
    raw_source, _ = make_source(reader)
    pump = ContinuousPcmFrameSource(
        raw_source,
        expected_frame_bytes=FRAME_BYTES,
        maximum_queue_frames=4,
        capture_stderr=False,
    )
    try:
        wait_until(lambda: pump.snapshot()["queue_depth_frames"] == 3)
        discarded = pump.discard_available(FRAME_BYTES * 4)
        reader.append(fresh_frame)

        assert discarded == len(b"".join(stale_frames))
        assert pump.read_frame(FRAME_BYTES, timeout_seconds=1.0) == fresh_frame
        snapshot = pump.snapshot()
        assert snapshot["candidate_reset_discarded_frames"] == 3
        assert snapshot["candidate_reset_discarded_bytes"] == 3 * FRAME_BYTES
        assert snapshot["queue_overflow_dropped_frames"] == 0
    finally:
        pump.close()


def test_safe_pcm_stream_runner_wraps_one_arecord_process_in_continuous_pump(
    monkeypatch,
):
    reader = ScriptedRawReader([b"\x01\x00" * 320])
    raw_source, factory = make_source(reader)

    monkeypatch.setattr(
        "core.LinuxAlsaMicrophone.SubprocessPcmFrameSource",
        lambda args: raw_source,
    )
    runner = SafePcmStreamRunner()
    pump = runner.start(
        [
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
        ]
    )
    try:
        assert isinstance(pump, ContinuousPcmFrameSource)
        assert pump.read_frame(640, timeout_seconds=1.0) == b"\x01\x00" * 320
        snapshot = pump.snapshot()
        assert snapshot["process_pid"] == factory.process.pid
        assert snapshot["process_exit_status"] is None
        assert snapshot["process_alive"] is True
        assert snapshot["process_liveness_observable"] is True
        assert snapshot["stdout_transport_mode"] == "raw_pcm_pipe_continuous_pump"
        assert snapshot["stderr_transport_mode"] == "separate_bounded_pipe"
    finally:
        pump.close()


def test_continuous_pump_drains_stderr_without_mixing_it_into_pcm():
    pcm = pcm_frame(1200, -1200, 600, -600)
    pcm_reader = ScriptedRawReader([pcm])
    stderr_payload = b"arecord: harmless diagnostic on stderr\n"
    stderr_reader = ScriptedRawReader([stderr_payload])
    raw_source, _ = make_source(pcm_reader)
    pump = ContinuousPcmFrameSource(
        raw_source,
        expected_frame_bytes=FRAME_BYTES,
        maximum_queue_frames=3,
        stderr_selector=stderr_reader.select,
        stderr_raw_reader=stderr_reader,
    )
    try:
        wait_until(
            lambda: pump.snapshot()["stderr_bytes_captured"]
            == len(stderr_payload)
        )

        assert pump.read_frame(FRAME_BYTES, timeout_seconds=1.0) == pcm
        snapshot = pump.snapshot()
        assert snapshot["stderr_preview"] == stderr_payload.decode("utf-8")
        assert snapshot["stderr_transport_mode"] == "separate_bounded_pipe"
        assert snapshot["valid_full_pcm_frames"] == 1
        assert snapshot["fresh_microphone_bytes_delivered_to_vad"] == len(pcm)
        assert stderr_payload not in pcm
    finally:
        pump.close()


def test_valid_byte_delivery_counts_frames_not_discarded_stale_bytes():
    first = pcm_frame(11, 12, 13, 14)
    discarded = pcm_frame(21, 22, 23, 24)
    second = pcm_frame(31, 32, 33, 34)
    reader = ScriptedRawReader([first])
    source, _ = make_source(reader)

    assert source.read_frame(FRAME_BYTES, timeout_seconds=1.0) == first
    reader.append(discarded)
    assert source.discard_available(FRAME_BYTES) == FRAME_BYTES
    reader.append(second)
    assert source.read_frame(FRAME_BYTES, timeout_seconds=1.0) == second

    snapshot = source.snapshot()
    assert snapshot["discarded_bytes"] == FRAME_BYTES
    assert snapshot["valid_full_pcm_frames"] == 2
    assert snapshot["valid_microphone_bytes_delivered_to_vad"] == (
        len(first) + len(second)
    )
    assert snapshot["total_low_level_reads"] == 3
    assert snapshot["zero_filled_bytes"] == 0


def test_read_error_is_reported_without_advancing_valid_frame_counters():
    reader = ScriptedRawReader([OSError("alsa read failed")])
    source, _ = make_source(reader)

    with pytest.raises(OSError, match="alsa read failed"):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)

    snapshot = source.snapshot()
    assert snapshot["total_low_level_reads"] == 1
    assert snapshot["read_errors"] == 1
    assert snapshot["valid_full_pcm_frames"] == 0
    assert snapshot["valid_microphone_bytes_delivered_to_vad"] == 0
    assert snapshot["zero_filled_bytes"] == 0


def test_selector_error_is_counted_without_advancing_low_level_or_valid_reads():
    reader = ScriptedRawReader()
    source, _ = make_source(reader)

    def fail_select(read, write, errors, timeout):
        raise OSError("selector failed")

    source.selector = fail_select

    with pytest.raises(OSError, match="selector failed"):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)

    snapshot = source.snapshot()
    assert snapshot["read_errors"] == 1
    assert snapshot["total_low_level_reads"] == 0
    assert snapshot["valid_full_pcm_frames"] == 0
    assert snapshot["valid_microphone_bytes_delivered_to_vad"] == 0
    assert snapshot["pending_partial_bytes"] == 0


def test_discard_selector_error_restores_pending_partial_bytes():
    pending = b"\x01\x02\x03"
    reader = ScriptedRawReader([pending])
    source, _ = make_source(reader)

    with pytest.raises(TimeoutError, match="pcm_frame_read_timeout"):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    assert source.snapshot()["pending_partial_bytes"] == len(pending)

    def fail_select(read, write, errors, timeout):
        raise OSError("discard selector failed")

    source.selector = fail_select
    with pytest.raises(OSError, match="discard selector failed"):
        source.discard_available(FRAME_BYTES)

    snapshot = source.snapshot()
    assert snapshot["read_errors"] == 1
    assert snapshot["discarded_bytes"] == 0
    assert snapshot["pending_partial_bytes"] == len(pending)
    assert snapshot["valid_full_pcm_frames"] == 0


def test_discard_copy_error_restores_pending_partial_bytes(monkeypatch):
    pending = b"\x01\x02\x03"
    reader = ScriptedRawReader([pending])
    source, _ = make_source(reader)

    with pytest.raises(TimeoutError, match="pcm_frame_read_timeout"):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    reader.append(bytearray(b"\x04\x05\x06\x07\x08"))

    def fail_copy(value):
        raise ValueError("mutable PCM copy failed")

    monkeypatch.setattr(source, "_copy_source_bytes", fail_copy)
    with pytest.raises(ValueError, match="mutable PCM copy failed"):
        source.discard_available(FRAME_BYTES)

    snapshot = source.snapshot()
    assert snapshot["read_errors"] == 1
    assert snapshot["discarded_bytes"] == 0
    assert snapshot["pending_partial_bytes"] == len(pending)
    assert snapshot["valid_full_pcm_frames"] == 0


def test_discard_resets_repeated_frame_hash_continuity():
    delivered = pcm_frame(100, 200, 300, 400)
    stale = pcm_frame(500, 600, 700, 800)
    reader = ScriptedRawReader([delivered])
    source, _ = make_source(reader)

    assert source.read_frame(FRAME_BYTES, timeout_seconds=1.0) == delivered
    reader.append(stale)
    assert source.discard_available(FRAME_BYTES) == FRAME_BYTES
    reader.append(delivered)
    assert source.read_frame(FRAME_BYTES, timeout_seconds=1.0) == delivered

    snapshot = source.snapshot()
    assert snapshot["repeated_frame_hashes"] == 0
    assert snapshot["valid_full_pcm_frames"] == 2
    assert snapshot["discarded_bytes"] == FRAME_BYTES


def test_rolling_source_pairs_replay_and_live_frame_and_byte_counters():
    first = pcm_frame(1, 2, 3, 4)
    second = pcm_frame(5, 6, 7, 8)
    reader = ScriptedRawReader([first, second])
    low_level, _ = make_source(reader)
    rolling = RollingPcmFrameSource(
        low_level,
        maximum_history_frames=2,
        expected_frame_bytes=FRAME_BYTES,
    )

    assert rolling.read_frame(FRAME_BYTES, timeout_seconds=1.0) == first
    assert rolling.begin_window(1) == 1
    assert rolling.read_frame(FRAME_BYTES, timeout_seconds=1.0) == first
    assert rolling.read_frame(FRAME_BYTES, timeout_seconds=1.0) == second

    snapshot = rolling.snapshot()
    assert snapshot["read_sequence"] == 3
    assert snapshot["live_frame_count"] == 2
    assert snapshot["replayed_frame_count"] == 1
    assert snapshot["total_bytes_returned"] == 3 * FRAME_BYTES
    assert snapshot["total_live_bytes_read"] == 2 * FRAME_BYTES
    assert snapshot["valid_full_pcm_frames"] == 2
    assert snapshot["valid_pcm_frames_delivered_to_vad"] == 3
    assert snapshot["fresh_full_pcm_frames"] == 2
    assert snapshot["valid_microphone_bytes_delivered_to_vad"] == 3 * FRAME_BYTES
    assert snapshot["fresh_microphone_bytes_delivered_to_vad"] == 2 * FRAME_BYTES


def test_direct_subprocess_source_exposes_exact_vad_observability_aliases(tmp_path):
    samples_per_frame = 320

    def canonical_frame(amplitude: int) -> bytes:
        return struct.pack("<h", amplitude) * samples_per_frame

    frames = [
        canonical_frame(400),
        canonical_frame(450),
        *([canonical_frame(20)] * 5),
    ]
    reader = ScriptedRawReader(frames)
    source, _ = make_source(reader)
    detector = RmsVoiceActivityCapture()
    detector.start()

    result = detector.execute(
        VoiceActivityCaptureRequestV1(
            output_wav_path=str(tmp_path / "direct-source.wav"),
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
        source,
    )

    assert result.status == VAD_STATUS_COMPLETED_AFTER_SILENCE
    assert result.data["source_observability_available"] is True
    assert result.data["source_frames_read_delta"] == len(frames)
    assert result.data["source_live_frames_read_delta"] == len(frames)
    assert result.data["source_bytes_read_delta"] == len(frames) * 640
    assert result.data["source_live_bytes_read_delta"] == len(frames) * 640
    assert result.data["total_low_level_reads"] == len(frames)
    assert result.data["valid_full_pcm_frames"] == len(frames)
    assert result.data["valid_microphone_bytes_delivered_to_vad"] == (
        len(frames) * 640
    )


def test_rolling_wrapper_error_counter_is_not_masked_by_low_level_zero():
    class StaleSnapshotSource:
        def read_frame(self, frame_bytes, timeout_seconds):
            raise OSError("wrapper-visible failure")

        def snapshot(self):
            return {"read_errors": 0}

        def close(self):
            return None

    rolling = RollingPcmFrameSource(
        StaleSnapshotSource(),
        expected_frame_bytes=FRAME_BYTES,
    )

    with pytest.raises(OSError, match="wrapper-visible failure"):
        rolling.read_frame(FRAME_BYTES, timeout_seconds=1.0)

    snapshot = rolling.snapshot()
    assert snapshot["read_errors"] == 1
    assert snapshot["process_liveness_observable"] is False
    assert snapshot["process_alive"] is True


def test_close_uses_kill_fallback_when_terminate_fails_and_closes_pipes():
    class TerminateFailureProcess(FakeProcess):
        def __init__(self):
            super().__init__()
            self.terminate_count = 0
            self.kill_count = 0

        def terminate(self):
            self.terminate_count += 1
            raise OSError("terminate failed")

        def wait(self, timeout):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("arecord", timeout)
            return self.returncode

        def kill(self):
            self.kill_count += 1
            self.returncode = -9

    process = TerminateFailureProcess()
    source = SubprocessPcmFrameSource(
        ["/usr/bin/arecord", "-t", "raw", "-"],
        process_factory=lambda _args, **_kwargs: process,
    )

    source.close()

    assert source.closed is True
    assert process.terminate_count == 1
    assert process.kill_count == 1
    assert process.returncode == -9
    assert process.stdout.closed is True
    assert process.stderr.closed is True


def test_unreaped_process_keeps_source_retryable_and_reports_cleanup_failure():
    class UnreapableProcess(FakeProcess):
        def __init__(self):
            super().__init__()
            self.allow_cleanup = False

        def terminate(self):
            if not self.allow_cleanup:
                raise OSError("terminate failed")
            self.returncode = 0

        def wait(self, timeout):
            if not self.allow_cleanup:
                raise subprocess.TimeoutExpired("arecord", timeout)
            return self.returncode

        def kill(self):
            if not self.allow_cleanup:
                raise OSError("kill failed")
            self.returncode = -9

    process = UnreapableProcess()
    source = SubprocessPcmFrameSource(
        ["/usr/bin/arecord", "-t", "raw", "-"],
        process_factory=lambda _args, **_kwargs: process,
    )

    with pytest.raises(RuntimeError, match="arecord_pcm_stream_cleanup_failed"):
        source.close()

    assert source.closed is False
    assert process.stdout.closed is True
    assert process.stderr.closed is True
    failed_stop = source.last_stop_result
    assert failed_stop.process_reaped is False
    assert failed_stop.cleanup_completed is False
    assert failed_stop.unexpected_failure is True
    assert failed_stop.status == "cleanup_incomplete"
    assert failed_stop.final_health_effect == "unhealthy"

    process.allow_cleanup = True
    source.close()

    assert source.closed is True
    assert process.returncode == 0


def test_valid_pcm_then_intentional_arecord_interrupted_exit_is_controlled():
    class InterruptedStopProcess(FakeProcess):
        def __init__(self):
            super().__init__()
            self.stderr = PayloadPipe(
                DESCRIPTOR + 1,
                b"arecord: pcm_read:2272: read error: Interrupted system call\n",
            )

        def terminate(self):
            self.returncode = 1

    process = InterruptedStopProcess()
    reader = ScriptedRawReader([pcm_frame(100, -200, 300, -400)])
    source = SubprocessPcmFrameSource(
        ["/usr/bin/arecord", "-t", "raw", "-"],
        process_factory=lambda _args, **_kwargs: process,
        selector=reader.select,
        raw_reader=reader,
    )

    assert source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    source.close()

    stopped = source.snapshot()["controlled_stop"]
    assert stopped["stop_requested"] is True
    assert stopped["valid_pcm_received"] is True
    assert stopped["child_exit_code"] == 1
    assert stopped["child_signal"] is None
    assert stopped["process_reaped"] is True
    assert stopped["cleanup_completed"] is True
    assert stopped["unexpected_failure"] is False
    assert stopped["status"] == "controlled_stop"
    assert stopped["final_health_effect"] == "none"
    assert "Interrupted system call" in stopped["stderr"]


def test_valid_pcm_then_intentional_sigterm_is_controlled():
    class SigtermStopProcess(FakeProcess):
        def terminate(self):
            self.returncode = -15

    process = SigtermStopProcess()
    reader = ScriptedRawReader([pcm_frame(10, 20, 30, 40)])
    source = SubprocessPcmFrameSource(
        ["/usr/bin/arecord", "-t", "raw", "-"],
        process_factory=lambda _args, **_kwargs: process,
        selector=reader.select,
        raw_reader=reader,
    )

    source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    source.close()

    stopped = source.last_stop_result
    assert stopped.child_exit_code == -15
    assert stopped.child_signal == 15
    assert stopped.termination_signal_requested == "SIGTERM"
    assert stopped.status == "controlled_stop"
    assert stopped.final_health_effect == "none"


def test_valid_pcm_then_intentional_sigint_is_controlled():
    class SigintStopProcess(FakeProcess):
        def terminate(self):
            self.returncode = -2

    process = SigintStopProcess()
    reader = ScriptedRawReader([pcm_frame(10, 20, 30, 40)])
    source = SubprocessPcmFrameSource(
        ["/usr/bin/arecord", "-t", "raw", "-"],
        process_factory=lambda _args, **_kwargs: process,
        selector=reader.select,
        raw_reader=reader,
    )

    source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    source.close()

    assert source.last_stop_result.child_signal == 2
    assert source.last_stop_result.status == "controlled_stop"
    assert source.last_stop_result.final_health_effect == "none"


def test_valid_pcm_sigkill_escalation_is_degraded_but_reusable_when_reaped():
    class KillEscalationProcess(FakeProcess):
        def terminate(self):
            return None

        def wait(self, timeout):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("arecord", timeout)
            return self.returncode

    class SteppingClock:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            self.value += 1.0
            return self.value

    process = KillEscalationProcess()
    reader = ScriptedRawReader([pcm_frame(11, 22, 33, 44)])
    source = SubprocessPcmFrameSource(
        ["/usr/bin/arecord", "-t", "raw", "-"],
        process_factory=lambda _args, **_kwargs: process,
        selector=reader.select,
        raw_reader=reader,
    )
    source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    source.clock = SteppingClock()

    source.close()

    stopped = source.last_stop_result
    assert stopped.child_exit_code == -9
    assert stopped.child_signal == 9
    assert stopped.termination_escalated is True
    assert stopped.process_reaped is True
    assert stopped.unexpected_failure is False
    assert stopped.status == "controlled_stop_degraded"
    assert stopped.final_health_effect == "degraded_reusable"

    next_reader = ScriptedRawReader([pcm_frame(55, 66, 77, 88)])
    next_source, _ = make_source(next_reader)
    assert next_source.read_frame(FRAME_BYTES, timeout_seconds=1.0) == pcm_frame(
        55, 66, 77, 88
    )
    next_source.close()
    assert next_source.last_stop_result.unexpected_failure is False


def test_exit_one_before_valid_pcm_remains_an_active_transport_failure():
    reader = ScriptedRawReader([b""])
    source, factory = make_source(reader)
    factory.process.returncode = 1

    with pytest.raises(RuntimeError, match="arecord_process_exited:1"):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    source.close()

    stopped = source.last_stop_result
    assert stopped.valid_pcm_received is False
    assert stopped.active_failure_before_stop is True
    assert stopped.unexpected_failure is True
    assert stopped.status == "unexpected_failure"
    assert stopped.final_health_effect == "unhealthy"


def test_interrupted_system_call_during_active_read_is_not_controlled_cleanup():
    error = OSError("Interrupted system call")
    reader = ScriptedRawReader([error])
    source, _ = make_source(reader)

    with pytest.raises(OSError, match="Interrupted system call"):
        source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    source.close()

    stopped = source.last_stop_result
    assert stopped.valid_pcm_received is False
    assert stopped.active_failure_before_stop is True
    assert stopped.unexpected_failure is True
    assert stopped.final_health_effect == "unhealthy"


def test_exit_one_with_unrelated_stderr_is_not_hidden_by_stop_request():
    class FailedStopProcess(FakeProcess):
        def __init__(self):
            super().__init__()
            self.stderr = PayloadPipe(
                DESCRIPTOR + 1,
                b"arecord: pcm_read:2272: read error: Input/output error\n",
            )

        def terminate(self):
            self.returncode = 1

    process = FailedStopProcess()
    reader = ScriptedRawReader([pcm_frame(10, 20, 30, 40)])
    source = SubprocessPcmFrameSource(
        ["/usr/bin/arecord", "-t", "raw", "-"],
        process_factory=lambda _args, **_kwargs: process,
        selector=reader.select,
        raw_reader=reader,
    )

    source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    source.close()

    stopped = source.last_stop_result
    assert stopped.child_exit_code == 1
    assert stopped.valid_pcm_received is True
    assert stopped.unexpected_failure is True
    assert stopped.status == "unexpected_failure"
    assert stopped.final_health_effect == "unhealthy"
    assert "Input/output error" in stopped.stderr


def test_exit_one_with_mixed_interrupted_and_real_error_stderr_is_not_controlled():
    class MixedErrorProcess(FakeProcess):
        def __init__(self):
            super().__init__()
            self.stderr = PayloadPipe(
                DESCRIPTOR + 1,
                (
                    b"arecord: pcm_read:2272: read error: Interrupted system call\n"
                    b"arecord: pcm_read:2272: read error: Input/output error\n"
                ),
            )

        def terminate(self):
            self.returncode = 1

    process = MixedErrorProcess()
    reader = ScriptedRawReader([pcm_frame(10, 20, 30, 40)])
    source = SubprocessPcmFrameSource(
        ["/usr/bin/arecord", "-t", "raw", "-"],
        process_factory=lambda _args, **_kwargs: process,
        selector=reader.select,
        raw_reader=reader,
    )

    source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    source.close()

    assert source.last_stop_result.unexpected_failure is True
    assert source.last_stop_result.final_health_effect == "unhealthy"


def test_unrelated_negative_signal_is_not_treated_as_controlled_shutdown():
    class SegfaultProcess(FakeProcess):
        def terminate(self):
            self.returncode = -11

    process = SegfaultProcess()
    reader = ScriptedRawReader([pcm_frame(10, 20, 30, 40)])
    source = SubprocessPcmFrameSource(
        ["/usr/bin/arecord", "-t", "raw", "-"],
        process_factory=lambda _args, **_kwargs: process,
        selector=reader.select,
        raw_reader=reader,
    )

    source.read_frame(FRAME_BYTES, timeout_seconds=1.0)
    source.close()

    assert source.last_stop_result.child_signal == 11
    assert source.last_stop_result.unexpected_failure is True
    assert source.last_stop_result.final_health_effect == "unhealthy"
