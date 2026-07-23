from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from core.AresIdentity import (
    CANONICAL_ARES_NAME,
    DEFAULT_ARES_NAME_ALIASES,
    clean_spoken_phrase,
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
    "ares goodbye",
    "go to standby ares",
    "go to sleep ares",
    "standby ares",
    "ares standby",
    "sleep ares",
)
DEFAULT_LIFECYCLE_SHUTDOWN_PHRASES = (
    "shutdown ares",
    "ares shutdown",
)

LIFECYCLE_ALIAS_TYPE_CANONICAL = "canonical"
LIFECYCLE_ALIAS_TYPE_PRONUNCIATION = "pronunciation_alias"
LIFECYCLE_ALIAS_TYPE_ACOUSTIC = "acoustic_alias"

_OUTER_TERMINAL_PUNCTUATION = " \t\r\n.!?,;:"
_GOOD_BYE = re.compile(r"\bgood\s+bye\b")
_SHUT_DOWN = re.compile(r"\bshut\s+down\b")
_LIFECYCLE_ACOUSTIC_NAME_FORMS = (
    (("rs",), "rs"),
    (("r", "s"), "r s"),
    (("are", "s"), "are s"),
)
_NEGATION_TOKEN_SEQUENCES = (
    ("do", "not"),
    ("don", "t"),
    ("dont",),
    ("never",),
    ("should", "not"),
    ("shouldn", "t"),
    ("shouldnt",),
)


@dataclass(frozen=True)
class _LifecycleAliasMatch:
    canonical_phrase: str
    matched_alias: str
    alias_type: str


