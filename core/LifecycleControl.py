from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from core.AresIdentity import (
    CANONICAL_ARES_NAME,
    DEFAULT_ARES_NAME_ALIASES,
    clean_spoken_phrase,
    match_ares_alias_phrase,
    normalize_spoken_phrase,
    validate_ares_name_aliases,
)


LIFECYCLE_ACTION_NONE = "none"
LIFECYCLE_ACTION_ACTIVATE = "activate"
LIFECYCLE_ACTION_STANDBY = "standby"
LIFECYCLE_ACTION_SHUTDOWN = "shutdown"

DEFAULT_LIFECYCLE_ACTIVATION_PHRASES = (
    "ares",
    "hey ares",
    "hello ares",
    "wake up ares",
)
DEFAULT_LIFECYCLE_STANDBY_PHRASES = (
    "goodbye ares",
    "go to standby ares",
    "go to sleep ares",
    "standby ares",
    "sleep ares",
)
DEFAULT_LIFECYCLE_SHUTDOWN_PHRASES = ("shutdown ares",)

_OUTER_TERMINAL_PUNCTUATION = " \t\r\n.!?,;:"
_GOOD_BYE = re.compile(r"\bgood\s+bye\b")
_SHUT_DOWN = re.compile(r"\bshut\s+down\b")


@dataclass(frozen=True)
class LifecycleCommandResult:
    """Deterministic, whole-phrase lifecycle classification.

    ``normalized_transcript`` canonicalizes a name alias only after the complete
    phrase matches. Ordinary input therefore remains suitable for normal skill
    routing (for example, ``aries horoscope`` is not rewritten to use ARES's
    name).
    """

    raw_transcript: str
    cleaned_transcript: str
    normalized_transcript: str
    canonical_name: str = ""
    action: str = LIFECYCLE_ACTION_NONE
    matched_phrase: str = ""
    rejection_reason: str = "exact_lifecycle_phrase_not_matched"

    @property
    def matched(self) -> bool:
        return self.action != LIFECYCLE_ACTION_NONE

    # Compatibility properties for callers of the former classifier contract.
    @property
    def cleaned_input(self) -> str:
        return _normalize_lifecycle_words(self.cleaned_transcript)

    @property
    def canonicalized_input(self) -> str:
        return self.normalized_transcript

    @property
    def routing_reason(self) -> str:
        if self.action == LIFECYCLE_ACTION_ACTIVATE:
            return "exact_activation_phrase_after_alias_canonicalization"
        if self.action == LIFECYCLE_ACTION_STANDBY:
            return "exact_standby_phrase_after_alias_canonicalization"
        if self.action == LIFECYCLE_ACTION_SHUTDOWN:
            return "exact_shutdown_phrase_after_alias_canonicalization"
        return self.rejection_reason or "exact_lifecycle_phrase_not_matched"


# Backward-compatible type name; all classification is implemented by
# normalize_lifecycle_command below.
LifecycleControlClassification = LifecycleCommandResult


def normalize_lifecycle_command(
    raw_transcript: str,
    *,
    activation_phrases: Sequence[str] = DEFAULT_LIFECYCLE_ACTIVATION_PHRASES,
    standby_phrases: Sequence[str] = DEFAULT_LIFECYCLE_STANDBY_PHRASES,
    shutdown_phrases: Sequence[str] = DEFAULT_LIFECYCLE_SHUTDOWN_PHRASES,
    ares_name_aliases: Sequence[str] = DEFAULT_ARES_NAME_ALIASES,
) -> LifecycleCommandResult:
    """Normalize and classify one exact lifecycle command.

    Matching is deterministic and whole-phrase only. Punctuation, casing,
    repeated whitespace, ``good bye``, and ``shut down`` are normalized before
    matching. No substring, fuzzy, edit-distance, or semantic matching is used.
    """

    raw = str(raw_transcript or "")
    cleaned = clean_spoken_phrase(raw).strip(_OUTER_TERMINAL_PUNCTUATION)
    normalized = _normalize_lifecycle_words(cleaned)
    if not normalized:
        return LifecycleCommandResult(
            raw_transcript=raw,
            cleaned_transcript=cleaned,
            normalized_transcript="",
            rejection_reason="empty_transcript",
        )

    aliases = validate_ares_name_aliases(ares_name_aliases)
    activation = _canonical_phrases(
        activation_phrases,
        aliases=aliases,
        field_name="activation_phrases",
        allow_empty=True,
    )
    standby = _canonical_phrases(
        standby_phrases,
        aliases=aliases,
        field_name="standby_phrases",
    )
    shutdown = _canonical_phrases(
        shutdown_phrases,
        aliases=aliases,
        field_name="shutdown_phrases",
    )
    if set(activation) & set(standby):
        raise ValueError("activation and standby lifecycle phrases must not overlap")
    if set(activation) & set(shutdown):
        raise ValueError("activation and shutdown lifecycle phrases must not overlap")
    if set(standby) & set(shutdown):
        raise ValueError("standby and shutdown lifecycle phrases must not overlap")

    for action, phrases in (
        (LIFECYCLE_ACTION_SHUTDOWN, shutdown),
        (LIFECYCLE_ACTION_STANDBY, standby),
        (LIFECYCLE_ACTION_ACTIVATE, activation),
    ):
        matched = match_ares_alias_phrase(normalized, phrases, aliases)
        if matched:
            return LifecycleCommandResult(
                raw_transcript=raw,
                cleaned_transcript=cleaned,
                normalized_transcript=matched,
                canonical_name=CANONICAL_ARES_NAME,
                action=action,
                matched_phrase=matched,
                rejection_reason="",
            )

    return LifecycleCommandResult(
        raw_transcript=raw,
        cleaned_transcript=cleaned,
        normalized_transcript=normalized,
    )


def classify_lifecycle_control(
    text: str,
    *,
    standby_phrases: Sequence[str],
    shutdown_phrases: Sequence[str],
    ares_name_aliases: Sequence[str] = DEFAULT_ARES_NAME_ALIASES,
) -> LifecycleCommandResult:
    """Compatibility wrapper for standby/shutdown-only callers."""

    return normalize_lifecycle_command(
        text,
        activation_phrases=(),
        standby_phrases=standby_phrases,
        shutdown_phrases=shutdown_phrases,
        ares_name_aliases=ares_name_aliases,
    )


def _normalize_lifecycle_words(value: str) -> str:
    normalized = normalize_spoken_phrase(value)
    normalized = _GOOD_BYE.sub("goodbye", normalized)
    normalized = _SHUT_DOWN.sub("shutdown", normalized)
    return " ".join(normalized.split())


def _canonical_phrases(
    values: Sequence[str],
    *,
    aliases: Sequence[str],
    field_name: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of phrases")
    if not values and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    canonical: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must contain strings")
        normalized = _normalize_lifecycle_words(value)
        if not normalized or len(normalized) > 64:
            raise ValueError(f"{field_name} contains an empty or oversized phrase")
        matched = match_ares_alias_phrase(normalized, (normalized,), aliases)
        if not matched or CANONICAL_ARES_NAME not in matched.split():
            raise ValueError(f"{field_name} must contain an exact ARES name alias")
        if matched not in canonical:
            canonical.append(matched)
    return tuple(canonical)
