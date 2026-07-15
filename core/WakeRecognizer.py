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
    raw_recognition_result: str = ""
    recognized_text: str = ""
    normalized_phrase: str = ""
    confidence: Optional[float] = None
    confidence_available: bool = False
    classification: str = "rejected"
    classification_reason: str = ""
    rejection_reason: str = ""
    selected_alias: str = ""
    selected_wake_phrase: str = ""
    canonical_wake_phrase: str = ""
    model_path: str = ""
    grammar_phrase_count: int = 0
    processing_time_seconds: float = 0.0


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
    minimum_confidence: float = 0.8,
    recognizer_name: str = "vosk_constrained_grammar",
    runtime_id: str = "",
    lifecycle_state: str = "STANDBY",
    correlation_id: str = "",
    model_path: str = "",
    grammar_phrase_count: int = 0,
    processing_time_seconds: float = 0.0,
) -> WakeRecognizerResultV1:
    """Classify only a complete constrained-grammar result.

    Confidence is the minimum confidence of all recognized words. This fails
    closed when one word is uncertain or when Vosk did not return word detail.
    """

    threshold = _bounded_confidence(minimum_confidence)
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
        normalized_words.extend(word.split())
        confidence = item.get("conf")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence_error = "missing_word_confidence"
            break
        value = float(confidence)
        if not isfinite(value) or not 0.0 <= value <= 1.0:
            confidence_error = "invalid_word_confidence"
            break
        confidences.append(value)

    confidence_value = min(confidences) if confidences else None
    category = WAKE_CATEGORY_NON_WAKE
    matched = ""
    selected_alias = ""
    wake_detected = False
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
    if unknown:
        rejection_reason = reason = "unknown_token_result"
    elif intended_category == WAKE_CATEGORY_NON_WAKE:
        rejection_reason = reason = "exact_constrained_phrase_not_matched"
    elif confidence_error:
        rejection_reason = reason = confidence_error
    elif not confidences:
        rejection_reason = reason = "missing_word_confidence"
    elif tuple(normalized.split()) != tuple(normalized_words):
        rejection_reason = reason = "recognition_word_result_mismatch"
    elif confidence_value is None or confidence_value < threshold:
        rejection_reason = reason = "wake_confidence_below_threshold"
    else:
        category = intended_category
        matched = normalized
        if category == WAKE_CATEGORY_ACTIVATION:
            selected_alias = next(
                (alias for alias in aliases if alias in normalized.split()),
                "",
            )
            if not selected_alias:
                rejection_reason = reason = "wake_alias_missing"
                category = WAKE_CATEGORY_NON_WAKE
            else:
                wake_detected = True
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
        selected_wake_phrase=normalized if wake_detected else "",
        canonical_wake_phrase=canonical if wake_detected else "",
        confidence=(round(confidence_value, 6) if confidence_value is not None else None),
        confidence_available=bool(confidences) and not confidence_error,
        minimum_confidence=threshold,
        classification_reason=reason,
        rejection_reason=rejection_reason,
        unknown_token_detected=unknown,
        recognized_token_count=len(normalized.split()),
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
