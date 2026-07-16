from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from core.AresIdentity import (
    DEFAULT_ARES_NAME_ALIASES,
    match_ares_alias_phrase,
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
    standby = {
        match_ares_alias_phrase(value, (value,), ares_name_aliases)
        for value in standby_phrases
    }
    shutdown = {
        match_ares_alias_phrase(value, (value,), ares_name_aliases)
        for value in shutdown_phrases
    }
    standby.discard("")
    shutdown.discard("")
    if standby & shutdown:
        raise ValueError("standby and shutdown lifecycle phrases must not overlap")
    shutdown_match = match_ares_alias_phrase(cleaned, tuple(shutdown), ares_name_aliases)
    standby_match = match_ares_alias_phrase(cleaned, tuple(standby), ares_name_aliases)
    canonicalized = shutdown_match or standby_match or cleaned
    if shutdown_match:
        return LifecycleControlClassification(
            cleaned_input=cleaned,
            canonicalized_input=canonicalized,
            action=LIFECYCLE_ACTION_SHUTDOWN,
            matched_phrase=shutdown_match,
            routing_reason="exact_shutdown_phrase_after_alias_canonicalization",
        )
    if standby_match:
        return LifecycleControlClassification(
            cleaned_input=cleaned,
            canonicalized_input=canonicalized,
            action=LIFECYCLE_ACTION_STANDBY,
            matched_phrase=standby_match,
            routing_reason="exact_standby_phrase_after_alias_canonicalization",
        )
    return LifecycleControlClassification(
        cleaned_input=cleaned,
        canonicalized_input=canonicalized,
    )
