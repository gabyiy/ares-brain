from __future__ import annotations

import pytest

from core import (
    LIFECYCLE_ACTION_ATTENTION_ONLY,
    LIFECYCLE_ACTION_NONE,
    LIFECYCLE_ACTION_SHUTDOWN,
    LIFECYCLE_ACTION_STANDBY,
    normalize_active_lifecycle_command,
)


@pytest.mark.parametrize(
    ("transcript", "alias"),
    [
        ("Ares", "ares"),
        ("Aris", "aris"),
        ("Aries", "aries"),
        ("RS", "rs"),
        ("R S", "r s"),
        ("Hey Ares", "ares"),
        ("Hello Aris", "aris"),
        ("Hi RS", "rs"),
        ("Wake up Ares", "ares"),
        ("Wake up RS", "rs"),
    ],
)
def test_active_wake_or_name_only_input_is_attention_only(transcript, alias):
    result = normalize_active_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_ATTENTION_ONLY
    assert result.normalized_transcript == ""
    assert result.canonicalized_transcript == ""
    assert result.canonical_name == "ares"
    assert result.assistant_alias_removed == alias
    assert result.alias_position == "standalone"
    assert result.rejection_reason == ""


@pytest.mark.parametrize(
    ("transcript", "normalized", "alias", "position"),
    [
        ("Goodbye", "goodbye", "", "none"),
        ("Goodbye, Ares.", "goodbye", "ares", "suffix"),
        ("Goodbye Aris", "goodbye", "aris", "suffix"),
        ("Good bye Ares", "goodbye", "ares", "suffix"),
        ("Bye Ares", "bye", "ares", "suffix"),
        ("Ares goodbye", "goodbye", "ares", "prefix"),
        ("Standby", "standby", "", "none"),
        ("Stand by Aris", "standby", "aris", "suffix"),
        ("Ares go to standby", "go to standby", "ares", "prefix"),
        ("Go to sleep Ares", "go to sleep", "ares", "suffix"),
        ("Sleep RS", "sleep", "rs", "suffix"),
    ],
)
def test_active_standby_variants_match_command_body(
    transcript,
    normalized,
    alias,
    position,
):
    result = normalize_active_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_STANDBY
    assert result.normalized_transcript == normalized
    assert result.canonicalized_transcript == normalized
    assert result.assistant_alias_removed == alias
    assert result.alias_position == position
    assert result.matched_phrase == normalized


@pytest.mark.parametrize(
    ("transcript", "normalized", "alias", "position"),
    [
        ("Shutdown", "shutdown", "", "none"),
        ("Shutdown Ares", "shutdown", "ares", "suffix"),
        ("Shut down Aris", "shutdown", "aris", "suffix"),
        ("Ares shut down", "shutdown", "ares", "prefix"),
        ("Shutdown RS", "shutdown", "rs", "suffix"),
        ("Turn off Ares", "turn off", "ares", "suffix"),
        ("Ares power down", "power down", "ares", "prefix"),
    ],
)
def test_active_shutdown_variants_match_command_body(
    transcript,
    normalized,
    alias,
    position,
):
    result = normalize_active_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_SHUTDOWN
    assert result.normalized_transcript == normalized
    assert result.canonicalized_transcript == normalized
    assert result.assistant_alias_removed == alias
    assert result.alias_position == position
    assert result.matched_phrase == normalized


def test_active_lifecycle_diagnostics_preserve_cleaned_text_and_removed_alias():
    result = normalize_active_lifecycle_command("  Goodbye,   Aris. ")

    assert result.raw_transcript == "  Goodbye,   Aris. "
    assert result.cleaned_transcript == "goodbye aris"
    assert result.assistant_alias_removed == "aris"
    assert result.alias_removed == "aris"
    assert result.alias_position == "suffix"
    assert result.normalized_transcript == "goodbye"
    assert result.routed_transcript == "Goodbye"
    assert result.negation_detected is False
    assert result.action == LIFECYCLE_ACTION_STANDBY


