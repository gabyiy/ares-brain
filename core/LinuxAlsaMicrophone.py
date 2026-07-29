from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
import errno
import hashlib
import math
import os
import re
import select
import signal
import shutil
import subprocess
import tempfile
from threading import Condition, Event, RLock, Thread
import time
import traceback
import wave

from core.BoundedSubprocess import BoundedProcessRunner
from typing import Any, Callable, Dict, List, Optional, Sequence

from core.Contracts import VoiceActivityCaptureRequestV1, VoiceActivityCaptureResultV1
from core.Microphone import AudioChunk, CancelCheck, MicrophoneAdapter, MicrophoneResult
from core.PcmIntegrity import (
    CANONICAL_PCM_FRAME_BYTES,
    CANONICAL_PCM_FRAME_DURATION_MS,
    CANONICAL_PCM_SAMPLE_FORMAT,
    CANONICAL_PCM_SAMPLES_PER_FRAME,
    calculate_s16_le_rms,
    canonical_pcm_contract,
    decode_s16_le_samples,
)
from core.VoiceActivityDetection import (
    RmsVoiceActivityCapture,
    VAD_STATUS_DEVICE_ERROR,
    VAD_STATUS_INVALID_AUDIO,
    VAD_STATUS_TIMEOUT,
    VoiceActivityStreamCalibration,
    validate_voice_activity_request,
)
from core.WavAudio import (
    CANONICAL_CHANNELS,
    CANONICAL_SAMPLE_RATE_HZ,
    CANONICAL_SAMPLE_WIDTH_BYTES,
    analyze_wav_audio,
    normalize_wav_audio,
    pcm_frame_sample_count,
    read_audio_chunk_wav,
    validate_duration_invariant,
    validate_canonical_wav,
    write_audio_chunk_wav,
)


DEFAULT_ALSA_SAMPLE_RATE_HZ = 16000
DEFAULT_ALSA_CHANNELS = 1
DEFAULT_ALSA_SAMPLE_FORMAT = CANONICAL_PCM_SAMPLE_FORMAT
DEFAULT_ALSA_RECORD_SECONDS = 3
DEFAULT_ALSA_TIMEOUT_PADDING_SECONDS = 5
MAX_ALSA_RECORD_SECONDS = 60
MAX_ALSA_TIMEOUT_SECONDS = 120
DEFAULT_PCM_PUMP_QUEUE_FRAMES = 50
DEFAULT_PCM_PUMP_READ_TIMEOUT_SECONDS = 0.10
DEFAULT_PCM_NON_SILENT_RMS = 20.0
DEFAULT_PCM_PATHOLOGICAL_DUPLICATE_FRAMES = 10
DEFAULT_PCM_TINY_RMS = 8.0

ALSA_STATUS_ARECORD_MISSING = "arecord_missing"
ALSA_STATUS_NO_CAPTURE_DEVICE = "no_capture_device"
ALSA_STATUS_INVALID_DEVICE = "invalid_device"
ALSA_STATUS_RECORDING_TIMEOUT = "recording_timeout"
ALSA_STATUS_RECORDING_FAILED = "recording_failed"
ALSA_STATUS_OUTPUT_MISSING = "wav_output_missing"
ALSA_STATUS_OUTPUT_EMPTY = "wav_output_empty"
ALSA_STATUS_INVALID_WAV = "invalid_wav_output"


@dataclass(frozen=True)
class SafeProcessResult:
    args: List[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "args": list(self.args),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "error_message": self.error_message,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PcmStreamStopResult:
    """Truthful outcome of one explicit raw-PCM transport stop.

    A child exit code is not sufficient to decide whether ALSA failed.  In
    particular, ``arecord`` may report an interrupted blocking read after ARES
    deliberately terminates it.  This result keeps the control-plane evidence
    beside the raw process outcome so callers can distinguish that expected
    stop from a transport failure which happened while capture was active.
    """

    stop_requested: bool = False
    valid_pcm_received: bool = False
    valid_full_pcm_frames: int = 0
    child_exit_code: Optional[int] = None
    child_signal: Optional[int] = None
    termination_signal_requested: str = ""
    termination_escalated: bool = False
    stderr: str = ""
    process_reaped: bool = False
    cleanup_completed: bool = False
    active_failure_before_stop: bool = False
    unexpected_ownership_loss: bool = False
    unexpected_failure: bool = False
    status: str = "not_stopped"
    final_health_effect: str = "unknown"
    cleanup_errors: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stop_requested": self.stop_requested,
            "valid_pcm_received": self.valid_pcm_received,
            "valid_full_pcm_frames": self.valid_full_pcm_frames,
            "child_exit_code": self.child_exit_code,
            "child_signal": self.child_signal,
            "termination_signal_requested": self.termination_signal_requested,
            "termination_escalated": self.termination_escalated,
            "stderr": self.stderr,
            "process_reaped": self.process_reaped,
            "cleanup_completed": self.cleanup_completed,
            "active_failure_before_stop": self.active_failure_before_stop,
            "unexpected_ownership_loss": self.unexpected_ownership_loss,
            "unexpected_failure": self.unexpected_failure,
            "status": self.status,
            "final_health_effect": self.final_health_effect,
            "cleanup_errors": list(self.cleanup_errors),
        }


@dataclass(frozen=True)
class CaptureStagePaths:
    directory: Path
    raw: Path
    assembled: Path
    normalized: Path


@dataclass
class PersistentPcmStreamHandle:
    """One explicitly owned ALSA PCM stream used across bounded VAD polls."""

    stream_id: str
    owner: str
    requested_device: str
    resolved_device: str
    command: tuple[str, ...]
    frame_source: Any
    opened_at: float
    sample_rate_hz: int = CANONICAL_SAMPLE_RATE_HZ
    channels: int = CANONICAL_CHANNELS
    sample_width_bytes: int = CANONICAL_SAMPLE_WIDTH_BYTES
    sample_format: str = CANONICAL_PCM_SAMPLE_FORMAT
    frame_duration_ms: int = CANONICAL_PCM_FRAME_DURATION_MS
    samples_per_frame: int = CANONICAL_PCM_SAMPLES_PER_FRAME
    frame_bytes: int = CANONICAL_PCM_FRAME_BYTES
    format_verification_status: str = "requested_raw_contract_unheadered_stream"
    alsa_handle_id: str = ""
    closed: bool = False


class SafeSubprocessRunner:
    """Bounded process-group boundary for ALSA utilities."""

    def __init__(self, runner: Optional[BoundedProcessRunner] = None):
        self._runner = runner or BoundedProcessRunner()

    def which(self, executable: str) -> Optional[str]:
        return self._runner.which(executable)

    def run(self, args: Sequence[str], timeout_seconds: float) -> SafeProcessResult:
        safe_args = [str(arg) for arg in args]
        completed = self._runner.run(safe_args, timeout_seconds=timeout_seconds)
        return SafeProcessResult(
            args=safe_args,
            returncode=int(completed.returncode),
            stdout=str(completed.stdout or ""),
            stderr=str(completed.stderr or ""),
            timed_out=bool(completed.timed_out),
            error_message=str(completed.error_message or ""),
            metadata=dict(completed.metadata),
        )

    def cancel_current(self, reason: str = "cancelled") -> bool:
        return self._runner.cancel_current(reason)


