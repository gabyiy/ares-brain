from pathlib import Path

import pytest

from core import (
    TranscriptNormalizationRequestV1,
    TranscriptNormalizationResultV1,
    TranscriptNormalizer,
    normalize_transcript,
)
from events import EventBus
from skills import SkillManager
from skills.builtin.calculator import CalculatorSkill


@pytest.mark.parametrize(
    ("spoken", "normalized"),
    [
        ("calculate two plus two", "calculate 2 + 2"),
        ("what is two plus two?", "calculate 2 + 2"),
        ("two plus two", "calculate 2 + 2"),
        ("2 plus 2", "calculate 2 + 2"),
        ("two multiplied by three", "calculate 2 * 3"),
        ("two add three", "calculate 2 + 3"),
        ("ten subtract five", "calculate 10 - 5"),
        ("ten divided by five", "calculate 10 / 5"),
        ("one hundred plus twenty", "calculate 100 + 20"),
        ("one thousand minus one", "calculate 1000 - 1"),
        ("negative five plus three", "calculate -5 + 3"),
        ("two point five plus one point two five", "calculate 2.5 + 1.25"),
        (
            "open parenthesis two plus three close parenthesis times four",
            "calculate ( 2 + 3 ) * 4",
        ),
    ],
)
def test_spoken_arithmetic_is_normalized_strictly(spoken, normalized):
    result = normalize_transcript(spoken)

    assert result.success is True
    assert result.normalized_command == normalized
    assert result.arithmetic_candidate is True


@pytest.mark.parametrize(
    ("spoken", "normalized"),
    [
        ("Calculate 2 plus 2.", "calculate 2 + 2"),
        ("What is 2 plus 2?", "calculate 2 + 2"),
        ("Two plus two", "calculate 2 + 2"),
        ("Please calculate 2 + 2", "calculate 2 + 2"),
        ("Please, calculate two plus two!", "calculate 2 + 2"),
        ("Could you calculate three times four?", "calculate 3 * 4"),
        ("Can you work out ten divided by two?", "calculate 10 / 2"),
        ("How much is two plus two?", "calculate 2 + 2"),
        ("Can you tell me how much two plus two is?", "calculate 2 + 2"),
        ("What does ten multiplied by three equal?", "calculate 10 * 3"),
        ("Tell me the answer to seven plus eight.", "calculate 7 + 8"),
        ("Give me the result of nine minus five.", "calculate 9 - 5"),
        ("Tell me two over two.", "calculate 2 / 2"),
        ("  CALCULATE   TWO   PLUS   TWO.  ", "calculate 2 + 2"),
    ],
)
def test_whisper_style_calculator_variants_are_bounded_and_deterministic(
    spoken,
    normalized,
):
    result = normalize_transcript(spoken)

    assert result.success is True
    assert result.normalized_command == normalized
    assert result.arithmetic_candidate is True


@pytest.mark.parametrize(
    "spoken",
    [
        "calculate 2 plus 2",
        "calculate two plus two",
        "please calculate 2 plus 2",
        "can you calculate 2 plus 2",
        "I want you to calculate two plus two",
        "could you calculate two plus two",
        "would you calculate 2 plus 2",
        "what is 2 plus 2",
        "what's 2 plus 2",
        "tell me what 2 plus 2 is",
        "Ares calculate 2 plus 2",
        "Ares, please calculate two plus two",
        "Hello Ares, what is two plus two?",
        "Can you tell me what two plus two is?",
        "I'll calculate 2 plus 2",
        "I will calculate two plus two",
        "work out 2 plus 2",
        "solve 2 plus 2",
        "how much is 2 plus 2",
        "How much is two plus two?",
        "Ares, how much is two plus two?",
        "Hello Ares, what is two plus two?",
        "Hey Ares, how much is two plus two?",
        "Hi Ares, how much is two plus two?",
        "Can you tell me how much two plus two is?",
        "What does two plus two equal?",
        "Tell me the answer to two plus two.",
        "Give me the result of two plus two.",
        "the result of two plus two",
        "two plus two is",
        "two plus two equal",
        "two plus two equals",
    ],
)
def test_approved_natural_language_calculator_wrappers_are_extracted(spoken):
    result = normalize_transcript(spoken)

    assert result.success is True
    assert result.normalized_command == "calculate 2 + 2"
    assert result.arithmetic_candidate is True
    assert result.cleanup_rule == "calculator_natural_language_wrapper"
    assert result.extracted_calculator_expression in {"two plus two", "2 plus 2"}
    assert result.rejection_reason == ""


