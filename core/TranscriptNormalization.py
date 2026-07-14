from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from core.Contracts import (
    CONTRACT_TRANSCRIPT_NORMALIZATION_REQUEST,
    TranscriptNormalizationRequestV1,
    TranscriptNormalizationResultV1,
    validate_contract,
)
from core.OwnerMemory import (
    owner_memory_uses_explicit_store,
    parse_owner_memory_command,
)


_UNITS = {
    "zero": 0,
    "oh": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
_TEENS = {
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_NUMBER_WORDS = frozenset((*_UNITS, *_TEENS, *_TENS, "hundred", "thousand", "and"))
_OPERATORS = {"plus": "+", "add": "+", "minus": "-", "subtract": "-", "times": "*"}
_SYMBOLS = frozenset({"+", "-", "*", "/", "(", ")"})
CALCULATOR_NATURAL_LANGUAGE_WRAPPER = "calculator_natural_language_wrapper"
_MULTIWORD_REPLACEMENTS = (
    (r"\badded\s+to\b", " + "),
    (r"\bmultiplied\s+by\b", " * "),
    (r"\bdivided\s+by\b", " / "),
    (r"\bopen\s+parenthes(?:is|es)\b", " ( "),
    (r"\bclose\s+parenthes(?:is|es)\b", " ) "),
    (r"\bdecimal\s+point\b", " point "),
)
_TOKEN_PATTERN = re.compile(r"\d+(?:\.\d+)?|[a-z]+|[()+*/-]")
_SAFE_ARITHMETIC_SOURCE = re.compile(r"^[a-z0-9\s()+*/.\-]+$")
MAX_TRANSCRIPT_LENGTH = 1024
MAX_ARITHMETIC_SOURCE_LENGTH = 256


@dataclass(frozen=True)
class _CalculatorWrapperRule:
    prefix: str
    explicit_calculator_request: bool = True
    allowed_trailing_suffixes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _CalculatorWrapperExtraction:
    source: str
    applied: bool = False
    explicit_calculator_request: bool = False


_CALCULATOR_WRAPPER_RULES = (
    _CalculatorWrapperRule(
        "can you tell me how much",
        explicit_calculator_request=False,
        allowed_trailing_suffixes=("is", "equal", "equals"),
    ),
    _CalculatorWrapperRule(
        "tell me how much",
        explicit_calculator_request=False,
        allowed_trailing_suffixes=("is", "equal", "equals"),
    ),
    _CalculatorWrapperRule(
        "how much does",
        explicit_calculator_request=False,
        allowed_trailing_suffixes=("equal", "equals"),
    ),
    _CalculatorWrapperRule(
        "what does",
        explicit_calculator_request=False,
        allowed_trailing_suffixes=("equal", "equals"),
    ),
    _CalculatorWrapperRule("tell me the answer to", explicit_calculator_request=False),
    _CalculatorWrapperRule("tell me the result of", explicit_calculator_request=False),
    _CalculatorWrapperRule("give me the result of", explicit_calculator_request=False),
    _CalculatorWrapperRule("the result of", explicit_calculator_request=False),
    _CalculatorWrapperRule("could you please calculate"),
    _CalculatorWrapperRule("would you please calculate"),
    _CalculatorWrapperRule("can you please calculate"),
    _CalculatorWrapperRule("could you calculate"),
    _CalculatorWrapperRule("would you calculate"),
    _CalculatorWrapperRule("can you calculate"),
    _CalculatorWrapperRule("i want you to calculate"),
    _CalculatorWrapperRule("i'll calculate"),
    _CalculatorWrapperRule("i will calculate"),
    _CalculatorWrapperRule("please calculate"),
    _CalculatorWrapperRule(
        "can you tell me what",
        explicit_calculator_request=False,
        allowed_trailing_suffixes=("is", "equal", "equals"),
    ),
    _CalculatorWrapperRule("tell me what is", explicit_calculator_request=False),
    _CalculatorWrapperRule(
        "tell me what",
        explicit_calculator_request=False,
        allowed_trailing_suffixes=("is", "equal", "equals"),
    ),
    _CalculatorWrapperRule("how much is", explicit_calculator_request=False),
    _CalculatorWrapperRule("what's", explicit_calculator_request=False),
    _CalculatorWrapperRule("what is", explicit_calculator_request=False),
    _CalculatorWrapperRule("could you work out"),
    _CalculatorWrapperRule("would you work out"),
    _CalculatorWrapperRule("can you work out"),
    _CalculatorWrapperRule("please work out"),
    _CalculatorWrapperRule("work out"),
    _CalculatorWrapperRule("solve"),
    _CalculatorWrapperRule("calculate"),
    _CalculatorWrapperRule("compute"),
    _CalculatorWrapperRule("tell me", explicit_calculator_request=False),
)
_POLITE_ACTION_WRAPPERS = frozenset({"calculate", "compute", "solve", "work out"})


class TranscriptNormalizer:
    """Deterministic STT cleanup and strict spoken-arithmetic normalization."""

    def normalize(
        self,
        request: TranscriptNormalizationRequestV1,
    ) -> TranscriptNormalizationResultV1:
        if not isinstance(request, TranscriptNormalizationRequestV1):
            return self._failure(None, "transcript_normalization_request_required")
        compatibility = validate_contract(
            request,
            expected_contract_name=CONTRACT_TRANSCRIPT_NORMALIZATION_REQUEST,
        )
        if not compatibility.success:
            return self._failure(request, compatibility.error_message or compatibility.status)
        if not 1 <= int(request.repetition_limit) <= 10:
            return self._failure(request, "repetition_limit_out_of_range")

        raw = str(request.raw_transcript or "")
        cleaned_base = _clean_text(raw)
        if len(cleaned_base) > MAX_TRANSCRIPT_LENGTH:
            return self._failure(request, "transcript_too_long")
        initial_owner_command = parse_owner_memory_command(cleaned_base)
        initial_owner_candidate = owner_memory_uses_explicit_store(
            initial_owner_command
        )
        initial_extraction = _extract_calculator_wrapper(cleaned_base.casefold())
        if (
            not initial_owner_candidate
            and _is_arithmetic_candidate(cleaned_base, initial_extraction)
            and len(cleaned_base) > MAX_ARITHMETIC_SOURCE_LENGTH
        ):
            return self._failure(
                request,
                "arithmetic_expression_too_long",
                cleaned_transcript=cleaned_base,
                arithmetic_candidate=True,
                cleanup_rule=(
                    CALCULATOR_NATURAL_LANGUAGE_WRAPPER
                    if initial_extraction.applied
                    else "none"
                ),
                extracted_calculator_expression=initial_extraction.source,
            )
        cleaned, detected, removed, cleanup_rule = _collapse_repetition_loops(
            cleaned_base,
            int(request.repetition_limit),
        )
        if not cleaned:
            return self._failure(
                request,
                "blank_transcript",
                cleaned_transcript="",
                repetition_detected=detected,
                repetitions_removed=removed,
                cleanup_rule=cleanup_rule,
            )

        owner_command = parse_owner_memory_command(cleaned)
        if owner_memory_uses_explicit_store(owner_command):
            normalized = owner_command.routing_text or cleaned.casefold()
            if normalized != cleaned.casefold():
                cleanup_rule = _merge_cleanup_rules(
                    cleanup_rule,
                    owner_command.parser_rule,
                )
            return TranscriptNormalizationResultV1(
                success=True,
                raw_transcript=raw,
                cleaned_transcript=cleaned,
                normalized_command=normalized,
                arithmetic_candidate=False,
                repetition_detected=detected,
                repetitions_removed=removed,
                cleanup_rule=cleanup_rule,
                correlation_id=request.correlation_id,
                session_id=request.session_id,
                data={
                    "safe": True,
                    "normalizer": "deterministic_v1",
                    "owner_memory_candidate": True,
                    "owner_memory_action": owner_command.action,
                    "owner_memory_key": owner_command.normalized_key,
                    "owner_memory_parser_rule": owner_command.parser_rule,
                    "owner_memory_kind": owner_command.memory_kind,
                    "owner_memory_type": str(owner_command.memory.get("memory_type") or ""),
                    "owner_memory_persistence": owner_command.persistence,
                    "owner_memory_explicit": owner_command.explicit,
                },
                metadata={**dict(request.metadata or {}), "safe": True},
            )

        extraction = _extract_calculator_wrapper(cleaned.casefold())
        arithmetic_candidate = _is_arithmetic_candidate(cleaned, extraction)
        if arithmetic_candidate:
            if extraction.applied:
                cleanup_rule = _merge_cleanup_rules(
                    cleanup_rule,
                    CALCULATOR_NATURAL_LANGUAGE_WRAPPER,
                )
            expression, rejection = _spoken_arithmetic_expression(extraction.source)
            if rejection:
                return self._failure(
                    request,
                    rejection,
                    cleaned_transcript=cleaned,
                    arithmetic_candidate=True,
                    repetition_detected=detected,
                    repetitions_removed=removed,
                    cleanup_rule=cleanup_rule,
                    extracted_calculator_expression=extraction.source,
                )
            normalized = f"calculate {expression}"
        else:
            normalized = cleaned.casefold()

        return TranscriptNormalizationResultV1(
            success=True,
            raw_transcript=raw,
            cleaned_transcript=cleaned,
            normalized_command=normalized,
            extracted_calculator_expression=(
                extraction.source if arithmetic_candidate else ""
            ),
            arithmetic_candidate=arithmetic_candidate,
            repetition_detected=detected,
            repetitions_removed=removed,
            cleanup_rule=cleanup_rule,
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            data={"safe": True, "normalizer": "deterministic_v1"},
            metadata={**dict(request.metadata or {}), "safe": True},
        )

    def _failure(
        self,
        request: Optional[TranscriptNormalizationRequestV1],
        reason: str,
        cleaned_transcript: str = "",
        arithmetic_candidate: bool = False,
        repetition_detected: bool = False,
        repetitions_removed: int = 0,
        cleanup_rule: str = "none",
        extracted_calculator_expression: str = "",
    ) -> TranscriptNormalizationResultV1:
        return TranscriptNormalizationResultV1(
            success=False,
            raw_transcript=request.raw_transcript if request else "",
            cleaned_transcript=cleaned_transcript,
            extracted_calculator_expression=extracted_calculator_expression,
            arithmetic_candidate=arithmetic_candidate,
            repetition_detected=repetition_detected,
            repetitions_removed=repetitions_removed,
            cleanup_rule=cleanup_rule,
            rejection_reason=reason,
            correlation_id=request.correlation_id if request else "",
            session_id=request.session_id if request else "",
            data={"safe": True, "normalizer": "deterministic_v1"},
            metadata={**(dict(request.metadata or {}) if request else {}), "safe": True},
        )


def normalize_transcript(
    text: str,
    correlation_id: str = "",
    session_id: str = "",
    repetition_limit: int = 2,
) -> TranscriptNormalizationResultV1:
    return TranscriptNormalizer().normalize(
        TranscriptNormalizationRequestV1(
            raw_transcript=text,
            repetition_limit=repetition_limit,
            correlation_id=correlation_id,
            session_id=session_id,
        )
    )


def _clean_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.translate(
        str.maketrans(
            {
                "\u2018": "'",
                "\u2019": "'",
                "\u201c": '"',
                "\u201d": '"',
                "\u2212": "-",
                "\u00d7": "*",
                "\u00f7": "/",
            }
        )
    )
    normalized = " ".join(normalized.strip().split())
    return normalized.rstrip(" \t\r\n?!.,;:")


def _collapse_repetition_loops(text: str, repetition_limit: int) -> Tuple[str, bool, int, str]:
    tokens = text.split()
    if len(tokens) < 6:
        return text, False, 0, "none"

    output: List[str] = []
    removed = 0
    index = 0
    while index < len(tokens):
        best: Optional[Tuple[int, int]] = None
        maximum_block = min(12, (len(tokens) - index) // (repetition_limit + 1))
        for block_size in range(maximum_block, 1, -1):
            block = [item.casefold() for item in tokens[index : index + block_size]]
            count = 1
            cursor = index + block_size
            while [item.casefold() for item in tokens[cursor : cursor + block_size]] == block:
                count += 1
                cursor += block_size
            if count > repetition_limit:
                best = (block_size, count)
                break
        if best is None:
            output.append(tokens[index])
            index += 1
            continue
        block_size, count = best
        output.extend(tokens[index : index + block_size])
        removed += count - 1
        index += block_size * count

    cleaned = " ".join(output)
    return cleaned, bool(removed), removed, "adjacent_phrase_loop_v1" if removed else "none"


def _is_arithmetic_candidate(
    text: str,
    extraction: Optional[_CalculatorWrapperExtraction] = None,
) -> bool:
    lowered = text.lower()
    wrapper = extraction or _extract_calculator_wrapper(lowered)
    if wrapper.applied and wrapper.explicit_calculator_request:
        return True
    if re.search(r"\d\s*(?:[+*/-]|\b(?:plus|minus|times|over)\b)", lowered):
        return True
    number_pattern = "|".join(sorted((_NUMBER_WORDS - {"and"}), key=len, reverse=True))
    operator_pattern = (
        r"plus|add|added\s+to|minus|subtract|times|"
        r"multiplied\s+by|divided\s+by|over"
    )
    return bool(
        re.search(rf"\b(?:{number_pattern})\b", lowered)
        and re.search(rf"\b(?:{operator_pattern})\b", lowered)
    )


def _spoken_arithmetic_expression(text: str) -> Tuple[str, str]:
    source = text.casefold()
    for pattern, replacement in _MULTIWORD_REPLACEMENTS:
        source = re.sub(pattern, replacement, source)
    source = re.sub(r"\bnegative\b", " - ", source)
    for word, symbol in _OPERATORS.items():
        source = re.sub(rf"\b{re.escape(word)}\b", f" {symbol} ", source)
    source = re.sub(r"\bover\b", " / ", source)
    source = " ".join(source.split())

    if len(source) > MAX_ARITHMETIC_SOURCE_LENGTH:
        return "", "arithmetic_expression_too_long"
    if not source or not _SAFE_ARITHMETIC_SOURCE.fullmatch(source):
        return "", "unsupported_arithmetic_text"
    tokens = _TOKEN_PATTERN.findall(source)
    if _TOKEN_PATTERN.sub(" ", source).strip():
        return "", "unsupported_arithmetic_text"
    if "".join(tokens).replace(".", "") == "":
        return "", "arithmetic_expression_required"

    output: List[str] = []
    index = 0
    expect_operand = True
    while index < len(tokens):
        token = tokens[index]
        if token == "(" and expect_operand:
            output.append(token)
            index += 1
            continue
        if token == ")" and not expect_operand:
            output.append(token)
            index += 1
            continue
        if token in _SYMBOLS:
            if token == "-" and expect_operand:
                value, next_index, error = _parse_number(tokens, index + 1)
                if error:
                    return "", error
                output.append(f"-{value}")
                index = next_index
                expect_operand = False
                continue
            if expect_operand or token in {"(", ")"}:
                return "", "invalid_arithmetic_operator_order"
            output.append(token)
            expect_operand = True
            index += 1
            continue

        if not expect_operand:
            return "", f"unsupported_arithmetic_word:{token}"
        value, next_index, error = _parse_number(tokens, index)
        if error:
            return "", error
        output.append(value)
        index = next_index
        expect_operand = False

    if expect_operand or not any(token in {"+", "-", "*", "/"} for token in output):
        return "", "incomplete_arithmetic_expression"
    expression = " ".join(output)
    return expression, ""


def _extract_calculator_wrapper(source: str) -> _CalculatorWrapperExtraction:
    original = source
    working = source
    for vocative in (
        "hello, ares",
        "hello ares",
        "hey, ares",
        "hey ares",
        "hi, ares",
        "hi ares",
        "ares",
    ):
        candidate, matched = _strip_anchored_prefix(working, vocative)
        if matched:
            working = candidate
            break
    match = _match_calculator_wrapper(working)

    if match is None:
        polite_source, polite = _strip_anchored_prefix(working, "please")
        if polite:
            match = _match_calculator_wrapper(
                polite_source,
                allowed_prefixes=_POLITE_ACTION_WRAPPERS,
            )

    if match is None:
        for suffix in ("equals", "equal", "is"):
            remainder, matched = _strip_anchored_suffix(working, suffix)
            if not matched:
                continue
            probe = _CalculatorWrapperExtraction(source=remainder)
            if _is_arithmetic_candidate(remainder, probe):
                return _CalculatorWrapperExtraction(
                    source=remainder,
                    applied=True,
                    explicit_calculator_request=False,
                )
        return _CalculatorWrapperExtraction(source=original)

    remainder, rule = match
    for suffix in rule.allowed_trailing_suffixes:
        candidate, matched = _strip_anchored_suffix(remainder, suffix)
        if matched:
            remainder = candidate
            break
    return _CalculatorWrapperExtraction(
        source=remainder,
        applied=True,
        explicit_calculator_request=rule.explicit_calculator_request,
    )


def _match_calculator_wrapper(
    source: str,
    allowed_prefixes: Optional[frozenset[str]] = None,
) -> Optional[Tuple[str, _CalculatorWrapperRule]]:
    for rule in _CALCULATOR_WRAPPER_RULES:
        if allowed_prefixes is not None and rule.prefix not in allowed_prefixes:
            continue
        remainder, matched = _strip_anchored_prefix(source, rule.prefix)
        if matched:
            return remainder, rule
    return None


def _strip_anchored_prefix(source: str, prefix: str) -> Tuple[str, bool]:
    if source == prefix:
        return "", True
    if not source.startswith(prefix):
        return source, False
    remainder = source[len(prefix) :]
    if not remainder or not (remainder[0].isspace() or remainder[0] in ",:;-"):
        return source, False
    return remainder.lstrip(" ,:;-"), True


def _strip_anchored_suffix(source: str, suffix: str) -> Tuple[str, bool]:
    if source == suffix:
        return "", True
    marker = f" {suffix}"
    if not source.endswith(marker):
        return source, False
    return source[: -len(marker)].rstrip(), True


def _merge_cleanup_rules(*rules: str) -> str:
    applied = [rule for rule in rules if rule and rule != "none"]
    return "+".join(dict.fromkeys(applied)) if applied else "none"


def _parse_number(tokens: List[str], start: int) -> Tuple[str, int, str]:
    if start >= len(tokens):
        return "", start, "number_required"
    token = tokens[start]
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return token, start + 1, ""

    number_tokens: List[str] = []
    index = start
    while index < len(tokens):
        current = tokens[index]
        if current in _SYMBOLS:
            break
        if current == "point" or current in _NUMBER_WORDS:
            number_tokens.append(current)
            index += 1
            continue
        break
    if not number_tokens:
        return "", start, f"unsupported_arithmetic_word:{token}"

    if "point" in number_tokens:
        if number_tokens.count("point") != 1:
            return "", start, "invalid_decimal_number"
        point_index = number_tokens.index("point")
        whole_tokens = number_tokens[:point_index]
        decimal_tokens = number_tokens[point_index + 1 :]
        whole, error = _parse_integer_words(whole_tokens or ["zero"])
        if error:
            return "", start, error
        digits: List[str] = []
        for decimal_token in decimal_tokens:
            if decimal_token in _UNITS:
                digits.append(str(_UNITS[decimal_token]))
            elif decimal_token.isdigit():
                digits.extend(decimal_token)
            else:
                return "", start, "invalid_decimal_number"
        if not digits:
            return "", start, "invalid_decimal_number"
        return f"{whole}.{''.join(digits)}", index, ""

    value, error = _parse_integer_words(number_tokens)
    if error:
        return "", start, error
    return str(value), index, ""


def _parse_integer_words(tokens: Iterable[str]) -> Tuple[int, str]:
    words = list(tokens)
    if not words:
        return 0, "number_required"
    if words == ["one", "thousand"]:
        return 1000, ""
    if "thousand" in words:
        return 0, "number_words_out_of_range"

    if "hundred" in words:
        if words.count("hundred") != 1 or len(words) < 2:
            return 0, "invalid_number_words"
        hundred_index = words.index("hundred")
        if hundred_index != 1 or words[0] not in _UNITS or _UNITS[words[0]] == 0:
            return 0, "invalid_number_words"
        value = _UNITS[words[0]] * 100
        remainder = words[2:]
        if remainder and remainder[0] == "and":
            remainder = remainder[1:]
        if "and" in remainder:
            return 0, "invalid_number_words"
        if remainder:
            below, error = _parse_below_hundred(remainder)
            if error:
                return 0, error
            value += below
        return value, ""

    if "and" in words:
        return 0, "invalid_number_words"
    return _parse_below_hundred(words)


def _parse_below_hundred(words: List[str]) -> Tuple[int, str]:
    if len(words) == 1:
        word = words[0]
        if word in _UNITS:
            return _UNITS[word], ""
        if word in _TEENS:
            return _TEENS[word], ""
        if word in _TENS:
            return _TENS[word], ""
        return 0, f"unsupported_arithmetic_word:{word}"
    if len(words) == 2 and words[0] in _TENS and words[1] in _UNITS:
        if _UNITS[words[1]] == 0:
            return 0, "invalid_number_words"
        return _TENS[words[0]] + _UNITS[words[1]], ""
    return 0, "invalid_number_words"