class SubprocessPcmFrameSource:
    """Bounded raw-PCM reader for one foreground arecord process."""

    def __init__(
        self,
        args: Sequence[str],
        *,
        process_factory: Optional[Any] = None,
        selector: Optional[Any] = None,
        raw_reader: Optional[Any] = None,
        clock: Any = time.monotonic,
    ):
        self.args = [str(arg) for arg in args]
        factory = process_factory or subprocess.Popen
        process_kwargs: Dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
            "bufsize": 0,
        }
        if os.name == "posix":
            process_kwargs["start_new_session"] = True
        self.process = factory(self.args, **process_kwargs)
        self.process_group_owned = bool(
            os.name == "posix" and process_factory is None
        )
        self.process_group_id = self._resolve_process_group_id()
        self.selector = selector or select.select
        self.raw_reader = raw_reader or os.read
        self.clock = clock
        self.closed = False
        self.stream_ended = False
        self.stderr = ""
        self._pending = bytearray()
        self._discard_continuation_bytes = 0
        self._expected_frame_bytes = 0
        self._last_frame_hash = b""
        self._last_mutable_source_id: Optional[int] = None
        self.total_low_level_reads = 0
        self.valid_full_pcm_frames = 0
        self.partial_reads = 0
        self.empty_reads = 0
        self.read_errors = 0
        self.discarded_bytes = 0
        self.zero_filled_bytes = 0
        self.repeated_frame_hashes = 0
        self.mutable_buffer_reuse_detected = 0
        self.valid_microphone_bytes_delivered_to_vad = 0
        self.accumulated_partial_bytes = 0
        self.low_level_read_size_counts: Dict[str, int] = {}
        self.eof_count = 0
        self.unexpected_eof_count = 0
        self.dead_process_detected = False
        self.odd_trailing_byte_count = 0
        self.incomplete_trailing_byte_count = 0
        self.stdout_closed_while_process_alive_count = 0
        self.terminal_reason = ""
        self.last_read_timestamp = 0.0
        self._stop_requested = False
        self._active_failure_before_stop = False
        self._active_failure_reason = ""
        self.last_stop_result = PcmStreamStopResult()

    def read_frame(self, frame_bytes: int, timeout_seconds: float) -> bytes:
        if self.closed or self.process.stdout is None:
            raise RuntimeError("pcm_stream_closed")
        if self.stream_ended:
            raise EOFError("arecord_pcm_stream_ended")
        expected = self._set_expected_frame_bytes(frame_bytes)
        deadline = self.clock() + max(0.01, float(timeout_seconds))
        descriptor = self.process.stdout.fileno()
        while len(self._pending) < expected:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("pcm_frame_read_timeout")
            try:
                readable, _, _ = self.selector([descriptor], [], [], remaining)
            except OSError as error:
                self.read_errors += 1
                self._record_active_failure(
                    f"pcm_select_error:{error.__class__.__name__}"
                )
                raise
            if not readable:
                returncode = self._process_exit_status()
                if returncode is not None:
                    self._raise_terminal_stream_state(returncode=returncode)
                raise TimeoutError("pcm_frame_read_timeout")
            requested = expected - len(self._pending)
            self.total_low_level_reads += 1
            try:
                chunk = self.raw_reader(descriptor, requested)
            except OSError as error:
                self.read_errors += 1
                self._record_active_failure(
                    f"pcm_read_error:{error.__class__.__name__}"
                )
                raise
            if not chunk:
                self.empty_reads += 1
                self.eof_count += 1
                self.low_level_read_size_counts["0"] = (
                    self.low_level_read_size_counts.get("0", 0) + 1
                )
                self._raise_terminal_stream_state(
                    returncode=self._process_exit_status(),
                )
            try:
                immutable_chunk = self._copy_source_bytes(chunk)
            except (TypeError, ValueError) as error:
                self.read_errors += 1
                self._record_active_failure(
                    f"pcm_payload_error:{error.__class__.__name__}"
                )
                raise
            read_size = len(immutable_chunk)
            read_size_key = str(read_size)
            self.low_level_read_size_counts[read_size_key] = (
                self.low_level_read_size_counts.get(read_size_key, 0) + 1
            )
            if len(immutable_chunk) < requested:
                self.partial_reads += 1
                self.accumulated_partial_bytes += len(immutable_chunk)
            if self._discard_continuation_bytes:
                discarded_prefix = min(
                    self._discard_continuation_bytes,
                    len(immutable_chunk),
                )
                immutable_chunk = immutable_chunk[discarded_prefix:]
                self._discard_continuation_bytes -= discarded_prefix
                self.discarded_bytes += discarded_prefix
            self._pending.extend(immutable_chunk)
        immutable_frame = bytes(self._pending[:expected])
        del self._pending[:expected]
        self.valid_full_pcm_frames += 1
        self.valid_microphone_bytes_delivered_to_vad += len(immutable_frame)
        self.last_read_timestamp = float(self.clock())
        current_hash = hashlib.sha256(immutable_frame).digest()
        if self._last_frame_hash and current_hash == self._last_frame_hash:
            self.repeated_frame_hashes += 1
        self._last_frame_hash = current_hash
        return immutable_frame

    def discard_pending_bytes(self, maximum_bytes: Optional[int] = None) -> int:
        """Discard only already-owned partial PCM while preserving S16 alignment.

        The continuous pump pauses its only reader before calling this method.
        This method never reads the process descriptor and therefore cannot race
        another stdout reader or discard post-boundary microphone audio.
        """

        available = len(self._pending)
        bounded = available
        if maximum_bytes is not None:
            bounded = min(available, max(0, int(maximum_bytes)))
        if bounded <= 0:
            return 0
        del self._pending[:bounded]
        if bounded % CANONICAL_SAMPLE_WIDTH_BYTES:
            self._discard_continuation_bytes = 1
        self.discarded_bytes += bounded
        self._last_frame_hash = b""
        return bounded

    def discard_available(self, maximum_bytes: int) -> int:
        """Discard stale bytes while preserving signed-16 sample alignment."""

        if self.closed or self.stream_ended or self.process.stdout is None:
            return 0
        bounded_maximum = max(0, min(int(maximum_bytes), 16000 * 2 * 3))
        if bounded_maximum <= 0:
            return 0
        bounded_maximum -= bounded_maximum % CANONICAL_SAMPLE_WIDTH_BYTES
        if bounded_maximum <= 0:
            return 0
        buffered = bytearray(self._pending)
        self._pending.clear()
        alignment_discarded = 0
        descriptor = self.process.stdout.fileno()
        while len(buffered) < bounded_maximum:
            try:
                readable, _, _ = self.selector([descriptor], [], [], 0.0)
            except OSError as error:
                self.read_errors += 1
                self._record_active_failure(
                    f"pcm_select_error:{error.__class__.__name__}"
                )
                self._pending.extend(buffered)
                raise
            if not readable:
                break
            requested = min(16384, bounded_maximum - len(buffered))
            self.total_low_level_reads += 1
            try:
                chunk = self.raw_reader(descriptor, requested)
            except OSError as error:
                self.read_errors += 1
                self._record_active_failure(
                    f"pcm_read_error:{error.__class__.__name__}"
                )
                self._pending.extend(buffered)
                raise
            if not chunk:
                self.empty_reads += 1
                self.eof_count += 1
                self.low_level_read_size_counts["0"] = (
                    self.low_level_read_size_counts.get("0", 0) + 1
                )
                self.stream_ended = True
                break
            try:
                immutable_chunk = self._copy_source_bytes(chunk)
            except (TypeError, ValueError) as error:
                self.read_errors += 1
                self._record_active_failure(
                    f"pcm_payload_error:{error.__class__.__name__}"
                )
                self._pending.extend(buffered)
                raise
            read_size = len(immutable_chunk)
            read_size_key = str(read_size)
            self.low_level_read_size_counts[read_size_key] = (
                self.low_level_read_size_counts.get(read_size_key, 0) + 1
            )
            if len(immutable_chunk) < requested:
                self.partial_reads += 1
                self.accumulated_partial_bytes += len(immutable_chunk)
            if self._discard_continuation_bytes:
                discarded_prefix = min(
                    self._discard_continuation_bytes,
                    len(immutable_chunk),
                )
                immutable_chunk = immutable_chunk[discarded_prefix:]
                self._discard_continuation_bytes -= discarded_prefix
                self.discarded_bytes += discarded_prefix
                alignment_discarded += discarded_prefix
            buffered.extend(immutable_chunk)
        discarded_now = min(len(buffered), bounded_maximum)
        residual = buffered[discarded_now:]
        if discarded_now % CANONICAL_SAMPLE_WIDTH_BYTES:
            self._discard_continuation_bytes = 1
        self._pending.extend(residual)
        self.discarded_bytes += discarded_now
        if discarded_now:
            self._last_frame_hash = b""
        return alignment_discarded + discarded_now

    def snapshot(self) -> Dict[str, Any]:
        returncode = self._process_exit_status()
        return {
            "transport_argv": list(self.args),
            "stdout_transport_mode": "raw_pcm_pipe",
            "stderr_transport_mode": "separate_pipe",
            "process_pid": int(getattr(self.process, "pid", 0) or 0),
            "process_exit_status": returncode,
            "process_alive": bool(returncode is None and not self.closed),
            "process_liveness_observable": True,
            "total_low_level_reads": self.total_low_level_reads,
            "valid_full_pcm_frames": self.valid_full_pcm_frames,
            "valid_pcm_frames_delivered_to_vad": self.valid_full_pcm_frames,
            "fresh_full_pcm_frames": self.valid_full_pcm_frames,
            "partial_reads": self.partial_reads,
            "empty_reads": self.empty_reads,
            "read_errors": self.read_errors,
            "discarded_bytes": self.discarded_bytes,
            "zero_filled_bytes": self.zero_filled_bytes,
            "repeated_frame_hashes": self.repeated_frame_hashes,
            "mutable_buffer_reuse_detected": self.mutable_buffer_reuse_detected,
            "valid_microphone_bytes_delivered_to_vad": (
                self.valid_microphone_bytes_delivered_to_vad
            ),
            "read_sequence": self.valid_full_pcm_frames,
            "live_frame_count": self.valid_full_pcm_frames,
            "total_bytes_returned": self.valid_microphone_bytes_delivered_to_vad,
            "total_live_bytes_read": self.valid_microphone_bytes_delivered_to_vad,
            "last_source_frame_sequence": self.valid_full_pcm_frames,
            "last_frame_was_replay": False,
            "last_frame_bytes": (
                self._expected_frame_bytes if self.valid_full_pcm_frames else 0
            ),
            "last_read_timestamp": self.last_read_timestamp,
            "accumulated_partial_bytes": self.accumulated_partial_bytes,
            "pending_partial_bytes": len(self._pending),
            "pending_discard_alignment_bytes": self._discard_continuation_bytes,
            "low_level_read_size_counts": dict(self.low_level_read_size_counts),
            "expected_frame_bytes": self._expected_frame_bytes,
            "eof_count": self.eof_count,
            "unexpected_eof_count": self.unexpected_eof_count,
            "dead_process_detected": self.dead_process_detected,
            "odd_trailing_byte_count": self.odd_trailing_byte_count,
            "incomplete_trailing_byte_count": self.incomplete_trailing_byte_count,
            "stdout_closed_while_process_alive_count": (
                self.stdout_closed_while_process_alive_count
            ),
            "terminal_reason": self.terminal_reason,
            "stop_requested": self._stop_requested,
            "active_failure_before_stop": self._active_failure_before_stop,
            "active_failure_reason": self._active_failure_reason,
            "controlled_stop": self.last_stop_result.to_dict(),
            "closed": self.closed,
            "stream_ended": self.stream_ended,
        }

    def _process_exit_status(self) -> Optional[int]:
        try:
            value = self.process.poll()
        except (OSError, RuntimeError):
            return None
        return int(value) if value is not None else None

    def _raise_terminal_stream_state(self, *, returncode: Optional[int]) -> None:
        self.stream_ended = True
        pending_bytes = len(self._pending)
        if pending_bytes % CANONICAL_SAMPLE_WIDTH_BYTES:
            self.odd_trailing_byte_count += pending_bytes
            self.unexpected_eof_count += 1
            self.terminal_reason = "odd_trailing_pcm_corruption"
            self._record_active_failure(self.terminal_reason)
            raise ValueError(
                f"odd_trailing_pcm_corruption:{pending_bytes}"
            )
        if pending_bytes:
            self.incomplete_trailing_byte_count += pending_bytes
            self.unexpected_eof_count += 1
            self.terminal_reason = "incomplete_trailing_pcm_frame"
            self._record_active_failure(self.terminal_reason)
            raise EOFError(
                f"arecord_pcm_stream_ended_with_partial_frame:{pending_bytes}"
            )
        if returncode is None:
            self.stdout_closed_while_process_alive_count += 1
            self.unexpected_eof_count += 1
            self.terminal_reason = "stdout_closed_while_process_alive"
            self._record_active_failure(self.terminal_reason)
            raise RuntimeError("arecord_stdout_closed_while_process_alive")
        if returncode != 0:
            self.dead_process_detected = True
            self.unexpected_eof_count += 1
            self.terminal_reason = "arecord_process_exited"
            self._record_active_failure(self.terminal_reason)
            raise RuntimeError(f"arecord_process_exited:{returncode}")
        self.terminal_reason = "clean_eof"
        self._record_active_failure(self.terminal_reason)
        raise EOFError("arecord_pcm_stream_ended")

    def _record_active_failure(self, reason: str) -> None:
        if self._stop_requested:
            return
        self._active_failure_before_stop = True
        if not self._active_failure_reason:
            self._active_failure_reason = str(reason or "pcm_active_failure")[:160]

    def _set_expected_frame_bytes(self, frame_bytes: int) -> int:
        if isinstance(frame_bytes, bool) or int(frame_bytes) <= 0:
            raise ValueError("frame_bytes must be positive")
        expected = int(frame_bytes)
        if expected % CANONICAL_SAMPLE_WIDTH_BYTES:
            raise ValueError("PCM frame bytes must contain complete S16_LE samples")
        if self._expected_frame_bytes and expected != self._expected_frame_bytes:
            raise ValueError("pcm_frame_size_changed")
        self._expected_frame_bytes = expected
        return expected

    def _copy_source_bytes(self, value: Any) -> bytes:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("PCM reader returned a non-bytes payload")
        mutable_source_id: Optional[int] = None
        if isinstance(value, bytearray):
            mutable_source_id = id(value)
        elif isinstance(value, memoryview) and not value.readonly:
            mutable_source_id = id(value.obj)
        if mutable_source_id is not None:
            if mutable_source_id == self._last_mutable_source_id:
                self.mutable_buffer_reuse_detected += 1
            self._last_mutable_source_id = mutable_source_id
        actual_length = len(value)
        return bytes(value[:actual_length])

    def close(self) -> None:
        if self.closed:
            return
        cleanup_errors: list[str] = []
        self._stop_requested = True
        termination_signal_requested = ""
        termination_escalated = False

        def process_returncode() -> Optional[int]:
            try:
                value = self.process.poll()
            except (OSError, RuntimeError) as error:
                cleanup_errors.append(f"poll:{error.__class__.__name__}")
                return None
            return int(value) if value is not None else None

        returncode = process_returncode()
        process_was_alive_at_stop = returncode is None
        if returncode is None:
            try:
                termination_signal_requested = "SIGTERM"
                self._signal_process_group(getattr(signal, "SIGTERM", 15))
            except (OSError, RuntimeError) as error:
                cleanup_errors.append(f"terminate:{error.__class__.__name__}")
            try:
                returncode = self._bounded_wait(timeout_seconds=2.0)
            except subprocess.TimeoutExpired:
                cleanup_errors.append("terminate_wait:TimeoutExpired")
            except (OSError, RuntimeError) as error:
                cleanup_errors.append(
                    f"terminate_wait:{error.__class__.__name__}"
                )
            if returncode is None:
                try:
                    termination_signal_requested = "SIGKILL"
                    termination_escalated = True
                    self._signal_process_group(getattr(signal, "SIGKILL", 9))
                except (OSError, RuntimeError) as error:
                    cleanup_errors.append(f"kill:{error.__class__.__name__}")
                try:
                    returncode = self._bounded_wait(timeout_seconds=2.0)
                except subprocess.TimeoutExpired:
                    cleanup_errors.append("kill_wait:TimeoutExpired")
                except (OSError, RuntimeError) as error:
                    cleanup_errors.append(
                        f"kill_wait:{error.__class__.__name__}"
                    )
        if returncode is None:
            returncode = process_returncode()
        if returncode is not None and self.process.stderr is not None:
            try:
                self.stderr = self.process.stderr.read(1000).decode(
                    "utf-8",
                    errors="replace",
                )
            except (AttributeError, OSError, RuntimeError, ValueError):
                self.stderr = ""
        pipes_closed = True
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, RuntimeError) as error:
                    pipes_closed = False
                    cleanup_errors.append(
                        f"pipe_close:{error.__class__.__name__}"
                    )
        if returncode is None:
            try:
                termination_signal_requested = "SIGKILL"
                termination_escalated = True
                self._signal_process_group(getattr(signal, "SIGKILL", 9))
            except (OSError, RuntimeError) as error:
                cleanup_errors.append(f"final_kill:{error.__class__.__name__}")
            try:
                returncode = self._bounded_wait(timeout_seconds=2.0)
            except (subprocess.TimeoutExpired, OSError, RuntimeError) as error:
                cleanup_errors.append(
                    f"final_wait:{error.__class__.__name__}"
                )
        if returncode is None:
            returncode = process_returncode()
        reaped = returncode is not None
        cleanup_completed = bool(reaped and pipes_closed)
        self.last_stop_result = self._classify_stop_result(
            process_was_alive_at_stop=process_was_alive_at_stop,
            returncode=returncode,
            termination_signal_requested=termination_signal_requested,
            termination_escalated=termination_escalated,
            process_reaped=reaped,
            cleanup_completed=cleanup_completed,
            cleanup_errors=cleanup_errors,
        )
        if returncode is None:
            self.closed = False
            raise RuntimeError(
                "arecord_pcm_stream_cleanup_failed:"
                + ",".join(cleanup_errors[-8:])
            )
        self.closed = True

    def _classify_stop_result(
        self,
        *,
        process_was_alive_at_stop: bool,
        returncode: Optional[int],
        termination_signal_requested: str,
        termination_escalated: bool,
        process_reaped: bool,
        cleanup_completed: bool,
        cleanup_errors: Sequence[str],
    ) -> PcmStreamStopResult:
        valid_frames = int(self.valid_full_pcm_frames)
        valid_pcm_received = valid_frames > 0
        child_signal = (
            abs(int(returncode))
            if returncode is not None and int(returncode) < 0
            else None
        )
        stderr_text = str(self.stderr or "")
        interrupted_arecord_read = _stderr_is_controlled_arecord_interrupt(
            stderr_text
        )
        expected_signal = bool(
            child_signal in {
                int(getattr(signal, "SIGINT", 2)),
                int(getattr(signal, "SIGTERM", 15)),
            }
            or (
                termination_escalated
                and child_signal == int(getattr(signal, "SIGKILL", 9))
            )
        )
        expected_exit = bool(
            returncode == 0
            or expected_signal
            or (returncode == 1 and interrupted_arecord_read)
        )
        active_failure = bool(
            self._active_failure_before_stop or not process_was_alive_at_stop
        )
        controlled = bool(
            self._stop_requested
            and process_was_alive_at_stop
            and valid_pcm_received
            and not active_failure
            and process_reaped
            and cleanup_completed
            and expected_exit
        )
        degraded = bool(controlled and termination_escalated)
        unexpected_failure = not controlled
        status = (
            "controlled_stop_degraded"
            if degraded
            else "controlled_stop"
            if controlled
            else "cleanup_incomplete"
            if not cleanup_completed
            else "unexpected_failure"
        )
        final_health_effect = (
            "degraded_reusable"
            if degraded
            else "none"
            if controlled
            else "unhealthy"
        )
        return PcmStreamStopResult(
            stop_requested=self._stop_requested,
            valid_pcm_received=valid_pcm_received,
            valid_full_pcm_frames=valid_frames,
            child_exit_code=returncode,
            child_signal=child_signal,
            termination_signal_requested=termination_signal_requested,
            termination_escalated=termination_escalated,
            stderr=stderr_text,
            process_reaped=process_reaped,
            cleanup_completed=cleanup_completed,
            active_failure_before_stop=active_failure,
            unexpected_ownership_loss=False,
            unexpected_failure=unexpected_failure,
            status=status,
            final_health_effect=final_health_effect,
            cleanup_errors=tuple(str(value) for value in cleanup_errors),
        )

    def _resolve_process_group_id(self) -> int:
        pid = int(getattr(self.process, "pid", 0) or 0)
        if os.name != "posix" or not self.process_group_owned or pid <= 0:
            return pid
        try:
            return int(os.getpgid(pid))
        except OSError:
            return pid

    def _signal_process_group(self, signal_number: int) -> None:
        if os.name == "posix" and self.process_group_owned and self.process_group_id > 0:
            try:
                os.killpg(self.process_group_id, int(signal_number))
                return
            except ProcessLookupError:
                return
            except OSError as error:
                if getattr(error, "errno", None) == errno.ESRCH:
                    return
        method = (
            getattr(self.process, "terminate", None)
            if int(signal_number) == int(getattr(signal, "SIGTERM", 15))
            else getattr(self.process, "kill", None)
        )
        if callable(method):
            method()

    def _bounded_wait(self, *, timeout_seconds: float) -> int:
        timeout = max(0.01, float(timeout_seconds))
        deadline = self.clock() + timeout
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self.args, timeout)
            try:
                value = self.process.wait(timeout=min(0.10, remaining))
            except subprocess.TimeoutExpired:
                continue
            if value is None:
                polled = self.process.poll()
                if polled is None:
                    continue
                value = polled
            return int(value)


