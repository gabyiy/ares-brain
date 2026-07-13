from __future__ import annotations

from array import array
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
from typing import Any, Dict
import wave

from core.Microphone import AudioChunk


CANONICAL_SAMPLE_RATE_HZ = 16000
CANONICAL_CHANNELS = 1
CANONICAL_SAMPLE_WIDTH_BYTES = 2
MIN_SUPPORTED_SAMPLE_RATE_HZ = 8000
MAX_SUPPORTED_SAMPLE_RATE_HZ = 192000
MAX_SUPPORTED_CHANNELS = 8


@dataclass(frozen=True)
class WavNormalizationResult:
    success: bool
    status: str
    source_path: str
    output_path: str = ""
    source_sample_rate_hz: int = 0
    source_channels: int = 0
    source_sample_width_bytes: int = 0
    source_duration_seconds: float = 0.0
    normalized_sample_rate_hz: int = CANONICAL_SAMPLE_RATE_HZ
    normalized_channels: int = CANONICAL_CHANNELS
    normalized_sample_width_bytes: int = CANONICAL_SAMPLE_WIDTH_BYTES
    normalized_duration_seconds: float = 0.0
    changed: bool = False
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "source_path": self.source_path,
            "output_path": self.output_path,
            "source_sample_rate_hz": self.source_sample_rate_hz,
            "source_channels": self.source_channels,
            "source_sample_width_bytes": self.source_sample_width_bytes,
            "source_duration_seconds": self.source_duration_seconds,
            "normalized_sample_rate_hz": self.normalized_sample_rate_hz,
            "normalized_channels": self.normalized_channels,
            "normalized_sample_width_bytes": self.normalized_sample_width_bytes,
            "normalized_duration_seconds": self.normalized_duration_seconds,
            "changed": self.changed,
            "error_message": self.error_message,
            "data": dict(self.data),
        }


def analyze_pcm_audio(frame_data: bytes, sample_width_bytes: int = 2) -> Dict[str, Any]:
    """Return bounded signal statistics for raw PCM without retaining samples."""

    return _pcm_signal_stats(bytes(frame_data or b""), sample_width_bytes)


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


def pcm_frame_sample_count(sample_rate_hz: int, frame_duration_ms: int) -> int:
    """Return frame samples from the format that actually supplies the PCM."""

    rate = int(sample_rate_hz)
    duration = int(frame_duration_ms)
    if rate <= 0 or duration <= 0:
        raise ValueError("sample_rate_and_frame_duration_must_be_positive")
    numerator = rate * duration
    if numerator % 1000:
        raise ValueError("frame_duration_does_not_align_with_sample_rate")
    return numerator // 1000


def validate_canonical_wav(path: str | Path) -> Dict[str, Any]:
    analysis = analyze_wav_audio(path)
    if not analysis.get("success"):
        return analysis
    actual = {
        "sample_rate_hz": int(analysis.get("sample_rate_hz", 0)),
        "channels": int(analysis.get("channels", 0)),
        "sample_width_bytes": int(analysis.get("sample_width_bytes", 0)),
    }
    expected = {
        "sample_rate_hz": CANONICAL_SAMPLE_RATE_HZ,
        "channels": CANONICAL_CHANNELS,
        "sample_width_bytes": CANONICAL_SAMPLE_WIDTH_BYTES,
    }
    if actual != expected:
        return {
            **analysis,
            "success": False,
            "error_message": "audio_format_not_canonical",
            "actual_format": actual,
            "expected_format": expected,
        }
    return {**analysis, "status": "valid_wav", "canonical": True}


