from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Tuple


OWNER_MEMORY_SAVE = "save"
OWNER_MEMORY_UPDATE = "update"
OWNER_MEMORY_RECALL = "recall"
OWNER_MEMORY_FORGET = "forget"
OWNER_MEMORY_REJECT = "reject"

OWNER_MEMORY_EXPLICIT_RULE = "owner_memory_explicit_v1"
OWNER_MEMORY_DECLARATIVE_RULE = "owner_memory_declarative_v1"
OWNER_MEMORY_UPDATE_RULE = "owner_memory_update_v1"
OWNER_MEMORY_DELETE_RULE = "owner_memory_delete_v1"
OWNER_MEMORY_WHISPER_ALIAS_RULE = "owner_memory_whisper_alias_v1"

MAX_OWNER_FACT_KEY_SOURCE_LENGTH = 80
MAX_OWNER_FACT_KEY_LENGTH = 64
MAX_OWNER_FACT_VALUE_LENGTH = 256
SENSITIVE_ENTITY_FIELDS = "_sensitive_fields"

_SAVE_PATTERNS = (
    (
        re.compile(
            r"^remember(?:\s+that)?\s+my\s+(?P<key>.+?)\s+is\s+(?P<value>.+)$",
            flags=re.IGNORECASE,
        ),
        OWNER_MEMORY_SAVE,
        OWNER_MEMORY_EXPLICIT_RULE,
    ),
    (
        re.compile(
            r"^my\s+(?P<key>.+?)\s+is\s+(?P<value>.+)$",
            flags=re.IGNORECASE,
        ),
        OWNER_MEMORY_SAVE,
        OWNER_MEMORY_DECLARATIVE_RULE,
    ),
    (
        re.compile(
            r"^update\s+my\s+(?P<key>.+?)\s+to\s+(?P<value>.+)$",
            flags=re.IGNORECASE,
        ),
        OWNER_MEMORY_UPDATE,
        OWNER_MEMORY_UPDATE_RULE,
    ),
)
_WHISPER_ALIAS_SAVE_PATTERN = re.compile(
    r"^remember(?:\s+that)?\s+"
    r"(?P<key>modified\s+white\s+colou?r)\s+is\s+(?P<value>.+)$",
    flags=re.IGNORECASE,
)
_RECALL_PATTERNS = (
    re.compile(r"^(?:what\s+is|what's)\s+my\s+(?P<key>.+)$", re.IGNORECASE),
    re.compile(r"^do\s+you\s+remember\s+my\s+(?P<key>.+)$", re.IGNORECASE),
)
_FORGET_PATTERNS = (
    (
        re.compile(r"^forget\s+my\s+(?P<key>.+)$", re.IGNORECASE),
        OWNER_MEMORY_EXPLICIT_RULE,
    ),
    (
        re.compile(r"^delete\s+my\s+(?P<key>.+)$", re.IGNORECASE),
        OWNER_MEMORY_DELETE_RULE,
    ),
)
_OWNER_PREFIXES = (
    "remember that my",
    "remember my",
    "what is my",
    "what's my",
    "do you remember my",
    "forget my",
    "delete my",
    "update my",
)
_WHISPER_KEY_ALIASES = {
    # Exact, owner-command-only correction observed from the local Whisper model.
    "modified white color": "favorite color",
    "modified white colour": "favorite color",
}
_PROTECTED_TOKEN_SEQUENCES: Tuple[Tuple[str, ...], ...] = (
    ("password",),
    ("passcode",),
    ("pin",),
    ("api", "key"),
    ("authentication", "token"),
    ("access", "token"),
    ("token",),
    ("private", "key"),
    ("recovery", "phrase"),
    ("seed", "phrase"),
    ("bank", "information"),
    ("bank", "account"),
    ("account", "number"),
    ("routing", "number"),
    ("credit", "card"),
    ("debit", "card"),
    ("card", "number"),
    ("cvv",),
    ("iban",),
)
_AMBIGUOUS_VALUE_PATTERN = re.compile(
    r"\b(?:and\s+)?(?:delete|open|run|execute|shutdown|restart)\b|"
    r"\b(?:remember|forget)(?:\s+that)?\s+my\b",
    flags=re.IGNORECASE,
)


class OwnerMemoryValidationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        normalized_key: str = "",
        protected: bool = False,
    ):
        super().__init__(message)
        self.code = str(code or "owner_memory_validation_failed")
        self.message = str(message or "Owner memory validation failed.")
        self.normalized_key = str(normalized_key or "")
        self.protected = bool(protected)