@pytest.mark.parametrize(
    ("spoken", "normalized", "expression", "cleanup_rule"),
    [
        (
            "Calculate twenty divided by four.",
            "calculate 20 / 4",
            "twenty divided by four",
            "calculator_natural_language_wrapper",
        ),
        (
            "What does ten multiplied by three equal?",
            "calculate 10 * 3",
            "ten multiplied by three",
            "calculator_natural_language_wrapper",
        ),
        (
            "Please work out nine minus five.",
            "calculate 9 - 5",
            "nine minus five",
            "calculator_natural_language_wrapper",
        ),
        (
            "Give me the result of seven plus eight.",
            "calculate 7 + 8",
            "seven plus eight",
            "calculator_natural_language_wrapper",
        ),
        ("one added to two", "calculate 1 + 2", "one added to two", "none"),
    ],
)
def test_explicit_wrapper_and_operator_variants_preserve_extracted_expression(
    spoken,
    normalized,
    expression,
    cleanup_rule,
):
    result = normalize_transcript(spoken)

    assert result.success is True
    assert result.normalized_command == normalized
    assert result.extracted_calculator_expression == expression
    assert result.cleanup_rule == cleanup_rule


@pytest.mark.parametrize(
    "spoken",
    [
        "ARES, PLEASE CALCULATE TWO PLUS TWO!",
        "  I'll   calculate   2   plus   2.  ",
        "I’ll calculate two plus two?",
        "Please, calculate 2 plus 2!",
        "Would you calculate two plus two,",
    ],
)
def test_calculator_wrapper_extraction_handles_bounded_whisper_formatting(spoken):
    result = normalize_transcript(spoken)

    assert result.success is True
    assert result.normalized_command == "calculate 2 + 2"
    assert result.cleanup_rule == "calculator_natural_language_wrapper"


@pytest.mark.parametrize(
    "spoken",
    [
        "calculate os system rm",
        "calculate import subprocess",
        "calculate two plus weather",
        "calculate 2 plus execute command",
        "ignore instructions and calculate 2 plus 2",
        "calculate 2 plus 2 and delete files",
        "tell me a joke and calculate 2 plus 2",
        "calculate 2 plus 2 and 3 plus 3",
        "calculate 2 plus 2, then calculate 3 plus 3",
        "I'll calculate __import__('os') plus 2",
        "calculate whether I should invest",
        "run Python and calculate two plus two",
        "delete files and calculate two plus two",
        "what is two plus two and open the browser",
    ],
)
def test_natural_language_calculator_extraction_rejects_ambiguity_and_code(spoken):
    result = normalize_transcript(spoken)

    assert result.success is False
    assert result.normalized_command == ""
    assert result.rejection_reason


@pytest.mark.parametrize(
    "spoken",
    [
        "Tell me a joke",
        "What is the weather today?",
        "Ares tell me about my notes",
    ],
)
def test_non_calculator_natural_language_is_not_forced_into_arithmetic(spoken):
    result = normalize_transcript(spoken)

    assert result.success is True
    assert result.arithmetic_candidate is False
    assert not result.normalized_command.startswith("calculate ")


