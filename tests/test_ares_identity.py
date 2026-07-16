from __future__ import annotations

import pytest

from core import (
    AresNamePolicy,
    LIFECYCLE_ACTION_NONE,
    LIFECYCLE_ACTION_SHUTDOWN,
    LIFECYCLE_ACTION_STANDBY,
    canonicalize_ares_name_tokens,
    classify_lifecycle_control,
)


STANDBY = (
    "goodbye ares",
    "go to standby ares",
    "standby ares",
)
SHUTDOWN = ("shutdown ares", "shut down ares")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ares", "ares"),
        ("aris", "ares"),
        ("goodbye aris", "goodbye ares"),
        ("shutdown aris", "shutdown ares"),
        ("paris", "paris"),
        ("harris", "harris"),
        ("aries", "ares"),
    ],
)
def test_shared_name_policy_canonicalizes_complete_alias_tokens_only(text, expected):
    assert canonicalize_ares_name_tokens(text) == expected


def test_name_policy_is_immutable_normalized_and_requires_canonical_alias():
    policy = AresNamePolicy(("Aris!", "ARES", "aris"))
    assert policy.aliases == ("ares", "aris")
    with pytest.raises(ValueError, match="include the canonical alias"):
        AresNamePolicy(("aris",))


@pytest.mark.parametrize(
    "text",
    [
        "goodbye ares",
        "goodbye aris",
        "go to standby ares",
        "go to standby aris",
        "standby ares",
        "standby aris",
        "goodbye aries",
        "go to standby aries",
        "standby aries",
    ],
)
def test_lifecycle_parser_accepts_exact_standby_alias_forms(text):
    result = classify_lifecycle_control(
        text,
        standby_phrases=STANDBY,
        shutdown_phrases=SHUTDOWN,
    )
    assert result.action == LIFECYCLE_ACTION_STANDBY
    assert result.matched_phrase.endswith("ares")


@pytest.mark.parametrize(
    "text",
    [
        "shutdown ares",
        "shutdown aris",
        "shutdown aries",
        "shut down ares",
        "shut down aris",
        "shut down aries",
    ],
)
def test_lifecycle_parser_accepts_exact_shutdown_alias_forms(text):
    result = classify_lifecycle_control(
        text,
        standby_phrases=STANDBY,
        shutdown_phrases=SHUTDOWN,
    )
    assert result.action == LIFECYCLE_ACTION_SHUTDOWN
    assert result.matched_phrase.endswith("ares")


@pytest.mark.parametrize(
    "text",
    [
        "goodbye",
        "I said goodbye to Ares yesterday",
        "do not shutdown Ares",
        "explain standby",
        "calculate two plus two Ares",
        "Paris",
        "Harris",
        "Aries",
        "unknown goodbye Ares",
        "goodbye Ares now",
    ],
)
def test_lifecycle_parser_rejects_partial_and_sentence_matches(text):
    result = classify_lifecycle_control(
        text,
        standby_phrases=STANDBY,
        shutdown_phrases=SHUTDOWN,
    )
    assert result.action == LIFECYCLE_ACTION_NONE
    assert not result.matched
