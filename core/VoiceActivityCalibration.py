from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import Iterable, Sequence

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


@dataclass(frozen=True)
class RobustAmbientCalibration:
    success: bool
    quality_reason: str
    statistics: AmbientStatistics
    minimum_rms: float
    percentile_20_rms: float
    percentile_80_rms: float
    maximum_rms: float
    quiet_sample_count: int
    quiet_sample_fraction: float
    speech_frame_count: int
    non_speech_frame_count: int
    longest_non_speech_sequence: int
    bootstrap_threshold_rms: float
    clipped_frame_count: int
    clipped_frame_fraction: float
    zero_frame_count: int


def analyze_robust_ambient_calibration(
    levels: Sequence[float],
    peaks: Sequence[int],
    *,
    quiet_sample_fraction: float,
    minimum_quiet_frame_fraction: float,
    maximum_speech_frame_fraction: float,
    maximum_noise_floor_rms: float,
    maximum_clipped_frame_fraction: float,
    bootstrap_speech_multiplier: float,
    bootstrap_speech_margin_rms: float,
    minimum_bootstrap_speech_rms: float,
) -> RobustAmbientCalibration:
    """Estimate standby noise from a bounded low-energy sample set.

    The analysis never requires one uninterrupted silent window. High-energy
    frames are excluded after a provisional low-percentile bootstrap, while
    unusable, zero, clipped, or speech-dominated input fails closed.
    """

    clean = [float(level) for level in levels]
    if not clean or len(clean) != len(peaks):
        raise ValueError("ambient_calibration_frames_required")
    if any(not math.isfinite(level) or level < 0 for level in clean):
        raise ValueError("ambient_calibration_level_invalid")

    ordered = sorted(clean)
    frame_count = len(ordered)
    minimum_rms = ordered[0]
    maximum_rms = ordered[-1]
    percentile_20 = _linear_percentile(ordered, 0.20)
    percentile_80 = _linear_percentile(ordered, 0.80)
    initial_quiet_count = max(1, math.ceil(frame_count * quiet_sample_fraction))
    provisional_floor = float(statistics.median(ordered[:initial_quiet_count]))
    bounded_floor = min(provisional_floor, float(maximum_noise_floor_rms))
    bootstrap_threshold = max(
        float(minimum_bootstrap_speech_rms),
        bounded_floor * float(bootstrap_speech_multiplier),
        bounded_floor + float(bootstrap_speech_margin_rms),
    )
    speech_flags = [level >= bootstrap_threshold for level in clean]
    speech_frame_count = sum(speech_flags)
    non_speech_levels = [
        level for level, speech_like in zip(clean, speech_flags) if not speech_like
    ]
    non_speech_frame_count = len(non_speech_levels)
    longest_non_speech_sequence = _longest_false_run(speech_flags)
    minimum_quiet_count = max(
        1,
        math.ceil(frame_count * float(minimum_quiet_frame_fraction)),
    )
    selected_quiet_count = max(initial_quiet_count, minimum_quiet_count)
    quiet_levels = sorted(non_speech_levels)[:selected_quiet_count]
    clipped_frame_count = sum(int(peak) >= 32760 for peak in peaks)
    clipped_frame_fraction = clipped_frame_count / frame_count
    zero_frame_count = sum(level == 0.0 and int(peak) == 0 for level, peak in zip(clean, peaks))
    speech_frame_fraction = speech_frame_count / frame_count

    quality_reason = "calibration_quality_passed"
    if zero_frame_count == frame_count:
        quality_reason = "calibration_all_zero_pcm"
    elif clipped_frame_fraction > float(maximum_clipped_frame_fraction):
        quality_reason = "calibration_pcm_clipped"
    elif speech_frame_fraction > float(maximum_speech_frame_fraction):
        quality_reason = "calibration_speech_dominated"
    elif non_speech_frame_count < minimum_quiet_count:
        quality_reason = "calibration_quiet_samples_insufficient"
    elif not quiet_levels:
        quality_reason = "calibration_quiet_samples_insufficient"

    selected_noise_floor = (
        float(statistics.median(quiet_levels)) if quiet_levels else provisional_floor
    )
    if (
        quality_reason == "calibration_quality_passed"
        and selected_noise_floor > float(maximum_noise_floor_rms)
    ):
        quality_reason = "calibration_noise_floor_unusable"

    ambient = AmbientStatistics(
        mean_rms=round(statistics.fmean(clean), 6),
        median_rms=round(float(statistics.median(ordered)), 6),
        percentile_rms=round(percentile_80, 6),
        peak_rms=round(maximum_rms, 6),
        noise_floor_rms=round(selected_noise_floor, 6),
        sample_count=frame_count,
    )
    return RobustAmbientCalibration(
        success=quality_reason == "calibration_quality_passed",
        quality_reason=quality_reason,
        statistics=ambient,
        minimum_rms=round(minimum_rms, 6),
        percentile_20_rms=round(percentile_20, 6),
        percentile_80_rms=round(percentile_80, 6),
        maximum_rms=round(maximum_rms, 6),
        quiet_sample_count=len(quiet_levels),
        quiet_sample_fraction=round(len(quiet_levels) / frame_count, 6),
        speech_frame_count=speech_frame_count,
        non_speech_frame_count=non_speech_frame_count,
        longest_non_speech_sequence=longest_non_speech_sequence,
        bootstrap_threshold_rms=round(bootstrap_threshold, 6),
        clipped_frame_count=clipped_frame_count,
        clipped_frame_fraction=round(clipped_frame_fraction, 6),
        zero_frame_count=zero_frame_count,
    )


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


def _linear_percentile(ordered: Sequence[float], percentile: float) -> float:
    if not ordered:
        raise ValueError("ambient_calibration_frames_required")
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * float(percentile)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower]) * (1.0 - weight) + float(ordered[upper]) * weight


def _longest_false_run(flags: Sequence[bool]) -> int:
    longest = 0
    current = 0
    for flag in flags:
        if flag:
            current = 0
            continue
        current += 1
        longest = max(longest, current)
    return longest
