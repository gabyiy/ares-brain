from __future__ import annotations

import pytest

from core import (
    LIFECYCLE_ACTION_ACTIVATE,
    LIFECYCLE_ACTION_NONE,
    LIFECYCLE_ACTION_SHUTDOWN,
    LIFECYCLE_ACTION_STANDBY,
    LIFECYCLE_ALIAS_TYPE_ACOUSTIC,
    LIFECYCLE_ALIAS_TYPE_CANONICAL,
    LIFECYCLE_ALIAS_TYPE_PRONUNCIATION,
    LifecycleCommandResult,
    canonicalize_ares_name_tokens,
    normalize_lifecycle_command,
)


@pytest.mark.parametrize(
    ("name", "alias_type"),
    [
        ("Ares", LIFECYCLE_ALIAS_TYPE_CANONICAL),
        ("Aris", LIFECYCLE_ALIAS_TYPE_PRONUNCIATION),
        ("Aries", LIFECYCLE_ALIAS_TYPE_PRONUNCIATION),
    ],
)
def test_exact_supported_name_alias_activates(name, alias_type):
    result = normalize_lifecycle_command(f"  {name}.  ")

    assert result.action == LIFECYCLE_ACTION_ACTIVATE
    assert result.canonicalized_transcript == "ares"
    assert result.canonical_name == "ares"
    assert result.matched_alias == name.casefold()
    assert result.alias_type == alias_type
    assert result.matched_phrase == "ares"
    assert result.rejection_reason == ""


@pytest.mark.parametrize(
    "transcript",
    [
        "Goodbye Ares",
        "Goodbye Aris",
        "Goodbye Aries",
        "Goodbye RS",
        "Ares goodbye",
        "Aris goodbye",
        "Aries goodbye",
        "RS goodbye",
        "Standby Ares",
        "Standby Aris",
        "Standby Aries",
        "Standby RS",
        "Ares standby",
        "Aris standby",
        "Aries standby",
        "RS standby",
        "Go to standby Ares",
        "Go to standby Aris",
        "Go to standby Aries",
        "Go to standby RS",
        "Good bye Ares",
        "Go to sleep Ares",
        "Sleep Ares",
    ],
)
def test_exact_supported_standby_phrases(transcript):
    result = normalize_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_STANDBY
    assert result.canonical_name == "ares"
    assert result.matched_phrase in {
        "goodbye ares",
        "ares goodbye",
        "standby ares",
        "ares standby",
        "go to standby ares",
        "go to sleep ares",
        "sleep ares",
    }


@pytest.mark.parametrize(
    ("transcript", "normalized", "canonicalized", "matched_alias", "alias_type"),
    [
        ("Shut down RS", "shutdown rs", "shutdown ares", "rs", LIFECYCLE_ALIAS_TYPE_ACOUSTIC),
        ("Shutdown RS", "shutdown rs", "shutdown ares", "rs", LIFECYCLE_ALIAS_TYPE_ACOUSTIC),
        ("RS shut down", "rs shutdown", "ares shutdown", "rs", LIFECYCLE_ALIAS_TYPE_ACOUSTIC),
        ("RS shutdown", "rs shutdown", "ares shutdown", "rs", LIFECYCLE_ALIAS_TYPE_ACOUSTIC),
        ("Shut down R S", "shutdown r s", "shutdown ares", "r s", LIFECYCLE_ALIAS_TYPE_ACOUSTIC),
        ("R S shut down", "r s shutdown", "ares shutdown", "r s", LIFECYCLE_ALIAS_TYPE_ACOUSTIC),
        ("Shut down Are S", "shutdown are s", "shutdown ares", "are s", LIFECYCLE_ALIAS_TYPE_ACOUSTIC),
        ("Are S shut down", "are s shutdown", "ares shutdown", "are s", LIFECYCLE_ALIAS_TYPE_ACOUSTIC),
        ("Ares shut down", "ares shutdown", "ares shutdown", "ares", LIFECYCLE_ALIAS_TYPE_CANONICAL),
        ("Ares shutdown", "ares shutdown", "ares shutdown", "ares", LIFECYCLE_ALIAS_TYPE_CANONICAL),
        ("Shut down Ares", "shutdown ares", "shutdown ares", "ares", LIFECYCLE_ALIAS_TYPE_CANONICAL),
        ("Shutdown Ares", "shutdown ares", "shutdown ares", "ares", LIFECYCLE_ALIAS_TYPE_CANONICAL),
        ("Aris shut down", "aris shutdown", "ares shutdown", "aris", LIFECYCLE_ALIAS_TYPE_PRONUNCIATION),
        ("Aries shut down", "aries shutdown", "ares shutdown", "aries", LIFECYCLE_ALIAS_TYPE_PRONUNCIATION),
    ],
)
def test_exact_supported_shutdown_phrases(
    transcript,
    normalized,
    canonicalized,
    matched_alias,
    alias_type,
):
    result = normalize_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_SHUTDOWN
    assert result.normalized_transcript == normalized
    assert result.canonicalized_transcript == canonicalized
    assert result.canonical_name == "ares"
    assert result.matched_alias == matched_alias
    assert result.alias_type == alias_type
    assert result.matched_phrase == canonicalized
    assert result.rejection_reason == ""