def normalize_wav_audio(
    source_path: str | Path,
    output_path: str | Path,
    overwrite: bool = True,
) -> WavNormalizationResult:
    """Validate PCM WAV input and atomically create canonical Whisper/VAD audio."""

    source = Path(source_path).expanduser()
    output = Path(output_path).expanduser()
    source_label = str(source)
    if not source.exists():
        return WavNormalizationResult(
            success=False,
            status="source_missing",
            source_path=source_label,
            error_message="audio_file_missing",
        )
    try:
        if output.exists() and output != source and not overwrite:
            raise ValueError("output_path_already_exists")
        with wave.open(str(source), "rb") as wav_file:
            if wav_file.getcomptype() != "NONE":
                raise ValueError("compressed_wav_not_supported")
            source_rate = int(wav_file.getframerate())
            source_channels = int(wav_file.getnchannels())
            source_width = int(wav_file.getsampwidth())
            source_frames = int(wav_file.getnframes())
            pcm = wav_file.readframes(source_frames)
    except (EOFError, OSError, ValueError, wave.Error) as error:
        return WavNormalizationResult(
            success=False,
            status="invalid_source_wav",
            source_path=source_label,
            error_message=_safe_audio_error(error),
        )

    expected_bytes = source_frames * source_channels * source_width
    source_duration = source_frames / source_rate if source_rate > 0 else 0.0
    source_fields = {
        "source_sample_rate_hz": source_rate,
        "source_channels": source_channels,
        "source_sample_width_bytes": source_width,
        "source_duration_seconds": source_duration,
    }
    validation_error = _source_format_error(
        source_rate,
        source_channels,
        source_width,
        source_frames,
        len(pcm),
        expected_bytes,
    )
    if validation_error:
        return WavNormalizationResult(
            success=False,
            status="unsupported_or_truncated_source",
            source_path=source_label,
            error_message=validation_error,
            **source_fields,
        )

    try:
        normalized_pcm = _canonical_pcm(
            pcm,
            source_frames=source_frames,
            source_rate_hz=source_rate,
            source_channels=source_channels,
            source_width_bytes=source_width,
        )
        _write_pcm_wav_atomic(
            output,
            normalized_pcm,
            sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
            channels=CANONICAL_CHANNELS,
            sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
        )
    except (OSError, ValueError, wave.Error) as error:
        return WavNormalizationResult(
            success=False,
            status="normalization_failed",
            source_path=source_label,
            output_path=str(output),
            error_message=_safe_audio_error(error),
            **source_fields,
        )

    final = validate_canonical_wav(output)
    if not final.get("success"):
        return WavNormalizationResult(
            success=False,
            status="normalized_output_invalid",
            source_path=source_label,
            output_path=str(output),
            error_message=str(final.get("error_message") or "normalized_output_invalid"),
            **source_fields,
            data={"validation": final},
        )
    changed = (
        source_rate != CANONICAL_SAMPLE_RATE_HZ
        or source_channels != CANONICAL_CHANNELS
        or source_width != CANONICAL_SAMPLE_WIDTH_BYTES
        or source != output
    )
    return WavNormalizationResult(
        success=True,
        status="normalized" if changed else "already_canonical",
        source_path=source_label,
        output_path=str(output),
        normalized_duration_seconds=float(final.get("duration_seconds", 0.0)),
        changed=changed,
        **source_fields,
        data={
            "source_frame_count": source_frames,
            "normalized_frame_count": int(final.get("frames", 0)),
            "source_byte_count": len(pcm),
            "normalized_byte_count": max(0, int(final.get("byte_count", 0)) - 44),
            "resampling": "linear_pcm_v1",
            "byte_reinterpretation": False,
            "validation": final,
        },
    )


def _source_format_error(
    sample_rate_hz: int,
    channels: int,
    sample_width_bytes: int,
    frame_count: int,
    actual_bytes: int,
    expected_bytes: int,
) -> str:
    if not MIN_SUPPORTED_SAMPLE_RATE_HZ <= sample_rate_hz <= MAX_SUPPORTED_SAMPLE_RATE_HZ:
        return "unsupported_sample_rate"
    if not 1 <= channels <= MAX_SUPPORTED_CHANNELS:
        return "unsupported_channel_count"
    if sample_width_bytes not in {1, 2, 3, 4}:
        return "unsupported_sample_width"
    if frame_count <= 0:
        return "audio_has_no_frames"
    if actual_bytes != expected_bytes:
        return "truncated_pcm_data"
    return ""


def _canonical_pcm(
    pcm: bytes,
    source_frames: int,
    source_rate_hz: int,
    source_channels: int,
    source_width_bytes: int,
) -> bytes:
    target_frames = max(
        1,
        round(source_frames * CANONICAL_SAMPLE_RATE_HZ / source_rate_hz),
    )
    samples = array("h")
    for target_index in range(target_frames):
        source_position = target_index * source_rate_hz
        lower_index, remainder = divmod(source_position, CANONICAL_SAMPLE_RATE_HZ)
        lower_index = min(lower_index, source_frames - 1)
        upper_index = min(lower_index + 1, source_frames - 1)
        lower = _mono_frame_sample(
            pcm,
            lower_index,
            source_channels,
            source_width_bytes,
        )
        upper = _mono_frame_sample(
            pcm,
            upper_index,
            source_channels,
            source_width_bytes,
        )
        interpolated = (
            lower * (CANONICAL_SAMPLE_RATE_HZ - remainder) + upper * remainder
        ) // CANONICAL_SAMPLE_RATE_HZ
        samples.append(max(-32768, min(32767, interpolated)))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def _mono_frame_sample(
    pcm: bytes,
    frame_index: int,
    channels: int,
    sample_width: int,
) -> int:
    frame_offset = frame_index * channels * sample_width
    total = 0
    for channel in range(channels):
        offset = frame_offset + channel * sample_width
        if sample_width == 1:
            sample = (pcm[offset] - 128) << 8
        elif sample_width == 2:
            sample = struct.unpack_from("<h", pcm, offset)[0]
        elif sample_width == 3:
            raw = int.from_bytes(pcm[offset : offset + 3], "little", signed=False)
            signed = raw - (1 << 24) if raw & (1 << 23) else raw
            sample = signed >> 8
        else:
            sample = struct.unpack_from("<i", pcm, offset)[0] >> 16
        total += sample
    return int(round(total / channels))


def _write_pcm_wav_atomic(
    output: Path,
    pcm: bytes,
    sample_rate_hz: int,
    channels: int,
    sample_width_bytes: int,
) -> None:
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
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width_bytes)
            wav_file.setframerate(sample_rate_hz)
            wav_file.writeframes(pcm)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _safe_audio_error(error: Exception) -> str:
    if isinstance(error, ValueError):
        return str(error)[:160]
    return f"invalid_wav:{error.__class__.__name__}"


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
