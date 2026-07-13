from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import Iterable

from core.Contracts import VoiceActivityCaptureRequestV1


@dataclass(frozen=True)
class AmbientStatistics:
    mean_rms: float
    median_rms: float
    percentile_rms: float
    peak_rms: float
    noise_floor_rms: float
    sample_count: int

    def to_dict(self) -> dict:
        return {
            "mean_rms": self.mean_rms,
            "median_rms": self.median_rms,
            "percentile_rms": self.percentile_rms,
            "peak_rms": self.peak_rms,
            "noise_floor_rms": self.noise_floor_rms,
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True)
class VoiceActivityThresholds:
    speech_start_rms: float
    speech_continue_rms: float
    silence_rms: float

    def to_dict(self) -> dict:
        return {
            "speech_start_rms": self.speech_start_rms,
            "speech_continue_rms": self.speech_continue_rms,
            "silence_rms": self.silence_rms,
        }


def calculate_ambient_statistics(
    levels: Iterable[float],
    percentile: float = 0.90,
) -> AmbientStatistics:
    clean = [float(level) for level in levels if math.isfinite(float(level)) and float(level) >= 0]
    if not clean:
        raise ValueError("ambient_calibration_frames_required")
    ordered = sorted(clean)
    rank = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    median = statistics.median(ordered)
    percentile_level = ordered[rank]
    # Derivation intentionally ignores the absolute peak so one tap cannot set the floor.
    noise_floor = max(float(median), float(percentile_level))
    return AmbientStatistics(
        mean_rms=round(statistics.fmean(ordered), 6),
        median_rms=round(float(median), 6),
        percentile_rms=round(float(percentile_level), 6),
        peak_rms=round(max(ordered), 6),
        noise_floor_rms=round(noise_floor, 6),
        sample_count=len(ordered),
    )


def manual_thresholds(request: VoiceActivityCaptureRequestV1) -> VoiceActivityThresholds:
    return VoiceActivityThresholds(
        speech_start_rms=float(request.speech_start_rms),
        speech_continue_rms=float(request.speech_continue_rms),
        silence_rms=float(request.silence_rms),
    )


def derive_thresholds(
    statistics_result: AmbientStatistics,
    request: VoiceActivityCaptureRequestV1,
) -> VoiceActivityThresholds:
    noise = statistics_result.noise_floor_rms
    silence = _clamp(
        noise + max(20.0, noise * 0.75),
        request.minimum_silence_rms,
        request.maximum_silence_rms,
    )
    continue_level = _clamp(
        noise + max(60.0, noise * 1.50),
        request.minimum_speech_continue_rms,
        request.maximum_speech_continue_rms,
    )
    start = _clamp(
        noise + max(120.0, noise * 2.50),
        request.minimum_speech_start_rms,
        request.maximum_speech_start_rms,
    )
    return _ordered_thresholds(start, continue_level, silence, request)


def adapt_post_speech_thresholds(
    thresholds: VoiceActivityThresholds,
    noise_floor_rms: float,
    speech_reference_rms: float,
    request: VoiceActivityCaptureRequestV1,
) -> VoiceActivityThresholds:
    """Raise only bounded post-speech gates; never alter the speech-start gate."""

    continue_target = max(
        thresholds.speech_continue_rms,
        min(thresholds.speech_start_rms - 10.0, speech_reference_rms * 0.15),
    )
    continue_level = _clamp(
        continue_target,
        request.minimum_speech_continue_rms,
        min(request.maximum_speech_continue_rms, thresholds.speech_start_rms - 10.0),
    )
    silence_target = max(
        thresholds.silence_rms,
        noise_floor_rms + max(20.0, noise_floor_rms * 0.50),
    )
    silence = _clamp(
        silence_target,
        request.minimum_silence_rms,
        min(request.maximum_silence_rms, continue_level - 10.0),
    )
    return VoiceActivityThresholds(
        speech_start_rms=thresholds.speech_start_rms,
        speech_continue_rms=round(continue_level, 6),
        silence_rms=round(silence, 6),
    )


def update_noise_floor(current: float, sample: float, alpha: float = 0.05) -> float:
    bounded_alpha = max(0.0, min(0.25, float(alpha)))
    return round((1.0 - bounded_alpha) * float(current) + bounded_alpha * float(sample), 6)


def _ordered_thresholds(
    start: float,
    continue_level: float,
    silence: float,
    request: VoiceActivityCaptureRequestV1,
) -> VoiceActivityThresholds:
    gap = 10.0
    continue_level = max(continue_level, silence + gap)
    start = max(start, continue_level + gap)
    if continue_level > request.maximum_speech_continue_rms:
        continue_level = request.maximum_speech_continue_rms
    if start > request.maximum_speech_start_rms:
        start = request.maximum_speech_start_rms
    silence = min(silence, continue_level - gap)
    if not 0 < silence < continue_level < start:
        raise ValueError("derived_voice_activity_thresholds_invalid")
    return VoiceActivityThresholds(
        speech_start_rms=round(start, 6),
        speech_continue_rms=round(continue_level, 6),
        silence_rms=round(silence, 6),
    )


def _clamp(value: float, minimum: float, maximum: float) -> float:
    if minimum > maximum:
        raise ValueError("voice_activity_threshold_bounds_invalid")
    return max(float(minimum), min(float(maximum), float(value)))
