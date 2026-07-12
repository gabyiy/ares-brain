from __future__ import annotations

import math
from pathlib import Path
import struct
from typing import Any, Dict
import wave

from core.Microphone import AudioChunk


def analyze_wav_audio(path: str | Path) -> Dict[str, Any]:
    wav_path = Path(path).expanduser()
    if not wav_path.exists():
        return {"success": False, "error_message": "audio_file_missing", "path": str(wav_path)}
    try:
        byte_count = wav_path.stat().st_size
    except OSError as error:
        return {
            "success": False,
            "error_message": f"audio_stat_failed:{error.__class__.__name__}",
            "path": str(wav_path),
        }
    if byte_count <= 44:
        return {
            "success": False,
            "error_message": "audio_file_empty",
            "path": str(wav_path),
            "byte_count": byte_count,
        }
    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            frames = wav_file.getnframes()
            sample_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            pcm = wav_file.readframes(frames)
    except (wave.Error, EOFError, OSError) as error:
        return {
            "success": False,
            "error_message": f"invalid_wav:{error.__class__.__name__}",
            "path": str(wav_path),
            "byte_count": byte_count,
        }
    if frames <= 0 or sample_rate <= 0:
        return {
            "success": False,
            "error_message": "audio_has_no_frames",
            "path": str(wav_path),
            "byte_count": byte_count,
        }
    try:
        signal = _pcm_signal_stats(pcm, sample_width)
    except ValueError as error:
        return {
            "success": False,
            "error_message": str(error),
            "path": str(wav_path),
            "byte_count": byte_count,
        }
    return {
        "success": True,
        "path": str(wav_path),
        "byte_count": byte_count,
        "frames": frames,
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "duration_seconds": frames / sample_rate,
        **signal,
    }


def write_audio_chunk_wav(audio_chunk: AudioChunk, path: str | Path) -> Path:
    wav_path = Path(path).expanduser()
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(audio_chunk.channels)
        wav_file.setsampwidth(audio_chunk.sample_width_bytes)
        wav_file.setframerate(audio_chunk.sample_rate_hz)
        wav_file.writeframes(audio_chunk.data)
    return wav_path


def read_audio_chunk_wav(path: str | Path, source: str = "wav_audio") -> AudioChunk:
    wav_path = Path(path).expanduser()
    with wave.open(str(wav_path), "rb") as wav_file:
        frames = wav_file.readframes(wav_file.getnframes())
        return AudioChunk(
            data=frames,
            sample_rate_hz=wav_file.getframerate(),
            channels=wav_file.getnchannels(),
            sample_width_bytes=wav_file.getsampwidth(),
            source=source,
            metadata={"wav_path": str(wav_path), "encoding": "pcm_from_wav"},
        )


def _pcm_signal_stats(frame_data: bytes, sample_width: int) -> Dict[str, Any]:
    samples = list(_iter_pcm_samples(frame_data, sample_width))
    if not samples:
        return {"peak_amplitude": 0, "rms_amplitude": 0.0}
    peak = max(abs(sample) for sample in samples)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    return {"peak_amplitude": int(peak), "rms_amplitude": round(rms, 6)}


def _iter_pcm_samples(frame_data: bytes, sample_width: int):
    if sample_width == 1:
        for value in frame_data:
            yield value - 128
        return
    formats = {2: "<h", 4: "<i"}
    if sample_width in formats:
        for offset in range(0, len(frame_data) - sample_width + 1, sample_width):
            yield struct.unpack_from(formats[sample_width], frame_data, offset)[0]
        return
    if sample_width == 3:
        for offset in range(0, len(frame_data) - 2, 3):
            raw = int.from_bytes(frame_data[offset : offset + 3], "little", signed=False)
            yield raw - (1 << 24) if raw & (1 << 23) else raw
        return
    raise ValueError(f"unsupported_sample_width:{sample_width}")