class ContinuousPcmFrameSource:
    """Continuously drain one arecord PCM pipe into a bounded owned-frame queue.

    The subprocess and microphone owner do not change. A single producer owns
    stdout for the lifetime of the process, reconstructs exact immutable frames,
    and drops the oldest queued frame when a paused consumer falls behind. This
    prevents a persistent arecord process from blocking on a full stdout pipe and
    later serving stale pre-prompt audio as if it were live microphone input.
    Stderr is drained independently and is never mixed into PCM.
    """

    def __init__(
        self,
        source: SubprocessPcmFrameSource,
        *,
        expected_frame_bytes: int = CANONICAL_PCM_FRAME_BYTES,
        maximum_queue_frames: int = DEFAULT_PCM_PUMP_QUEUE_FRAMES,
        read_timeout_seconds: float = DEFAULT_PCM_PUMP_READ_TIMEOUT_SECONDS,
        non_silent_rms: float = DEFAULT_PCM_NON_SILENT_RMS,
        pathological_duplicate_frames: int = (
            DEFAULT_PCM_PATHOLOGICAL_DUPLICATE_FRAMES
        ),
        tiny_rms: float = DEFAULT_PCM_TINY_RMS,
        capture_stderr: bool = True,
        stderr_selector: Optional[Any] = None,
        stderr_raw_reader: Optional[Any] = None,
        clock: Any = time.monotonic,
    ):
        if not callable(getattr(source, "read_frame", None)):
            raise ValueError("continuous PCM source must support read_frame")
        if (
            isinstance(expected_frame_bytes, bool)
            or int(expected_frame_bytes) <= 0
            or int(expected_frame_bytes) % CANONICAL_SAMPLE_WIDTH_BYTES
        ):
            raise ValueError("expected_frame_bytes must contain complete S16_LE samples")
        if not 2 <= int(maximum_queue_frames) <= 500:
            raise ValueError("maximum_queue_frames must be between 2 and 500")
        if not 0.01 <= float(read_timeout_seconds) <= 1.0:
            raise ValueError("read_timeout_seconds must be between 0.01 and 1.0")
        if float(non_silent_rms) < 0 or float(tiny_rms) < 0:
            raise ValueError("PCM integrity RMS boundaries cannot be negative")
        if not 2 <= int(pathological_duplicate_frames) <= 500:
            raise ValueError(
                "pathological_duplicate_frames must be between 2 and 500"
            )

        self.source = source
        self.args = list(getattr(source, "args", ()) or ())
        self.process = getattr(source, "process", None)
        self.expected_frame_bytes = int(expected_frame_bytes)
        self.maximum_queue_frames = int(maximum_queue_frames)
        self.read_timeout_seconds = float(read_timeout_seconds)
        self.non_silent_rms = float(non_silent_rms)
        self.pathological_duplicate_frames = int(pathological_duplicate_frames)
        self.tiny_rms = float(tiny_rms)
        self.clock = clock
        self.closed = False
        self.stream_ended = False
        self.terminal_reason = ""
        self.stderr = ""
        self._condition = Condition(RLock())
        self._stop_requested = Event()
        self._queue: deque[tuple[int, bytes, float]] = deque()
        self._reset_epoch = 0
        self._inflight_epoch: Optional[int] = None
        self._resetting = False
        self._terminal_error: Optional[tuple[type[BaseException], str]] = None
        self._last_pumped_hash = b""
        self._last_pumped_non_silent = False
        self._current_repeated_non_silent_frames = 0
        self._current_tiny_rms_frames = 0
        self._stderr_bytes = bytearray()
        self._stderr_maximum_bytes = 8192
        self._stderr_discarded_bytes = 0
        self._stderr_selector = stderr_selector or select.select
        self._stderr_raw_reader = stderr_raw_reader or os.read

        self.pumped_frame_count = 0
        self.delivered_frame_count = 0
        self.pumped_byte_count = 0
        self.delivered_byte_count = 0
        self.queue_overflow_dropped_frames = 0
        self.queue_overflow_dropped_bytes = 0
        self.candidate_reset_discarded_frames = 0
        self.candidate_reset_discarded_bytes = 0
        self.repeated_frame_hashes = 0
        self.maximum_consecutive_repeated_non_silent_frames = 0
        self.pathological_duplicate_frame_detected = False
        self.integrity_failure_count = 0
        self.integrity_guard_discarded_frames = 0
        self.integrity_guard_discarded_bytes = 0
        self.maximum_consecutive_tiny_rms_frames = 0
        self.producer_timeouts = 0
        self.reset_boundary_timeouts = 0
        self.last_source_frame_sequence = 0
        self.last_frame_was_replay = False
        self.last_frame_bytes = 0
        self.last_read_timestamp = 0.0
        self.last_frame_rms = 0.0
        self.last_frame_minimum_sample = 0
        self.last_frame_maximum_sample = 0

        self._producer_thread = Thread(
            target=self._producer_loop,
            name="ares-persistent-pcm-pump",
            daemon=True,
        )
        self._producer_thread.start()
        self._stderr_thread: Optional[Thread] = None
        stderr_pipe = getattr(self.process, "stderr", None)
        if capture_stderr and stderr_pipe is not None:
            self._stderr_thread = Thread(
                target=self._stderr_loop,
                name="ares-persistent-pcm-stderr",
                daemon=True,
            )
            self._stderr_thread.start()

    def read_frame(self, frame_bytes: int, timeout_seconds: float) -> bytes:
        if int(frame_bytes) != self.expected_frame_bytes:
            raise ValueError("persistent PCM frame size changed")
        deadline = self.clock() + max(0.01, float(timeout_seconds))
        with self._condition:
            while not self._queue:
                if self._terminal_error is not None:
                    self._raise_saved_terminal_error()
                if self.closed:
                    raise EOFError("persistent PCM stream is closed")
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError("pcm_frame_read_timeout")
                self._condition.wait(timeout=min(remaining, 0.10))
            source_sequence, frame, captured_at = self._queue.popleft()
            immutable_frame = bytes(frame)
            self.delivered_frame_count += 1
            self.delivered_byte_count += len(immutable_frame)
            self.last_source_frame_sequence = int(source_sequence)
            self.last_frame_was_replay = False
            self.last_frame_bytes = len(immutable_frame)
            self.last_read_timestamp = float(captured_at)
            return immutable_frame

    def discard_available(self, maximum_bytes: int) -> int:
        """Establish a current-audio boundary without racing the stdout reader."""

        bounded_maximum = max(
            0,
            min(int(maximum_bytes), self.expected_frame_bytes * self.maximum_queue_frames),
        )
        if bounded_maximum <= 0:
            return 0
        reset_started = self.clock()
        with self._condition:
            self._resetting = True
            self._reset_epoch += 1
            current_epoch = self._reset_epoch
            discarded_before = self.candidate_reset_discarded_bytes
            while (
                self._queue
                and self.candidate_reset_discarded_bytes - discarded_before
                + self.expected_frame_bytes
                <= bounded_maximum
            ):
                self._queue.popleft()
                self.candidate_reset_discarded_frames += 1
                self.candidate_reset_discarded_bytes += self.expected_frame_bytes
            wait_bound = max(0.10, self.read_timeout_seconds * 3.0)
            while (
                self._inflight_epoch is not None
                and self._inflight_epoch < current_epoch
                and self.clock() - reset_started < wait_bound
            ):
                self._condition.wait(timeout=min(0.02, wait_bound))
            if (
                self._inflight_epoch is not None
                and self._inflight_epoch < current_epoch
            ):
                self.reset_boundary_timeouts += 1
                self._resetting = False
                self._condition.notify_all()
                raise TimeoutError("pcm_reset_boundary_timeout")

            remaining = max(
                0,
                bounded_maximum
                - (self.candidate_reset_discarded_bytes - discarded_before),
            )
            discard_pending = getattr(self.source, "discard_pending_bytes", None)
            if remaining and callable(discard_pending):
                discarded_partial = int(discard_pending(remaining))
                self.candidate_reset_discarded_bytes += max(0, discarded_partial)
            self._resetting = False
            self._condition.notify_all()
            return max(
                0,
                self.candidate_reset_discarded_bytes - discarded_before,
            )

    def snapshot(self) -> Dict[str, Any]:
        source_snapshot = getattr(self.source, "snapshot", None)
        low_level = source_snapshot() if callable(source_snapshot) else {}
        if not isinstance(low_level, dict):
            low_level = {}
        with self._condition:
            queue_depth = len(self._queue)
            exit_status = low_level.get("process_exit_status")
            process_pid = int(
                low_level.get(
                    "process_pid",
                    getattr(self.process, "pid", 0),
                )
                or 0
            )
            process_alive_value = low_level.get("process_alive")
            liveness_observable = bool(
                low_level.get(
                    "process_liveness_observable",
                    isinstance(process_alive_value, bool) or process_pid > 0,
                )
            )
            return {
                **low_level,
                "transport_argv": list(
                    low_level.get("transport_argv", self.args) or self.args
                ),
                "stdout_transport_mode": "raw_pcm_pipe_continuous_pump",
                "stderr_transport_mode": "separate_bounded_pipe",
                "process_pid": process_pid,
                "process_exit_status": exit_status,
                "process_alive": bool(
                    process_alive_value
                    if isinstance(process_alive_value, bool)
                    else exit_status is None and not self.closed
                ),
                "process_liveness_observable": liveness_observable,
                "valid_full_pcm_frames": self.pumped_frame_count,
                "fresh_full_pcm_frames": self.pumped_frame_count,
                "valid_pcm_frames_delivered_to_vad": self.delivered_frame_count,
                "valid_microphone_bytes_delivered_to_vad": (
                    self.delivered_byte_count
                ),
                "fresh_microphone_bytes_delivered_to_vad": (
                    self.pumped_byte_count
                ),
                "read_sequence": self.delivered_frame_count,
                "live_frame_count": self.pumped_frame_count,
                "total_bytes_returned": self.delivered_byte_count,
                "total_live_bytes_read": self.pumped_byte_count,
                "last_source_frame_sequence": self.last_source_frame_sequence,
                "last_frame_was_replay": False,
                "last_frame_bytes": self.last_frame_bytes,
                "last_read_timestamp": self.last_read_timestamp,
                "expected_frame_bytes": self.expected_frame_bytes,
                "queue_capacity_frames": self.maximum_queue_frames,
                "queue_depth_frames": queue_depth,
                "queue_depth_bytes": queue_depth * self.expected_frame_bytes,
                "queue_overflow_dropped_frames": (
                    self.queue_overflow_dropped_frames
                ),
                "queue_overflow_dropped_bytes": self.queue_overflow_dropped_bytes,
                "candidate_reset_discarded_frames": (
                    self.candidate_reset_discarded_frames
                ),
                "candidate_reset_discarded_bytes": (
                    self.candidate_reset_discarded_bytes
                ),
                "repeated_frame_hashes": self.repeated_frame_hashes,
                "maximum_consecutive_repeated_non_silent_frames": (
                    self.maximum_consecutive_repeated_non_silent_frames
                ),
                "pathological_duplicate_frame_detected": (
                    self.pathological_duplicate_frame_detected
                ),
                "integrity_failure_count": self.integrity_failure_count,
                "integrity_guard_discarded_frames": (
                    self.integrity_guard_discarded_frames
                ),
                "integrity_guard_discarded_bytes": (
                    self.integrity_guard_discarded_bytes
                ),
                "maximum_consecutive_tiny_rms_frames": (
                    self.maximum_consecutive_tiny_rms_frames
                ),
                "producer_timeouts": self.producer_timeouts,
                "reset_boundary_timeouts": self.reset_boundary_timeouts,
                "last_frame_rms": self.last_frame_rms,
                "last_frame_minimum_sample": self.last_frame_minimum_sample,
                "last_frame_maximum_sample": self.last_frame_maximum_sample,
                "stderr_bytes_captured": len(self._stderr_bytes),
                "stderr_discarded_bytes": self._stderr_discarded_bytes,
                "stderr_preview": bytes(self._stderr_bytes).decode(
                    "utf-8",
                    errors="replace",
                ),
                "stream_ended": self.stream_ended
                or bool(low_level.get("stream_ended", False)),
                "terminal_reason": self.terminal_reason
                or str(low_level.get("terminal_reason", "") or ""),
                "closed": self.closed,
            }

    def close(self) -> None:
        if self.closed:
            return
        self._stop_requested.set()
        with self._condition:
            self._resetting = False
            self._condition.notify_all()
        self._producer_thread.join(timeout=max(0.25, self.read_timeout_seconds * 4.0))
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.25)
        try:
            self.source.close()
        except (OSError, RuntimeError):
            self.closed = False
            raise
        self._producer_thread.join(timeout=0.25)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.25)
        source_stderr = str(getattr(self.source, "stderr", "") or "")
        captured_stderr = bytes(self._stderr_bytes).decode("utf-8", errors="replace")
        self.stderr = (captured_stderr + source_stderr)[:1000]
        with self._condition:
            self.closed = True
            self._queue.clear()
            self._condition.notify_all()

    def _producer_loop(self) -> None:
        while not self._stop_requested.is_set():
            with self._condition:
                while self._resetting and not self._stop_requested.is_set():
                    self._condition.wait(timeout=0.02)
                if self._stop_requested.is_set():
                    return
                read_epoch = self._reset_epoch
                self._inflight_epoch = read_epoch
            try:
                frame = self.source.read_frame(
                    self.expected_frame_bytes,
                    self.read_timeout_seconds,
                )
                if not isinstance(frame, (bytes, bytearray, memoryview)):
                    raise TypeError("PCM pump received a non-bytes frame")
                actual_length = len(frame)
                immutable_frame = bytes(frame[:actual_length])
                if actual_length != self.expected_frame_bytes:
                    raise ValueError(
                        f"PCM pump received incomplete frame:{actual_length}"
                    )
                if actual_length % CANONICAL_SAMPLE_WIDTH_BYTES:
                    raise ValueError("PCM pump received odd-byte frame")
            except TimeoutError:
                with self._condition:
                    self.producer_timeouts += 1
                    self._inflight_epoch = None
                    self._condition.notify_all()
                continue
            except (EOFError, OSError, RuntimeError, TypeError, ValueError) as error:
                if self._stop_requested.is_set():
                    return
                with self._condition:
                    self._inflight_epoch = None
                    self.stream_ended = True
                    source_terminal = getattr(self.source, "terminal_reason", "")
                    self.terminal_reason = str(source_terminal or str(error))[:160]
                    self._terminal_error = (error.__class__, str(error))
                    self._condition.notify_all()
                return

            captured_at = float(self.clock())
            with self._condition:
                self._inflight_epoch = None
                if read_epoch != self._reset_epoch:
                    self.candidate_reset_discarded_frames += 1
                    self.candidate_reset_discarded_bytes += len(immutable_frame)
                    self._condition.notify_all()
                    continue
                self.pumped_frame_count += 1
                source_sequence = self.pumped_frame_count
                self.pumped_byte_count += len(immutable_frame)
                self._record_frame_integrity(immutable_frame)
                if self.pathological_duplicate_frame_detected:
                    discarded_frames = len(self._queue)
                    self._queue.clear()
                    self.integrity_failure_count += 1
                    self.integrity_guard_discarded_frames += discarded_frames
                    self.integrity_guard_discarded_bytes += (
                        discarded_frames * self.expected_frame_bytes
                    )
                    self.stream_ended = True
                    self.terminal_reason = (
                        "pathological_repeated_non_silent_pcm"
                    )
                    self._terminal_error = (
                        RuntimeError,
                        self.terminal_reason,
                    )
                    self._condition.notify_all()
                    return
                if len(self._queue) >= self.maximum_queue_frames:
                    self._queue.popleft()
                    self.queue_overflow_dropped_frames += 1
                    self.queue_overflow_dropped_bytes += self.expected_frame_bytes
                self._queue.append(
                    (source_sequence, immutable_frame, captured_at)
                )
                self._condition.notify_all()

    def _record_frame_integrity(self, frame: bytes) -> None:
        current_hash = hashlib.sha256(frame).digest()
        samples = decode_s16_le_samples(frame)
        rms = calculate_s16_le_rms(frame)
        minimum = min(samples, default=0)
        maximum = max(samples, default=0)
        non_silent = rms >= self.non_silent_rms
        if self._last_pumped_hash and current_hash == self._last_pumped_hash:
            self.repeated_frame_hashes += 1
        if (
            non_silent
            and self._last_pumped_non_silent
            and current_hash == self._last_pumped_hash
        ):
            self._current_repeated_non_silent_frames = max(
                2,
                self._current_repeated_non_silent_frames + 1,
            )
        else:
            self._current_repeated_non_silent_frames = 1 if non_silent else 0
        self.maximum_consecutive_repeated_non_silent_frames = max(
            self.maximum_consecutive_repeated_non_silent_frames,
            self._current_repeated_non_silent_frames,
        )
        if (
            self.maximum_consecutive_repeated_non_silent_frames
            >= self.pathological_duplicate_frames
        ):
            self.pathological_duplicate_frame_detected = True
        if rms <= self.tiny_rms:
            self._current_tiny_rms_frames += 1
        else:
            self._current_tiny_rms_frames = 0
        self.maximum_consecutive_tiny_rms_frames = max(
            self.maximum_consecutive_tiny_rms_frames,
            self._current_tiny_rms_frames,
        )
        self.last_frame_rms = rms
        self.last_frame_minimum_sample = int(minimum)
        self.last_frame_maximum_sample = int(maximum)
        self._last_pumped_hash = current_hash
        self._last_pumped_non_silent = non_silent

    def _stderr_loop(self) -> None:
        stderr_pipe = getattr(self.process, "stderr", None)
        if stderr_pipe is None:
            return
        try:
            descriptor = stderr_pipe.fileno()
        except (AttributeError, OSError, ValueError):
            return
        while not self._stop_requested.is_set():
            try:
                readable, _, _ = self._stderr_selector(
                    [descriptor],
                    [],
                    [],
                    0.10,
                )
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                chunk = self._stderr_raw_reader(descriptor, 1024)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            immutable_chunk = bytes(chunk)
            remaining = self._stderr_maximum_bytes - len(self._stderr_bytes)
            if remaining > 0:
                self._stderr_bytes.extend(immutable_chunk[:remaining])
            self._stderr_discarded_bytes += max(0, len(immutable_chunk) - remaining)

    def _raise_saved_terminal_error(self) -> None:
        assert self._terminal_error is not None
        error_type, message = self._terminal_error
        if issubclass(error_type, EOFError):
            raise EOFError(message)
        if issubclass(error_type, ValueError):
            raise ValueError(message)
        if issubclass(error_type, TypeError):
            raise TypeError(message)
        if issubclass(error_type, OSError):
            raise OSError(message)
        raise RuntimeError(message)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)


class SafePcmStreamRunner:
    """Start one allowlisted arecord process with a continuous bounded PCM pump."""

    def start(self, args: Sequence[str]) -> ContinuousPcmFrameSource:
        raw_source = SubprocessPcmFrameSource(args)
        return ContinuousPcmFrameSource(
            raw_source,
            expected_frame_bytes=CANONICAL_PCM_FRAME_BYTES,
        )


