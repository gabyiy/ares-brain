from __future__ import annotations

import math
from pathlib import Path
import struct
import wave

import pytest

from core.WavAudio import (
    CANONICAL_SAMPLE_RATE_HZ,
    analyze_wav_audio,
    normalize_wav_audio,
    pcm_frame_sample_count,
    validate_canonical_wav,
)


def write_tone_wav(
    path: Path,
    sample_rate_hz: int,
    duration_seconds: float = 0.25,
    amplitude: int = 8000,
) -> None:
    frame_count = round(sample_rate_hz * duration_seconds)
    pcm = bytearray()
    for index in range(frame_count):
        sample = round(amplitude * math.sin(2.0 * math.pi * 440.0 * index / sample_rate_hz))
        pcm.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate_hz)
        wav_file.writeframes(bytes(pcm))


def test_44100_hz_pcm_is_normalized_to_canonical_16000_hz(tmp_path):
    source = tmp_path / "hardware-44100.wav"
    output = tmp_path / "canonical-16000.wav"
    write_tone_wav(source, 44100)

    result = normalize_wav_audio(source, output)
    actual = validate_canonical_wav(output)

    assert result.success is True
    assert result.source_sample_rate_hz == 44100
    assert result.normalized_sample_rate_hz == 16000
    assert result.data["byte_reinterpretation"] is False
    assert actual["success"] is True
    assert actual["sample_rate_hz"] == CANONICAL_SAMPLE_RATE_HZ
    assert actual["channels"] == 1
    assert actual["sample_width_bytes"] == 2


def test_wav_header_drives_source_rate_instead_of_requested_rate(tmp_path):
    source = tmp_path / "actual.wav"
    output = tmp_path / "normalized.wav"
    write_tone_wav(source, 44100)

    result = normalize_wav_audio(source, output)

    assert result.source_sample_rate_hz == 44100
    assert result.data["source_frame_count"] == round(44100 * 0.25)
    assert result.data["normalized_frame_count"] == round(16000 * 0.25)


def test_resampling_preserves_duration_and_clear_signal(tmp_path):
    source = tmp_path / "speech-like.wav"
    output = tmp_path / "normalized.wav"
    write_tone_wav(source, 44100, duration_seconds=0.5, amplitude=12000)

    result = normalize_wav_audio(source, output)
    normalized = analyze_wav_audio(output)

    assert result.success is True
    assert result.normalized_duration_seconds == pytest.approx(0.5, abs=1 / 16000)
    assert normalized["rms_amplitude"] > 7000
    assert normalized["peak_amplitude"] > 11000


def test_silence_remains_silence_after_resampling(tmp_path):
    source = tmp_path / "silence.wav"
    output = tmp_path / "normalized.wav"
    write_tone_wav(source, 44100, amplitude=0)

    result = normalize_wav_audio(source, output)
    normalized = analyze_wav_audio(output)

    assert result.success is True
    assert normalized["peak_amplitude"] == 0
    assert normalized["rms_amplitude"] == 0.0


def test_stereo_pcm_is_downmixed_to_canonical_mono(tmp_path):
    source = tmp_path / "stereo.wav"
    output = tmp_path / "mono.wav"
    with wave.open(str(source), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(44100)
        wav_file.writeframes(struct.pack("<hh", 12000, 4000) * 4410)

    result = normalize_wav_audio(source, output)
    normalized = validate_canonical_wav(output)

    assert result.success is True
    assert result.source_channels == 2
    assert normalized["channels"] == 1
    assert normalized["rms_amplitude"] == pytest.approx(8000.0, abs=1.0)


def test_vad_frame_sample_count_uses_supplied_rate():
    assert pcm_frame_sample_count(44100, 20) == 882
    assert pcm_frame_sample_count(16000, 20) == 320


def test_noncanonical_wav_is_rejected_before_canonical_boundary(tmp_path):
    source = tmp_path / "hardware.wav"
    write_tone_wav(source, 44100)

    result = validate_canonical_wav(source)

    assert result["success"] is False
    assert result["error_message"] == "audio_format_not_canonical"
    assert result["actual_format"]["sample_rate_hz"] == 44100


def test_corrupt_wav_fails_without_creating_output(tmp_path):
    source = tmp_path / "corrupt.wav"
    output = tmp_path / "normalized.wav"
    source.write_bytes(b"not a wave")

    result = normalize_wav_audio(source, output)

    assert result.success is False
    assert result.status == "invalid_source_wav"
    assert output.exists() is False


def test_truncated_wav_fails_without_replacing_existing_output(tmp_path):
    source = tmp_path / "truncated.wav"
    output = tmp_path / "normalized.wav"
    write_tone_wav(source, 44100)
    source.write_bytes(source.read_bytes()[:-17])
    output.write_bytes(b"preserve-me")

    result = normalize_wav_audio(source, output)

    assert result.success is False
    assert output.read_bytes() == b"preserve-me"