@dataclass(frozen=True)
class OwnerMemoryCommand:
    recognized: bool
    action: str = ""
    normalized_key: str = ""
    display_key: str = ""
    value: str = ""
    rejection_reason: str = ""
    protected: bool = False
    parser_rule: str = OWNER_MEMORY_EXPLICIT_RULE

    @property
    def safe_raw_text(self) -> str:
        if not self.recognized:
            return ""
        key = self.normalized_key or "fact"
        return f"owner memory {self.action or OWNER_MEMORY_REJECT} {key}"

    @property
    def routing_text(self) -> str:
        if not self.recognized or self.action == OWNER_MEMORY_REJECT:
            return ""
        key = self.display_key or owner_fact_display_name(self.normalized_key)
        if self.action == OWNER_MEMORY_SAVE:
            return f"remember that my {key} is {self.value}"
        if self.action == OWNER_MEMORY_UPDATE:
            return f"update my {key} to {self.value}"
        if self.action == OWNER_MEMORY_RECALL:
            return f"what is my {key}"
        if self.action == OWNER_MEMORY_FORGET:
            return f"forget my {key}"
        return ""

    def to_entities(self) -> Dict[str, Any]:
        entities: Dict[str, Any] = {
            "action": self.action,
            "normalized_key": self.normalized_key,
            "display_key": self.display_key,
            "protected": self.protected,
            "parser_rule": self.parser_rule,
        }
        if self.rejection_reason:
            entities["rejection_reason"] = self.rejection_reason
        if self.value and not self.protected:
            entities["value"] = self.value
            entities[SENSITIVE_ENTITY_FIELDS] = ["value"]
        return entities


def parse_owner_memory_command(text: str) -> OwnerMemoryCommand:
    source = _normalize_source(text)
    if not source:
        return OwnerMemoryCommand(False)

    if _contains_control_character(str(text or "")):
        if _looks_like_owner_prefix(source):
            return _rejected_command("control_character_rejected")
        return OwnerMemoryCommand(False)

    clean = _normalize_command_punctuation(source).rstrip(" \t\r\n?!.,;:").strip()
    for pattern, action, parser_rule in _SAVE_PATTERNS:
        save_match = pattern.fullmatch(clean)
        if save_match:
            return _save_command(
                save_match.group("key"),
                save_match.group("value"),
                action=action,
                parser_rule=parser_rule,
            )

    whisper_match = _WHISPER_ALIAS_SAVE_PATTERN.fullmatch(clean)
    if whisper_match:
        return _save_command(
            whisper_match.group("key"),
            whisper_match.group("value"),
            action=OWNER_MEMORY_SAVE,
            parser_rule=OWNER_MEMORY_WHISPER_ALIAS_RULE,
        )

    for pattern in _RECALL_PATTERNS:
        match = pattern.fullmatch(clean)
        if match:
            return _key_command(OWNER_MEMORY_RECALL, match.group("key"))

    for pattern, parser_rule in _FORGET_PATTERNS:
        forget_match = pattern.fullmatch(clean)
        if forget_match:
            return _key_command(
                OWNER_MEMORY_FORGET,
                forget_match.group("key"),
                parser_rule=parser_rule,
            )

    if _looks_like_owner_prefix(clean):
        return _rejected_command("malformed_owner_memory_command")
    return OwnerMemoryCommand(False)


def owner_memory_uses_explicit_store(command: OwnerMemoryCommand) -> bool:
    if not command.recognized:
        return False
    if command.normalized_key in {"name", "birthday"}:
        return False
    if (
        command.parser_rule == OWNER_MEMORY_DECLARATIVE_RULE
        and command.normalized_key != "favorite_color"
    ):
        return False
    if (
        command.action == OWNER_MEMORY_RECALL
        and command.normalized_key.startswith("favorite_")
        and command.normalized_key != "favorite_color"
    ):
        return False
    return True


def normalize_owner_fact_key(value: str) -> str:
    raw = unicodedata.normalize("NFKC", str(value or ""))
    if _contains_control_character(raw):
        raise OwnerMemoryValidationError(
            "control_character_rejected",
            "Owner fact keys cannot contain control characters.",
        )
    source = raw.strip().rstrip(" ?!.,;").strip()
    if not source:
        raise OwnerMemoryValidationError("empty_key", "Owner fact key is required.")
    if len(source) > MAX_OWNER_FACT_KEY_SOURCE_LENGTH:
        raise OwnerMemoryValidationError(
            "key_too_long",
            "Owner fact key is too long.",
        )
    if ".." in source or "/" in source or "\\" in source or ":" in source:
        raise OwnerMemoryValidationError(
            "path_like_key_rejected",
            "Path-like owner fact keys are not allowed.",
        )

    lowered = source.casefold().replace("favourite", "favorite").replace("colour", "color")
    if re.search(r"[^a-z0-9\s_'\-.]", lowered):
        raise OwnerMemoryValidationError(
            "unsupported_key_characters",
            "Owner fact key contains unsupported characters.",
        )
    words = re.findall(r"[a-z0-9]+", lowered)
    normalized = "_".join(words)
    if not normalized:
        raise OwnerMemoryValidationError("empty_key", "Owner fact key is required.")
    if len(normalized) > MAX_OWNER_FACT_KEY_LENGTH:
        raise OwnerMemoryValidationError(
            "key_too_long",
            "Owner fact key is too long.",
            normalized_key=normalized,
        )
    if is_protected_owner_fact_key(normalized):
        raise OwnerMemoryValidationError(
            "protected_key_rejected",
            "Protected owner information cannot be stored or recalled through this skill.",
            normalized_key=normalized,
            protected=True,
        )
    return normalized