class DiagnosticPcmFrameSource:
    """Bounded tee used only when owner-requested audio diagnostics are enabled."""

    def __init__(
        self,
        source: Any,
        maximum_bytes: int,
        *,
        close_source: bool = True,
    ):
        self.source = source
        self.maximum_bytes = max(1, int(maximum_bytes))
        self.captured = bytearray()
        self.close_source = bool(close_source)

    def read_frame(self, frame_bytes: int, timeout_seconds: float) -> bytes:
        frame = self.source.read_frame(frame_bytes, timeout_seconds)
        if not isinstance(frame, (bytes, bytearray, memoryview)):
            raise TypeError("diagnostic PCM source returned a non-bytes frame")
        actual_length = len(frame)
        immutable_frame = bytes(frame[:actual_length])
        if actual_length != int(frame_bytes):
            raise ValueError("diagnostic PCM source returned an incomplete frame")
        if len(self.captured) + len(immutable_frame) > self.maximum_bytes:
            raise RuntimeError("diagnostic_pcm_buffer_limit_exceeded")
        self.captured.extend(immutable_frame)
        return immutable_frame

    def close(self) -> None:
        if self.close_source:
            self.source.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)


class RollingPcmFrameSource:
    """Bounded replay of recent live frames across foreground VAD windows."""

    def __init__(
        self,
        source: Any,
        *,
        maximum_history_frames: int = 100,
        expected_frame_bytes: int = CANONICAL_PCM_FRAME_BYTES,
        clock: Any = time.monotonic,
    ):
        if maximum_history_frames < 1 or maximum_history_frames > 250:
            raise ValueError("maximum_history_frames must be between 1 and 250")
        self.source = source
        self.clock = clock
        self.maximum_history_frames = int(maximum_history_frames)
        if isinstance(expected_frame_bytes, bool) or int(expected_frame_bytes) <= 0:
            raise ValueError("expected_frame_bytes must be positive")
        self.expected_frame_bytes = int(expected_frame_bytes)
        self._history: deque[tuple[int, bytes]] = deque(
            maxlen=self.maximum_history_frames
        )
        self._replay: deque[tuple[int, bytes]] = deque()
        self.live_frame_count = 0
        self.replayed_frame_count = 0
        self.read_sequence = 0
        self.total_bytes_returned = 0
        self.total_live_bytes_read = 0
        self.last_source_frame_sequence = 0
        self.last_frame_was_replay = False
        self.last_frame_bytes = 0
        self.last_read_timestamp = 0.0
        self.candidate_reset_count = 0
        self.discarded_stale_byte_count = 0
        self.partial_reads = 0
        self.empty_reads = 0
        self.read_errors = 0
        self.zero_filled_bytes = 0
        self.repeated_frame_hashes = 0
        self.mutable_buffer_reuse_detected = 0
        self._last_live_frame_hash = b""
        self._last_mutable_source_id: Optional[int] = None
        self.closed = False

    def begin_window(self, pre_roll_frames: int) -> int:
        count = max(0, min(int(pre_roll_frames), self.maximum_history_frames))
        retained = list(self._history)[-count:] if count else []
        self._replay = deque(retained)
        return len(retained)

    def read_frame(self, frame_bytes: int, timeout_seconds: float) -> bytes:
        if self.closed:
            raise EOFError("persistent PCM stream is closed")
        if int(frame_bytes) != self.expected_frame_bytes:
            raise ValueError("persistent PCM frame size changed")
        if self._replay:
            source_sequence, frame = self._replay.popleft()
            if len(frame) != frame_bytes:
                raise ValueError("replayed PCM frame size changed")
            self.replayed_frame_count += 1
            self._record_returned_frame(
                frame,
                source_sequence=source_sequence,
                replayed=True,
            )
            return frame
        try:
            frame = self.source.read_frame(frame_bytes, timeout_seconds)
        except EOFError:
            self.empty_reads += 1
            raise
        except TimeoutError:
            raise
        except OSError:
            self.read_errors += 1
            raise
        if not isinstance(frame, (bytes, bytearray, memoryview)):
            raise TypeError("persistent PCM source returned a non-bytes frame")
        mutable_source_id: Optional[int] = None
        if isinstance(frame, bytearray):
            mutable_source_id = id(frame)
        elif isinstance(frame, memoryview) and not frame.readonly:
            mutable_source_id = id(frame.obj)
        if mutable_source_id is not None:
            if mutable_source_id == self._last_mutable_source_id:
                self.mutable_buffer_reuse_detected += 1
            self._last_mutable_source_id = mutable_source_id
        actual_length = len(frame)
        immutable_frame = bytes(frame[:actual_length])
        if actual_length != int(frame_bytes):
            self.partial_reads += 1
            raise ValueError("persistent PCM source returned an incomplete frame")
        self.live_frame_count += 1
        source_sequence = self.live_frame_count
        current_hash = hashlib.sha256(immutable_frame).digest()
        if self._last_live_frame_hash and current_hash == self._last_live_frame_hash:
            self.repeated_frame_hashes += 1
        self._last_live_frame_hash = current_hash
        self._history.append((source_sequence, immutable_frame))
        self.total_live_bytes_read += len(immutable_frame)
        self._record_returned_frame(
            immutable_frame,
            source_sequence=source_sequence,
            replayed=False,
        )
        return immutable_frame

    def _record_returned_frame(
        self,
        frame: bytes,
        *,
        source_sequence: int,
        replayed: bool,
    ) -> None:
        self.read_sequence += 1
        self.total_bytes_returned += len(frame)
        self.last_source_frame_sequence = int(source_sequence)
        self.last_frame_was_replay = bool(replayed)
        self.last_frame_bytes = len(frame)
        self.last_read_timestamp = float(self.clock())

    def clear_history(self) -> None:
        self._history.clear()
        self._replay.clear()

    def reset_candidate(self, frame_bytes: int, maximum_discard_frames: int) -> int:
        """Clear candidate history and bounded PCM accumulated during recognition."""

        self.clear_history()
        self.candidate_reset_count += 1
        maximum_bytes = max(0, int(frame_bytes) * int(maximum_discard_frames))
        discard = getattr(self.source, "discard_available", None)
        discarded_bytes = int(discard(maximum_bytes)) if callable(discard) else 0
        self.discarded_stale_byte_count += max(0, discarded_bytes)
        if discarded_bytes > 0:
            self._last_live_frame_hash = b""
        if frame_bytes <= 0:
            return 0
        return math.ceil(max(0, discarded_bytes) / frame_bytes)

    def snapshot(self) -> Dict[str, Any]:
        source_snapshot = getattr(self.source, "snapshot", None)
        low_level = source_snapshot() if callable(source_snapshot) else {}
        if not isinstance(low_level, dict):
            low_level = {}
        process_pid = int(low_level.get("process_pid", 0) or 0)
        process_alive_value = low_level.get("process_alive")
        process_liveness_observable = bool(
            low_level.get(
                "process_liveness_observable",
                isinstance(process_alive_value, bool) or process_pid > 0,
            )
        )
        return {
            "history_frame_count": len(self._history),
            "pending_replay_frame_count": len(self._replay),
            "live_frame_count": self.live_frame_count,
            "replayed_frame_count": self.replayed_frame_count,
            "read_sequence": self.read_sequence,
            "total_bytes_returned": self.total_bytes_returned,
            "total_live_bytes_read": self.total_live_bytes_read,
            "last_source_frame_sequence": self.last_source_frame_sequence,
            "last_frame_was_replay": self.last_frame_was_replay,
            "last_frame_bytes": self.last_frame_bytes,
            "last_read_timestamp": self.last_read_timestamp,
            "candidate_reset_count": self.candidate_reset_count,
            "discarded_stale_byte_count": self.discarded_stale_byte_count,
            "total_low_level_reads": int(
                low_level.get("total_low_level_reads", self.live_frame_count) or 0
            ),
            "valid_full_pcm_frames": self.live_frame_count,
            "valid_pcm_frames_delivered_to_vad": self.read_sequence,
            "fresh_full_pcm_frames": self.live_frame_count,
            "partial_reads": int(
                max(
                    int(low_level.get("partial_reads", 0) or 0),
                    self.partial_reads,
                )
            ),
            "empty_reads": int(
                max(
                    int(low_level.get("empty_reads", 0) or 0),
                    self.empty_reads,
                )
            ),
            "read_errors": int(
                max(
                    int(low_level.get("read_errors", 0) or 0),
                    self.read_errors,
                )
            ),
            "discarded_bytes": int(low_level.get("discarded_bytes", 0) or 0),
            "zero_filled_bytes": int(low_level.get("zero_filled_bytes", 0) or 0)
            + self.zero_filled_bytes,
            "repeated_frame_hashes": max(
                int(low_level.get("repeated_frame_hashes", 0) or 0),
                self.repeated_frame_hashes,
            ),
            "mutable_buffer_reuse_detected": max(
                int(low_level.get("mutable_buffer_reuse_detected", 0) or 0),
                self.mutable_buffer_reuse_detected,
            ),
            "valid_microphone_bytes_delivered_to_vad": self.total_bytes_returned,
            "fresh_microphone_bytes_delivered_to_vad": self.total_live_bytes_read,
            "pending_partial_bytes": int(
                low_level.get("pending_partial_bytes", 0) or 0
            ),
            "accumulated_partial_bytes": int(
                low_level.get("accumulated_partial_bytes", 0) or 0
            ),
            "low_level_read_size_counts": dict(
                low_level.get("low_level_read_size_counts", {}) or {}
            ),
            "expected_frame_bytes": self.expected_frame_bytes,
            "process_pid": process_pid,
            "process_exit_status": low_level.get("process_exit_status"),
            "process_alive": (
                bool(process_alive_value)
                if isinstance(process_alive_value, bool)
                else True
            ),
            "process_liveness_observable": process_liveness_observable,
            "eof_count": int(low_level.get("eof_count", 0) or 0),
            "unexpected_eof_count": int(
                low_level.get("unexpected_eof_count", 0) or 0
            ),
            "dead_process_detected": bool(
                low_level.get("dead_process_detected", False)
            ),
            "stream_ended": bool(low_level.get("stream_ended", False)),
            "terminal_reason": str(low_level.get("terminal_reason", "") or ""),
            "transport_argv": list(low_level.get("transport_argv", []) or []),
            "stdout_transport_mode": str(
                low_level.get("stdout_transport_mode", "") or ""
            ),
            "stderr_transport_mode": str(
                low_level.get("stderr_transport_mode", "") or ""
            ),
            "maximum_consecutive_repeated_non_silent_frames": int(
                low_level.get(
                    "maximum_consecutive_repeated_non_silent_frames",
                    0,
                )
                or 0
            ),
            "pathological_duplicate_frame_detected": bool(
                low_level.get("pathological_duplicate_frame_detected", False)
            ),
            "integrity_failure_count": int(
                low_level.get("integrity_failure_count", 0) or 0
            ),
            "integrity_guard_discarded_frames": int(
                low_level.get("integrity_guard_discarded_frames", 0) or 0
            ),
            "integrity_guard_discarded_bytes": int(
                low_level.get("integrity_guard_discarded_bytes", 0) or 0
            ),
            "maximum_consecutive_tiny_rms_frames": int(
                low_level.get("maximum_consecutive_tiny_rms_frames", 0) or 0
            ),
            "queue_depth_frames": int(
                low_level.get("queue_depth_frames", 0) or 0
            ),
            "queue_depth_bytes": int(
                low_level.get("queue_depth_bytes", 0) or 0
            ),
            "queue_overflow_dropped_frames": int(
                low_level.get("queue_overflow_dropped_frames", 0) or 0
            ),
            "queue_overflow_dropped_bytes": int(
                low_level.get("queue_overflow_dropped_bytes", 0) or 0
            ),
            "candidate_reset_discarded_frames": int(
                low_level.get("candidate_reset_discarded_frames", 0) or 0
            ),
            "candidate_reset_discarded_bytes": int(
                low_level.get("candidate_reset_discarded_bytes", 0) or 0
            ),
            "reset_boundary_timeouts": int(
                low_level.get("reset_boundary_timeouts", 0) or 0
            ),
            "controlled_stop": dict(low_level.get("controlled_stop", {}) or {}),
            "closed": self.closed,
        }

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.source.close()
        except (OSError, RuntimeError):
            self.closed = False
            raise
        self.closed = True
        self._history.clear()
        self._replay.clear()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)


