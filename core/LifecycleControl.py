from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from core.AresIdentity import (
    DEFAULT_ARES_NAME_ALIASES,
    canonicalize_ares_name_tokens,
    normalize_spoken_phrase,
)


LIFECYCLE_ACTION_NONE = "none"
LIFECYCLE_ACTION_STANDBY = "standby"
LIFECYCLE_ACTION_SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class LifecycleControlClassification:
    cleaned_input: str
    canonicalized_input: str
    action: str = LIFECYCLE_ACTION_NONE
    matched_phrase: str = ""
    routing_reason: str = "not_lifecycle_control"

    @property
    def matched(self) -> bool:
        return self.action != LIFECYCLE_ACTION_NONE


def classify_lifecycle_control(
    text: str,
    *,
    standby_phrases: Sequence[str],
    shutdown_phrases: Sequence[str],
    ares_name_aliases: Sequence[str] = DEFAULT_ARES_NAME_ALIASES,
) -> LifecycleControlClassification:
    """Classify only a complete, configured lifecycle-control phrase."""

    cleaned = normalize_spoken_phrase(text)
    canonicalized = canonicalize_ares_name_tokens(cleaned, ares_name_aliases)
    standby = {
        canonicalize_ares_name_tokens(value, ares_name_aliases)
        for value in standby_phrases
    }
    shutdown = {
        canonicalize_ares_name_tokens(value, ares_name_aliases)
        for value in shutdown_phrases
    }
    if standby & shutdown:
        raise ValueError("standby and shutdown lifecycle phrases must not overlap")
    if canonicalized in shutdown:
        return LifecycleControlClassification(
            cleaned_input=cleaned,
            canonicalized_input=canonicalized,
            action=LIFECYCLE_ACTION_SHUTDOWN,
            matched_phrase=canonicalized,
            routing_reason="exact_shutdown_phrase_after_alias_canonicalization",
        )
    if canonicalized in standby:
        return LifecycleControlClassification(
            cleaned_input=cleaned,
            canonicalized_input=canonicalized,
            action=LIFECYCLE_ACTION_STANDBY,
            matched_phrase=canonicalized,
            routing_reason="exact_standby_phrase_after_alias_canonicalization",
        )
    return LifecycleControlClassification(
        cleaned_input=cleaned,
        canonicalized_input=canonicalized,
    )