def normalize_owner_fact_value(value: str) -> str:
    source = unicodedata.normalize("NFKC", str(value or ""))
    if _contains_control_character(source):
        raise OwnerMemoryValidationError(
            "control_character_rejected",
            "Owner fact values cannot contain control characters.",
        )
    clean = " ".join(source.strip().split()).rstrip(" ?!.,;:").strip()
    if not clean:
        raise OwnerMemoryValidationError("empty_value", "Owner fact value is required.")
    if len(clean) > MAX_OWNER_FACT_VALUE_LENGTH:
        raise OwnerMemoryValidationError(
            "value_too_long",
            "Owner fact value is too long.",
        )
    if _AMBIGUOUS_VALUE_PATTERN.search(clean):
        raise OwnerMemoryValidationError(
            "ambiguous_value_rejected",
            "Owner fact value contains another command.",
        )
    return clean


def owner_fact_display_name(normalized_key: str) -> str:
    return " ".join(part for part in str(normalized_key or "").split("_") if part)


def is_protected_owner_fact_key(normalized_key: str) -> bool:
    tokens = tuple(part for part in str(normalized_key or "").split("_") if part)
    if not tokens:
        return False
    for protected in _PROTECTED_TOKEN_SEQUENCES:
        if _contains_token_sequence(tokens, protected):
            return True
    return "api" in tokens and "key" in tokens


def _save_command(
    key_source: str,
    value_source: str,
    *,
    action: str = OWNER_MEMORY_SAVE,
    parser_rule: str = OWNER_MEMORY_EXPLICIT_RULE,
) -> OwnerMemoryCommand:
    key_source, alias_applied = _canonicalize_owner_key_source(key_source)
    if alias_applied:
        parser_rule = OWNER_MEMORY_WHISPER_ALIAS_RULE
    try:
        key = normalize_owner_fact_key(key_source)
        value = normalize_owner_fact_value(value_source)
    except OwnerMemoryValidationError as error:
        return _rejected_command(
            error.code,
            normalized_key=error.normalized_key,
            protected=error.protected,
        )
    return OwnerMemoryCommand(
        True,
        action=action,
        normalized_key=key,
        display_key=owner_fact_display_name(key),
        value=value,
        parser_rule=parser_rule,
    )


def _key_command(
    action: str,
    key_source: str,
    *,
    parser_rule: str = OWNER_MEMORY_EXPLICIT_RULE,
) -> OwnerMemoryCommand:
    key_source, alias_applied = _canonicalize_owner_key_source(key_source)
    if alias_applied:
        parser_rule = OWNER_MEMORY_WHISPER_ALIAS_RULE
    try:
        key = normalize_owner_fact_key(key_source)
    except OwnerMemoryValidationError as error:
        return _rejected_command(
            error.code,
            normalized_key=error.normalized_key,
            protected=error.protected,
        )
    return OwnerMemoryCommand(
        True,
        action=action,
        normalized_key=key,
        display_key=owner_fact_display_name(key),
        parser_rule=parser_rule,
    )


def _rejected_command(
    reason: str,
    *,
    normalized_key: str = "",
    protected: bool = False,
) -> OwnerMemoryCommand:
    return OwnerMemoryCommand(
        True,
        action=OWNER_MEMORY_REJECT,
        normalized_key=normalized_key,
        display_key=owner_fact_display_name(normalized_key),
        rejection_reason=reason,
        protected=protected,
    )


def _normalize_source(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = normalized.translate(str.maketrans({"\u2018": "'", "\u2019": "'"}))
    return " ".join(normalized.strip().split())


def _normalize_command_punctuation(value: str) -> str:
    source = str(value or "")
    source = re.sub(
        r"^(remember(?:\s+that)?|what\s+is|do\s+you\s+remember|forget|delete|update)"
        r"\s*[,;:]\s*",
        r"\1 ",
        source,
        flags=re.IGNORECASE,
    )
    source = re.sub(r"\bmy\s*[,;:]\s*", "my ", source, flags=re.IGNORECASE)
    return " ".join(source.split())


def _canonicalize_owner_key_source(value: str) -> tuple[str, bool]:
    source = " ".join(str(value or "").strip().split())
    alias = _WHISPER_KEY_ALIASES.get(source.casefold())
    return (alias, True) if alias else (source, False)


def _looks_like_owner_prefix(value: str) -> bool:
    lowered = str(value or "").casefold()
    if any(
        lowered == prefix or lowered.startswith(f"{prefix} ")
        for prefix in _OWNER_PREFIXES
    ):
        return True
    if lowered.startswith("my ") and " is " in lowered:
        return True
    return bool(
        re.match(
            r"^remember(?:\s+that)?\s+modified\s+white\s+colou?r(?:\s|$)",
            lowered,
        )
    )


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in str(value or ""))


def _contains_token_sequence(tokens: Tuple[str, ...], sequence: Tuple[str, ...]) -> bool:
    size = len(sequence)
    return any(tokens[index : index + size] == sequence for index in range(len(tokens) - size + 1))
