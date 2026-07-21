from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from core.Contracts import (
    WakeRecognizerRequestV1,
    WakeRecognizerResultV1,
    new_correlation_id,
)
from core.StandbyWakeListener import (
    WAKE_CATEGORY_ACTIVATION,
    WAKE_CATEGORY_NON_WAKE,
    WAKE_CATEGORY_SHUTDOWN,
    WAKE_CATEGORY_STANDBY,
    WAKE_STATUS_CONTROL_DETECTED,
    WAKE_STATUS_DETECTED,
    WAKE_STATUS_NON_WAKE_SPEECH,
    expand_control_phrase_aliases,
    normalize_wake_phrase,
)


WAKE_RECOGNIZER_STOPPED = "stopped"
WAKE_RECOGNIZER_READY = "ready"
WAKE_RECOGNIZER_RECOGNIZING = "recognizing"
WAKE_RECOGNIZER_ERROR = "error"


@dataclass(frozen=True)
class WakeRecognizerLocalDiagnostics:
    """Ephemeral recognizer details for an explicitly enabled owner terminal."""

    recognizer_name: str = ""
    attempt_id: str = ""
    stream_generation: int = 0
    candidate_number: int = 0
    raw_recognition_result: str = ""
    recognized_text: str = ""
    normalized_phrase: str = ""
    confidence: Optional[float] = None
    confidence_available: bool = False
    minimum_word_confidence: Optional[float] = None
    mean_word_confidence: Optional[float] = None
    canonical_confidence: Optional[float] = None
    duplicate_collapse_used: bool = False
    confidence_tier: str = ""
    confirmation_count: int = 0
    confirmation_required_count: int = 0
    classification: str = "rejected"
    classification_reason: str = ""
    rejection_reason: str = ""
    selected_alias: str = ""
    selected_wake_phrase: str = ""
    canonical_wake_phrase: str = ""
    model_path: str = ""
    grammar_phrase_count: int = 0
    processing_time_seconds: float = 0.0


@dataclass(frozen=True)
class WakeRecognitionAttempt:
    """A recognition result and its terminal-only diagnostics from one call."""

    result: WakeRecognizerResultV1
    diagnostics: WakeRecognizerLocalDiagnostics

    def __post_init__(self) -> None:
        if self.result.attempt_id != self.diagnostics.attempt_id:
            raise ValueError("wake recognition attempt IDs do not match")
        if self.result.stream_generation != self.diagnostics.stream_generation:
            raise ValueError("wake recognition stream generations do not match")
        if self.result.candidate_number != self.diagnostics.candidate_number:
            raise ValueError("wake recognition candidate numbers do not match")


@runtime_checkable
class WakeRecognizer(Protocol):
    recognizer_name: str

    def start(self) -> WakeRecognizerResultV1:
        ...

    def health_check(self) -> WakeRecognizerResultV1:
        ...

    def recognize_wav(self, request: WakeRecognizerRequestV1) -> WakeRecognizerResultV1:
        ...

    def cancel(self) -> WakeRecognizerResultV1:
        ...

    def stop(self) -> WakeRecognizerResultV1:
        ...


