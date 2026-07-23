from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import struct
from typing import Any, Dict, Iterable, Sequence

from core.WavAudio import (
    CANONICAL_CHANNELS,
    CANONICAL_SAMPLE_RATE_HZ,
    CANONICAL_SAMPLE_WIDTH_BYTES,
)


CANONICAL_PCM_SAMPLE_FORMAT = "S16_LE"
CANONICAL_PCM_BYTE_ORDER = "little"
CANONICAL_PCM_SIGNED = True
CANONICAL_PCM_FRAME_DURATION_MS = 20
CANONICAL_PCM_SAMPLES_PER_FRAME = (
    CANONICAL_SAMPLE_RATE_HZ * CANONICAL_PCM_FRAME_DURATION_MS // 1000
)
CANONICAL_PCM_FRAME_BYTES = (
    CANONICAL_PCM_SAMPLES_PER_FRAME
    * CANONICAL_CHANNELS
    * CANONICAL_SAMPLE_WIDTH_BYTES
)


@dataclass(frozen=True)
class PcmIntegrityStatistics:
    byte_count: int
    sample_count: int
    duration_seconds: float
    minimum_signed_sample: int
    maximum_signed_sample: int
    mean_absolute_amplitude: float
    rms: float
    peak: int
    nonzero_sample_count: int
    distinct_sample_count: int
    zero_sample_percentage: float
    absolute_sample_percentage_above_100: float
    absolute_sample_percentage_above_300: float
    absolute_sample_percentage_above_1000: float
    absolute_sample_percentage_above_3000: float
    frame_count: int
    partial_frame_bytes: int
    repeated_frame_count: int
    repeated_frame_percentage: float
    unique_frame_hash_count: int
    first_32_decoded_signed_samples: tuple[int, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "byte_count": self.byte_count,
            "sample_count": self.sample_count,
            "duration_seconds": self.duration_seconds,
            "minimum_signed_sample": self.minimum_signed_sample,
            "maximum_signed_sample": self.maximum_signed_sample,
            "mean_absolute_amplitude": self.mean_absolute_amplitude,
            "rms": self.rms,
            "peak": self.peak,
            "nonzero_sample_count": self.nonzero_sample_count,
            "distinct_sample_count": self.distinct_sample_count,
            "zero_sample_percentage": self.zero_sample_percentage,
            "absolute_sample_percentage_above_100": (
                self.absolute_sample_percentage_above_100
            ),
            "absolute_sample_percentage_above_300": (
                self.absolute_sample_percentage_above_300
            ),
            "absolute_sample_percentage_above_1000": (
                self.absolute_sample_percentage_above_1000
            ),
            "absolute_sample_percentage_above_3000": (
                self.absolute_sample_percentage_above_3000
            ),
            "frame_count": self.frame_count,
            "partial_frame_bytes": self.partial_frame_bytes,
            "repeated_frame_count": self.repeated_frame_count,
            "repeated_frame_percentage": self.repeated_frame_percentage,
            "unique_frame_hash_count": self.unique_frame_hash_count,
            "first_32_decoded_signed_samples": list(
                self.first_32_decoded_signed_samples
            ),
        }


def canonical_pcm_contract() -> Dict[str, Any]:
    """Return the one PCM contract shared by standby capture and diagnostics."""

    return {
        "sample_rate_hz": CANONICAL_SAMPLE_RATE_HZ,
        "channels": CANONICAL_CHANNELS,
        "sample_format": CANONICAL_PCM_SAMPLE_FORMAT,
        "sample_width_bytes": CANONICAL_SAMPLE_WIDTH_BYTES,
        "byte_order": CANONICAL_PCM_BYTE_ORDER,
        "signed": CANONICAL_PCM_SIGNED,
        "frame_duration_ms": CANONICAL_PCM_FRAME_DURATION_MS,
        "samples_per_frame": CANONICAL_PCM_SAMPLES_PER_FRAME,
        "frame_bytes": CANONICAL_PCM_FRAME_BYTES,
    }


def decode_s16_le_samples(pcm: bytes | bytearray | memoryview) -> tuple[int, ...]:
    """Decode signed little-endian int16 PCM without normalization."""

    immutable_pcm = bytes(pcm)
    if len(immutable_pcm) % CANONICAL_SAMPLE_WIDTH_BYTES:
        raise ValueError("s16_le_pcm_requires_complete_samples")
    return tuple(
        sample[0] for sample in struct.iter_unpack("<h", immutable_pcm)
    )


def calculate_s16_le_rms(pcm: bytes | bytearray | memoryview) -> float:
    samples = decode_s16_le_samples(pcm)
    if not samples:
        return 0.0
    return round(
        math.sqrt(sum(sample * sample for sample in samples) / len(samples)),
        6,
    )


