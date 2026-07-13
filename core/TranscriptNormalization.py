from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Optional, Tuple

from core.Contracts import (
    CONTRACT_TRANSCRIPT_NORMALIZATION_REQUEST,
    TranscriptNormalizationRequestV1,
    TranscriptNormalizationResultV1,
    validate_contract,
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
_WRAPPERS = (
    "can you calculate",
    "please calculate",
    "tell me what is",
    "tell me what",
    "how much is",
    "what is",
    "calculate",
    "compute",
)
_MULTIWORD_REPLACEMENTS = (
    (r"\bmultiplied\s+by\b", " * "),
    (r"\bdivided\s+by\b", " / "),
    (r"\bopen\s+parenthes(?:is|es)\b", " ( "),
    (r"\bclose\s+parenthes(?:is|es)\b", " ) "),
    (r"\bdecimal\s+point\b", " point "),
)
_TOKEN_PATTERN = re.compile(r"\d+(?:\.\d+)?|[a-z]+|[()+*/-]")
_SAFE_ARITHMETIC_SOURCE = re.compile(r"^[a-z0-9\s()+*/.\-]+$")


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

        arithmetic_candidate = _is_arithmetic_candidate(cleaned)
        if arithmetic_candidate:
            expression, rejection = _spoken_arithmetic_expression(cleaned)
            if rejection:
                return self._failure(
                    request,
                    rejection,
                    cleaned_transcript=cleaned,
                    arithmetic_candidate=True,
                    repetition_detected=detected,
                    repetitions_removed=removed,
                    cleanup_rule=cleanup_rule,
                )
            normalized = f"calculate {expression}"
        else:
            normalized = cleaned.casefold()

        return TranscriptNormalizationResultV1(
            success=True,
            raw_transcript=raw,
            cleaned_transcript=cleaned,
            normalized_command=normalized,
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
    ) -> TranscriptNormalizationResultV1:
        return TranscriptNormalizationResultV1(
            success=False,
            raw_transcript=request.raw_transcript if request else "",
            cleaned_transcript=cleaned_transcript,
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


def _is_arithmetic_candidate(text: str) -> bool:
    lowered = text.lower()
    explicit_wrappers = ("calculate", "compute", "please calculate", "can you calculate")
    if any(
        lowered == wrapper or lowered.startswith(f"{wrapper} ")
        for wrapper in explicit_wrappers
    ):
        return True
    if re.search(r"\d\s*(?:[+*/-]|\b(?:plus|minus|times|over)\b)", lowered):
        return True
    number_pattern = "|".join(sorted((_NUMBER_WORDS - {"and"}), key=len, reverse=True))
    operator_pattern = r"plus|add|minus|subtract|times|multiplied\s+by|divided\s+by|over"
    return bool(
        re.search(rf"\b(?:{number_pattern})\b", lowered)
        and re.search(rf"\b(?:{operator_pattern})\b", lowered)
    )


def _spoken_arithmetic_expression(text: str) -> Tuple[str, str]:
    source = text.casefold()
    for wrapper in _WRAPPERS:
        if source == wrapper:
            source = ""
            break
        if source.startswith(f"{wrapper} "):
            source = source[len(wrapper) :].strip()
            break
    source = re.sub(r"^is\s+", "", source)
    for pattern, replacement in _MULTIWORD_REPLACEMENTS:
        source = re.sub(pattern, replacement, source)
    source = re.sub(r"\bnegative\b", " - ", source)
    for word, symbol in _OPERATORS.items():
        source = re.sub(rf"\b{re.escape(word)}\b", f" {symbol} ", source)
    source = re.sub(r"\bover\b", " / ", source)
    source = " ".join(source.split())

    if not source or not _SAFE_ARITHMETIC_SOURCE.fullmatch(source):
        return "", "unsupported_arithmetic_text"
    tokens = _TOKEN_PATTERN.findall(source)
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