def classify_constrained_recognition(
    recognized_text: str,
    word_results: Sequence[Mapping[str, Any]],
    *,
    wake_phrases: Sequence[str],
    wake_phrase_aliases: Sequence[str],
    standby_phrases: Sequence[str] = (),
    shutdown_phrases: Sequence[str] = (),
    canonical_wake_phrase: str = "ares",
    minimum_confidence: float = 0.55,
    medium_confidence: float = 0.40,
    allow_exact_wake_without_confidence: bool = False,
    medium_confirmation_repetitions: int = 2,
    recognizer_name: str = "vosk_constrained_grammar",
    runtime_id: str = "",
    lifecycle_state: str = "STANDBY",
    correlation_id: str = "",
    model_path: str = "",
    grammar_phrase_count: int = 0,
    processing_time_seconds: float = 0.0,
    audio_duration_seconds: float = 0.0,
    maximum_duplicate_collapse_audio_seconds: float = 4.0,
) -> WakeRecognizerResultV1:
    """Classify only a complete constrained-grammar result.

    For an exact phrase, confidence remains the conservative minimum word
    confidence. A two-token canonical-alias collapse also exposes the mean but
    deliberately uses the minimum, so one weak token cannot be hidden by a
    stronger duplicate. Missing confidence is accepted only for a single exact
    configured activation phrase after bounded candidate validation.
    """

    threshold = _bounded_confidence(minimum_confidence)
    medium_threshold = _bounded_confidence(medium_confidence)
    if medium_threshold >= threshold:
        raise ValueError("medium_confidence must be below minimum_confidence")
    if not isinstance(allow_exact_wake_without_confidence, bool):
        raise ValueError("allow_exact_wake_without_confidence must be a boolean")
    audio_duration = _bounded_duration(audio_duration_seconds, "audio_duration_seconds", 5.0)
    duplicate_duration_limit = _bounded_duration(
        maximum_duplicate_collapse_audio_seconds,
        "maximum_duplicate_collapse_audio_seconds",
        4.0,
        minimum=0.25,
    )
    if (
        isinstance(medium_confirmation_repetitions, bool)
        or not isinstance(medium_confirmation_repetitions, int)
        or not 2 <= medium_confirmation_repetitions <= 3
    ):
        raise ValueError("medium_confirmation_repetitions must be between 2 and 3")
    normalized = normalize_wake_phrase(recognized_text)
    wake = _normalized_unique(wake_phrases, "wake_phrases", required=True)
    aliases = _normalized_unique(wake_phrase_aliases, "wake_phrase_aliases", required=True)
    standby = expand_control_phrase_aliases(standby_phrases, aliases)
    shutdown = expand_control_phrase_aliases(shutdown_phrases, aliases)
    canonical = normalize_wake_phrase(canonical_wake_phrase)
    if not canonical:
        raise ValueError("canonical_wake_phrase must not be empty")
    if set(wake) & set(standby) or set(wake) & set(shutdown) or set(standby) & set(shutdown):
        raise ValueError("wake, standby, and shutdown phrases must not overlap")

    normalized_words: list[str] = []
    confidences: list[float] = []
    confidence_error = ""
    unknown = "[unk]" in str(recognized_text or "").casefold()
    for item in word_results:
        if not isinstance(item, Mapping):
            confidence_error = "missing_word_confidence"
            break
        raw_word = str(item.get("word") or "").strip()
        word = normalize_wake_phrase(raw_word)
        if raw_word.casefold() == "[unk]" or word == "unk":
            unknown = True
        if not word:
            confidence_error = "missing_word_confidence"
            break
        word_tokens = word.split()
        if len(word_tokens) != 1:
            confidence_error = "invalid_word_token_structure"
            break
        normalized_words.extend(word_tokens)
        confidence = item.get("conf")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence_error = "missing_word_confidence"
            break
        value = float(confidence)
        if not isfinite(value) or not 0.0 <= value <= 1.0:
            confidence_error = "invalid_word_confidence"
            break
        confidences.append(value)

    minimum_word_confidence = min(confidences) if confidences else None
    mean_word_confidence = (
        sum(confidences) / len(confidences) if confidences else None
    )
    # This is intentionally conservative for a collapsed duplicate: every
    # alias token must independently satisfy the configured confidence policy.
    confidence_value = minimum_word_confidence
    category = WAKE_CATEGORY_NON_WAKE
    matched = ""
    selected_alias = ""
    wake_detected = False
    confidence_tier = ""
    confirmation_required = False
    rejection_reason = ""
    reason = ""

    intended_category = (
        WAKE_CATEGORY_SHUTDOWN
        if normalized in shutdown
        else WAKE_CATEGORY_STANDBY
        if normalized in standby
        else WAKE_CATEGORY_ACTIVATION
        if normalized in wake
        else WAKE_CATEGORY_NON_WAKE
    )
    duplicate_collapse_used = False
    collapsed_canonical_phrase = ""
    duplicate_alias = ""
    if (
        intended_category == WAKE_CATEGORY_NON_WAKE
        and not unknown
        and confidence_error != "invalid_word_token_structure"
    ):
        duplicate_alias = _canonical_duplicate_alias(
            normalized,
            aliases=aliases,
            audio_duration_seconds=audio_duration,
            maximum_audio_seconds=duplicate_duration_limit,
        )
        if duplicate_alias:
            intended_category = WAKE_CATEGORY_ACTIVATION
            duplicate_collapse_used = True
            collapsed_canonical_phrase = canonical
    if confidence_value is None:
        confidence_tier = "missing"
    elif confidence_value >= threshold:
        confidence_tier = "high"
    elif confidence_value >= medium_threshold:
        confidence_tier = "medium"
    else:
        confidence_tier = "low"

    words_match = (
        not normalized_words
        or tuple(normalized.split()) == tuple(normalized_words)
    )
    missing_confidence = (
        not confidences or confidence_error == "missing_word_confidence"
    )

    if unknown:
        rejection_reason = reason = "unknown_token_result"
    elif confidence_error and confidence_error != "missing_word_confidence":
        rejection_reason = reason = confidence_error
    elif intended_category == WAKE_CATEGORY_NON_WAKE:
        rejection_reason = reason = "exact_constrained_phrase_not_matched"
    elif not words_match:
        rejection_reason = reason = "recognition_word_result_mismatch"
    elif (
        intended_category == WAKE_CATEGORY_ACTIVATION
        and missing_confidence
        and allow_exact_wake_without_confidence
        and not duplicate_collapse_used
    ):
        matched = normalized
        selected_alias = next(
            (alias for alias in aliases if alias in normalized.split()),
            "",
        )
        if selected_alias:
            category = WAKE_CATEGORY_ACTIVATION
            wake_detected = True
            reason = "accepted_exact_wake_without_confidence"
        else:
            rejection_reason = reason = "wake_alias_missing"
    elif missing_confidence:
        rejection_reason = reason = "missing_word_confidence"
    elif confidence_value is None:
        rejection_reason = reason = "missing_word_confidence"
    elif confidence_value < medium_threshold:
        rejection_reason = reason = "wake_confidence_below_medium_threshold"
    elif confidence_value < threshold:
        if intended_category == WAKE_CATEGORY_ACTIVATION:
            matched = canonical if duplicate_collapse_used else normalized
            selected_alias = next(
                (alias for alias in aliases if alias in normalized.split()),
                "",
            )
            confirmation_required = bool(selected_alias)
            rejection_reason = reason = (
                "medium_confidence_confirmation_required"
                if selected_alias
                else "wake_alias_missing"
            )
        else:
            rejection_reason = reason = "control_confidence_below_high_threshold"
    else:
        category = intended_category
        matched = normalized
        if category == WAKE_CATEGORY_ACTIVATION:
            selected_alias = duplicate_alias or next(
                (alias for alias in aliases if alias in normalized.split()),
                "",
            )
            if not selected_alias:
                rejection_reason = reason = "wake_alias_missing"
                category = WAKE_CATEGORY_NON_WAKE
            else:
                wake_detected = True
                if duplicate_collapse_used:
                    matched = canonical
                    reason = "accepted_canonical_duplicate_wake"
                else:
                    reason = "accepted_vosk_constrained_grammar"
        elif category == WAKE_CATEGORY_SHUTDOWN:
            reason = "accepted_exact_shutdown_control"
        else:
            reason = "accepted_exact_standby_control"

    status = (
        WAKE_STATUS_DETECTED
        if wake_detected
        else WAKE_STATUS_CONTROL_DETECTED
        if category in {WAKE_CATEGORY_STANDBY, WAKE_CATEGORY_SHUTDOWN}
        else WAKE_STATUS_NON_WAKE_SPEECH
    )
    return WakeRecognizerResultV1(
        success=True,
        status=status,
        runtime_id=runtime_id,
        lifecycle_state=lifecycle_state,
        recognizer_name=recognizer_name,
        wake_detected=wake_detected,
        command_category=category,
        normalized_wake_phrase=canonical if wake_detected else "",
        matched_phrase=matched,
        selected_alias=selected_alias,
        selected_wake_phrase=(
            canonical if wake_detected and duplicate_collapse_used
            else normalized if wake_detected
            else ""
        ),
        canonical_wake_phrase=canonical if wake_detected else "",
        confidence=(round(confidence_value, 6) if confidence_value is not None else None),
        confidence_available=bool(confidences) and not confidence_error,
        minimum_word_confidence=(
            round(minimum_word_confidence, 6)
            if minimum_word_confidence is not None
            else None
        ),
        mean_word_confidence=(
            round(mean_word_confidence, 6)
            if mean_word_confidence is not None
            else None
        ),
        canonical_confidence=(
            round(confidence_value, 6) if confidence_value is not None else None
        ),
        minimum_confidence=threshold,
        medium_confidence=medium_threshold,
        confidence_tier=confidence_tier,
        confirmation_required=confirmation_required,
        confirmation_count=1 if confirmation_required else 0,
        confirmation_required_count=(
            medium_confirmation_repetitions if confirmation_required else 0
        ),
        classification_reason=reason,
        rejection_reason=rejection_reason,
        unknown_token_detected=unknown,
        recognized_token_count=len(normalized.split()),
        duplicate_collapse_used=duplicate_collapse_used,
        collapsed_canonical_phrase=collapsed_canonical_phrase,
        audio_duration_seconds=round(audio_duration, 6),
        processing_time_seconds=round(max(0.0, float(processing_time_seconds)), 6),
        model_path=str(model_path or ""),
        grammar_phrase_count=max(0, int(grammar_phrase_count)),
        correlation_id=correlation_id or new_correlation_id("wake-recognizer"),
        metadata={"safe": True, "contains_transcript": False, "contains_audio": False},
    )