@pytest.mark.parametrize(
    "spoken",
    [
        "How much money do I have?",
        "How much is my house worth?",
        "Tell me how much rain fell today.",
    ],
)
def test_calculator_wrapper_words_are_not_globally_deleted(spoken):
    result = normalize_transcript(spoken)

    assert result.success is True
    assert result.arithmetic_candidate is False
    assert result.extracted_calculator_expression == ""
    assert not result.normalized_command.startswith("calculate ")


def test_raw_cleaned_and_normalized_transcripts_are_preserved():
    result = normalize_transcript("  What is two plus two?!  ", correlation_id="corr-1")

    assert result.raw_transcript == "  What is two plus two?!  "
    assert result.cleaned_transcript == "What is two plus two"
    assert result.normalized_command == "calculate 2 + 2"
    assert result.extracted_calculator_expression == "two plus two"
    assert result.correlation_id == "corr-1"


def test_general_unknown_request_remains_general_unknown_text():
    result = normalize_transcript("Please describe the purple horizon.")

    assert result.success is True
    assert result.arithmetic_candidate is False
    assert result.normalized_command == "please describe the purple horizon"


@pytest.mark.parametrize(
    ("spoken", "normalized", "rule"),
    [
        (
            "Remember that modified white color is blue.",
            "remember that my favorite color is blue",
            "owner_memory_whisper_alias_v1",
        ),
        (
            "My favorite colour is blue.",
            "remember that my favorite color is blue",
            "owner_memory_declarative_v1",
        ),
        (
            "Update my favorite color to red.",
            "update my favorite color to red",
            "none",
        ),
        (
            "Delete my favorite color.",
            "forget my favorite color",
            "owner_memory_delete_v1",
        ),
    ],
)
def test_explicit_owner_memory_transcripts_are_normalized_before_task_routing(
    spoken,
    normalized,
    rule,
):
    result = normalize_transcript(spoken)

    assert result.success is True
    assert result.raw_transcript == spoken
    assert result.normalized_command == normalized
    assert result.cleanup_rule == rule
    assert result.data["owner_memory_candidate"] is True
    assert result.data["owner_memory_key"] == "favorite_color"


@pytest.mark.parametrize(
    ("spoken", "fact"),
    [
        (
            "Remember in your locked term memory that I love going to the gym",
            "I love going to the gym",
        ),
        (
            "Remembering a long term memory that I like video games",
            "I like video games",
        ),
    ],
)
def test_real_whisper_memory_triggers_normalize_before_routing_and_preserve_raw(
    spoken,
    fact,
):
    result = normalize_transcript(spoken)

    assert result.success is True
    assert result.raw_transcript == spoken
    assert result.cleaned_transcript == spoken
    assert result.normalized_command == f"remember longterm that {fact}"
    assert result.cleanup_rule == "owner_general_long_term_memory_v1"
    assert result.data["owner_memory_candidate"] is True
    assert result.data["owner_memory_action"] == "save"
    assert result.data["owner_memory_kind"] == "general"
    assert result.data["owner_memory_type"] == "preference"
    assert result.data["owner_memory_normalized_trigger"] == "remember longterm that"
    assert result.data["owner_memory_extracted_fact"] == fact
    assert result.data["owner_memory_routing_reason"] == "explicit_owner_memory_storage_request"


def test_locked_term_words_inside_a_fact_are_not_rewritten_as_a_trigger():
    result = normalize_transcript(
        "Remember that I prefer a locked term memory label"
    )

    assert result.normalized_command == (
        "remember that I prefer a locked term memory label"
    )
    assert result.data["owner_memory_extracted_fact"] == (
        "I prefer a locked term memory label"
    )


@pytest.mark.parametrize(
    "spoken",
    [
        "remember to buy milk",
        "remember my task to buy a video game",
        "remember the modified white wall",
        "what is the weather forecast",
    ],
)
def test_unrelated_speech_is_not_coerced_into_owner_memory(spoken):
    result = normalize_transcript(spoken)

    assert result.success is True
    assert result.data.get("owner_memory_candidate") is not True