@dataclass(frozen=True)
class LifecycleCommandResult:
    """Deterministic, whole-phrase lifecycle classification.

    ``normalized_transcript`` is punctuation/compound-word normalized but keeps
    the recognized name token. ``canonicalized_transcript`` replaces the name
    only after a complete lifecycle phrase matches. Ordinary input therefore
    remains suitable for normal skill routing (for example, ``what does RS
    mean`` is never rewritten to use ARES's name).
    """

    raw_transcript: str
    cleaned_transcript: str
    normalized_transcript: str
    canonicalized_transcript: str = ""
    canonical_name: str = ""
    matched_alias: str = ""
    alias_type: str = ""
    action: str = LIFECYCLE_ACTION_NONE
    matched_phrase: str = ""
    negation_detected: bool = False
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
        return self.canonicalized_transcript or self.normalized_transcript

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
            canonicalized_transcript="",
            rejection_reason="empty_transcript",
        )

    aliases = validate_ares_name_aliases(ares_name_aliases)
    activation = _canonical_phrases(
        activation_phrases,
        aliases=aliases,
        field_name="activation_phrases",
        allow_empty=True,
        allow_acoustic_aliases=False,
    )
    standby = _canonical_phrases(
        standby_phrases,
        aliases=aliases,
        field_name="standby_phrases",
        allow_acoustic_aliases=True,
    )
    shutdown = _canonical_phrases(
        shutdown_phrases,
        aliases=aliases,
        field_name="shutdown_phrases",
        allow_acoustic_aliases=True,
    )
    if set(activation) & set(standby):
        raise ValueError("activation and standby lifecycle phrases must not overlap")
    if set(activation) & set(shutdown):
        raise ValueError("activation and shutdown lifecycle phrases must not overlap")
    if set(standby) & set(shutdown):
        raise ValueError("standby and shutdown lifecycle phrases must not overlap")

    negation_detected = _contains_lifecycle_negation(normalized)
    if negation_detected:
        return LifecycleCommandResult(
            raw_transcript=raw,
            cleaned_transcript=cleaned,
            normalized_transcript=normalized,
            canonicalized_transcript=normalized,
            negation_detected=True,
            rejection_reason="negated_lifecycle_command",
        )

    for action, phrases, allow_acoustic_aliases in (
        (LIFECYCLE_ACTION_SHUTDOWN, shutdown, True),
        (LIFECYCLE_ACTION_STANDBY, standby, True),
        (LIFECYCLE_ACTION_ACTIVATE, activation, False),
    ):
        matched = _match_lifecycle_phrase(
            normalized,
            phrases,
            aliases=aliases,
            allow_acoustic_aliases=allow_acoustic_aliases,
        )
        if matched is not None:
            return LifecycleCommandResult(
                raw_transcript=raw,
                cleaned_transcript=cleaned,
                normalized_transcript=normalized,
                canonicalized_transcript=matched.canonical_phrase,
                canonical_name=CANONICAL_ARES_NAME,
                matched_alias=matched.matched_alias,
                alias_type=matched.alias_type,
                action=action,
                matched_phrase=matched.canonical_phrase,
                rejection_reason="",
            )

    return LifecycleCommandResult(
        raw_transcript=raw,
        cleaned_transcript=cleaned,
        normalized_transcript=normalized,
        canonicalized_transcript=normalized,
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
    allow_acoustic_aliases: bool,
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
        matched = _canonicalize_configured_lifecycle_phrase(
            normalized,
            aliases=aliases,
            allow_acoustic_aliases=allow_acoustic_aliases,
        )
        if not matched or CANONICAL_ARES_NAME not in matched.split():
            raise ValueError(f"{field_name} must contain an exact ARES name alias")
        if matched not in canonical:
            canonical.append(matched)
    return tuple(canonical)


def _match_lifecycle_phrase(
    normalized: str,
    canonical_phrases: Sequence[str],
    *,
    aliases: Sequence[str],
    allow_acoustic_aliases: bool,
) -> _LifecycleAliasMatch | None:
    candidate_tokens = tuple(normalized.split())
    for phrase in canonical_phrases:
        canonical_tokens = tuple(phrase.split())
        name_slots = [
            index
            for index, token in enumerate(canonical_tokens)
            if token == CANONICAL_ARES_NAME
        ]
        if len(name_slots) != 1:
            continue
        slot = name_slots[0]
        prefix = canonical_tokens[:slot]
        suffix = canonical_tokens[slot + 1 :]
        for alias_tokens, matched_alias, alias_type in _lifecycle_alias_forms(
            aliases,
            allow_acoustic_aliases=allow_acoustic_aliases,
        ):
            if candidate_tokens == prefix + alias_tokens + suffix:
                return _LifecycleAliasMatch(
                    canonical_phrase=" ".join(canonical_tokens),
                    matched_alias=matched_alias,
                    alias_type=alias_type,
                )
    return None


def _canonicalize_configured_lifecycle_phrase(
    normalized: str,
    *,
    aliases: Sequence[str],
    allow_acoustic_aliases: bool,
) -> str:
    tokens = tuple(normalized.split())
    matches: list[tuple[int, tuple[str, ...]]] = []
    for alias_tokens, _matched_alias, _alias_type in _lifecycle_alias_forms(
        aliases,
        allow_acoustic_aliases=allow_acoustic_aliases,
    ):
        width = len(alias_tokens)
        for index in range(0, len(tokens) - width + 1):
            if tokens[index : index + width] == alias_tokens:
                matches.append((index, alias_tokens))
    if len(matches) != 1:
        return ""
    index, alias_tokens = matches[0]
    canonical = (
        tokens[:index]
        + (CANONICAL_ARES_NAME,)
        + tokens[index + len(alias_tokens) :]
    )
    return " ".join(canonical)


def _lifecycle_alias_forms(
    aliases: Sequence[str],
    *,
    allow_acoustic_aliases: bool,
) -> tuple[tuple[tuple[str, ...], str, str], ...]:
    forms: list[tuple[tuple[str, ...], str, str]] = []
    for alias in validate_ares_name_aliases(aliases):
        forms.append(
            (
                (alias,),
                alias,
                (
                    LIFECYCLE_ALIAS_TYPE_CANONICAL
                    if alias == CANONICAL_ARES_NAME
                    else LIFECYCLE_ALIAS_TYPE_PRONUNCIATION
                ),
            )
        )
    if allow_acoustic_aliases:
        forms.extend(
            (tokens, matched_alias, LIFECYCLE_ALIAS_TYPE_ACOUSTIC)
            for tokens, matched_alias in _LIFECYCLE_ACOUSTIC_NAME_FORMS
        )
    return tuple(forms)


def _contains_lifecycle_negation(normalized: str) -> bool:
    tokens = tuple(normalized.split())
    return any(
        tokens[index : index + len(sequence)] == sequence
        for sequence in _NEGATION_TOKEN_SEQUENCES
        for index in range(0, len(tokens) - len(sequence) + 1)
    )