@pytest.mark.parametrize(
    ("transcript", "action", "canonicalized"),
    [
        (" Ares,   shut down. ", LIFECYCLE_ACTION_SHUTDOWN, "ares shutdown"),
        (" Shut down,   Ares. ", LIFECYCLE_ACTION_SHUTDOWN, "shutdown ares"),
        (" RS,   shut down. ", LIFECYCLE_ACTION_SHUTDOWN, "ares shutdown"),
        (" Shut down   RS. ", LIFECYCLE_ACTION_SHUTDOWN, "shutdown ares"),
        (" aReS   ShUtDoWn. ", LIFECYCLE_ACTION_SHUTDOWN, "ares shutdown"),
        (" GOOD   BYE,   rs! ", LIFECYCLE_ACTION_STANDBY, "goodbye ares"),
    ],
)
def test_case_punctuation_and_repeated_whitespace_are_bounded(
    transcript,
    action,
    canonicalized,
):
    result = normalize_lifecycle_command(transcript)

    assert result.action == action
    assert result.canonicalized_transcript == canonicalized


def test_result_preserves_raw_cleaned_normalized_and_canonical_diagnostics():
    raw = "  Shut down,   RS.  "

    result = normalize_lifecycle_command(raw)

    assert isinstance(result, LifecycleCommandResult)
    assert result.raw_transcript == raw
    assert result.cleaned_transcript == "Shut down, RS"
    assert result.normalized_transcript == "shutdown rs"
    assert result.canonicalized_transcript == "shutdown ares"
    assert result.matched_alias == "rs"
    assert result.alias_type == LIFECYCLE_ALIAS_TYPE_ACOUSTIC
    assert result.canonical_name == "ares"
    assert result.negation_detected is False
    assert result.action == LIFECYCLE_ACTION_SHUTDOWN
    assert result.matched_phrase == "shutdown ares"
    assert result.rejection_reason == ""


@pytest.mark.parametrize(
    "transcript",
    [
        "What does RS mean?",
        "Tell me about shutdown procedures.",
        "I said goodbye to Ares yesterday.",
        "Where is Ares?",
        "Aries horoscope.",
        "Paris shutdown.",
        "Harris shut down.",
        "Shut down the computer.",
        "Goodbye.",
        "Shutdown.",
        "RS.",
        "Ares goodbye message",
        "Ares calculate two plus two",
        "why did Ares shut down",
        "tell me about standby",
        "stop Ares completely",
    ],
)
def test_longer_unrelated_or_incomplete_phrases_are_not_lifecycle_commands(
    transcript,
):
    result = normalize_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.canonical_name == ""
    assert result.matched_alias == ""
    assert result.matched_phrase == ""
    assert result.canonicalized_transcript == result.normalized_transcript
    assert result.rejection_reason == "exact_lifecycle_phrase_not_matched"


@pytest.mark.parametrize(
    "transcript",
    [
        "Do not shut down Ares.",
        "Don't shutdown Ares.",
        "Dont shutdown Ares.",
        "Never shutdown Ares.",
        "Ares should not shut down.",
        "Ares shouldn't shut down.",
        "Do not say goodbye Ares.",
        "Never standby Ares.",
    ],
)
def test_negation_takes_priority_and_never_triggers_lifecycle(transcript):
    result = normalize_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.negation_detected is True
    assert result.canonical_name == ""
    assert result.matched_phrase == ""
    assert result.rejection_reason == "negated_lifecycle_command"


def test_unmatched_rs_and_aries_text_is_not_rewritten_for_ordinary_routing():
    rs = normalize_lifecycle_command("What does RS mean?")
    zodiac = normalize_lifecycle_command("Aries horoscope")

    assert rs.normalized_transcript == "what does rs mean"
    assert rs.canonicalized_transcript == "what does rs mean"
    assert zodiac.normalized_transcript == "aries horoscope"
    assert zodiac.canonicalized_transcript == "aries horoscope"
    assert canonicalize_ares_name_tokens("ordinary rs text") == "ordinary rs text"


@pytest.mark.parametrize("transcript", ["", "   ", "...", "\t?! "])
def test_empty_transcript_has_nonterminal_rejection(transcript):
    result = normalize_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.normalized_transcript == ""
    assert result.canonicalized_transcript == ""
    assert result.rejection_reason == "empty_transcript"


def test_configured_control_phrase_must_have_an_exact_supported_name_alias():
    with pytest.raises(ValueError, match="exact ARES name alias"):
        normalize_lifecycle_command(
            "shutdown ares",
            shutdown_phrases=("shutdown",),
        )
