from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import tempfile
import wave

from core.WavAudio import analyze_pcm_audio


@dataclass(frozen=True)
class WakeAudioTrimResult:
    source_path: str
    output_path: str
    source_duration_seconds: float
    trimmed_duration_seconds: float
    leading_trimmed_seconds: float
    trailing_trimmed_seconds: float
    first_speech_frame: int
    last_speech_frame: int
    frame_count: int
    speech_threshold_rms: float


def trim_canonical_wake_wav(
    source_path: str | Path,
    output_path: str | Path,
    *,
    speech_threshold_rms: float,
    frame_duration_ms: int,
    leading_padding_seconds: float,
    trailing_padding_seconds: float,
) -> WakeAudioTrimResult:
    """Trim excessive wake-candidate silence while preserving bounded padding."""

    threshold = _finite_number(speech_threshold_rms, "speech_threshold_rms")
    if threshold <= 0:
        raise ValueError("speech_threshold_rms must be positive")
    if isinstance(frame_duration_ms, bool) or not isinstance(frame_duration_ms, int):
        raise ValueError("frame_duration_ms must be an integer")
    if not 10 <= frame_duration_ms <= 40:
        raise ValueError("frame_duration_ms must be between 10 and 40")
    leading_padding = _bounded_padding(
        leading_padding_seconds,
        "leading_padding_seconds",
    )
    trailing_padding = _bounded_padding(
        trailing_padding_seconds,
        "trailing_padding_seconds",
    )

    source = Path(source_path).expanduser()
    output = Path(output_path).expanduser()
    if not source.is_file() or source.stat().st_size <= 44:
        raise ValueError("wake trim source WAV is missing or empty")
    with wave.open(str(source), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        compression = wav_file.getcomptype()
        sample_count = wav_file.getnframes()
        pcm = wav_file.readframes(sample_count)
    if (
        sample_rate != 16000
        or channels != 1
        or sample_width != 2
        or compression != "NONE"
    ):
        raise ValueError("wake trim source must be canonical 16 kHz mono 16-bit PCM")
    if sample_count <= 0 or len(pcm) != sample_count * sample_width:
        raise ValueError("wake trim source PCM is incomplete")

    samples_per_frame = max(1, round(sample_rate * (frame_duration_ms / 1000.0)))
    bytes_per_frame = samples_per_frame * sample_width
    levels: list[float] = []
    for offset in range(0, len(pcm), bytes_per_frame):
        frame = pcm[offset : offset + bytes_per_frame]
        if not frame:
            continue
        levels.append(float(analyze_pcm_audio(frame, sample_width)["rms_amplitude"]))
    speech_indexes = [index for index, rms in enumerate(levels) if rms >= threshold]
    if not speech_indexes:
        raise ValueError("wake trim found no speech above the continuation threshold")

    first_speech = speech_indexes[0]
    last_speech = speech_indexes[-1]
    leading_samples = round(leading_padding * sample_rate)
    trailing_samples = round(trailing_padding * sample_rate)
    start_sample = max(0, first_speech * samples_per_frame - leading_samples)
    end_sample = min(
        sample_count,
        (last_speech + 1) * samples_per_frame + trailing_samples,
    )
    if end_sample <= start_sample:
        raise ValueError("wake trim produced an empty PCM range")
    trimmed_pcm = pcm[start_sample * sample_width : end_sample * sample_width]
    _write_canonical_wav_atomic(output, trimmed_pcm, sample_rate)

    source_duration = sample_count / sample_rate
    trimmed_duration = (end_sample - start_sample) / sample_rate
    return WakeAudioTrimResult(
        source_path=str(source),
        output_path=str(output),
        source_duration_seconds=round(source_duration, 6),
        trimmed_duration_seconds=round(trimmed_duration, 6),
        leading_trimmed_seconds=round(start_sample / sample_rate, 6),
        trailing_trimmed_seconds=round(
            max(0, sample_count - end_sample) / sample_rate,
            6,
        ),
        first_speech_frame=first_speech + 1,
        last_speech_frame=last_speech + 1,
        frame_count=len(levels),
        speech_threshold_rms=round(threshold, 6),
    )


def _write_canonical_wav_atomic(output: Path, pcm: bytes, sample_rate: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=str(output.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with wave.open(str(temporary), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    return number


def _bounded_padding(value: object, field_name: str) -> float:
    number = _finite_number(value, field_name)
    if not 0.0 <= number <= 0.5:
        raise ValueError(f"{field_name} must be between 0 and 0.5")
    return number
