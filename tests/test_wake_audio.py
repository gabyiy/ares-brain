from __future__ import annotations

from pathlib import Path
import wave

import pytest

from core import trim_canonical_wake_wav


SAMPLE_RATE = 16000


def _pcm(amplitude: int, seconds: float) -> bytes:
    sample = int(amplitude).to_bytes(2, "little", signed=True)
    return sample * int(SAMPLE_RATE * seconds)


def _write(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)


def _duration(path: Path) -> float:
    with wave.open(str(path), "rb") as source:
        assert source.getframerate() == SAMPLE_RATE
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        return source.getnframes() / source.getframerate()


def test_wake_trim_removes_excess_silence_and_preserves_bounded_word_padding(tmp_path):
    source = tmp_path / "source.wav"
    output = tmp_path / "trimmed.wav"
    _write(
        source,
        _pcm(30, 0.50) + _pcm(900, 0.30) + _pcm(30, 0.60),
    )
    result = trim_canonical_wake_wav(
        source,
        output,
        speech_threshold_rms=160,
        frame_duration_ms=20,
        leading_padding_seconds=0.24,
        trailing_padding_seconds=0.20,
    )
    assert result.source_duration_seconds == pytest.approx(1.4, abs=0.001)
    assert result.trimmed_duration_seconds == pytest.approx(0.74, abs=0.03)
    assert result.leading_trimmed_seconds == pytest.approx(0.26, abs=0.03)
    assert result.trailing_trimmed_seconds == pytest.approx(0.40, abs=0.03)
    assert _duration(output) == pytest.approx(result.trimmed_duration_seconds)


def test_wake_trim_preserves_audio_when_existing_padding_is_already_bounded(tmp_path):
    source = tmp_path / "source.wav"
    output = tmp_path / "trimmed.wav"
    _write(source, _pcm(30, 0.10) + _pcm(900, 0.30) + _pcm(30, 0.10))
    result = trim_canonical_wake_wav(
        source,
        output,
        speech_threshold_rms=160,
        frame_duration_ms=20,
        leading_padding_seconds=0.24,
        trailing_padding_seconds=0.20,
    )
    assert result.leading_trimmed_seconds == 0
    assert result.trailing_trimmed_seconds == 0
    assert _duration(output) == pytest.approx(0.5, abs=0.001)


def test_wake_trim_fails_closed_when_no_speech_crosses_threshold(tmp_path):
    source = tmp_path / "source.wav"
    _write(source, _pcm(30, 0.5))
    with pytest.raises(ValueError, match="no speech"):
        trim_canonical_wake_wav(
            source,
            tmp_path / "trimmed.wav",
            speech_threshold_rms=160,
            frame_duration_ms=20,
            leading_padding_seconds=0.24,
            trailing_padding_seconds=0.20,
        )
