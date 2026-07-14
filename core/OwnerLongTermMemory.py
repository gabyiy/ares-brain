from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple


GENERAL_MEMORY_TYPES = {
    "preference",
    "dislike",
    "routine",
    "personal_fact",
    "relationship",
    "possession",
    "goal",
    "biographical_fact",
    "instruction_preference",
}
GENERAL_MEMORY_STATUSES = {"active", "superseded", "forgotten"}
GENERAL_MEMORY_SOURCE = "explicit_owner_statement"
GENERAL_MEMORY_PERSISTENCE = "long_term"
GENERAL_MEMORY_CONFIDENCE = 1.0

MAX_GENERAL_MEMORIES = 100
MAX_GENERAL_MEMORY_HISTORY = 20
MAX_MEMORY_TEXT_LENGTH = 320
MAX_CANONICAL_TEXT_LENGTH = 360
MAX_MEMORY_TOPICS = 8
MAX_MEMORY_TOPIC_LENGTH = 48
MAX_MEMORY_RETRIEVAL_RESULTS = 5
MAX_SPOKEN_MEMORY_RESULTS = 5

GENERAL_MEMORY_RULE = "owner_general_long_term_memory_v1"
GENERAL_MEMORY_QUERY_RULE = "owner_general_long_term_query_v1"
GENERAL_MEMORY_FORGET_RULE = "owner_general_long_term_forget_v1"
GENERAL_MEMORY_UPDATE_RULE = "owner_general_long_term_update_v1"
GENERAL_MEMORY_DELETE_ALL_RULE = "owner_general_long_term_delete_all_v1"
DELETE_ALL_CONFIRMATION_PHRASE = "Yes, delete all my long-term owner memory"
_DELETE_ALL_CONFIRMATION_NORMALIZED = "yes delete all my longterm owner memory"

_CONTROL_CATEGORY = "Cc"
_UNSAFE_MEMORY_PATTERN = re.compile(
    r"(?:__import__|\b(?:eval|exec)\s*\(|<script\b|\bimport\s+(?:os|sys|subprocess)\b|"
    r"\b(?:run|execute|launch)\s+(?:a\s+)?(?:shell|command|python|powershell)\b|"
    r"\bignore\s+(?:all\s+)?(?:(?:previous|system)\s+){1,2}instructions\b|"
    r"\b(?:change|replace|override|reveal)\s+(?:the\s+)?system\s+(?:prompt|instructions)\b|"
    r"\band\s+(?:delete|open|run|execute|shutdown|restart)\b)",
    re.IGNORECASE,
)
_PATH_OR_BINARY_PATTERN = re.compile(
    r"(?:\.\.[/\\]|(?:^|\s)[a-z]:[/\\]|(?:^|\s)/(?:etc|usr|var|home|root)/|"
    r"https?://|data:[^\s]+;base64,)",
    re.IGNORECASE,
)
_PROTECTED_MEMORY_PATTERN = re.compile(
    r"\b(?:password|passcode|pin|api\s+key|authentication\s+token|access\s+token|"
    r"private\s+key|recovery\s+phrase|seed\s+phrase|credit\s+card|debit\s+card|"
    r"card\s+number|cvv|iban|bank\s+account)\b",
    re.IGNORECASE,
)
_TEMPORARY_MARKERS = re.compile(
    r"\b(?:right\s+now|at\s+the\s+moment|currently|today|tonight|this\s+(?:morning|afternoon|evening)|"
    r"yesterday|tomorrow)\b",
    re.IGNORECASE,
)

_MEMORY_TRIGGER_CONTEXT = re.compile(
    r"^(?:i\s+want\s+you\s+to\s+remember|remember(?:ing)?|save|store|keep|add|"
    r"do\s+not\s+forget|yes\s+delete|forget|delete|remove|update|change|make|note)\b",
    re.IGNORECASE,
)
_LONG_TERM_VARIANTS = (
    re.compile(
        r"(?:(?:in|on|for|to)\s+)?(?:(?:a|the|your)\s+)?"
        r"(?:long[\s-]*(?:term|time|turn)|longtime|long|lock(?:ed)?[\s-]*term|"
        r"lifetime|permanent|persistent)\s+memory\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bpermanently\b", re.IGNORECASE),
    re.compile(r"(?:for\s+(?:the\s+)?)?long[\s-]*(?:term|time)\b", re.IGNORECASE),
)

