from __future__ import annotations

import pytest

from core import (
    LIFECYCLE_ACTION_ACTIVATE,
    LIFECYCLE_ACTION_NONE,
    LIFECYCLE_ACTION_SHUTDOWN,
    LIFECYCLE_ACTION_STANDBY,
    LifecycleCommandResult,
    normalize_lifecycle_command,
)


@pytest.mark.parametrize("name", ["Ares", "Aris", "Aries"])
def test_exact_supported_name_alias_activates(name):
    result = normalize_lifecycle_command(f"  {name}.  ")

    assert result.action == LIFECYCLE_ACTION_ACTIVATE
    assert result.normalized_transcript == "ares"
    assert result.canonical_name == "ares"
    assert result.matched_phrase == "ares"
    assert result.rejection_reason == ""


@pytest.mark.parametrize(
    "transcript",
    [
        "Goodbye, Ares.",
        "Goodbye, Aris.",
        "Goodbye, Aries.",
        "Good bye Ares.",
        "Standby Ares.",
        "Go to standby, Ares.",
    ],
)
def test_exact_supported_standby_phrases(transcript):
    result = normalize_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_STANDBY
    assert result.canonical_name == "ares"
    assert result.matched_phrase in {
        "goodbye ares",
        "standby ares",
        "go to standby ares",
    }


@pytest.mark.parametrize(
    "transcript",
    [
        "Shutdown Ares.",
        "Shut down Ares.",
        "Shutdown Aris.",
        "Shut down Aris.",
        "Shutdown Aries.",
        "Shut down Aries.",
    ],
)
def test_exact_supported_shutdown_phrases(transcript):
    result = normalize_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_SHUTDOWN
    assert result.normalized_transcript == "shutdown ares"
    assert result.canonical_name == "ares"
    assert result.matched_phrase == "shutdown ares"


def test_result_preserves_raw_cleaned_and_canonical_diagnostics():
    raw = "  Goodbye,   Aris.  "

    result = normalize_lifecycle_command(raw)

    assert isinstance(result, LifecycleCommandResult)
    assert result.raw_transcript == raw
    assert result.cleaned_transcript == "Goodbye, Aris"
    assert result.normalized_transcript == "goodbye ares"
    assert result.canonical_name == "ares"
    assert result.action == "standby"
    assert result.matched_phrase == "goodbye ares"
    assert result.rejection_reason == ""


def test_case_punctuation_and_repeated_whitespace_are_normalized_deterministically():
    standby = normalize_lifecycle_command(" \tGOOD   BYE,\n ARIES!!! ")
    shutdown = normalize_lifecycle_command(" \tShUt   DoWn,\n ArIs?! ")

    assert standby.normalized_transcript == "goodbye ares"
    assert standby.action == LIFECYCLE_ACTION_STANDBY
    assert shutdown.normalized_transcript == "shutdown ares"
    assert shutdown.action == LIFECYCLE_ACTION_SHUTDOWN


@pytest.mark.parametrize(
    "transcript",
    [
        "I said goodbye to Ares yesterday",
        "tell me about shutdown procedures",
        "where is Ares",
        "Aries horoscope",
        "goodbye",
        "shutdown",
        "shut down the computer",
        "Ares goodbye message",
        "do not shut down Ares",
        "stop Ares completely",
        "Paris",
        "Harris",
    ],
)
def test_longer_unrelated_or_unsupported_phrases_are_not_lifecycle_commands(transcript):
    result = normalize_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.canonical_name == ""
    assert result.matched_phrase == ""
    assert result.rejection_reason == "exact_lifecycle_phrase_not_matched"


def test_unmatched_aries_text_is_not_rewritten_for_ordinary_routing():
    result = normalize_lifecycle_command("Aries horoscope")

    assert result.normalized_transcript == "aries horoscope"
    assert result.action == LIFECYCLE_ACTION_NONE


@pytest.mark.parametrize("transcript", ["", "   ", "...", "\t?! "])
def test_empty_transcript_has_nonterminal_rejection(transcript):
    result = normalize_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.normalized_transcript == ""
    assert result.rejection_reason == "empty_transcript"


def test_configured_control_phrase_must_have_an_exact_supported_name_alias():
    with pytest.raises(ValueError, match="exact ARES name alias"):
        normalize_lifecycle_command(
            "shutdown ares",
            shutdown_phrases=("shutdown",),
        )