class LinuxAlsaMicrophoneAdapter(MicrophoneAdapter):
    """Linux ALSA microphone adapter backed by arecord.

    This adapter is hardware-specific and remains outside the Brain. It performs
    bounded one-shot capture and an explicitly owned foreground PCM stream. It
    does not start STT, classify wake words, access the internet, or emit audio.
    """

    def __init__(
        self,
        device: Optional[str] = None,
        record_seconds: int = DEFAULT_ALSA_RECORD_SECONDS,
        sample_rate_hz: int = DEFAULT_ALSA_SAMPLE_RATE_HZ,
        channels: int = DEFAULT_ALSA_CHANNELS,
        sample_format: str = DEFAULT_ALSA_SAMPLE_FORMAT,
        timeout_seconds: Optional[float] = None,
        arecord_command: str = "arecord",
        runner: Optional[SafeSubprocessRunner] = None,
        stream_runner: Optional[SafePcmStreamRunner] = None,
        voice_activity_capture: Optional[RmsVoiceActivityCapture] = None,
        source: str = "linux_alsa_microphone_adapter",
    ):
        self.device = _normalize_optional_device(device)
        self.record_seconds = _bounded_record_seconds(record_seconds)
        self.sample_rate_hz = _positive_int(sample_rate_hz, "sample_rate_hz")
        self.channels = _positive_int(channels, "channels")
        self.sample_format = str(sample_format or DEFAULT_ALSA_SAMPLE_FORMAT).strip()
        self.timeout_seconds = _bounded_timeout(
            timeout_seconds
            if timeout_seconds is not None
            else self.record_seconds + DEFAULT_ALSA_TIMEOUT_PADDING_SECONDS
        )
        self.arecord_command = str(arecord_command or "arecord").strip()
        self.runner = runner or SafeSubprocessRunner()
        self.stream_runner = stream_runner or SafePcmStreamRunner()
        self.voice_activity_capture = voice_activity_capture or RmsVoiceActivityCapture()
        self.source = source
        self.started = False
        self.start_count = 0
        self.stop_count = 0
        self.read_count = 0
        self.record_count = 0
        self.audio_hardware_accessed = False
        self._stream_lock = RLock()
        self._active_stream: Optional[Any] = None
        self._active_stream_owner = ""
        self._persistent_stream: Optional[PersistentPcmStreamHandle] = None
        self._last_pcm_stop_result: Dict[str, Any] = {}
        self.persistent_stream_open_count = 0
        self.persistent_stream_close_count = 0

    def start(self) -> MicrophoneResult:
        self.start_count += 1
        health = self.health_check()
        if not health.success:
            return health
        vad_start = self.voice_activity_capture.start()
        if not vad_start.success:
            return self._failure(
                status=VAD_STATUS_DEVICE_ERROR,
                text="Linux ALSA microphone VAD component failed to start.",
                error_message=vad_start.error_message or vad_start.status,
                data={"voice_activity_capture": vad_start.to_dict()},
            )
        self.started = True
        return self._success(
            status="started",
            text="Linux ALSA microphone adapter is ready for one-shot recording.",
            data={"health": health.to_dict()},
        )

    def preflight_pcm_stream(
        self,
        *,
        device: Optional[str] = None,
        frame_duration_ms: int = CANONICAL_PCM_FRAME_DURATION_MS,
        frame_read_timeout_seconds: float = 1.0,
        diagnostic_traceback: bool = False,
        owner: str = "diagnostic_active_capture",
    ) -> MicrophoneResult:
        """Open the production raw transport and validate one complete frame.

        ``start()`` verifies dependencies but deliberately does not open ALSA.
        This bounded preflight crosses the real hardware boundary using the same
        command and stream runner as one-shot VAD capture.  A structurally valid
        all-zero frame is accepted: signal amplitude is an observation, not an
        open/read criterion.
        """

        started_at = time.monotonic()
        clean_owner = str(owner or "")
        expected_samples = CANONICAL_PCM_SAMPLES_PER_FRAME
        expected_frame_bytes = (
            expected_samples * CANONICAL_CHANNELS * CANONICAL_SAMPLE_WIDTH_BYTES
        )
        requested_device = self.device
        resolved_device = self.device
        command: List[str] = []
        source: Optional[Any] = None
        first_frame = b""
        source_before_close: Dict[str, Any] = {}
        source_after_close: Dict[str, Any] = {}
        open_called = False
        open_success = False
        read_called = False
        read_success = False
        close_called = False
        close_success = source is None
        close_attempts = 0
        failure_reason = ""
        failing_method = ""
        caught_error: Optional[BaseException] = None
        exception_traceback = ""
        last_close_error: Optional[BaseException] = None
        last_close_traceback = ""

        with self._stream_lock:
            ownership_before = self._active_stream_owner

        try:
            clean_owner = _normalize_stream_owner(owner)
            requested_device = (
                _normalize_optional_device(device) if device is not None else self.device
            )
            resolved_device = resolve_alsa_capture_device(
                requested_device,
                require_conversion=True,
            )
            if not self.started:
                raise RuntimeError("microphone_not_started")
            if int(frame_duration_ms) != CANONICAL_PCM_FRAME_DURATION_MS:
                raise ValueError(
                    "PCM preflight requires canonical 20 ms frames"
                )
            if (
                isinstance(frame_read_timeout_seconds, bool)
                or not 0.01 <= float(frame_read_timeout_seconds) <= 5.0
            ):
                raise ValueError(
                    "frame_read_timeout_seconds must be between 0.01 and 5"
                )
            if self.sample_format.upper() != DEFAULT_ALSA_SAMPLE_FORMAT:
                raise ValueError("voice_activity_capture_requires_s16_le")
            arecord_path = self._find_arecord()
            if not arecord_path:
                raise FileNotFoundError("arecord_missing")
            command = self._stream_command(
                arecord_path=arecord_path,
                device=resolved_device,
            )
            failing_method = "SafePcmStreamRunner.start"
            with self._stream_lock:
                if self._active_stream is not None:
                    raise RuntimeError(
                        "microphone_capture_already_owned:"
                        f"{self._active_stream_owner or 'unknown'}"
                    )
                open_called = True
                source = self.stream_runner.start(command)
                if not callable(getattr(source, "read_frame", None)):
                    raise TypeError(
                        "PCM stream runner returned an invalid frame source"
                    )
                self._active_stream = source
                self._active_stream_owner = clean_owner
                open_success = True
                self.audio_hardware_accessed = True

            failing_method = "read_frame"
            read_called = True
            frame = source.read_frame(
                expected_frame_bytes,
                float(frame_read_timeout_seconds),
            )
            if not isinstance(frame, (bytes, bytearray, memoryview)):
                failure_reason = "invalid_frame_error"
                raise TypeError("PCM preflight returned a non-bytes frame")
            first_frame = bytes(frame[: len(frame)])
            if len(first_frame) != expected_frame_bytes:
                failure_reason = "invalid_frame_error"
                raise ValueError(
                    "PCM preflight returned an incomplete frame:"
                    f"{len(first_frame)}:expected:{expected_frame_bytes}"
                )
            if len(first_frame) % CANONICAL_SAMPLE_WIDTH_BYTES:
                failure_reason = "invalid_frame_error"
                raise ValueError("PCM preflight returned an odd-byte frame")
            read_success = True
        except (EOFError, FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as error:
            caught_error = error
            if bool(diagnostic_traceback):
                exception_traceback = traceback.format_exc()
            if not failure_reason:
                failure_reason = (
                    "microphone_open_error"
                    if not open_success
                    else "pcm_read_error"
                )
        finally:
            if source is not None:
                source_before_close = _safe_pcm_source_snapshot(source)
                close_called = True
                for _ in range(2):
                    close_attempts += 1
                    try:
                        close_source = getattr(source, "close", None)
                        if not callable(close_source):
                            raise TypeError(
                                "PCM stream source does not support close"
                            )
                        close_source()
                        close_success = True
                        break
                    except Exception as close_error:
                        close_success = False
                        last_close_error = close_error
                        if bool(diagnostic_traceback):
                            last_close_traceback = traceback.format_exc()
                source_after_close = _safe_pcm_source_snapshot(source)
                if close_success:
                    with self._stream_lock:
                        if self._active_stream is source:
                            self._active_stream = None
                            self._active_stream_owner = ""
                elif caught_error is None and last_close_error is not None:
                    caught_error = last_close_error
                    failing_method = "close"
                    failure_reason = "microphone_cleanup_error"
                    if bool(diagnostic_traceback):
                        exception_traceback = last_close_traceback

        with self._stream_lock:
            ownership_after = self._active_stream_owner
        source_snapshot = {
            **source_before_close,
            **source_after_close,
        }
        controlled_stop = dict(source_snapshot.get("controlled_stop") or {})
        if controlled_stop and controlled_stop.get("status") != "not_stopped":
            controlled_stop["unexpected_ownership_loss"] = bool(
                ownership_after
            )
            if ownership_after:
                controlled_stop.update(
                    {
                        "unexpected_failure": True,
                        "status": "unexpected_ownership_loss",
                        "final_health_effect": "unhealthy",
                    }
                )
            self._remember_pcm_stop_result(controlled_stop)
        if (
            caught_error is None
            and controlled_stop
            and controlled_stop.get("final_health_effect") == "unhealthy"
        ):
            caught_error = RuntimeError(
                "pcm_stream_stop_unhealthy:"
                + str(controlled_stop.get("status") or "unexpected_failure")
            )
            failing_method = "close"
            failure_reason = "microphone_cleanup_error"
        if (
            caught_error is not None
            and open_success
            and not read_success
            and (
                source_snapshot.get("process_exit_status") is not None
                or "open error" in str(
                    source_snapshot.get("stderr_preview", "")
                    or source_snapshot.get("stderr", "")
                    or ""
                ).casefold()
            )
        ):
            failure_reason = "microphone_open_error"
        samples = (
            decode_s16_le_samples(first_frame)
            if len(first_frame) == expected_frame_bytes
            else []
        )
        process_pid = int(source_snapshot.get("process_pid", 0) or 0)
        stderr_text = str(
            getattr(source, "stderr", "") if source is not None else ""
        ) or str(
            source_snapshot.get("stderr_preview", "")
            or source_snapshot.get("stderr", "")
            or ""
        )
        cleanup_result = (
            "completed"
            if source is not None and close_success and not ownership_after
            else "not_required"
            if source is None
            else "incomplete"
        )
        data: Dict[str, Any] = {
            "adapter_class": (
                f"{self.__class__.__module__}.{self.__class__.__qualname__}"
            ),
            "failing_method": failing_method if caught_error is not None else "",
            "microphone_device": requested_device or "",
            "requested_device": requested_device or "",
            "resolved_capture_device": resolved_device or "",
            "requested_pcm_format": {
                "sample_rate_hz": CANONICAL_SAMPLE_RATE_HZ,
                "channels": CANONICAL_CHANNELS,
                "sample_width_bytes": CANONICAL_SAMPLE_WIDTH_BYTES,
                "sample_format": CANONICAL_PCM_SAMPLE_FORMAT,
                "frame_duration_ms": int(frame_duration_ms),
                "samples_per_frame": expected_samples,
                "expected_frame_bytes": expected_frame_bytes,
            },
            "stream_lifecycle_state": (
                "closed"
                if close_success and source is not None
                else "open"
                if source is not None
                else "not_opened"
            ),
            "adapter_started": self.started,
            "start_called": self.start_count > 0,
            "open_called": open_called,
            "open_success": open_success,
            "read_called": read_called,
            "first_pcm_read_success": read_success,
            "first_frame_byte_count": len(first_frame),
            "expected_frame_byte_count": expected_frame_bytes,
            "first_frame_nonzero": any(first_frame),
            "first_frame_sample_count": len(samples),
            "first_frame_minimum_sample": min(samples, default=0),
            "first_frame_maximum_sample": max(samples, default=0),
            "first_frame_rms": (
                calculate_s16_le_rms(first_frame)
                if len(first_frame) == expected_frame_bytes
                else 0.0
            ),
            "process_id": os.getpid(),
            "alsa_child_process_id": process_pid,
            "alsa_process_exit_status": source_snapshot.get(
                "process_exit_status"
            ),
            "alsa_stderr": stderr_text,
            "exact_capture_command": list(command),
            "stdout_transport_mode": str(
                source_snapshot.get("stdout_transport_mode", "raw_pcm_pipe")
                or "raw_pcm_pipe"
            ),
            "microphone_ownership_before": ownership_before,
            "microphone_ownership_acquired": bool(
                open_success and clean_owner
            ),
            "microphone_ownership_owner": clean_owner,
            "microphone_ownership_after": ownership_after,
            "microphone_ownership_released": not bool(ownership_after),
            "close_called": close_called,
            "close_attempts": close_attempts,
            "close_success": close_success,
            "cleanup_result": cleanup_result,
            "controlled_stop": controlled_stop,
            "failure_reason": failure_reason,
            "source_snapshot": source_snapshot,
            "processing_time_seconds": round(time.monotonic() - started_at, 6),
        }
        if caught_error is not None:
            data["exception_class"] = caught_error.__class__.__name__
            data["exception_message"] = str(caught_error)
            if bool(diagnostic_traceback):
                data["traceback"] = exception_traceback
            return self._failure(
                status=failure_reason or "pcm_preflight_error",
                text="Linux ALSA PCM preflight failed.",
                error_message=(
                    f"{caught_error.__class__.__name__}:{str(caught_error)}"
                ),
                data=data,
            )
        if not close_success:
            return self._failure(
                status="microphone_cleanup_error",
                text="Linux ALSA PCM preflight cleanup was incomplete.",
                error_message="microphone_preflight_cleanup_incomplete",
                data=data,
            )
        return self._success(
            status="pcm_preflight_passed",
            text="Linux ALSA PCM preflight received one complete canonical frame.",
            data=data,
        )

    def stop(self) -> MicrophoneResult:
        self.stop_count += 1
        try:
            self.cancel_current()
        except (OSError, RuntimeError) as error:
            vad_stop = self.voice_activity_capture.stop()
            return self._failure(
                status="stop_failed",
                text="Linux ALSA microphone adapter could not release capture ownership.",
                error_message=f"{error.__class__.__name__}:{str(error)[:120]}",
                data={"voice_activity_capture": vad_stop.to_dict()},
            )
        vad_stop = self.voice_activity_capture.stop()
        self.started = False
        return self._success(
            status="stopped",
            text="Linux ALSA microphone adapter stopped. No background capture is running.",
            data={"voice_activity_capture": vad_stop.to_dict()},
        )

    def cancel_current(self) -> None:
        with self._stream_lock:
            stream = self._active_stream
            handle = self._persistent_stream
        if stream is not None:
            try:
                stream.close()
            except (OSError, RuntimeError):
                raise
            self._remember_pcm_stop_result(
                dict(_safe_pcm_source_snapshot(stream).get("controlled_stop") or {})
            )
        with self._stream_lock:
            if self._active_stream is stream:
                self._active_stream = None
                self._active_stream_owner = ""
            if self._persistent_stream is handle:
                self._persistent_stream = None
        if handle is not None and not handle.closed:
            handle.closed = True
            self.persistent_stream_close_count += 1

    def open_persistent_stream(
        self,
        *,
        owner: str,
        device: Optional[str] = None,
    ) -> PersistentPcmStreamHandle:
        """Open one canonical raw PCM stream for a named foreground owner."""

        clean_owner = _normalize_stream_owner(owner)
        requested_device = (
            _normalize_optional_device(device) if device is not None else self.device
        )
        resolved_device = resolve_alsa_capture_device(
            requested_device,
            require_conversion=True,
        )
        with self._stream_lock:
            current = self._persistent_stream
            if (
                current is not None
                and not current.closed
                and current.owner == clean_owner
                and current.resolved_device == (resolved_device or "")
            ):
                return current
            if self._active_stream is not None:
                raise RuntimeError(
                    "microphone_capture_already_owned:"
                    f"{self._active_stream_owner or 'unknown'}"
                )
            if not self.started:
                raise RuntimeError("microphone_not_started")
            if self.sample_format.upper() != DEFAULT_ALSA_SAMPLE_FORMAT:
                raise ValueError("persistent_pcm_stream_requires_s16_le")
            arecord_path = self._find_arecord()
            if not arecord_path:
                raise FileNotFoundError("arecord_missing")
            command = self._stream_command(
                arecord_path=arecord_path,
                device=resolved_device,
            )
            raw_source = self.stream_runner.start(command)
            source = RollingPcmFrameSource(
                raw_source,
                expected_frame_bytes=CANONICAL_PCM_FRAME_BYTES,
            )
            self.persistent_stream_open_count += 1
            process_id = getattr(getattr(raw_source, "process", None), "pid", None)
            stream_id = f"alsa-pcm-stream-{self.persistent_stream_open_count}"
            handle = PersistentPcmStreamHandle(
                stream_id=stream_id,
                owner=clean_owner,
                requested_device=requested_device or "",
                resolved_device=resolved_device or "",
                command=tuple(command),
                frame_source=source,
                opened_at=time.monotonic(),
                alsa_handle_id=(
                    f"arecord-pid-{int(process_id)}"
                    if isinstance(process_id, int) and process_id > 0
                    else f"{stream_id}-handle"
                ),
            )
            self._active_stream = source
            self._active_stream_owner = clean_owner
            self._persistent_stream = handle
            self.audio_hardware_accessed = True
            return handle

    def close_persistent_stream(
        self,
        handle: PersistentPcmStreamHandle,
        *,
        owner: str,
    ) -> MicrophoneResult:
        clean_owner = _normalize_stream_owner(owner)
        if not isinstance(handle, PersistentPcmStreamHandle):
            return self._failure(
                status="invalid_stream_handle",
                text="Persistent ALSA stream handle is invalid.",
                error_message="invalid_persistent_pcm_stream_handle",
            )
        with self._stream_lock:
            if handle.closed:
                return self._success(
                    status="already_closed",
                    text="Persistent ALSA stream was already closed.",
                )
            if handle.owner != clean_owner:
                return self._failure(
                    status="stream_owner_mismatch",
                    text="Persistent ALSA stream owner did not match.",
                    error_message="persistent_pcm_stream_owner_mismatch",
                )
            if self._persistent_stream is not handle:
                return self._failure(
                    status="stream_not_active",
                    text="Persistent ALSA stream is not the active stream.",
                    error_message="persistent_pcm_stream_not_active",
                )
        try:
            handle.frame_source.close()
        except (OSError, RuntimeError) as error:
            return self._failure(
                status="stream_close_failed",
                text="Persistent ALSA stream failed to close cleanly.",
                error_message=f"{error.__class__.__name__}:{str(error)[:120]}",
            )
        controlled_stop = dict(
            _safe_pcm_source_snapshot(handle.frame_source).get(
                "controlled_stop",
                {},
            )
            or {}
        )
        self._remember_pcm_stop_result(controlled_stop)
        with self._stream_lock:
            if self._persistent_stream is handle:
                self._active_stream = None
                self._active_stream_owner = ""
                self._persistent_stream = None
        handle.closed = True
        self.persistent_stream_close_count += 1
        if (
            controlled_stop
            and controlled_stop.get("final_health_effect") == "unhealthy"
        ):
            return self._failure(
                status="stream_close_unhealthy",
                text="Persistent ALSA stream closed after an unexpected transport failure.",
                error_message=(
                    "pcm_stream_stop_unhealthy:"
                    + str(
                        controlled_stop.get("status")
                        or "unexpected_failure"
                    )
                ),
                data={
                    "stream_id": handle.stream_id,
                    "owner": clean_owner,
                    "controlled_stop": controlled_stop,
                    "microphone_ownership_released": True,
                },
            )
        return self._success(
            status="closed",
            text="Persistent ALSA stream closed.",
            data={
                "stream_id": handle.stream_id,
                "owner": clean_owner,
                "controlled_stop": controlled_stop,
            },
        )

    def persistent_stream_snapshot(self) -> Dict[str, Any]:
        with self._stream_lock:
            handle = self._persistent_stream
            frame_source = handle.frame_source if handle is not None else None
            rolling = getattr(frame_source, "snapshot", None)
            return {
                "active": bool(handle is not None and not handle.closed),
                "stream_id": handle.stream_id if handle is not None else "",
                "alsa_handle_id": handle.alsa_handle_id if handle is not None else "",
                "owner": self._active_stream_owner,
                "requested_device": handle.requested_device if handle is not None else "",
                "resolved_device": handle.resolved_device if handle is not None else "",
                "pcm_contract": (
                    {
                        "sample_rate_hz": handle.sample_rate_hz,
                        "channels": handle.channels,
                        "sample_width_bytes": handle.sample_width_bytes,
                        "sample_format": handle.sample_format,
                        "frame_duration_ms": handle.frame_duration_ms,
                        "samples_per_frame": handle.samples_per_frame,
                        "frame_bytes": handle.frame_bytes,
                        "format_verification_status": (
                            handle.format_verification_status
                        ),
                    }
                    if handle is not None
                    else canonical_pcm_contract()
                ),
                "open_count": self.persistent_stream_open_count,
                "close_count": self.persistent_stream_close_count,
                "rolling_pre_roll": rolling() if callable(rolling) else {},
                "shell": False,
                "safe": True,
            }

    def calibrate_persistent_stream(
        self,
        handle: PersistentPcmStreamHandle,
        request: VoiceActivityCaptureRequestV1,
        *,
        cancel_requested: Optional[CancelCheck | Any] = None,
    ) -> VoiceActivityStreamCalibration:
        source = self._validated_persistent_stream(
            handle,
            requested_device=request.microphone_device,
        )
        return self.voice_activity_capture.calibrate_stream(
            request,
            source,
            cancel_requested=cancel_requested,
        )

    def record_persistent_until_silence(
        self,
        handle: PersistentPcmStreamHandle,
        output_path: str | Path,
        **kwargs: Any,
    ) -> VoiceActivityCaptureResultV1:
        if "persistent_stream" in kwargs:
            raise ValueError("persistent_stream is supplied by the stream handle")
        frame_duration_ms = int(kwargs.get("frame_duration_ms", 20))
        pre_roll_seconds = float(kwargs.get("pre_roll_seconds", 0.0))
        pre_roll_frames = max(
            0,
            math.ceil(pre_roll_seconds / (frame_duration_ms / 1000.0)),
        )
        prepare = getattr(handle.frame_source, "begin_window", None)
        if callable(prepare):
            prepare(pre_roll_frames)
        result = self.record_until_silence(
            output_path,
            persistent_stream=handle,
            **kwargs,
        )
        if str(getattr(result, "status", "")) in {
            VAD_STATUS_DEVICE_ERROR,
            VAD_STATUS_INVALID_AUDIO,
            VAD_STATUS_TIMEOUT,
        }:
            clear = getattr(handle.frame_source, "clear_history", None)
            if callable(clear):
                clear()
        return result

    def reset_persistent_candidate(
        self,
        handle: PersistentPcmStreamHandle,
        *,
        frame_duration_ms: int = 20,
        maximum_discard_seconds: float = 2.0,
    ) -> Dict[str, Any]:
        """Reset only per-candidate PCM state without reopening the ALSA stream."""

        source = self._validated_persistent_stream(
            handle,
            requested_device=handle.requested_device,
        )
        if not 10 <= int(frame_duration_ms) <= 40:
            raise ValueError("frame_duration_ms must be between 10 and 40")
        if (
            isinstance(maximum_discard_seconds, bool)
            or not isinstance(maximum_discard_seconds, (int, float))
            or not math.isfinite(float(maximum_discard_seconds))
            or not 0.0 <= float(maximum_discard_seconds) <= 3.0
        ):
            raise ValueError("maximum_discard_seconds must be between 0 and 3")
        frame_bytes = (
            pcm_frame_sample_count(CANONICAL_SAMPLE_RATE_HZ, int(frame_duration_ms))
            * CANONICAL_CHANNELS
            * CANONICAL_SAMPLE_WIDTH_BYTES
        )
        maximum_frames = math.ceil(
            float(maximum_discard_seconds) / (int(frame_duration_ms) / 1000.0)
        )
        reset = getattr(source, "reset_candidate", None)
        stale_frames = (
            int(reset(frame_bytes, maximum_frames)) if callable(reset) else 0
        )
        return {
            "success": True,
            "status": "candidate_state_reset",
            "stale_pcm_frames_discarded": stale_frames,
            "stream_id": handle.stream_id,
            "stream_remained_open": True,
            "safe": True,
        }

    def read_chunk(
        self,
        timeout_seconds: Optional[float] = None,
        cancel_requested: Optional[CancelCheck | Any] = None,
    ) -> MicrophoneResult:
        self.read_count += 1
        if _is_cancelled(cancel_requested):
            return self._failure(
                status="cancelled",
                text="Linux ALSA microphone read was cancelled before recording.",
                error_message="microphone_read_cancelled",
                data={"timeout_seconds": timeout_seconds},
            )
        if not self.started:
            return self._failure(
                status="not_started",
                text="Linux ALSA microphone must be started before reading audio.",
                error_message="microphone_not_started",
                data={"timeout_seconds": timeout_seconds},
            )

        with tempfile.TemporaryDirectory(prefix="ares_alsa_capture_") as temp_dir:
            wav_path = Path(temp_dir) / "capture.wav"
            return self.record_wav(
                wav_path,
                seconds=self.record_seconds,
                timeout_seconds=timeout_seconds,
                overwrite=True,
                temporary_output=True,
            )

    def get_status(self) -> MicrophoneResult:
        arecord_path = self._find_arecord()
        return self._success(
            status="started" if self.started else "stopped",
            text="Linux ALSA microphone adapter status discovered.",
            data={
                "source": self.source,
                "started": self.started,
                "arecord_available": bool(arecord_path),
                "arecord_path": arecord_path or "",
                "selected_device": self.device or "",
                "record_seconds": self.record_seconds,
                "voice_activity_capture_state": self.voice_activity_capture.state,
                "sample_rate_hz": self.sample_rate_hz,
                "channels": self.channels,
                "sample_format": self.sample_format,
                "timeout_seconds": self.timeout_seconds,
                "background_listening": "disabled",
                "stt": "not_configured",
            },
        )

    def get_capabilities(self) -> MicrophoneResult:
        return self._success(
            status="capabilities",
            text="Linux ALSA microphone adapter capabilities discovered.",
            data={
                "source": self.source,
                "supported_modes": ["arecord_wav_capture", "arecord_pcm_rms_auto_stop"],
                "supports_device_selection": True,
                "supports_capture_device_listing": True,
                "writes_wav_file": True,
                "sample_rate_hz": self.sample_rate_hz,
                "channels": self.channels,
                "sample_format": self.sample_format,
                "timeout_handling": "safe_timeout_result",
                "voice_activity_detection": "pcm_frame_rms_hysteresis",
                "automatic_end_of_speech": True,
                "canonical_pcm_contract": canonical_pcm_contract(),
                "pcm_frame_ownership": "immutable_bytes_copy",
                "partial_read_policy": "accumulate_until_complete_frame",
                "empty_read_policy": "fail_without_zero_fill",
                "background_listening": "disabled",
                "stt": "not_configured",
                "wake_word": "disabled",
                "internet": "disabled",
            },
        )

    def health_check(self) -> MicrophoneResult:
        devices = self.list_capture_devices()
        if not devices.success:
            return devices
        if self.device and _device_looks_like_hw(self.device):
            available = {device["alsa_device"] for device in devices.data.get("devices", [])}
            hardware_device = _hardware_device_id(self.device)
            if hardware_device not in available:
                return self._failure(
                    status=ALSA_STATUS_INVALID_DEVICE,
                    text=f"Selected ALSA capture device is not listed: {self.device}",
                    error_message="alsa_device_not_found",
                    data={
                        "selected_device": self.device,
                        "hardware_device": hardware_device,
                        "available_devices": sorted(available),
                    },
                )
        vad_health = self.voice_activity_capture.health_check()
        if not vad_health.success:
            return self._failure(
                status=VAD_STATUS_DEVICE_ERROR,
                text="Linux ALSA microphone VAD health check failed.",
                error_message=vad_health.error_message or vad_health.status,
                data={"voice_activity_capture": vad_health.to_dict()},
            )
        return self._success(
            status="healthy",
            text="Linux ALSA microphone health check passed.",
            data={
                "arecord_available": True,
                "device_count": len(devices.data.get("devices", [])),
                "selected_device": self.device or "",
                "devices": devices.data.get("devices", []),
                "voice_activity_capture": vad_health.to_dict(),
                "previous_controlled_stop_result": dict(
                    self._last_pcm_stop_result
                ),
            },
        )

    def list_capture_devices(self) -> MicrophoneResult:
        arecord_path = self._find_arecord()
        if not arecord_path:
            return self._failure(
                status=ALSA_STATUS_ARECORD_MISSING,
                text="Linux ALSA microphone adapter could not find arecord.",
                error_message="arecord_missing",
            )

        result = self.runner.run([arecord_path, "-l"], timeout_seconds=min(self.timeout_seconds, 10.0))
        if result.timed_out:
            return self._failure(
                status="device_list_timeout",
                text="Timed out while listing ALSA capture devices.",
                error_message="arecord_device_list_timeout",
                data={"process": _safe_process_data(result)},
            )
        if result.returncode != 0:
            return self._failure(
                status="device_list_failed",
                text="arecord failed while listing capture devices.",
                error_message=f"arecord_exit_{result.returncode}",
                data={"process": _safe_process_data(result)},
            )

        devices = parse_arecord_capture_devices(result.stdout)
        if not devices:
            return self._failure(
                status=ALSA_STATUS_NO_CAPTURE_DEVICE,
                text="arecord is available, but no ALSA capture devices were found.",
                error_message="no_capture_device",
                data={"process": _safe_process_data(result), "devices": []},
            )

        return self._success(
            status="devices",
            text=f"Detected {len(devices)} ALSA capture device(s).",
            data={"devices": devices, "process": _safe_process_data(result)},
        )

    def record_wav(
        self,
        output_path: str | Path,
        seconds: Optional[int] = None,
        device: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        overwrite: bool = False,
        temporary_output: bool = False,
        diagnostic_audio: bool = False,
    ) -> MicrophoneResult:
        """Record a WAV, then atomically normalize its actual format to canonical PCM."""

        self.record_count += 1
        arecord_path = self._find_arecord()
        if not arecord_path:
            return self._failure(
                status=ALSA_STATUS_ARECORD_MISSING,
                text="Linux ALSA microphone adapter could not find arecord.",
                error_message="arecord_missing",
            )

        try:
            requested_device = (
                _normalize_optional_device(device) if device is not None else self.device
            )
            resolved_device = resolve_alsa_capture_device(
                requested_device,
                require_conversion=False,
            )
            duration = _bounded_record_seconds(seconds or self.record_seconds)
            timeout = _bounded_timeout(
                timeout_seconds
                if timeout_seconds is not None
                else duration + DEFAULT_ALSA_TIMEOUT_PADDING_SECONDS
            )
            wav_path = Path(output_path).expanduser()
            _validate_output_path(wav_path, overwrite=overwrite)
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            raw_wav_path = _unique_raw_wav_path(wav_path)
        except ValueError as error:
            return self._failure(
                status=ALSA_STATUS_INVALID_DEVICE if "device" in str(error) else "invalid_request",
                text="Linux ALSA microphone request was rejected before recording.",
                error_message=str(error),
            )
        except OSError as error:
            return self._failure(
                status="output_path_error",
                text="Linux ALSA microphone could not prepare the WAV output path.",
                error_message=f"output_path_error:{error.__class__.__name__}",
            )

        command = self._record_command(
            arecord_path=arecord_path,
            wav_path=raw_wav_path,
            seconds=duration,
            device=resolved_device,
        )
        result = self.runner.run(command, timeout_seconds=timeout)
        self.audio_hardware_accessed = True

        base_capture_data = {
            "process": _safe_process_data(result),
            "requested_device": requested_device or "",
            "resolved_capture_device": resolved_device or "",
            "requested_sample_rate_hz": self.sample_rate_hz,
            "raw_wav_path": str(raw_wav_path) if diagnostic_audio else "",
            "normalized_wav_path": str(wav_path),
            "diagnostic_audio": bool(diagnostic_audio),
        }

        if result.timed_out:
            _discard_unrequested_raw(raw_wav_path, diagnostic_audio)
            return self._failure(
                status=ALSA_STATUS_RECORDING_TIMEOUT,
                text=f"arecord timed out after {timeout} seconds.",
                error_message="arecord_recording_timeout",
                data=base_capture_data,
            )
        if result.returncode != 0:
            status = (
                ALSA_STATUS_INVALID_DEVICE
                if _stderr_indicates_invalid_device(result.stderr)
                else ALSA_STATUS_RECORDING_FAILED
            )
            _discard_unrequested_raw(raw_wav_path, diagnostic_audio)
            return self._failure(
                status=status,
                text="arecord failed while recording microphone audio.",
                error_message=f"arecord_exit_{result.returncode}",
                data=base_capture_data,
            )

        raw_validation = _validate_wav_file(raw_wav_path)
        if not raw_validation["success"]:
            _discard_unrequested_raw(raw_wav_path, diagnostic_audio)
            return self._failure(
                status=str(raw_validation["status"]),
                text=str(raw_validation["text"]),
                error_message=str(raw_validation["error_message"]),
                data={
                    **base_capture_data,
                    "raw_validation": raw_validation,
                },
            )

        normalization = normalize_wav_audio(raw_wav_path, wav_path, overwrite=True)
        if not normalization.success:
            _discard_unrequested_raw(raw_wav_path, diagnostic_audio)
            return self._failure(
                status=ALSA_STATUS_INVALID_WAV,
                text="Recorded ALSA audio could not be normalized safely.",
                error_message=normalization.error_message or normalization.status,
                data={
                    **base_capture_data,
                    "raw_validation": raw_validation,
                    "normalization": normalization.to_dict(),
                },
            )

        normalized_validation = validate_canonical_wav(wav_path)
        if not normalized_validation.get("success"):
            _discard_unrequested_raw(raw_wav_path, diagnostic_audio)
            return self._failure(
                status=ALSA_STATUS_INVALID_WAV,
                text="Normalized ALSA output did not satisfy the canonical PCM contract.",
                error_message=str(
                    normalized_validation.get("error_message") or "canonical_validation_failed"
                ),
                data={
                    **base_capture_data,
                    "raw_validation": raw_validation,
                    "normalization": normalization.to_dict(),
                    "normalized_validation": normalized_validation,
                },
            )

        chunk = read_audio_chunk_wav(
            wav_path,
            source=self.source,
        )
        chunk = replace(
            chunk,
            metadata={
                **dict(chunk.metadata),
                "wav_path": str(wav_path),
                "temporary_output": temporary_output,
                "requested_device": requested_device or "",
                "resolved_capture_device": resolved_device or "",
                "arecord_command": " ".join(command[:1]),
                "canonical_audio": True,
            },
        )
        raw_path_for_result = str(raw_wav_path) if diagnostic_audio else ""
        _discard_unrequested_raw(raw_wav_path, diagnostic_audio)
        return self._success(
            status="recorded",
            text=f"Recorded {duration} second(s) of Linux ALSA microphone audio.",
            chunk=chunk,
            data={
                **base_capture_data,
                "wav_path": str(wav_path),
                "final_whisper_input_path": str(wav_path),
                "temporary_output": temporary_output,
                "duration_seconds": duration,
                "selected_device": requested_device or "",
                "requested_device": requested_device or "",
                "resolved_capture_device": resolved_device or "",
                "actual_sample_rate_hz": int(raw_validation.get("sample_rate_hz", 0)),
                "actual_channels": int(raw_validation.get("channels", 0)),
                "actual_sample_width_bytes": int(
                    raw_validation.get("sample_width_bytes", 0)
                ),
                "normalized_sample_rate_hz": CANONICAL_SAMPLE_RATE_HZ,
                "normalized_channels": CANONICAL_CHANNELS,
                "normalized_sample_width_bytes": CANONICAL_SAMPLE_WIDTH_BYTES,
                "raw_wav_path": raw_path_for_result,
                "normalized_wav_path": str(wav_path),
                "raw_duration_seconds": float(
                    raw_validation.get("duration_seconds", 0.0)
                ),
                "normalized_duration_seconds": float(
                    normalized_validation.get("duration_seconds", 0.0)
                ),
                "raw_wav": raw_validation,
                "wav": normalized_validation,
                "normalization": normalization.to_dict(),
                "chunk": chunk.to_dict(),
            },
        )

    def record_until_silence(
        self,
        output_path: str | Path,
        device: Optional[str] = None,
        calibration_enabled: bool = True,
        calibration_duration_seconds: float = 0.75,
        speech_start_rms: float = 200.0,
        speech_continue_rms: float = 160.0,
        silence_rms: float = 120.0,
        required_speech_frames: int = 3,
        required_continue_frames: int = 3,
        required_silence_frames: int = 5,
        silence_seconds: float = 0.9,
        speech_wait_timeout_seconds: float = 10.0,
        maximum_utterance_seconds: float = 15.0,
        pre_roll_seconds: float = 0.25,
        speech_end_padding_seconds: float = 0.0,
        frame_duration_ms: int = 20,
        frame_read_timeout_seconds: float = 1.0,
        minimum_speech_start_rms: float = 200.0,
        maximum_speech_start_rms: float = 1200.0,
        minimum_speech_continue_rms: float = 140.0,
        maximum_speech_continue_rms: float = 900.0,
        minimum_silence_rms: float = 80.0,
        maximum_silence_rms: float = 600.0,
        duration_loss_tolerance_seconds: float = 0.05,
        frame_debug_enabled: bool = False,
        capture_profile: str = "",
        minimum_speech_duration_seconds: float = 0.0,
        diagnostic_rms_interval_frames: int = 5,
        diagnostic_audio: bool = False,
        diagnostic_exception_traceback: bool = False,
        cancel_requested: Optional[CancelCheck | Any] = None,
        capture_ready_callback: Optional[
            Callable[[Dict[str, Any]], None]
        ] = None,
        correlation_id: str = "",
        session_id: str = "",
        persistent_stream: Optional[PersistentPcmStreamHandle] = None,
    ) -> VoiceActivityCaptureResultV1:
        """Capture one foreground utterance and trim terminal silence."""

        self.record_count += 1
        started_at = time.monotonic()
        requested_device = self.device
        resolved_device = self.device
        command: List[str] = []
        stage_paths: Optional[CaptureStagePaths] = None
        source: Optional[Any] = None
        stream: Optional[Any] = None
        owns_stream = False
        stream_cleanup: Dict[str, Any] = {
            "called": False,
            "attempts": 0,
            "completed": False,
            "status": "not_started",
        }
        failing_method = "record_until_silence"
        try:
            failing_method = "resolve_alsa_capture_device"
            requested_device = (
                _normalize_optional_device(device) if device is not None else self.device
            )
            resolved_device = resolve_alsa_capture_device(
                requested_device,
                require_conversion=True,
            )
            if not self.started:
                raise RuntimeError("microphone_not_started")
            if self.sample_format.upper() != DEFAULT_ALSA_SAMPLE_FORMAT:
                raise ValueError("voice_activity_capture_requires_s16_le")
            failing_method = "find_arecord"
            arecord_path = self._find_arecord()
            if not arecord_path:
                raise FileNotFoundError("arecord_missing")
            stage_paths = _unique_capture_stage_paths(Path(output_path).expanduser())
            request = VoiceActivityCaptureRequestV1(
                output_wav_path=str(stage_paths.assembled),
                microphone_device=resolved_device or "",
                sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                channels=CANONICAL_CHANNELS,
                sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
                frame_duration_ms=frame_duration_ms,
                calibration_enabled=calibration_enabled,
                calibration_duration_seconds=calibration_duration_seconds,
                speech_start_rms=speech_start_rms,
                speech_continue_rms=speech_continue_rms,
                silence_rms=silence_rms,
                required_speech_frames=required_speech_frames,
                required_continue_frames=required_continue_frames,
                required_silence_frames=required_silence_frames,
                silence_duration_seconds=silence_seconds,
                speech_wait_timeout_seconds=speech_wait_timeout_seconds,
                maximum_utterance_seconds=maximum_utterance_seconds,
                pre_roll_seconds=pre_roll_seconds,
                speech_end_padding_seconds=speech_end_padding_seconds,
                frame_read_timeout_seconds=frame_read_timeout_seconds,
                minimum_speech_start_rms=minimum_speech_start_rms,
                maximum_speech_start_rms=maximum_speech_start_rms,
                minimum_speech_continue_rms=minimum_speech_continue_rms,
                maximum_speech_continue_rms=maximum_speech_continue_rms,
                minimum_silence_rms=minimum_silence_rms,
                maximum_silence_rms=maximum_silence_rms,
                duration_loss_tolerance_seconds=duration_loss_tolerance_seconds,
                frame_debug_enabled=frame_debug_enabled,
                correlation_id=correlation_id,
                session_id=session_id,
                metadata={
                    "safe": True,
                    "source": self.source,
                    "hardware_specific": "linux_alsa",
                    "background_listening": False,
                    "requested_device": requested_device or "",
                    "resolved_capture_device": resolved_device or "",
                    "vad_profile": str(capture_profile or "")[:64],
                    "minimum_speech_duration_seconds": float(
                        minimum_speech_duration_seconds
                    ),
                    "diagnostic_rms_interval_frames": int(
                        diagnostic_rms_interval_frames
                    ),
                    "diagnostic_exception_traceback": bool(
                        diagnostic_exception_traceback
                    ),
                },
            )
            validate_voice_activity_request(request)
            if persistent_stream is not None:
                source = self._validated_persistent_stream(
                    persistent_stream,
                    requested_device=requested_device,
                )
                requested_device = persistent_stream.requested_device
                resolved_device = persistent_stream.resolved_device
                command = list(persistent_stream.command)
            else:
                failing_method = "SafePcmStreamRunner.start"
                command = self._stream_command(
                    arecord_path=arecord_path,
                    device=resolved_device,
                )
                with self._stream_lock:
                    if self._active_stream is not None:
                        raise RuntimeError(
                            "microphone_capture_already_owned:"
                            f"{self._active_stream_owner or 'unknown'}"
                        )
                    source = self.stream_runner.start(command)
                    self._active_stream = source
                    self._active_stream_owner = "one_shot_voice_activity"
                    owns_stream = True
            if diagnostic_audio:
                maximum_seconds = (
                    (calibration_duration_seconds if calibration_enabled else 0.0)
                    + speech_wait_timeout_seconds
                    + maximum_utterance_seconds
                    + max(0.1, (frame_duration_ms / 1000.0) * 3.0)
                )
                stream = DiagnosticPcmFrameSource(
                    source,
                    maximum_bytes=int(
                        maximum_seconds
                        * CANONICAL_SAMPLE_RATE_HZ
                        * CANONICAL_CHANNELS
                        * CANONICAL_SAMPLE_WIDTH_BYTES
                    ),
                    close_source=owns_stream,
                )
            else:
                stream = source
            self.audio_hardware_accessed = True
            failing_method = "RmsVoiceActivityCapture.execute"
            result = self.voice_activity_capture.execute(
                request,
                stream,
                cancel_requested=cancel_requested,
                capture_ready_callback=capture_ready_callback,
            )
            if owns_stream:
                failing_method = "close"
                stream_cleanup = _bounded_pcm_source_cleanup(
                    stream,
                    diagnostic_traceback=bool(diagnostic_exception_traceback),
                )
                controlled_stop = dict(
                    stream_cleanup.get("controlled_stop") or {}
                )
                self._remember_pcm_stop_result(controlled_stop)
                if stream_cleanup["completed"]:
                    with self._stream_lock:
                        if self._active_stream is source:
                            self._active_stream = None
                            self._active_stream_owner = ""
                if not stream_cleanup["completed"]:
                    result = replace(
                        result,
                        success=False,
                        status=VAD_STATUS_DEVICE_ERROR,
                        stop_reason=VAD_STATUS_DEVICE_ERROR,
                        error_message=(
                            "pcm_stream_cleanup_error:"
                            + str(
                                stream_cleanup.get("exception_class")
                                or "RuntimeError"
                            )
                            + ":"
                            + str(
                                stream_cleanup.get("exception_message")
                                or "cleanup_incomplete"
                            )
                        ),
                    )
                elif (
                    controlled_stop
                    and controlled_stop.get("final_health_effect") == "unhealthy"
                ):
                    # A successfully assembled candidate does not erase a real
                    # transport failure discovered while the owned stream is
                    # being stopped.  Only the structured controlled-stop
                    # contract may classify an expected cleanup interruption as
                    # harmless; unrelated exit-1 stderr, unexpected child death,
                    # or an unreaped process remains an audio-device failure.
                    result = replace(
                        result,
                        success=False,
                        status=VAD_STATUS_DEVICE_ERROR,
                        stop_reason=VAD_STATUS_DEVICE_ERROR,
                        error_message=(
                            "pcm_stream_stop_unhealthy:"
                            + str(
                                controlled_stop.get("status")
                                or "unexpected_failure"
                            )
                        ),
                    )
            failing_method = "capture_audio_finalization"
            raw_wav_path = ""
            raw_wav: Dict[str, Any] = {}
            if diagnostic_audio and isinstance(stream, DiagnosticPcmFrameSource):
                if stream.captured:
                    write_audio_chunk_wav(
                        AudioChunk(
                            data=bytes(stream.captured),
                            sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                            channels=CANONICAL_CHANNELS,
                            sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
                            source=self.source,
                            metadata={
                                "diagnostic_only": True,
                                "requested_device": requested_device or "",
                                "resolved_capture_device": resolved_device or "",
                            },
                        ),
                        stage_paths.raw,
                    )
                    raw_wav_path = str(stage_paths.raw)
                    raw_wav = analyze_wav_audio(stage_paths.raw)

            assembled_wav = (
                validate_canonical_wav(result.wav_path)
                if result.wav_path
                else {"success": False, "error_message": "assembled_wav_missing"}
            )
            normalization_data: Dict[str, Any] = {}
            normalized_wav: Dict[str, Any] = {
                "success": False,
                "error_message": "normalized_wav_not_created",
            }
            duration_invariant = {
                "success": False,
                "status": "not_checked",
                "allowed_loss_seconds": duration_loss_tolerance_seconds,
            }
            final_whisper_input_path = ""
            if result.success:
                if not assembled_wav.get("success"):
                    result = replace(
                        result,
                        success=False,
                        status=VAD_STATUS_INVALID_AUDIO,
                        stop_reason=VAD_STATUS_INVALID_AUDIO,
                        error_message=str(
                            assembled_wav.get("error_message")
                            or "assembled_audio_validation_failed"
                        ),
                    )
                else:
                    normalization = normalize_wav_audio(
                        result.wav_path,
                        stage_paths.normalized,
                    )
                    normalization_data = normalization.to_dict()
                    normalized_wav = (
                        validate_canonical_wav(stage_paths.normalized)
                        if normalization.success
                        else {
                            "success": False,
                            "error_message": normalization.error_message
                            or normalization.status,
                        }
                    )
                    if not normalized_wav.get("success"):
                        result = replace(
                            result,
                            success=False,
                            status=VAD_STATUS_INVALID_AUDIO,
                            stop_reason=VAD_STATUS_INVALID_AUDIO,
                            error_message=str(
                                normalized_wav.get("error_message")
                                or "canonical_audio_validation_failed"
                            ),
                        )
                    else:
                        duration_invariant = validate_duration_invariant(
                            float(assembled_wav.get("duration_seconds", 0.0)),
                            float(normalized_wav.get("duration_seconds", 0.0)),
                            duration_loss_tolerance_seconds,
                        )
                        if not duration_invariant["success"]:
                            result = replace(
                                result,
                                success=False,
                                status=VAD_STATUS_INVALID_AUDIO,
                                stop_reason=VAD_STATUS_INVALID_AUDIO,
                                error_message="audio_duration_invariant_failed",
                            )
                        else:
                            final_whisper_input_path = str(stage_paths.normalized)

            assembled_path_for_result = (
                str(stage_paths.assembled) if diagnostic_audio else ""
            )
            if not diagnostic_audio:
                _discard_capture_stage(stage_paths.raw)
                _discard_capture_stage(stage_paths.assembled)
            if not result.success and not diagnostic_audio:
                _discard_capture_stage(stage_paths.normalized)

            normalized_frames = int(normalized_wav.get("frames", 0))
            normalized_bytes = (
                normalized_frames
                * CANONICAL_CHANNELS
                * CANONICAL_SAMPLE_WIDTH_BYTES
            )
            raw_duration = float(
                raw_wav.get("duration_seconds", result.raw_duration_seconds)
            )
            normalized_duration = float(normalized_wav.get("duration_seconds", 0.0))
            final_path = final_whisper_input_path or result.wav_path
            result_data = dict(result.data)
            pcm_exception = dict(result_data.get("pcm_exception") or {})
            source_after_close = dict(
                stream_cleanup.get("source_snapshot_after_close") or {}
            )
            process_stderr = str(
                getattr(stream, "stderr", "") if stream is not None else ""
            ) or str(
                source_after_close.get("stderr_preview", "")
                or source_after_close.get("stderr", "")
                or ""
            )
            if pcm_exception:
                pcm_exception.update(
                    {
                        "cleanup_result": str(
                            stream_cleanup.get("status") or "not_required"
                        ),
                        "source_snapshot_after_close": source_after_close,
                        "alsa_stderr": process_stderr
                        or str(pcm_exception.get("alsa_stderr") or ""),
                        "alsa_process_exit_status": source_after_close.get(
                            "process_exit_status",
                            pcm_exception.get("alsa_process_exit_status"),
                        ),
                        "alsa_child_process_id": int(
                            source_after_close.get(
                                "process_pid",
                                pcm_exception.get("alsa_child_process_id", 0),
                            )
                            or 0
                        ),
                    }
                )
            return replace(
                result,
                wav_path=final_path,
                duration_seconds=normalized_duration or result.duration_seconds,
                requested_device=requested_device or "",
                resolved_capture_device=resolved_device or "",
                requested_sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                actual_sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                actual_channels=CANONICAL_CHANNELS,
                actual_sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
                normalized_sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                normalized_channels=CANONICAL_CHANNELS,
                normalized_sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
                raw_wav_path=raw_wav_path,
                assembled_wav_path=assembled_path_for_result,
                normalized_wav_path=final_whisper_input_path,
                raw_duration_seconds=raw_duration,
                assembled_duration_seconds=float(
                    assembled_wav.get("duration_seconds", result.assembled_duration_seconds)
                ),
                normalized_duration_seconds=normalized_duration,
                normalized_sample_count=normalized_frames,
                normalized_byte_count=normalized_bytes,
                whisper_input_duration_seconds=normalized_duration,
                duration_invariant_status=str(duration_invariant.get("status", "not_checked")),
                final_whisper_input_path=final_whisper_input_path,
                data={
                    **result_data,
                    **({"pcm_exception": pcm_exception} if pcm_exception else {}),
                    "requested_device": requested_device or "",
                    "resolved_capture_device": resolved_device or "",
                    "requested_sample_rate_hz": CANONICAL_SAMPLE_RATE_HZ,
                    "actual_sample_rate_hz": CANONICAL_SAMPLE_RATE_HZ,
                    "actual_channels": CANONICAL_CHANNELS,
                    "actual_sample_width_bytes": CANONICAL_SAMPLE_WIDTH_BYTES,
                    "normalized_sample_rate_hz": CANONICAL_SAMPLE_RATE_HZ,
                    "normalized_channels": CANONICAL_CHANNELS,
                    "normalized_sample_width_bytes": CANONICAL_SAMPLE_WIDTH_BYTES,
                    "raw_wav_path": raw_wav_path,
                    "assembled_wav_path": assembled_path_for_result,
                    "normalized_wav_path": final_whisper_input_path,
                    "raw_duration_seconds": raw_duration,
                    "assembled_duration_seconds": float(
                        assembled_wav.get(
                            "duration_seconds",
                            result.assembled_duration_seconds,
                        )
                    ),
                    "normalized_duration_seconds": normalized_duration,
                    "normalized_sample_count": normalized_frames,
                    "normalized_byte_count": normalized_bytes,
                    "whisper_input_duration_seconds": normalized_duration,
                    "duration_invariant_status": str(
                        duration_invariant.get("status", "not_checked")
                    ),
                    "raw_wav": raw_wav,
                    "assembled_wav": assembled_wav,
                    "wav": normalized_wav,
                    "normalization": normalization_data,
                    "duration_invariant": duration_invariant,
                    "final_whisper_input_path": final_whisper_input_path,
                    "canonical_audio_boundary": "alsa_plug_then_validated_pcm_v1",
                    "byte_reinterpretation": False,
                    "process": {
                        "args": command,
                        "shell": False,
                        "pid": int(source_after_close.get("process_pid", 0) or 0),
                        "stderr": _bounded_text(process_stderr, 8192),
                        "returncode": source_after_close.get(
                            "process_exit_status",
                            getattr(
                                getattr(stream, "process", None),
                                "returncode",
                                None,
                            ),
                        ),
                    },
                    "pcm_stream_cleanup": stream_cleanup,
                    "pcm_source_snapshot_after_close": source_after_close,
                },
                metadata={
                    **dict(result.metadata),
                    "subprocess_shell": False,
                    "speech_engine_accessed": False,
                    "requested_device": requested_device or "",
                    "resolved_capture_device": resolved_device or "",
                    "diagnostic_audio": bool(diagnostic_audio),
                },
            )
        except (
            FileNotFoundError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            exception_traceback = (
                traceback.format_exc()
                if bool(diagnostic_exception_traceback)
                else ""
            )
            target_source = stream or source
            if (
                owns_stream
                and target_source is not None
                and not bool(stream_cleanup.get("completed", False))
            ):
                stream_cleanup = _bounded_pcm_source_cleanup(
                    target_source,
                    diagnostic_traceback=bool(diagnostic_exception_traceback),
                )
                self._remember_pcm_stop_result(
                    dict(stream_cleanup.get("controlled_stop") or {})
                )
                if stream_cleanup["completed"]:
                    with self._stream_lock:
                        if self._active_stream is source:
                            self._active_stream = None
                            self._active_stream_owner = ""
            source_after_close = dict(
                stream_cleanup.get("source_snapshot_after_close") or {}
            )
            process_stderr = str(
                getattr(target_source, "stderr", "")
                if target_source is not None
                else ""
            ) or str(
                source_after_close.get("stderr_preview", "")
                or source_after_close.get("stderr", "")
                or ""
            )
            exception_data: Dict[str, Any] = {
                "exception_class": error.__class__.__name__,
                "exception_message": str(error),
                "failing_adapter_class": (
                    f"{self.__class__.__module__}.{self.__class__.__qualname__}"
                ),
                "failing_method": failing_method,
                "microphone_device": requested_device or "",
                "requested_pcm_format": canonical_pcm_contract(),
                "stream_lifecycle_state": (
                    "closed"
                    if bool(source_after_close.get("closed", False))
                    else "ended"
                    if bool(source_after_close.get("stream_ended", False))
                    else "open"
                    if target_source is not None
                    else "not_opened"
                ),
                "start_called": bool(self.started),
                "open_called": bool(source is not None),
                "read_called": bool(
                    source_after_close.get("total_low_level_reads", 0)
                    or source_after_close.get("read_sequence", 0)
                ),
                "process_id": os.getpid(),
                "alsa_child_process_id": int(
                    source_after_close.get("process_pid", 0) or 0
                ),
                "alsa_process_exit_status": source_after_close.get(
                    "process_exit_status"
                ),
                "alsa_stderr": process_stderr,
                "cleanup_result": str(
                    stream_cleanup.get("status") or "not_required"
                ),
                "source_snapshot_after_close": source_after_close,
            }
            if bool(diagnostic_exception_traceback):
                exception_data["traceback"] = exception_traceback
            if stage_paths is not None and not diagnostic_audio:
                for stage_path in (
                    stage_paths.raw,
                    stage_paths.assembled,
                    stage_paths.normalized,
                ):
                    _discard_capture_stage(stage_path)
            return VoiceActivityCaptureResultV1(
                success=False,
                status=VAD_STATUS_DEVICE_ERROR,
                selected_device=resolved_device or "",
                requested_device=requested_device or "",
                resolved_capture_device=resolved_device or "",
                requested_sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                normalized_sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                normalized_channels=CANONICAL_CHANNELS,
                normalized_sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
                stop_reason=VAD_STATUS_DEVICE_ERROR,
                processing_time_seconds=round(time.monotonic() - started_at, 6),
                error_message=str(error),
                correlation_id=correlation_id,
                session_id=session_id,
                metadata={
                    "safe": True,
                    "source": self.source,
                    "subprocess_shell": False,
                    "raw_audio_persisted_in_metadata": False,
                    "requested_device": requested_device or "",
                    "resolved_capture_device": resolved_device or "",
                },
                data={
                    "pcm_exception": exception_data,
                    "pcm_stream_cleanup": stream_cleanup,
                    "pcm_source_snapshot_after_close": source_after_close,
                    "process": {
                        "args": command,
                        "shell": False,
                        "pid": int(
                            source_after_close.get("process_pid", 0) or 0
                        ),
                        "stderr": _bounded_text(process_stderr, 8192),
                        "returncode": source_after_close.get(
                            "process_exit_status"
                        ),
                    },
                },
            )
        finally:
            if owns_stream:
                with self._stream_lock:
                    active = self._active_stream
                stream_to_close = (
                    active or stream
                    if not bool(stream_cleanup.get("completed", False))
                    else None
                )
            else:
                stream_to_close = (
                    stream if isinstance(stream, DiagnosticPcmFrameSource) else None
                )
            if stream_to_close is not None:
                try:
                    stream_to_close.close()
                except (OSError, RuntimeError):
                    pass
                else:
                    if owns_stream:
                        with self._stream_lock:
                            if self._active_stream is source:
                                self._active_stream = None
                                self._active_stream_owner = ""

    def _validated_persistent_stream(
        self,
        handle: PersistentPcmStreamHandle,
        *,
        requested_device: Optional[str],
    ) -> Any:
        if not isinstance(handle, PersistentPcmStreamHandle) or handle.closed:
            raise RuntimeError("persistent_pcm_stream_not_active")
        with self._stream_lock:
            if self._persistent_stream is not handle:
                raise RuntimeError("persistent_pcm_stream_not_active")
            if self._active_stream is not handle.frame_source:
                raise RuntimeError("persistent_pcm_stream_source_mismatch")
            resolved = resolve_alsa_capture_device(
                requested_device,
                require_conversion=True,
            )
            if (resolved or "") != handle.resolved_device:
                raise RuntimeError("persistent_pcm_stream_device_mismatch")
            return handle.frame_source

    def _remember_pcm_stop_result(self, result: Dict[str, Any]) -> None:
        clean = dict(result or {})
        if clean and clean.get("status") != "not_stopped":
            self._last_pcm_stop_result = clean

    def _find_arecord(self) -> str:
        found = self.runner.which(self.arecord_command)
        return str(found or "")

    def _record_command(
        self,
        arecord_path: str,
        wav_path: Path,
        seconds: int,
        device: Optional[str],
    ) -> List[str]:
        command = [
            arecord_path,
            "-q",
            "-f",
            self.sample_format,
            "-c",
            str(self.channels),
            "-r",
            str(self.sample_rate_hz),
            "-d",
            str(seconds),
            "-t",
            "wav",
        ]
        if device:
            command.extend(["-D", device])
        command.append(str(wav_path))
        return command

    def _stream_command(
        self,
        arecord_path: str,
        device: Optional[str],
    ) -> List[str]:
        command = [
            arecord_path,
            "-q",
            "-f",
            DEFAULT_ALSA_SAMPLE_FORMAT,
            "-c",
            str(CANONICAL_CHANNELS),
            "-r",
            str(CANONICAL_SAMPLE_RATE_HZ),
            "-t",
            "raw",
        ]
        if device:
            command.extend(["-D", device])
        command.append("-")
        return command

    def _success(
        self,
        status: str,
        text: str,
        chunk: Optional[AudioChunk] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> MicrophoneResult:
        return MicrophoneResult(
            success=True,
            status=status,
            text=text,
            chunk=chunk,
            data={**self._base_data(), **dict(data or {})},
            metadata=self._metadata(),
        )

    def _failure(
        self,
        status: str,
        text: str,
        error_message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> MicrophoneResult:
        return MicrophoneResult(
            success=False,
            status=status,
            text=text,
            error_message=error_message,
            data={**self._base_data(), **dict(data or {})},
            metadata=self._metadata(),
        )

    def _base_data(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "started": self.started,
            "selected_device": self.device or "",
            "audio_hardware_access": "linux_alsa_arecord",
            "background_listening": "disabled",
            "stt": "not_configured",
        }

    def _metadata(self) -> Dict[str, Any]:
        return {
            "safe": True,
            "source": self.source,
            "mock": False,
            "hardware_specific": "linux_alsa",
            "subprocess_shell": False,
            "audio_hardware_accessed": self.audio_hardware_accessed,
            "speech_engine_accessed": False,
        }


def parse_arecord_capture_devices(output: str) -> List[Dict[str, Any]]:
    devices: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"^card\s+(?P<card_index>\d+):\s*"
        r"(?P<card_id>[^\[]+)\[(?P<card_name>[^\]]+)\],\s*"
        r"device\s+(?P<device_index>\d+):\s*"
        r"(?P<device_id>[^\[]+)\[(?P<device_name>[^\]]+)\]"
    )
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        match = pattern.match(line)
        if not match:
            continue
        card_index = int(match.group("card_index"))
        device_index = int(match.group("device_index"))
        devices.append(
            {
                "card_index": card_index,
                "card_id": match.group("card_id").strip(),
                "card_name": match.group("card_name").strip(),
                "device_index": device_index,
                "device_id": match.group("device_id").strip(),
                "device_name": match.group("device_name").strip(),
                "alsa_device": f"hw:{card_index},{device_index}",
                "raw_line": line,
            }
        )
    return devices


def _safe_process_data(result: SafeProcessResult) -> Dict[str, Any]:
    return {
        "args": list(result.args),
        "returncode": result.returncode,
        "stdout_preview": _bounded_text(result.stdout),
        "stderr_preview": _bounded_text(result.stderr),
        "timed_out": result.timed_out,
        "error_message": result.error_message,
    }


def _safe_pcm_source_snapshot(source: Any) -> Dict[str, Any]:
    snapshot = getattr(source, "snapshot", None)
    if not callable(snapshot):
        return {}
    try:
        value = snapshot()
    except (OSError, RuntimeError, TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _bounded_pcm_source_cleanup(
    source: Any,
    *,
    diagnostic_traceback: bool = False,
    maximum_attempts: int = 2,
) -> Dict[str, Any]:
    """Close one PCM source with a small retry bound and truthful diagnostics."""

    before = _safe_pcm_source_snapshot(source)
    attempts = 0
    completed = False
    last_error: Optional[BaseException] = None
    last_traceback = ""
    for _ in range(max(1, min(int(maximum_attempts), 3))):
        attempts += 1
        try:
            source.close()
            completed = True
            break
        except (OSError, RuntimeError) as error:
            last_error = error
            if bool(diagnostic_traceback):
                last_traceback = traceback.format_exc()
    after = _safe_pcm_source_snapshot(source)
    result: Dict[str, Any] = {
        "called": True,
        "attempts": attempts,
        "completed": completed,
        "status": "completed" if completed else "incomplete",
        "source_snapshot_before_close": before,
        "source_snapshot_after_close": after,
        "controlled_stop": dict(after.get("controlled_stop") or {}),
    }
    if last_error is not None:
        result.update(
            {
                "exception_class": last_error.__class__.__name__,
                "exception_message": str(last_error),
            }
        )
        if bool(diagnostic_traceback):
            result["traceback"] = last_traceback
    return result


def _bounded_text(text: str, limit: int = 500) -> str:
    clean = str(text or "")
    return clean[:limit]


def _stderr_is_controlled_arecord_interrupt(stderr: str) -> bool:
    """Match only arecord's cleanup-time interrupted-read diagnostic."""

    lines = [line.strip() for line in str(stderr or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return False
    return bool(
        re.fullmatch(
            r"arecord:\s*pcm_read:\d+:\s*read error:\s*Interrupted system call",
            lines[0],
            flags=re.IGNORECASE,
        )
    )


def _positive_int(value: Any, name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _bounded_record_seconds(value: Any) -> int:
    seconds = int(value)
    if seconds <= 0:
        raise ValueError("record_seconds must be positive")
    if seconds > MAX_ALSA_RECORD_SECONDS:
        raise ValueError(f"record_seconds must be <= {MAX_ALSA_RECORD_SECONDS}")
    return seconds


def _bounded_timeout(value: Any) -> float:
    timeout = float(value)
    if timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    if timeout > MAX_ALSA_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be <= {MAX_ALSA_TIMEOUT_SECONDS}")
    return timeout


def _normalize_optional_device(device: Optional[str]) -> Optional[str]:
    if device is None:
        return None
    clean = str(device).strip()
    if not clean:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.,:+\-=]+", clean):
        raise ValueError("invalid ALSA device identifier")
    return clean


def _normalize_stream_owner(value: Any) -> str:
    owner = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", owner):
        raise ValueError("persistent PCM stream owner is invalid")
    return owner


def _device_looks_like_hw(device: str) -> bool:
    return bool(re.fullmatch(r"(?:plug)?hw:\d+,\d+", str(device or "")))


def resolve_alsa_capture_device(
    device: Optional[str],
    require_conversion: bool,
) -> Optional[str]:
    """Resolve only raw numeric hardware IDs when conversion is mandatory."""

    clean = _normalize_optional_device(device)
    if clean is None:
        return None
    if require_conversion and re.fullmatch(r"hw:\d+,\d+", clean):
        return f"plug{clean}"
    return clean


def _hardware_device_id(device: str) -> str:
    clean = str(device or "")
    if clean.startswith("plughw:"):
        return clean[len("plug") :]
    return clean


def _unique_raw_wav_path(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(
        prefix=f".{output.stem}.raw.",
        dir=str(output.parent),
    ))
    return directory / f"{output.stem}.wav"


def _unique_capture_stage_paths(output: Path) -> CaptureStagePaths:
    output.parent.mkdir(parents=True, exist_ok=True)
    directory = Path(
        tempfile.mkdtemp(
            prefix=f".{output.stem}.turn.",
            dir=str(output.parent),
        )
    )
    return CaptureStagePaths(
        directory=directory,
        raw=directory / "raw_capture.wav",
        assembled=directory / "assembled_utterance.wav",
        normalized=directory / "normalized_whisper_input.wav",
    )


def _discard_capture_stage(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except OSError:
        pass


def _discard_unrequested_raw(path: Path, diagnostic_audio: bool) -> None:
    if diagnostic_audio:
        return
    try:
        path.unlink(missing_ok=True)
        if path.parent.name.startswith(f".{path.stem}.raw."):
            path.parent.rmdir()
    except OSError:
        pass


def _validate_output_path(path: Path, overwrite: bool) -> None:
    if not path.name:
        raise ValueError("output_path must include a WAV filename")
    if path.exists() and not overwrite:
        raise ValueError("output_path already exists")


def _validate_wav_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "success": False,
            "status": ALSA_STATUS_OUTPUT_MISSING,
            "text": "arecord completed but did not create a WAV output file.",
            "error_message": "wav_output_missing",
        }
    size = path.stat().st_size
    if size <= 44:
        return {
            "success": False,
            "status": ALSA_STATUS_OUTPUT_EMPTY,
            "text": "arecord completed but WAV output was empty.",
            "error_message": "wav_output_empty",
            "byte_count": size,
        }
    try:
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            frame_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            duration = frames / frame_rate if frame_rate else 0.0
    except (wave.Error, EOFError, OSError) as error:
        return {
            "success": False,
            "status": ALSA_STATUS_INVALID_WAV,
            "text": "arecord output is not a valid WAV file.",
            "error_message": f"invalid_wav:{error.__class__.__name__}",
            "byte_count": size,
        }
    return {
        "success": True,
        "status": "valid_wav",
        "byte_count": size,
        "frames": frames,
        "sample_rate_hz": frame_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "duration_seconds": duration,
    }


def _stderr_indicates_invalid_device(stderr: str) -> bool:
    lower = str(stderr or "").lower()
    return any(
        marker in lower
        for marker in (
            "unknown pcm",
            "no such file or directory",
            "audio open error",
            "cannot open audio device",
            "device or resource busy",
        )
    )


def _is_cancelled(cancel_requested: Optional[CancelCheck | Any]) -> bool:
    if cancel_requested is None:
        return False
    is_set = getattr(cancel_requested, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(cancel_requested):
        return bool(cancel_requested())
    return bool(cancel_requested)