_SAVE_TRIGGER_PATTERNS = (
    re.compile(r"^i\s+want\s+you\s+to\s+remember(?:\s+longterm)?(?:\s+that)?\s+(?P<fact>.+)$", re.IGNORECASE),
    re.compile(r"^remember(?:\s+this)?(?:\s+longterm)?(?:\s+that)?\s+(?P<fact>.+)$", re.IGNORECASE),
    re.compile(r"^do\s+not\s+forget(?:\s+that)?\s+(?P<fact>.+)$", re.IGNORECASE),
    re.compile(r"^(?:save|store|keep|add)(?:\s+this)?(?:\s+(?:to|in))?(?:\s+longterm)(?:\s+that)?\s+(?P<fact>.+)$", re.IGNORECASE),
    re.compile(r"^make\s+(?:a\s+)?longterm(?:\s+memory)?(?:\s+that)?\s+(?P<fact>.+)$", re.IGNORECASE),
    re.compile(r"^note\s+longterm(?:\s+that)?\s+(?P<fact>.+)$", re.IGNORECASE),
)

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "for", "from", "i", "in", "is", "it",
    "me", "my", "of", "on", "one", "owner", "something", "that", "the", "this", "to", "you", "your",
    "do", "does", "did", "what", "when", "where", "about", "tell", "told", "remember",
    "memory", "long", "term", "going", "go",
}
_PREDICATE_EQUIVALENTS = {
    "likes": "likes",
    "loves": "likes",
    "enjoys": "likes",
    "prefers": "prefers",
    "prefers_assistant": "prefers_assistant",
    "dislikes": "dislikes",
    "hates": "dislikes",
}


class GeneralMemoryValidationError(ValueError):
    def __init__(self, code: str, message: str, *, protected: bool = False):
        super().__init__(message)
        self.code = str(code or "general_memory_validation_failed")
        self.message = str(message or "General owner memory validation failed.")
        self.protected = bool(protected)


@dataclass(frozen=True)
class GeneralMemoryParse:
    recognized: bool
    action: str = ""
    memory: Dict[str, Any] = field(default_factory=dict)
    query: Dict[str, Any] = field(default_factory=dict)
    fact_text: str = ""
    extracted_phrase: str = ""
    parser_rule: str = GENERAL_MEMORY_RULE
    rejection_reason: str = ""
    clarification_reason: str = ""
    protected: bool = False
    confirmation_required: bool = False
    normalized_trigger: str = ""
    routing_reason: str = ""


def parse_general_owner_memory(text: str) -> GeneralMemoryParse:
    source = normalize_general_memory_source(text)
    if not source:
        return GeneralMemoryParse(False)
    lowered = source.casefold().rstrip(" ?!.,;:")

    # Preserve the established note boundary. A structured long-term form such
    # as "remember this for the long term ..." is handled below, while an
    # idea/note label followed by a colon remains a note command.
    if re.match(r"^remember\s+this\s+(?:idea|note)(?:\s*:|\s+)", source, flags=re.IGNORECASE):
        return GeneralMemoryParse(False)

    if lowered == _DELETE_ALL_CONFIRMATION_NORMALIZED:
        return GeneralMemoryParse(True, action="delete_all_confirm", parser_rule=GENERAL_MEMORY_DELETE_ALL_RULE)
    if lowered in {
        "forget everything about me",
        "delete all long term memory",
        "delete all my long term memory",
        "erase my profile",
        "erase all my owner memory",
    }:
        return GeneralMemoryParse(
            True,
            action="delete_all_request",
            parser_rule=GENERAL_MEMORY_DELETE_ALL_RULE,
            confirmation_required=True,
        )

    # Existing bounded multi-step commands are planned one clause at a time.
    # Keep this allowlist limited to non-destructive planner operations.
    if _contains_planner_safe_chain(source):
        return GeneralMemoryParse(False)

    list_query = _parse_list_or_recall(source)
    if list_query is not None:
        action, query = list_query
        return GeneralMemoryParse(
            True,
            action=action,
            query=query,
            extracted_phrase=str(query.get("display_query") or ""),
            parser_rule=GENERAL_MEMORY_QUERY_RULE,
        )

    forget_query = _parse_forget(source)
    if forget_query is not None:
        return forget_query

    update_parse = _parse_update(source)
    if update_parse is not None:
        return update_parse

    if re.match(r"^remember\s+to\b", source, flags=re.IGNORECASE):
        return GeneralMemoryParse(False)
    if re.match(r"^remember\s+(?:my\s+)?task\b", source, flags=re.IGNORECASE):
        return GeneralMemoryParse(False)

    for pattern in _SAVE_TRIGGER_PATTERNS:
        match = pattern.fullmatch(source)
        if not match:
            continue
        fact_text = _clean_fact_text(match.group("fact"))
        if not fact_text:
            return _rejected("missing_general_memory_fact")
        if (
            re.match(r"^remember\s+this\b", source, flags=re.IGNORECASE)
            and "longterm" not in source.casefold()
            and not _has_supported_owner_fact_structure(fact_text)
        ):
            return GeneralMemoryParse(False)
        if (
            re.match(r"^remember\s+(?!that\b|this\b|longterm\b)", source, flags=re.IGNORECASE)
            and not _has_supported_owner_fact_structure(fact_text)
        ):
            return GeneralMemoryParse(False)
        if (
            re.match(r"^remember\s+(?!that\b|this\b|longterm\b)", source, flags=re.IGNORECASE)
            and _TEMPORARY_MARKERS.search(fact_text)
        ):
            return GeneralMemoryParse(False)
        if _sounds_temporary(fact_text):
            return GeneralMemoryParse(
                True,
                action="reject",
                fact_text=fact_text,
                extracted_phrase=fact_text,
                clarification_reason="temporary_memory_requires_clarification",
                rejection_reason="temporary_memory_requires_clarification",
            )
        try:
            memory = classify_general_memory(fact_text)
        except GeneralMemoryValidationError as error:
            return _rejected(error.code, protected=error.protected, fact_text=fact_text)
        return GeneralMemoryParse(
            True,
            action="save",
            memory=memory,
            fact_text=fact_text,
            extracted_phrase=fact_text,
            parser_rule=GENERAL_MEMORY_RULE,
            normalized_trigger=_normalized_save_trigger(source, match.start("fact")),
            routing_reason="explicit_owner_memory_storage_request",
        )

    if _looks_like_general_memory_prefix(source):
        return _rejected("malformed_general_memory_command")
    return GeneralMemoryParse(False)