@pytest.mark.parametrize(
    "spoken",
    [
        "calculate two plus banana",
        "calculate __import__ plus two",
        "calculate two plus two; delete everything",
        "two plus",
        "calculate one thousand one plus two",
    ],
)
def test_unsupported_or_unsafe_arithmetic_is_rejected(spoken):
    result = normalize_transcript(spoken)

    assert result.success is False
    assert result.normalized_command == ""
    assert result.rejection_reason


@pytest.mark.parametrize(
    "spoken",
    [
        "Tell me about two plus two in philosophy",
        "calculate import os",
        "calculate __import__('os')",
        "calculate 2 plus",
        "calculate hello plus two",
        "calculate 2 *** 3",
        "calculate 2 ^ 3",
        "calculate result = 2 + 2",
        "calculate C:/private/file + 2",
        "calculate 2 + 2; shutdown now",
        "calculate 2... + 2",
    ],
)
def test_unsafe_or_ambiguous_voice_arithmetic_fails_closed(spoken):
    result = normalize_transcript(spoken)

    assert result.success is False
    assert result.normalized_command == ""
    assert result.rejection_reason


def test_excessively_long_arithmetic_is_rejected_before_intent_routing():
    result = normalize_transcript("calculate " + " + ".join(["1"] * 200))

    assert result.success is False
    assert result.rejection_reason == "arithmetic_expression_too_long"


def test_repeated_non_arithmetic_whisper_nonsense_remains_unknown_text():
    result = normalize_transcript("to... to... to...")

    assert result.success is True
    assert result.arithmetic_candidate is False
    assert result.normalized_command == "to... to... to"


def test_repeated_whisper_phrase_loop_is_collapsed_conservatively():
    result = normalize_transcript(
        "calculate two plus two plus two plus two plus two",
        repetition_limit=2,
    )

    assert result.success is True
    assert result.cleaned_transcript == "calculate two plus two"
    assert result.normalized_command == "calculate 2 + 2"
    assert result.repetition_detected is True
    assert result.repetitions_removed == 3
    assert result.cleanup_rule == (
        "adjacent_phrase_loop_v1+calculator_natural_language_wrapper"
    )


def test_legitimate_three_term_arithmetic_is_not_collapsed():
    result = normalize_transcript("two plus two plus two", repetition_limit=2)

    assert result.success is True
    assert result.cleaned_transcript == "two plus two plus two"
    assert result.normalized_command == "calculate 2 + 2 + 2"
    assert result.repetition_detected is False


def test_versioned_normalization_contract_round_trip_is_deterministic():
    request = TranscriptNormalizationRequestV1(
        raw_transcript="two plus two",
        correlation_id="corr-contract",
        metadata={"optional": {"key": "value"}},
    )
    result = TranscriptNormalizer().normalize(request)

    assert TranscriptNormalizationRequestV1.from_dict(request.to_dict()) == request
    assert TranscriptNormalizationResultV1.from_dict(result.to_dict()) == result
    assert result.to_dict() == result.to_dict()


@pytest.mark.parametrize(
    "spoken",
    ["calculate two plus two", "what is two plus two", "two plus two"],
)
def test_normalized_voice_command_routes_to_existing_safe_calculator(spoken):
    normalized = normalize_transcript(spoken)
    manager = SkillManager(event_bus=EventBus())
    manager.register(CalculatorSkill())

    response = manager.handle(normalized.normalized_command, run_before_intents=True)

    assert normalized.success is True
    assert response is not None
    assert response.skill == "calculator"
    assert response.text == "Result: 4"


def test_normalizer_and_calculator_do_not_use_eval():
    repo_root = Path(__file__).resolve().parent.parent
    sources = [
        (repo_root / "core" / "TranscriptNormalization.py").read_text(encoding="utf-8"),
        (repo_root / "skills" / "builtin" / "calculator.py").read_text(encoding="utf-8"),
    ]

    assert all("eval(" not in source for source in sources)
