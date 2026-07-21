from __future__ import annotations

import math
import struct

import pytest

from core.PcmIntegrity import (
    CANONICAL_PCM_FRAME_BYTES,
    CANONICAL_PCM_SAMPLES_PER_FRAME,
    analyze_s16_le_pcm_integrity,
    calculate_s16_le_rms,
    concatenate_owned_pcm_frames,
    decode_s16_le_samples,
    repeated_consecutive_frame_count,
)


def pack_samples(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def test_decode_and_rms_use_signed_s16_little_endian_samples():
    samples = (-32768, -1234, -1, 0, 1, 1234, 32767)
    pcm = pack_samples(*samples)

    assert decode_s16_le_samples(pcm) == samples
    assert calculate_s16_le_rms(pcm) == round(
        math.sqrt(sum(sample * sample for sample in samples) / len(samples)),
        6,
    )


def test_canonical_sine_wave_rms_matches_decoded_sample_energy():
    amplitude = 12_000
    cycles = 4
    samples = tuple(
        round(
            amplitude
            * math.sin(
                2.0
                * math.pi
                * cycles
                * index
                / CANONICAL_PCM_SAMPLES_PER_FRAME
            )
        )
        for index in range(CANONICAL_PCM_SAMPLES_PER_FRAME)
    )
    pcm = pack_samples(*samples)
    expected_rms = round(
        math.sqrt(sum(sample * sample for sample in samples) / len(samples)),
        6,
    )

    statistics = analyze_s16_le_pcm_integrity(pcm)

    assert len(pcm) == CANONICAL_PCM_FRAME_BYTES
    assert statistics.frame_count == 1
    assert statistics.partial_frame_bytes == 0
    assert statistics.rms == expected_rms
    assert statistics.peak == max(abs(sample) for sample in samples)
    assert statistics.minimum_signed_sample < 0
    assert statistics.maximum_signed_sample > 0


def test_repeated_detection_counts_only_consecutive_identical_frames():
    first = pack_samples(100, -100, 200, -200)
    second = pack_samples(300, -300, 400, -400)
    frames = [first, first, second, first]

    statistics = analyze_s16_le_pcm_integrity(
        b"".join(frames),
        frame_bytes=len(first),
    )

    assert repeated_consecutive_frame_count(frames) == 1
    assert statistics.frame_count == 4
    assert statistics.repeated_frame_count == 1
    assert statistics.repeated_frame_percentage == pytest.approx(100.0 / 3.0)
    assert statistics.unique_frame_hash_count == 2


def test_concatenation_owns_immutable_copies_of_mutable_frames():
    first = bytearray(pack_samples(1, 2, 3, 4))
    second = bytearray(pack_samples(-1, -2, -3, -4))
    expected = bytes(first) + bytes(second)

    concatenated = concatenate_owned_pcm_frames(
        [first, memoryview(second)],
        frame_bytes=len(first),
    )
    first[:] = b"\x00" * len(first)
    second[:] = b"\xff" * len(second)

    assert isinstance(concatenated, bytes)
    assert concatenated == expected