@pytest.mark.parametrize(
    "transcript",
    [
        "Do not shut down Ares",
        "Don't shutdown",
        "Do not go to sleep",
        "Don't say goodbye",
        "Never standby Ares",
        "Ares should not shut down",
    ],
)
def test_active_negation_preempts_lifecycle_execution(transcript):
    result = normalize_active_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.negation_detected is True
    assert result.rejection_reason == "negated_lifecycle_command"


@pytest.mark.parametrize(
    "transcript",
    [
        "Why did you shut down",
        "Explain shutdown",
        "Schedule a shutdown tomorrow",
        "I said goodbye yesterday",
        "Ares goodbye message",
        "Paris shutdown",
        "Harris goodbye",
    ],
)
def test_active_lifecycle_words_inside_longer_input_do_not_match(transcript):
    result = normalize_active_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.negation_detected is False


@pytest.mark.parametrize(
    ("transcript", "normalized", "alias", "position"),
    [
        ("Ares calculate two plus two", "calculate two plus two", "ares", "prefix"),
        ("Calculate two plus two Aris", "calculate two plus two", "aris", "suffix"),
        ("RS remember my color is blue", "remember my color is blue", "rs", "prefix"),
    ],
)
def test_active_addressing_is_removed_for_ordinary_routing(
    transcript,
    normalized,
    alias,
    position,
):
    result = normalize_active_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.normalized_transcript == normalized
    assert result.canonicalized_transcript == normalized
    assert result.routed_transcript.casefold() == normalized
    assert result.assistant_alias_removed == alias
    assert result.alias_position == position


@pytest.mark.parametrize(
    "transcript",
    [
        "What does RS mean",
        "Tell me about Aries horoscope",
        "Compare Paris and Harris",
        "calculate areas plus two",
    ],
)
def test_aliases_are_never_replaced_inside_ordinary_input(transcript):
    result = normalize_active_lifecycle_command(transcript)

    expected = transcript.casefold()
    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.normalized_transcript == expected
    assert result.assistant_alias_removed == ""
    assert result.alias_position == "none"


def test_two_edge_aliases_are_ambiguous_and_cannot_manufacture_shutdown():
    result = normalize_active_lifecycle_command("Ares shutdown Aris")

    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.normalized_transcript == "ares shutdown aris"
    assert result.assistant_alias_removed == ""


@pytest.mark.parametrize(
    ("transcript", "routed"),
    [
        ("Ares, calculate 2 + 2.", "calculate 2 + 2"),
        ("Calculate (2 + 2) * 3, Aris.", "Calculate (2 + 2) * 3"),
        ("Ares don't overwrite my note.", "don't overwrite my note"),
        ("calculate 2 + 2", "calculate 2 + 2"),
    ],
)
def test_active_routed_transcript_preserves_non_address_command_content(
    transcript,
    routed,
):
    result = normalize_active_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.routed_transcript == routed


def test_active_commands_are_derived_from_configured_name_bearing_phrases():
    result = normalize_active_lifecycle_command(
        "Ares rest now",
        standby_phrases=("rest now ares",),
        shutdown_phrases=("power off ares",),
    )

    assert result.action == LIFECYCLE_ACTION_STANDBY
    assert result.normalized_transcript == "rest now"
    assert result.alias_position == "prefix"


def test_active_config_rejects_internal_name_slot():
    with pytest.raises(ValueError, match="beginning or end"):
        normalize_active_lifecycle_command(
            "goodbye",
            standby_phrases=("ask ares to rest",),
        )


@pytest.mark.parametrize("transcript", ["", "   ", "...", "\t?! "])
def test_active_empty_transcript_is_nonterminal(transcript):
    result = normalize_active_lifecycle_command(transcript)

    assert result.action == LIFECYCLE_ACTION_NONE
    assert result.normalized_transcript == ""
    assert result.rejection_reason == "empty_transcript"