def normalize_general_memory_source(text: str) -> str:
    source = unicodedata.normalize("NFKC", str(text or ""))
    source = source.translate(str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'}))
    if any(unicodedata.category(character) == _CONTROL_CATEGORY for character in source):
        return source
    source = re.sub(r"^(?:(?:hello|hey|hi)\s+)?ares\s*[,;:]?\s+", "", source, flags=re.IGNORECASE)
    source = re.sub(r"^actually\s*[,;:]?\s+", "actually ", source, flags=re.IGNORECASE)
    source = _normalize_long_term_trigger(source)
    source = re.sub(r"^remembering\b", "remember", source, flags=re.IGNORECASE)
    source = re.sub(r"^remember\s+this\s+that\b", "remember that", source, flags=re.IGNORECASE)
    source = re.sub(
        r"^(save|store|keep|add)\s+this\s+(?:in|to)\s+memory(?:\s+that)?\b",
        r"\1 longterm that",
        source,
        flags=re.IGNORECASE,
    )
    source = re.sub(
        r"^remember\s+(?:this\s+)?(?:a\s+)?longterm\b",
        "remember longterm",
        source,
        flags=re.IGNORECASE,
    )
    source = re.sub(
        r"^(save|store|keep|add)\s+(?:this\s+)?(?:a\s+)?longterm\b",
        r"\1 longterm",
        source,
        flags=re.IGNORECASE,
    )
    source = re.sub(r"\s*[,;]\s*", " ", source)
    return " ".join(source.strip().split()).rstrip(" ?!.")


def _normalize_long_term_trigger(source: str) -> str:
    context = _MEMORY_TRIGGER_CONTEXT.match(source)
    if context is None:
        return source
    for pattern in _LONG_TERM_VARIANTS:
        for match in pattern.finditer(source):
            between = source[context.end():match.start()]
            if len(between) > 64 or re.search(r"\bthat\b", between, flags=re.IGNORECASE):
                continue
            return f"{source[:match.start()]} longterm {source[match.end():]}"
    return source


def _normalized_save_trigger(source: str, fact_start: int) -> str:
    trigger = _clean_fragment(source[:fact_start]).casefold()
    if "longterm" in trigger:
        return "remember longterm that"
    if trigger.startswith("do not forget"):
        return "do not forget that"
    return "remember that"


def classify_general_memory(fact_text: str) -> Dict[str, Any]:
    fact = validate_general_memory_text(fact_text)
    lowered = fact.casefold()

    correction = re.fullmatch(r"i\s+prefer\s+(?P<new>.+?)\s+not\s+(?P<old>.+)", fact, flags=re.IGNORECASE)
    if correction:
        memory = _record("preference", "owner", "prefers", correction.group("new"), fact)
        memory["replacement_query"] = {"memory_type": "preference", "topics": list(extract_memory_topics(correction.group("old")))}
        return memory

    match = re.fullmatch(r"(?P<object>.+?)\s+is\s+something\s+i\s+(?:like|love|enjoy)", fact, flags=re.IGNORECASE)
    if match:
        return _record("preference", "owner", "likes", match.group("object"), fact)

    match = re.fullmatch(r"(?P<object>.+?)\s+is\s+one\s+of\s+my\s+favorite\s+(?P<category>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("preference", "owner", "likes", match.group("object"), fact, extra_topics=(match.group("category"),))

    match = re.fullmatch(r"i\s+(?P<verb>do\s+not\s+like|don't\s+like|dislike|hate)\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        predicate = "hates" if match.group("verb").casefold() == "hate" else "dislikes"
        return _record("dislike", "owner", predicate, match.group("object"), fact)

    match = re.fullmatch(r"i\s+(?P<verb>like|love|enjoy)\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        predicate = {"like": "likes", "love": "loves", "enjoy": "enjoys"}[match.group("verb").casefold()]
        return _record("preference", "owner", predicate, match.group("object"), fact)

    match = re.fullmatch(r"i\s+prefer\s+that\s+you\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("instruction_preference", "owner", "prefers_assistant", match.group("object"), fact)

    match = re.fullmatch(r"i\s+prefer\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("preference", "owner", "prefers", match.group("object"), fact)

    match = re.fullmatch(r"i\s+(?P<frequency>usually|normally|often)\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("routine", "owner", match.group("frequency").casefold(), match.group("object"), fact)

    match = re.fullmatch(r"i\s+work\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("routine", "owner", "works", match.group("object"), fact)

    match = re.fullmatch(r"i\s+(?P<verb>own|have)\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        predicate = "owns" if match.group("verb").casefold() == "own" else "has"
        return _record("possession", "owner", predicate, match.group("object"), fact)

    match = re.fullmatch(r"i\s+live\s+in\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("biographical_fact", "owner", "lives_in", match.group("object"), fact)

    match = re.fullmatch(r"i\s+am\s+from\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("biographical_fact", "owner", "from", match.group("object"), fact)

    match = re.fullmatch(r"i\s+was\s+born\s+in\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("biographical_fact", "owner", "born_in", match.group("object"), fact)

    match = re.fullmatch(r"my\s+(?P<subject>.+?)\s+is\s+named\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("relationship", f"owner_{_identifier(match.group('subject'))}", "named", match.group("object"), fact)

    match = re.fullmatch(r"my\s+goal\s+is\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("goal", "owner", "goal", match.group("object"), fact)

    match = re.fullmatch(r"i\s+want\s+to\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("goal", "owner", "wants", match.group("object"), fact)

    match = re.fullmatch(r"(?P<subject>.+?)\s+helps\s+me\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("personal_fact", _clean_fragment(match.group("subject")), "helps_owner", match.group("object"), fact)

    match = re.fullmatch(r"i\s+(?P<object>.+?\b(?:on|every)\b.+)", fact, flags=re.IGNORECASE)
    if match and re.search(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekday|weekend)s?\b", fact, re.IGNORECASE):
        return _record("routine", "owner", "does", match.group("object"), fact)

    match = re.fullmatch(r"i\s+(?P<object>.+)", fact, flags=re.IGNORECASE)
    if match:
        return _record("personal_fact", "owner", "stated", match.group("object"), fact)

    return _record("personal_fact", "owner_context", "stated", fact, fact)


def validate_general_memory_text(text: str) -> str:
    source = unicodedata.normalize("NFKC", str(text or ""))
    if any(unicodedata.category(character) == _CONTROL_CATEGORY for character in source):
        raise GeneralMemoryValidationError("control_character_rejected", "Long-term memory cannot contain control characters.")
    clean = " ".join(source.strip().split()).rstrip(" ?!.,;:").strip()
    if not clean:
        raise GeneralMemoryValidationError("empty_general_memory", "A long-term memory statement is required.")
    if len(clean) > MAX_MEMORY_TEXT_LENGTH:
        raise GeneralMemoryValidationError("general_memory_too_long", "The long-term memory statement is too long.")
    if _PROTECTED_MEMORY_PATTERN.search(clean):
        raise GeneralMemoryValidationError("protected_memory_rejected", "Protected information cannot be stored.", protected=True)
    if _UNSAFE_MEMORY_PATTERN.search(clean):
        raise GeneralMemoryValidationError("unsafe_memory_rejected", "Executable or instruction-changing memory content is not allowed.")
    if _PATH_OR_BINARY_PATTERN.search(clean):
        raise GeneralMemoryValidationError("unsafe_memory_content", "Path, URL, or binary memory content is not allowed.")
    return clean


def normalize_general_memory_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(record, Mapping):
        raise GeneralMemoryValidationError("malformed_general_memory", "General memory must be an object.")
    string_fields = ("memory_type", "subject", "predicate", "object", "canonical_text", "owner_spoken_text")
    if any(not isinstance(record.get(name), str) for name in string_fields):
        raise GeneralMemoryValidationError("malformed_general_memory", "General memory fields must be strings.")
    raw_topics = record.get("topics")
    if not isinstance(raw_topics, (list, tuple)) or any(not isinstance(topic, str) for topic in raw_topics):
        raise GeneralMemoryValidationError("malformed_general_memory", "General memory topics must be a string list.")
    memory_type = str(record.get("memory_type") or "")
    if memory_type not in GENERAL_MEMORY_TYPES:
        raise GeneralMemoryValidationError("unsupported_memory_type", "General memory type is unsupported.")
    subject = _clean_identifier_or_phrase(record.get("subject"), "subject")
    predicate = _clean_identifier_or_phrase(record.get("predicate"), "predicate", identifier=True)
    object_value = validate_general_memory_text(str(record.get("object") or ""))
    owner_spoken = validate_general_memory_text(str(record.get("owner_spoken_text") or ""))
    canonical = " ".join(str(record.get("canonical_text") or "").strip().split())
    if not canonical or len(canonical) > MAX_CANONICAL_TEXT_LENGTH:
        raise GeneralMemoryValidationError("invalid_canonical_text", "General memory canonical text is invalid.")
    topics = normalize_memory_topics(raw_topics)
    persistence = str(record.get("persistence") or GENERAL_MEMORY_PERSISTENCE)
    source = str(record.get("source") or GENERAL_MEMORY_SOURCE)
    confidence = record.get("confidence", GENERAL_MEMORY_CONFIDENCE)
    if persistence != GENERAL_MEMORY_PERSISTENCE or source != GENERAL_MEMORY_SOURCE or confidence != GENERAL_MEMORY_CONFIDENCE:
        raise GeneralMemoryValidationError("invalid_general_memory_metadata", "General memory metadata is invalid.")
    normalized = {
        "memory_type": memory_type,
        "subject": subject,
        "predicate": predicate,
        "object": object_value,
        "canonical_text": canonical,
        "owner_spoken_text": owner_spoken,
        "topics": list(topics),
        "persistence": persistence,
        "source": source,
        "confidence": GENERAL_MEMORY_CONFIDENCE,
    }
    replacement_query = record.get("replacement_query")
    if replacement_query:
        if not isinstance(replacement_query, Mapping):
            raise GeneralMemoryValidationError("malformed_memory_query", "Replacement query must be an object.")
        normalized["replacement_query"] = normalize_memory_query(replacement_query)
    return normalized


def normalize_memory_topics(values: Iterable[Any]) -> Tuple[str, ...]:
    topics = []
    for raw in values:
        for token in _semantic_tokens(str(raw or ""), include_equivalents=True):
            if not token or len(token) > MAX_MEMORY_TOPIC_LENGTH or token in topics:
                continue
            topics.append(token)
            if len(topics) >= MAX_MEMORY_TOPICS:
                return tuple(topics)
    return tuple(topics)


def extract_memory_topics(*values: str) -> Tuple[str, ...]:
    return normalize_memory_topics(values)


def normalize_memory_query(query: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(query, Mapping):
        raise GeneralMemoryValidationError("malformed_memory_query", "General memory query must be an object.")
    for name in ("memory_type", "predicate", "signature", "response_style", "display_query"):
        if name in query and not isinstance(query.get(name), str):
            raise GeneralMemoryValidationError("malformed_memory_query", "General memory query scalar fields must be strings.")
    for name in ("topics", "tokens"):
        values = query.get(name, ())
        if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) for value in values):
            raise GeneralMemoryValidationError("malformed_memory_query", "General memory query topics and tokens must be string lists.")
    if "match_all" in query and not isinstance(query.get("match_all"), bool):
        raise GeneralMemoryValidationError("malformed_memory_query", "General memory query match_all must be boolean.")
    memory_type = str(query.get("memory_type") or "")
    if memory_type and memory_type not in GENERAL_MEMORY_TYPES:
        raise GeneralMemoryValidationError("unsupported_memory_type", "General memory query type is unsupported.")
    predicate = str(query.get("predicate") or "").strip().casefold()
    topics = normalize_memory_topics(query.get("topics") or ())
    tokens = normalize_memory_topics(query.get("tokens") or ())
    signature = str(query.get("signature") or "")
    response_style = str(query.get("response_style") or "topic")
    return {
        "memory_type": memory_type,
        "predicate": predicate,
        "topics": list(topics),
        "tokens": list(tokens),
        "signature": signature,
        "response_style": response_style,
        "match_all": bool(query.get("match_all")),
        "display_query": _clean_fragment(str(query.get("display_query") or "")),
    }


def general_memory_signature(record: Mapping[str, Any]) -> str:
    predicate = _PREDICATE_EQUIVALENTS.get(str(record.get("predicate") or ""), str(record.get("predicate") or ""))
    subject = _identifier(str(record.get("subject") or ""))
    topics = normalize_memory_topics(record.get("topics") or extract_memory_topics(str(record.get("object") or "")))
    material = "|".join((str(record.get("memory_type") or ""), subject, predicate, " ".join(sorted(topics))))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def general_memory_id(record: Mapping[str, Any]) -> str:
    return f"mem-{general_memory_signature(record)[:16]}"


def likely_duplicate_memory(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return general_memory_signature(left) == general_memory_signature(right)


def score_general_memory(record: Mapping[str, Any], query: Mapping[str, Any]) -> int:
    if str(record.get("status") or "active") != "active":
        return -1
    clean_query = normalize_memory_query(query)
    score = 0
    requested_type = clean_query["memory_type"]
    if requested_type:
        if record.get("memory_type") != requested_type:
            return -1
        score += 8
    requested_predicate = clean_query["predicate"]
    if requested_predicate:
        expected = _PREDICATE_EQUIVALENTS.get(requested_predicate, requested_predicate)
        actual = _PREDICATE_EQUIVALENTS.get(str(record.get("predicate") or ""), str(record.get("predicate") or ""))
        if expected != actual:
            return -1
        score += 6
    if clean_query["signature"]:
        if clean_query["signature"] != general_memory_signature(record):
            return -1
        score += 40
    record_topics = set(normalize_memory_topics(record.get("topics") or ()))
    query_topics = set(clean_query["topics"])
    topic_overlap = len(record_topics & query_topics)
    if query_topics and topic_overlap == 0:
        return -1
    score += topic_overlap * 10
    record_tokens = set(_semantic_tokens(" ".join((str(record.get("canonical_text") or ""), str(record.get("object") or "")))))
    query_tokens = set(clean_query["tokens"])
    token_overlap = len(record_tokens & query_tokens)
    if query_tokens and token_overlap == 0 and not topic_overlap:
        return -1
    score += token_overlap * 2
    return score if score > 0 or clean_query["match_all"] else -1


def general_memory_clause(record: Mapping[str, Any], *, third_person: bool = False) -> str:
    predicate = str(record.get("predicate") or "")
    subject = str(record.get("subject") or "owner")
    object_value = str(record.get("object") or "")
    owner = "the owner" if third_person else "you"
    possessive = "the owner's" if third_person else "your"
    verbs = {
        "likes": "likes" if third_person else "like",
        "loves": "loves" if third_person else "love",
        "enjoys": "enjoys" if third_person else "enjoy",
        "prefers": "prefers" if third_person else "prefer",
        "dislikes": "dislikes" if third_person else "dislike",
        "hates": "hates" if third_person else "hate",
        "usually": "usually",
        "normally": "normally",
        "often": "often",
        "works": "works" if third_person else "work",
        "owns": "owns" if third_person else "own",
        "has": "has" if third_person else "have",
        "wants": "wants to" if third_person else "want to",
        "does": "does" if third_person else "do",
    }
    if predicate in verbs:
        return f"{owner} {verbs[predicate]} {object_value}"
    if predicate == "prefers_assistant":
        return f"{owner} prefer{'s' if third_person else ''} that ARES {object_value}" if third_person else f"you prefer that I {object_value}"
    if predicate == "lives_in":
        return f"{owner} live{'s' if third_person else ''} in {object_value}"
    if predicate == "from":
        return f"{owner} is from {object_value}" if third_person else f"you are from {object_value}"
    if predicate == "born_in":
        return f"{owner} was born in {object_value}" if third_person else f"you were born in {object_value}"
    if predicate == "goal":
        return f"{possessive} goal is {object_value}"
    if predicate == "named":
        label = subject.removeprefix("owner_").replace("_", " ")
        return f"{possessive} {label} is named {object_value}"
    if predicate == "helps_owner":
        return f"{subject} helps {owner} {object_value}"
    if predicate == "stated":
        return _first_person_to_owner(object_value, third_person=third_person)
    return f"{owner} {predicate.replace('_', ' ')} {object_value}".strip()


def _parse_list_or_recall(source: str) -> tuple[str, Dict[str, Any]] | None:
    clean = source.casefold()
    if clean in {
        "list my longterm memories",
        "list my long term memories",
        "what do you remember about me",
        "list what you know about me",
        "tell me what you remember about me",
    }:
        return "list", {"match_all": True, "response_style": "combined_list", "display_query": "me"}
    if clean in {"what are my preferences", "show my saved preferences", "what are some things i like", "what do i like", "what do i like doing", "what do i enjoy"}:
        return "recall", {
            "memory_type": "preference",
            "predicate": "likes" if "like" in clean or "enjoy" in clean else "",
            "match_all": True,
            "response_style": "preference_list",
            "display_query": "preferences",
        }
    if clean in {"what do i dislike", "what do i hate"}:
        return "recall", {"memory_type": "dislike", "match_all": True, "response_style": "preference_list", "display_query": "dislikes"}
    if clean in {"what is my routine", "what is my normal routine"}:
        return "recall", {"memory_type": "routine", "match_all": True, "response_style": "type_list", "display_query": "routine"}
    if clean in {"what goals do i have", "what are my goals"}:
        return "recall", {"memory_type": "goal", "match_all": True, "response_style": "type_list", "display_query": "goals"}
    match = re.fullmatch(r"when\s+do\s+i\s+(?:normally|usually)\s+(?P<topic>.+)", source, flags=re.IGNORECASE)
    if match:
        topic = _clean_fragment(match.group("topic"))
        return "recall", {"memory_type": "routine", "topics": list(extract_memory_topics(topic)), "tokens": list(extract_memory_topics(topic)), "response_style": "topic", "display_query": topic}
    match = re.fullmatch(r"what\s+do\s+you\s+(?:remember|know)\s+about\s+(?:my\s+)?(?P<topic>.+)", source, flags=re.IGNORECASE)
    if not match:
        match = re.fullmatch(r"what\s+did\s+i\s+tell\s+you\s+about\s+(?P<topic>.+)", source, flags=re.IGNORECASE)
    if match:
        topic = _clean_fragment(match.group("topic"))
        if topic.casefold().startswith("my "):
            return None
        return "recall", {"topics": list(extract_memory_topics(topic)), "tokens": list(extract_memory_topics(topic)), "response_style": "topic", "display_query": topic}
    match = re.fullmatch(r"do\s+i\s+(?P<fact>like|love|enjoy|prefer|dislike|hate)\s+(?P<object>.+)", source, flags=re.IGNORECASE)
    if match:
        fact = f"I {match.group('fact')} {match.group('object')}"
        memory = classify_general_memory(fact)
        return "recall", {"signature": general_memory_signature(memory), "topics": memory["topics"], "predicate": memory["predicate"], "response_style": "assertion", "display_query": _clean_fragment(match.group("object"))}
    match = re.fullmatch(r"do\s+you\s+remember\s+that\s+(?P<fact>.+)", source, flags=re.IGNORECASE)
    if match:
        memory = classify_general_memory(match.group("fact"))
        return "recall", {"signature": general_memory_signature(memory), "topics": memory["topics"], "predicate": memory["predicate"], "response_style": "assertion", "display_query": _clean_fragment(match.group("fact"))}
    match = re.fullmatch(r"what\s+do\s+you\s+know\s+about\s+my\s+(?P<topic>.+)", source, flags=re.IGNORECASE)
    if match:
        topic = _clean_fragment(match.group("topic"))
        return "recall", {"topics": list(extract_memory_topics(topic)), "tokens": list(extract_memory_topics(topic)), "response_style": "topic", "display_query": topic}
    return None


def _parse_forget(source: str) -> GeneralMemoryParse | None:
    patterns = (
        (re.compile(r"^(?:forget|delete)(?:\s+the)?(?:\s+longterm)?\s+memory\s+that\s+(?P<fact>.+)$", re.IGNORECASE), "fact"),
        (re.compile(r"^forget\s+that\s+(?P<fact>.+)$", re.IGNORECASE), "fact"),
        (re.compile(r"^(?:forget|delete)(?:\s+all)?\s+(?:memories|memory)\s+about\s+(?P<topic>.+)$", re.IGNORECASE), "topic"),
        (re.compile(r"^forget\s+what\s+i\s+told\s+you\s+about\s+(?P<topic>.+)$", re.IGNORECASE), "topic"),
        (re.compile(r"^remove\s+my\s+(?P<topic>.+?)\s+preference$", re.IGNORECASE), "preference"),
        (re.compile(r"^forget\s+all\s+my\s+saved\s+(?P<kind>preferences|routines|goals)$", re.IGNORECASE), "type"),
    )
    for pattern, kind in patterns:
        match = pattern.fullmatch(source)
        if not match:
            continue
        if kind == "fact":
            fact = _clean_fact_text(match.group("fact"))
            try:
                memory = classify_general_memory(fact)
            except GeneralMemoryValidationError as error:
                return _rejected(error.code, protected=error.protected, fact_text=fact)
            query = {"signature": general_memory_signature(memory), "topics": memory["topics"], "predicate": memory["predicate"], "response_style": "exact", "display_query": fact}
            return GeneralMemoryParse(True, action="forget", memory=memory, query=query, fact_text=fact, extracted_phrase=fact, parser_rule=GENERAL_MEMORY_FORGET_RULE)
        if kind == "topic":
            topic = _clean_fragment(match.group("topic"))
            query = {"topics": list(extract_memory_topics(topic)), "tokens": list(extract_memory_topics(topic)), "match_all": True, "response_style": "topic", "display_query": topic}
            return GeneralMemoryParse(True, action="forget", query=query, extracted_phrase=topic, parser_rule=GENERAL_MEMORY_FORGET_RULE)
        if kind == "preference":
            topic = _clean_fragment(match.group("topic"))
            query = {"memory_type": "preference", "topics": list(extract_memory_topics(topic)), "match_all": True, "response_style": "topic", "display_query": topic}
            return GeneralMemoryParse(True, action="forget", query=query, extracted_phrase=topic, parser_rule=GENERAL_MEMORY_FORGET_RULE)
        memory_type = {"preferences": "preference", "routines": "routine", "goals": "goal"}[match.group("kind").casefold()]
        query = {"memory_type": memory_type, "match_all": True, "response_style": "type", "display_query": match.group("kind")}
        return GeneralMemoryParse(True, action="forget", query=query, extracted_phrase=match.group("kind"), parser_rule=GENERAL_MEMORY_FORGET_RULE)
    return None


def _parse_update(source: str) -> GeneralMemoryParse | None:
    clean = re.sub(r"^actually\s+", "", source, flags=re.IGNORECASE)
    match = re.fullmatch(r"(?:update|change)\s+my\s+(?P<topic>.+?)\s+preference\s+to\s+(?P<value>.+)", clean, flags=re.IGNORECASE)
    if match:
        topic = _clean_fragment(match.group("topic"))
        value = _clean_fragment(match.group("value"))
        object_value = value if set(extract_memory_topics(value)) & set(extract_memory_topics(topic)) else f"{value} {topic}"
        fact = f"I prefer {object_value}"
        memory = classify_general_memory(fact)
        query = {"memory_type": "preference", "topics": list(extract_memory_topics(topic)), "match_all": True, "response_style": "topic", "display_query": topic}
        return GeneralMemoryParse(True, action="update", memory=memory, query=query, fact_text=fact, extracted_phrase=fact, parser_rule=GENERAL_MEMORY_UPDATE_RULE)
    match = re.fullmatch(r"change\s+the\s+memory\s+about\s+(?P<topic>.+?)\s*:?\s+(?P<fact>i\s+.+)", clean, flags=re.IGNORECASE)
    if match:
        fact = _clean_fact_text(match.group("fact"))
        memory = classify_general_memory(fact)
        topic = _clean_fragment(match.group("topic"))
        query = {"topics": list(extract_memory_topics(topic)), "match_all": True, "response_style": "topic", "display_query": topic}
        return GeneralMemoryParse(True, action="update", memory=memory, query=query, fact_text=fact, extracted_phrase=fact, parser_rule=GENERAL_MEMORY_UPDATE_RULE)
    for pattern in _SAVE_TRIGGER_PATTERNS:
        trigger = pattern.fullmatch(clean)
        if not trigger:
            continue
        fact = _clean_fact_text(trigger.group("fact"))
        try:
            memory = classify_general_memory(fact)
        except GeneralMemoryValidationError as error:
            return _rejected(error.code, protected=error.protected, fact_text=fact)
        replacement_query = dict(memory.get("replacement_query") or {})
        if replacement_query:
            return GeneralMemoryParse(
                True,
                action="update",
                memory=memory,
                query=replacement_query,
                fact_text=fact,
                extracted_phrase=fact,
                parser_rule=GENERAL_MEMORY_UPDATE_RULE,
            )
    return None


def _record(memory_type: str, subject: str, predicate: str, object_value: str, owner_spoken_text: str, *, extra_topics: Sequence[str] = ()) -> Dict[str, Any]:
    object_clean = validate_general_memory_text(_clean_fragment(object_value))
    spoken = validate_general_memory_text(owner_spoken_text)
    record = {
        "memory_type": memory_type,
        "subject": _clean_fragment(subject),
        "predicate": predicate,
        "object": object_clean,
        "canonical_text": "",
        "owner_spoken_text": spoken,
        "topics": list(extract_memory_topics(subject, object_clean, *extra_topics)),
        "persistence": GENERAL_MEMORY_PERSISTENCE,
        "source": GENERAL_MEMORY_SOURCE,
        "confidence": GENERAL_MEMORY_CONFIDENCE,
    }
    canonical = general_memory_clause(record, third_person=True).rstrip(".")
    record["canonical_text"] = f"{canonical[:1].upper() + canonical[1:]}."
    return normalize_general_memory_record(record)


def _sounds_temporary(fact_text: str) -> bool:
    lowered = fact_text.casefold()
    if re.search(r"\b(?:usually|normally|often|every)\b", lowered):
        return False
    return bool(_TEMPORARY_MARKERS.search(lowered))


def _has_supported_owner_fact_structure(fact_text: str) -> bool:
    clean = _clean_fragment(fact_text)
    return bool(
        re.match(r"^(?:i|my)\b", clean, flags=re.IGNORECASE)
        or re.search(r"\bhelps\s+me\b", clean, flags=re.IGNORECASE)
        or re.search(r"\bis\s+(?:something\s+i|one\s+of\s+my)\b", clean, flags=re.IGNORECASE)
    )


def _contains_planner_safe_chain(source: str) -> bool:
    return bool(
        re.search(
            r"\s+(?:and|then)\s+(?:calculate|compute|add\s+task|create\s+a\s+task|"
            r"save\s+(?:a\s+)?task|save\s+note|take\s+a\s+note)\b",
            source,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_general_memory_prefix(source: str) -> bool:
    lowered = source.casefold()
    return bool(
        re.match(r"^(?:remember|do\s+not\s+forget|i\s+want\s+you\s+to\s+remember|make\s+a\s+longterm|note\s+longterm)\b", lowered)
        or re.match(r"^(?:save|store|keep|add)\b.*\blongterm\b", lowered)
    )


def _rejected(reason: str, *, protected: bool = False, fact_text: str = "") -> GeneralMemoryParse:
    return GeneralMemoryParse(
        True,
        action="reject",
        fact_text=fact_text,
        extracted_phrase=fact_text,
        rejection_reason=reason,
        protected=protected,
    )


def _clean_fact_text(value: str) -> str:
    source = _clean_fragment(value)
    return re.sub(r"^that\s+", "", source, flags=re.IGNORECASE).strip()


def _clean_fragment(value: str) -> str:
    source = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(source.strip().split()).rstrip(" ?!.,;:").strip()


def _clean_identifier_or_phrase(value: Any, label: str, *, identifier: bool = False) -> str:
    clean = _clean_fragment(str(value or ""))
    if not clean or len(clean) > 120 or any(unicodedata.category(character) == _CONTROL_CATEGORY for character in clean):
        raise GeneralMemoryValidationError(f"invalid_memory_{label}", f"General memory {label} is invalid.")
    if identifier and not re.fullmatch(r"[a-z][a-z0-9_]*", clean):
        raise GeneralMemoryValidationError(f"invalid_memory_{label}", f"General memory {label} is invalid.")
    return clean


def _identifier(value: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", str(value or "")).casefold()))


def _semantic_tokens(value: str, *, include_equivalents: bool = True) -> Tuple[str, ...]:
    tokens = []
    for raw in re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", str(value or "")).casefold()):
        candidates = [_stem_semantic_token(raw)]
        if include_equivalents and raw.endswith("ing"):
            root = raw[:-3]
            if len(root) == 3:
                candidates.append(f"{root}e")
        for token in candidates:
            if token in _STOP_WORDS or len(token) < 2:
                continue
            if token not in tokens:
                tokens.append(token)
    return tuple(tokens)


def _stem_semantic_token(token: str) -> str:
    """Apply bounded grammatical normalization without domain-specific aliases."""

    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ied"):
        return f"{token[:-3]}y"
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _first_person_to_owner(text: str, *, third_person: bool) -> str:
    source = _clean_fragment(text)
    if third_person:
        return f"the owner {source}"
    return f"you {source}"
