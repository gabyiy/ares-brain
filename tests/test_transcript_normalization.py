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


def test_raw_cleaned_and_normalized_transcripts_are_preserved():
    result = normalize_transcript("  What is two plus two?!  ", correlation_id="corr-1")

    assert result.raw_transcript == "  What is two plus two?!  "
    assert result.cleaned_transcript == "What is two plus two"
    assert result.normalized_command == "calculate 2 + 2"
    assert result.correlation_id == "corr-1"


def test_general_unknown_request_remains_general_unknown_text():
    result = normalize_transcript("Please describe the purple horizon.")

    assert result.success is True
    assert result.arithmetic_candidate is False
    assert result.normalized_command == "please describe the purple horizon"


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
    assert result.cleanup_rule == "adjacent_phrase_loop_v1"


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
