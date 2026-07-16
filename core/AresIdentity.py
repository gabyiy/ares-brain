from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Sequence
import unicodedata


CANONICAL_ARES_NAME = "ares"
DEFAULT_ARES_NAME_ALIASES = (CANONICAL_ARES_NAME, "aris", "aries")
MAX_ARES_NAME_ALIASES = 8
MAX_ARES_NAME_ALIAS_LENGTH = 24

_PHRASE_COMPONENT = re.compile(r"[^a-z0-9]+")
_ALIAS_PATTERN = re.compile(r"^[a-z0-9]{1,24}$")


@dataclass(frozen=True)
class AresNamePolicy:
    """Validated whole-token aliases for ARES's spoken name."""

    aliases: tuple[str, ...] = DEFAULT_ARES_NAME_ALIASES
    canonical_name: str = CANONICAL_ARES_NAME

    def __post_init__(self) -> None:
        canonical = normalize_spoken_phrase(self.canonical_name)
        if canonical != CANONICAL_ARES_NAME:
            raise ValueError(f"canonical_name must be {CANONICAL_ARES_NAME}")
        object.__setattr__(self, "canonical_name", canonical)
        object.__setattr__(self, "aliases", validate_ares_name_aliases(self.aliases))

    def canonicalize(self, value: Any) -> str:
        return canonicalize_ares_name_tokens(value, self.aliases)


def clean_spoken_phrase(value: Any) -> str:
    cleaned = unicodedata.normalize("NFKC", str(value or ""))
    cleaned = cleaned.replace("\u2019", "'").replace("`", "'")
    return " ".join(cleaned.split()).strip()


def normalize_spoken_phrase(value: Any) -> str:
    return _PHRASE_COMPONENT.sub(" ", clean_spoken_phrase(value).casefold()).strip()


def validate_ares_name_aliases(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("ares_name_aliases must be a sequence of aliases")
    if any(not isinstance(value, str) for value in values):
        raise ValueError("ares_name_aliases must contain strings")
    normalized = tuple(dict.fromkeys(normalize_spoken_phrase(value) for value in values))
    if not normalized:
        raise ValueError("ares_name_aliases must contain at least one alias")
    if len(normalized) > MAX_ARES_NAME_ALIASES:
        raise ValueError(
            f"ares_name_aliases may contain at most {MAX_ARES_NAME_ALIASES} aliases"
        )
    if any(
        not _ALIAS_PATTERN.fullmatch(value)
        or len(value) > MAX_ARES_NAME_ALIAS_LENGTH
        for value in normalized
    ):
        raise ValueError(
            "ares_name_aliases must contain one safe word of at most "
            f"{MAX_ARES_NAME_ALIAS_LENGTH} characters"
        )
    if CANONICAL_ARES_NAME not in normalized:
        raise ValueError("ares_name_aliases must include the canonical alias ares")
    return (CANONICAL_ARES_NAME,) + tuple(
        value for value in normalized if value != CANONICAL_ARES_NAME
    )


def canonicalize_ares_name_tokens(
    value: Any,
    aliases: Sequence[str] = DEFAULT_ARES_NAME_ALIASES,
) -> str:
    """Normalize text and replace only complete configured alias tokens."""

    policy = set(validate_ares_name_aliases(aliases))
    return " ".join(
        CANONICAL_ARES_NAME if token in policy else token
        for token in normalize_spoken_phrase(value).split()
    )


def expand_ares_alias_phrases(
    phrases: Sequence[str],
    aliases: Sequence[str] = DEFAULT_ARES_NAME_ALIASES,
) -> tuple[str, ...]:
    """Expand canonical control phrases without substring substitutions."""

    normalized_aliases = validate_ares_name_aliases(aliases)
    expanded: list[str] = []
    for value in phrases:
        phrase = normalize_spoken_phrase(value)
        if not phrase:
            raise ValueError("control phrase must not be empty")
        tokens = phrase.split()
        if CANONICAL_ARES_NAME not in tokens:
            expanded.append(phrase)
            continue
        for alias in normalized_aliases:
            expanded.append(
                " ".join(
                    alias if token == CANONICAL_ARES_NAME else token
                    for token in tokens
                )
            )
    return tuple(dict.fromkeys(expanded))


def match_ares_alias_phrase(
    value: Any,
    canonical_phrases: Sequence[str],
    aliases: Sequence[str] = DEFAULT_ARES_NAME_ALIASES,
) -> str:
    """Return a canonical phrase only for an exact configured alias-slot match."""

    normalized = normalize_spoken_phrase(value)
    normalized_aliases = validate_ares_name_aliases(aliases)
    for phrase in canonical_phrases:
        canonical = canonicalize_ares_name_tokens(phrase, normalized_aliases)
        if normalized in expand_ares_alias_phrases((canonical,), normalized_aliases):
            return canonical
    return ""
