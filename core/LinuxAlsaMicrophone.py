from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
import hashlib
import math
import os
import re
import select
import shutil
import subprocess
import tempfile
from threading import RLock
import time
import wave
from typing import Any, Dict, List, Optional, Sequence

from core.Contracts import VoiceActivityCaptureRequestV1, VoiceActivityCaptureResultV1
from core.Microphone import AudioChunk, CancelCheck, MicrophoneAdapter, MicrophoneResult
from core.PcmIntegrity import (
    CANONICAL_PCM_FRAME_BYTES,
    CANONICAL_PCM_FRAME_DURATION_MS,
    CANONICAL_PCM_SAMPLE_FORMAT,
    CANONICAL_PCM_SAMPLES_PER_FRAME,
    canonical_pcm_contract,
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
    """Narrow subprocess boundary for arecord. Shell execution is never used."""

    def which(self, executable: str) -> Optional[str]:
        return shutil.which(executable)

    def run(self, args: Sequence[str], timeout_seconds: float) -> SafeProcessResult:
        safe_args = [str(arg) for arg in args]
        try:
            completed = subprocess.run(
                safe_args,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            return SafeProcessResult(
                args=safe_args,
                returncode=-1,
                stdout=str(error.stdout or ""),
                stderr=str(error.stderr or ""),
                timed_out=True,
                error_message="process_timeout",
            )
        except FileNotFoundError:
            return SafeProcessResult(
                args=safe_args,
                returncode=-1,
                error_message="process_not_found",
            )
        except OSError as error:
            return SafeProcessResult(
                args=safe_args,
                returncode=-1,
                error_message=f"process_os_error:{error.__class__.__name__}",
                metadata={"errno": getattr(error, "errno", None)},
            )

        return SafeProcessResult(
            args=safe_args,
            returncode=int(completed.returncode),
            stdout=str(completed.stdout or ""),
            stderr=str(completed.stderr or ""),
        )


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
        self.process = factory(
            self.args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
        )
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
        self.last_read_timestamp = 0.0

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
            except OSError:
                self.read_errors += 1
                raise
            if not readable:
                raise TimeoutError("pcm_frame_read_timeout")
            requested = expected - len(self._pending)
            self.total_low_level_reads += 1
            try:
                chunk = self.raw_reader(descriptor, requested)
            except OSError:
                self.read_errors += 1
                raise
            if not chunk:
                self.empty_reads += 1
                self.stream_ended = True
                raise EOFError("arecord_pcm_stream_ended")
            try:
                immutable_chunk = self._copy_source_bytes(chunk)
            except (TypeError, ValueError):
                self.read_errors += 1
                raise
            if len(immutable_chunk) < requested:
                self.partial_reads += 1
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
            except OSError:
                self.read_errors += 1
                self._pending.extend(buffered)
                raise
            if not readable:
                break
            requested = min(16384, bounded_maximum - len(buffered))
            self.total_low_level_reads += 1
            try:
                chunk = self.raw_reader(descriptor, requested)
            except OSError:
                self.read_errors += 1
                self._pending.extend(buffered)
                raise
            if not chunk:
                self.empty_reads += 1
                self.stream_ended = True
                break
            try:
                immutable_chunk = self._copy_source_bytes(chunk)
            except (TypeError, ValueError):
                self.read_errors += 1
                self._pending.extend(buffered)
                raise
            if len(immutable_chunk) < requested:
                self.partial_reads += 1
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

    def snapshot(self) -> Dict[str, int | bool]:
        return {
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
            "pending_partial_bytes": len(self._pending),
            "pending_discard_alignment_bytes": self._discard_continuation_bytes,
            "expected_frame_bytes": self._expected_frame_bytes,
            "closed": self.closed,
            "stream_ended": self.stream_ended,
        }

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

        def process_returncode() -> Optional[int]:
            try:
                value = self.process.poll()
            except (OSError, RuntimeError) as error:
                cleanup_errors.append(f"poll:{error.__class__.__name__}")
                return None
            return int(value) if value is not None else None

        returncode = process_returncode()
        if returncode is None:
            try:
                self.process.terminate()
            except (OSError, RuntimeError) as error:
                cleanup_errors.append(f"terminate:{error.__class__.__name__}")
            try:
                returncode = int(self.process.wait(timeout=2.0))
            except subprocess.TimeoutExpired:
                cleanup_errors.append("terminate_wait:TimeoutExpired")
            except (OSError, RuntimeError) as error:
                cleanup_errors.append(
                    f"terminate_wait:{error.__class__.__name__}"
                )
            if returncode is None:
                try:
                    self.process.kill()
                except (OSError, RuntimeError) as error:
                    cleanup_errors.append(f"kill:{error.__class__.__name__}")
                try:
                    returncode = int(self.process.wait(timeout=2.0))
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
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, RuntimeError) as error:
                    cleanup_errors.append(
                        f"pipe_close:{error.__class__.__name__}"
                    )
        if returncode is None:
            try:
                self.process.kill()
            except (OSError, RuntimeError) as error:
                cleanup_errors.append(f"final_kill:{error.__class__.__name__}")
            try:
                returncode = int(self.process.wait(timeout=2.0))
            except (subprocess.TimeoutExpired, OSError, RuntimeError) as error:
                cleanup_errors.append(
                    f"final_wait:{error.__class__.__name__}"
                )
        if returncode is None:
            returncode = process_returncode()
        if returncode is None:
            self.closed = False
            raise RuntimeError(
                "arecord_pcm_stream_cleanup_failed:"
                + ",".join(cleanup_errors[-8:])
            )
        self.closed = True


class SafePcmStreamRunner:
    """Starts allowlisted arecord argument lists; never invokes a shell."""

    def start(self, args: Sequence[str]) -> SubprocessPcmFrameSource:
        return SubprocessPcmFrameSource(args)


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

    def snapshot(self) -> Dict[str, int | float | bool]:
        source_snapshot = getattr(self.source, "snapshot", None)
        low_level = source_snapshot() if callable(source_snapshot) else {}
        if not isinstance(low_level, dict):
            low_level = {}
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
            "repeated_frame_hashes": self.repeated_frame_hashes,
            "mutable_buffer_reuse_detected": max(
                int(low_level.get("mutable_buffer_reuse_detected", 0) or 0),
                self.mutable_buffer_reuse_detected,
            ),
            "valid_microphone_bytes_delivered_to_vad": self.total_bytes_returned,
            "fresh_microphone_bytes_delivered_to_vad": self.total_live_bytes_read,
            "pending_partial_bytes": int(
                low_level.get("pending_partial_bytes", 0) or 0
            ),
            "expected_frame_bytes": self.expected_frame_bytes,
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
        with self._stream_lock:
            if self._persistent_stream is handle:
                self._active_stream = None
                self._active_stream_owner = ""
                self._persistent_stream = None
        handle.closed = True
        self.persistent_stream_close_count += 1
        return self._success(
            status="closed",
            text="Persistent ALSA stream closed.",
            data={"stream_id": handle.stream_id, "owner": clean_owner},
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
        cancel_requested: Optional[CancelCheck | Any] = None,
        correlation_id: str = "",
        session_id: str = "",
        persistent_stream: Optional[PersistentPcmStreamHandle] = None,
    ) -> VoiceActivityCaptureResultV1:
        """Capture one foreground utterance and trim terminal silence."""

        self.record_count += 1
        started_at = time.monotonic()
        requested_device = self.device
        resolved_device = self.device
        stage_paths: Optional[CaptureStagePaths] = None
        stream: Optional[Any] = None
        owns_stream = False
        try:
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
            result = self.voice_activity_capture.execute(
                request,
                stream,
                cancel_requested=cancel_requested,
            )
            if owns_stream:
                stream.close()
                with self._stream_lock:
                    if self._active_stream is source:
                        self._active_stream = None
                        self._active_stream_owner = ""
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
                    **dict(result.data),
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
                        "stderr": _bounded_text(getattr(stream, "stderr", "")),
                        "returncode": getattr(getattr(stream, "process", None), "returncode", None),
                    },
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
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
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
                error_message=str(error)[:200],
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
            )
        finally:
            if owns_stream:
                with self._stream_lock:
                    active = self._active_stream
                    if active is not None:
                        self._active_stream = None
                        self._active_stream_owner = ""
                stream_to_close = active or stream
            else:
                stream_to_close = (
                    stream if isinstance(stream, DiagnosticPcmFrameSource) else None
                )
            if stream_to_close is not None:
                try:
                    stream_to_close.close()
                except (OSError, RuntimeError):
                    pass

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


def _bounded_text(text: str, limit: int = 500) -> str:
    clean = str(text or "")
    return clean[:limit]


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