def analyze_s16_le_pcm_integrity(
    pcm: bytes | bytearray | memoryview,
    *,
    frame_bytes: int = CANONICAL_PCM_FRAME_BYTES,
    sample_rate_hz: int = CANONICAL_SAMPLE_RATE_HZ,
    channels: int = CANONICAL_CHANNELS,
) -> PcmIntegrityStatistics:
    """Calculate bounded signal and frame-repetition statistics for raw PCM."""

    if isinstance(frame_bytes, bool) or int(frame_bytes) <= 0:
        raise ValueError("frame_bytes must be positive")
    if int(frame_bytes) % CANONICAL_SAMPLE_WIDTH_BYTES:
        raise ValueError("frame_bytes must contain complete S16_LE samples")
    if isinstance(sample_rate_hz, bool) or int(sample_rate_hz) <= 0:
        raise ValueError("sample_rate_hz must be positive")
    if isinstance(channels, bool) or int(channels) <= 0:
        raise ValueError("channels must be positive")
    immutable_pcm = bytes(pcm)
    samples = decode_s16_le_samples(immutable_pcm)
    minimum = min(samples, default=0)
    maximum = max(samples, default=0)
    absolute = [abs(sample) for sample in samples]
    mean_absolute = (
        sum(absolute) / len(absolute) if absolute else 0.0
    )
    peak = max(absolute, default=0)
    nonzero_count = sum(1 for sample in samples if sample != 0)
    duration_seconds = (
        len(samples) / (int(sample_rate_hz) * int(channels))
        if samples
        else 0.0
    )
    zero_percentage = (
        (sum(1 for sample in samples if sample == 0) / len(samples)) * 100.0
        if samples
        else 0.0
    )
    complete_frames = tuple(
        immutable_pcm[offset : offset + int(frame_bytes)]
        for offset in range(0, len(immutable_pcm), int(frame_bytes))
        if len(immutable_pcm[offset : offset + int(frame_bytes)])
        == int(frame_bytes)
    )
    frame_hashes = tuple(_frame_hash(frame) for frame in complete_frames)
    repeated = sum(
        current == previous
        for previous, current in zip(frame_hashes, frame_hashes[1:])
    )
    comparisons = max(0, len(frame_hashes) - 1)
    repeated_percentage = (
        (repeated / comparisons) * 100.0 if comparisons else 0.0
    )

    def absolute_percentage_above(threshold: int) -> float:
        if not absolute:
            return 0.0
        return (
            sum(1 for value in absolute if value > threshold)
            / len(absolute)
            * 100.0
        )

    return PcmIntegrityStatistics(
        byte_count=len(immutable_pcm),
        sample_count=len(samples),
        duration_seconds=round(duration_seconds, 6),
        minimum_signed_sample=int(minimum),
        maximum_signed_sample=int(maximum),
        mean_absolute_amplitude=round(mean_absolute, 6),
        rms=calculate_s16_le_rms(immutable_pcm),
        peak=int(peak),
        nonzero_sample_count=int(nonzero_count),
        distinct_sample_count=len(set(samples)),
        zero_sample_percentage=round(zero_percentage, 6),
        absolute_sample_percentage_above_100=round(
            absolute_percentage_above(100),
            6,
        ),
        absolute_sample_percentage_above_300=round(
            absolute_percentage_above(300),
            6,
        ),
        absolute_sample_percentage_above_1000=round(
            absolute_percentage_above(1000),
            6,
        ),
        absolute_sample_percentage_above_3000=round(
            absolute_percentage_above(3000),
            6,
        ),
        frame_count=len(complete_frames),
        partial_frame_bytes=len(immutable_pcm) % int(frame_bytes),
        repeated_frame_count=int(repeated),
        repeated_frame_percentage=round(repeated_percentage, 6),
        unique_frame_hash_count=len(set(frame_hashes)),
        first_32_decoded_signed_samples=tuple(samples[:32]),
    )


def concatenate_owned_pcm_frames(
    frames: Iterable[bytes | bytearray | memoryview],
    *,
    frame_bytes: int = CANONICAL_PCM_FRAME_BYTES,
) -> bytes:
    """Copy complete PCM frames into one immutable diagnostic payload."""

    owned: list[bytes] = []
    for frame in frames:
        if not isinstance(frame, (bytes, bytearray, memoryview)):
            raise TypeError("pcm frame must be bytes-like")
        actual_length = len(frame)
        immutable_frame = bytes(frame[:actual_length])
        if actual_length != int(frame_bytes):
            raise ValueError("pcm frame size does not match canonical framing")
        owned.append(immutable_frame)
    return b"".join(owned)


def repeated_consecutive_frame_count(
    frames: Sequence[bytes | bytearray | memoryview],
) -> int:
    hashes = tuple(_frame_hash(bytes(frame)) for frame in frames)
    return sum(
        current == previous
        for previous, current in zip(hashes, hashes[1:])
    )


def _frame_hash(frame: bytes) -> bytes:
    return hashlib.sha256(frame).digest()