def _bounded_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("minimum_confidence must be a finite number")
    number = float(value)
    if not isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("minimum_confidence must be between 0 and 1")
    return number


def _bounded_duration(
    value: Any,
    field_name: str,
    maximum: float,
    *,
    minimum: float = 0.0,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    number = float(value)
    if not isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return number


def _canonical_duplicate_alias(
    normalized: str,
    *,
    aliases: Sequence[str],
    audio_duration_seconds: float,
    maximum_audio_seconds: float,
) -> str:
    tokens = tuple(normalized.split())
    if len(tokens) != 2:
        return ""
    alias_set = set(aliases)
    if any(token not in alias_set for token in tokens):
        return ""
    if tokens[0] != tokens[1]:
        return ""
    if audio_duration_seconds <= 0 or audio_duration_seconds > maximum_audio_seconds:
        return ""
    # Only one duplicated exact alias may collapse. Mixed aliases, prefixes,
    # unknowns, and unrelated words remain outside this wake-only exception.
    return tokens[0]


def _normalized_unique(
    values: Sequence[str],
    field_name: str,
    *,
    required: bool,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence")
    normalized = tuple(normalize_wake_phrase(value) for value in values)
    if required and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if any(not value or len(value) > 64 for value in normalized):
        raise ValueError(f"{field_name} contains an empty or oversized phrase")
    if len(normalized) > 32:
        raise ValueError(f"{field_name} contains too many phrases")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} contains duplicates after normalization")
    return normalized
