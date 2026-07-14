from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Tuple


OWNER_MEMORY_SAVE = "save"
OWNER_MEMORY_UPDATE = "update"
OWNER_MEMORY_RECALL = "recall"
OWNER_MEMORY_FORGET = "forget"
OWNER_MEMORY_LIST = "list"
OWNER_MEMORY_REJECT = "reject"

OWNER_MEMORY_EXPLICIT_RULE = "owner_memory_explicit_v1"
OWNER_MEMORY_DECLARATIVE_RULE = "owner_memory_declarative_v1"
OWNER_MEMORY_UPDATE_RULE = "owner_memory_update_v1"
OWNER_MEMORY_DELETE_RULE = "owner_memory_delete_v1"
OWNER_MEMORY_WHISPER_ALIAS_RULE = "owner_memory_whisper_alias_v1"
OWNER_MEMORY_GENERAL_RULE = "owner_memory_general_v2"
OWNER_MEMORY_LIST_RULE = "owner_memory_list_v2"

MAX_OWNER_FACT_KEY_SOURCE_LENGTH = 120
MAX_OWNER_FACT_KEY_LENGTH = 64
MAX_OWNER_FACT_VALUE_LENGTH = 256
MAX_OWNER_FACT_LIST_LENGTH = 10
SENSITIVE_ENTITY_FIELDS = "_sensitive_fields"

_SAVE_PATTERNS = (
    (
        re.compile(
            r"^remember(?:\s+that)?\s+my\s+(?P<key>.+?)\s+is\s+(?P<value>.+)$",
            re.IGNORECASE,
        ),
        OWNER_MEMORY_EXPLICIT_RULE,
    ),
    (
        re.compile(
            r"^remember\s+that\s+(?P<key>the\s+.+?\bmy\b.+?)\s+is\s+(?P<value>.+)$",
            re.IGNORECASE,
        ),
        OWNER_MEMORY_GENERAL_RULE,
    ),
    (
        re.compile(
            r"^(?:save|store|note)(?:\s+that)?\s+my\s+"
            r"(?P<key>.+?)\s+is\s+(?P<value>.+)$",
            re.IGNORECASE,
        ),
        OWNER_MEMORY_GENERAL_RULE,
    ),
    (
        re.compile(
            r"^my\s+(?P<key>.+?)\s+is\s+(?P<value>.+?)[,;:]?\s+remember\s+that$",
            re.IGNORECASE,
        ),
        OWNER_MEMORY_GENERAL_RULE,
    ),
    (
        re.compile(
            r"^remember\s+(?P<value>.+?)\s+as\s+my\s+(?P<key>.+)$",
            re.IGNORECASE,
        ),
        OWNER_MEMORY_GENERAL_RULE,
    ),
    # Backward-compatible explicit-profile form from the first owner-memory checkpoint.
    (
        re.compile(
            r"^my\s+(?P<key>favorite\s+colou?r)\s+is\s+(?P<value>.+)$",
            re.IGNORECASE,
        ),
        OWNER_MEMORY_DECLARATIVE_RULE,
    ),
    (
        re.compile(
            r"^my\s+(?P<key>.+?)\s+is\s+(?P<value>.+)$",
            re.IGNORECASE,
        ),
        OWNER_MEMORY_DECLARATIVE_RULE,
    ),
)
_LIVE_PATTERN = re.compile(
    r"^remember(?:\s+that)?\s+i\s+live\s+in\s+(?P<value>.+)$",
    re.IGNORECASE,
)
_WORK_PATTERN = re.compile(
    r"^remember(?:\s+that)?\s+i\s+work\s+(?P<value>.+)$",
    re.IGNORECASE,
)
_UPDATE_PATTERNS = (
    re.compile(r"^(?:change|update)\s+my\s+(?P<key>.+?)\s+to\s+(?P<value>.+)$", re.IGNORECASE),
    re.compile(r"^replace\s+my\s+(?P<key>.+?)\s+with\s+(?P<value>.+)$", re.IGNORECASE),
    re.compile(r"^my\s+(?P<key>.+?)\s+is\s+now\s+(?P<value>.+)$", re.IGNORECASE),
)
_WHISPER_ALIAS_SAVE_PATTERN = re.compile(
    r"^remember(?:\s+that)?\s+(?P<key>modified\s+white\s+colou?r)\s+is\s+(?P<value>.+)$",
    re.IGNORECASE,
)
_RECALL_PATTERNS = (
    re.compile(r"^(?:what\s+is|what's|when\s+is)\s+my\s+(?P<key>.+)$", re.IGNORECASE),
    re.compile(r"^do\s+you\s+remember\s+my\s+(?P<key>.+)$", re.IGNORECASE),
    re.compile(r"^what\s+did\s+i\s+tell\s+you\s+about\s+my\s+(?P<key>.+)$", re.IGNORECASE),
)
_FORGET_PATTERNS = (
    (re.compile(r"^(?:forget|delete)\s+my\s+(?P<key>.+)$", re.IGNORECASE), OWNER_MEMORY_DELETE_RULE),
    (re.compile(r"^remove\s+my\s+(?P<key>.+?)\s+from\s+memory$", re.IGNORECASE), OWNER_MEMORY_DELETE_RULE),
    (re.compile(r"^do\s+not\s+remember\s+my\s+(?P<key>.+?)\s+anymore$", re.IGNORECASE), OWNER_MEMORY_DELETE_RULE),
)
_LIST_PHRASES = {
    "what do you remember about me",
    "list what you know about me",
    "show my saved facts",
    "tell me what you remember about me",
}
_SPECIAL_RECALL_KEYS = {
    "where do i live": "city",
    "where am i from": "city",
    "what game do i like": "favorite_game",
    "what work schedule did i tell you": "work_schedule",
}
_OWNER_PREFIXES = (
    "remember that my",
    "remember my",
    "save that my",
    "save my",
    "store that my",
    "store my",
    "note that my",
    "note my",
    "what is my",
    "what's my",
    "when is my",
    "what did i tell you about my",
    "do you remember my",
    "forget my",
    "delete my",
    "remove my",
    "update my",
    "change my",
    "replace my",
)
_WHISPER_KEY_ALIASES = {
    "modified white color": "favorite color",
    "modified white colour": "favorite color",
}
_KEY_ALIASES = {
    "favourite colour": "favorite_color",
    "favorite colour": "favorite_color",
    "favourite color": "favorite_color",
    "favorite color": "favorite_color",
    "birth date": "birthday",
    "date of birth": "birthday",
    "birthday": "birthday",
    "town": "city",
    "home town": "city",
    "home city": "city",
    "location": "city",
    "city": "city",
    "favourite game": "favorite_game",
    "favorite videogame": "favorite_game",
    "favourite videogame": "favorite_game",
    "favorite video game": "favorite_game",
    "favorite game": "favorite_game",
    "favourite music": "favorite_music",
    "favorite music": "favorite_music",
    "dog's name": "dog_name",
    "dog name": "dog_name",
    "pet's name": "pet_name",
    "pet name": "pet_name",
    "work schedule": "work_schedule",
    "work shifts": "work_schedule",
    "shift schedule": "work_schedule",
    "preferred language": "preferred_language",
    "preferred name": "preferred_name",
}
_DISPLAY_NAMES = {
    "city": "city",
    "dog_name": "dog's name",
    "pet_name": "pet's name",
}
_PROTECTED_TOKEN_SEQUENCES: Tuple[Tuple[str, ...], ...] = (
    ("password",), ("passcode",), ("pin",), ("api", "key"),
    ("authentication", "token"), ("access", "token"), ("token",),
    ("private", "key"), ("recovery", "phrase"), ("seed", "phrase"),
    ("bank", "information"), ("bank", "account"), ("account", "number"),
    ("routing", "number"), ("credit", "card"), ("debit", "card"),
    ("card", "number"), ("cvv",), ("iban",),
)
_AMBIGUOUS_VALUE_PATTERN = re.compile(
    r"\b(?:and\s+)?(?:delete|open|run|execute|shutdown|restart)\b|"
    r"\b(?:remember|forget)(?:\s+that)?\s+my\b",
    re.IGNORECASE,
)
_UNSAFE_VALUE_PATTERN = re.compile(
    r"(?:__import__|\b(?:eval|exec)\s*\(|<script\b|\bimport\s+(?:os|sys|subprocess)\b|"
    r"\bignore\s+(?:all\s+)?(?:previous|system)\s+instructions\b|\bsystem\s+prompt\b)",
    re.IGNORECASE,
)


class OwnerMemoryValidationError(ValueError):
    def __init__(self, code: str, message: str, *, normalized_key: str = "", protected: bool = False):
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
    value: Any = ""
    rejection_reason: str = ""
    protected: bool = False
    parser_rule: str = OWNER_MEMORY_EXPLICIT_RULE

    @property
    def safe_raw_text(self) -> str:
        if not self.recognized:
            return ""
        key = self.normalized_key or "facts"
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
        if self.action == OWNER_MEMORY_LIST:
            return "what do you remember about me"
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
        if self.value not in ("", None) and not self.protected:
            entities["value"] = self.value
            entities[SENSITIVE_ENTITY_FIELDS] = ["value"]
        return entities


def parse_owner_memory_command(text: str) -> OwnerMemoryCommand:
    source = _normalize_source(text)
    if not source:
        return OwnerMemoryCommand(False)
    if _contains_control_character(str(text or "")):
        return _rejected_command("control_character_rejected") if _looks_like_owner_prefix(source) else OwnerMemoryCommand(False)

    clean = _strip_owner_address(_normalize_command_punctuation(source))
    clean = clean.rstrip(" \t\r\n?!.,;:").strip()
    lowered = clean.casefold()
    if lowered in _LIST_PHRASES:
        return OwnerMemoryCommand(True, action=OWNER_MEMORY_LIST, parser_rule=OWNER_MEMORY_LIST_RULE)
    if lowered in _SPECIAL_RECALL_KEYS:
        return _key_command(OWNER_MEMORY_RECALL, _SPECIAL_RECALL_KEYS[lowered], parser_rule=OWNER_MEMORY_GENERAL_RULE)

    live_match = _LIVE_PATTERN.fullmatch(clean)
    if live_match:
        return _save_command("city", live_match.group("value"), parser_rule=OWNER_MEMORY_GENERAL_RULE)
    work_match = _WORK_PATTERN.fullmatch(clean)
    if work_match:
        return _save_command("work schedule", work_match.group("value"), parser_rule=OWNER_MEMORY_GENERAL_RULE)

    for pattern in _UPDATE_PATTERNS:
        match = pattern.fullmatch(clean)
        if match:
            return _save_command(match.group("key"), match.group("value"), action=OWNER_MEMORY_UPDATE, parser_rule=OWNER_MEMORY_UPDATE_RULE)

    for pattern, parser_rule in _SAVE_PATTERNS:
        match = pattern.fullmatch(clean)
        if match:
            return _save_command(match.group("key"), match.group("value"), parser_rule=parser_rule)

    whisper_match = _WHISPER_ALIAS_SAVE_PATTERN.fullmatch(clean)
    if whisper_match:
        return _save_command(whisper_match.group("key"), whisper_match.group("value"), parser_rule=OWNER_MEMORY_WHISPER_ALIAS_RULE)

    for pattern in _RECALL_PATTERNS:
        match = pattern.fullmatch(clean)
        if match:
            return _key_command(OWNER_MEMORY_RECALL, match.group("key"), parser_rule=OWNER_MEMORY_GENERAL_RULE)

    for pattern, parser_rule in _FORGET_PATTERNS:
        match = pattern.fullmatch(clean)
        if match:
            return _key_command(OWNER_MEMORY_FORGET, match.group("key"), parser_rule=parser_rule)

    if _looks_like_owner_prefix(clean) or lowered in _LIST_PHRASES:
        return _rejected_command("malformed_owner_memory_command")
    return OwnerMemoryCommand(False)


def owner_memory_uses_explicit_store(command: OwnerMemoryCommand) -> bool:
    if not command.recognized:
        return False
    return not (
        command.parser_rule == OWNER_MEMORY_DECLARATIVE_RULE
        and command.normalized_key != "favorite_color"
    )


def normalize_owner_fact_key(value: str) -> str:
    raw = unicodedata.normalize("NFKC", str(value or ""))
    if _contains_control_character(raw):
        raise OwnerMemoryValidationError("control_character_rejected", "Owner fact keys cannot contain control characters.")
    source = raw.strip().rstrip(" ?!.,;").strip()
    if not source:
        raise OwnerMemoryValidationError("empty_key", "Owner fact key is required.")
    if len(source) > MAX_OWNER_FACT_KEY_SOURCE_LENGTH:
        raise OwnerMemoryValidationError("key_too_long", "Owner fact key is too long.")
    if ".." in source or "/" in source or "\\" in source or ":" in source or "://" in source:
        raise OwnerMemoryValidationError("path_like_key_rejected", "Path-like owner fact keys are not allowed.")

    lowered = _normalize_alias_text(source)
    alias = _KEY_ALIASES.get(lowered)
    if alias:
        normalized = alias
    else:
        if re.search(r"[^a-z0-9\s_'\-.]", lowered):
            raise OwnerMemoryValidationError("unsupported_key_characters", "Owner fact key contains unsupported characters.")
        normalized = "_".join(re.findall(r"[a-z0-9]+", lowered))
    if not normalized:
        raise OwnerMemoryValidationError("empty_key", "Owner fact key is required.")
    if len(normalized) > MAX_OWNER_FACT_KEY_LENGTH:
        raise OwnerMemoryValidationError("key_too_long", "Owner fact key is too long.", normalized_key=normalized)
    if is_protected_owner_fact_key(normalized):
        raise OwnerMemoryValidationError(
            "protected_key_rejected",
            "Protected owner information cannot be stored or recalled through this skill.",
            normalized_key=normalized,
            protected=True,
        )
    return normalized


def normalize_owner_fact_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 10**15:
            raise OwnerMemoryValidationError("numeric_value_out_of_range", "Owner fact number is out of range.")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > 10**15:
            raise OwnerMemoryValidationError("numeric_value_out_of_range", "Owner fact number is out of range.")
        return value
    if isinstance(value, (list, tuple)):
        if not value:
            raise OwnerMemoryValidationError("empty_value", "Owner fact value is required.")
        if len(value) > MAX_OWNER_FACT_LIST_LENGTH:
            raise OwnerMemoryValidationError("list_too_long", "Owner fact list is too long.")
        normalized = []
        for item in value:
            if isinstance(item, (list, tuple, dict, set)) or item is None:
                raise OwnerMemoryValidationError("unsupported_value_type", "Owner fact lists may contain only simple values.")
            normalized.append(normalize_owner_fact_value(item))
        return normalized
    if not isinstance(value, str):
        raise OwnerMemoryValidationError("unsupported_value_type", "Owner facts must be simple scalar values or short lists.")

    source = unicodedata.normalize("NFKC", value)
    if _contains_control_character(source):
        raise OwnerMemoryValidationError("control_character_rejected", "Owner fact values cannot contain control characters.")
    clean = " ".join(source.strip().split()).rstrip(" ?!.,;:").strip()
    if not clean:
        raise OwnerMemoryValidationError("empty_value", "Owner fact value is required.")
    if len(clean) > MAX_OWNER_FACT_VALUE_LENGTH:
        raise OwnerMemoryValidationError("value_too_long", "Owner fact value is too long.")
    if _AMBIGUOUS_VALUE_PATTERN.search(clean):
        raise OwnerMemoryValidationError("ambiguous_value_rejected", "Owner fact value contains another command.")
    if _UNSAFE_VALUE_PATTERN.search(clean):
        raise OwnerMemoryValidationError("unsafe_value_rejected", "Owner fact value contains unsafe executable or instruction content.")
    return clean


def owner_fact_display_name(normalized_key: str) -> str:
    key = str(normalized_key or "")
    return _DISPLAY_NAMES.get(key, " ".join(part for part in key.split("_") if part))


def is_protected_owner_fact_key(normalized_key: str) -> bool:
    tokens = tuple(part for part in str(normalized_key or "").split("_") if part)
    if not tokens:
        return False
    return any(_contains_token_sequence(tokens, protected) for protected in _PROTECTED_TOKEN_SEQUENCES) or ("api" in tokens and "key" in tokens)


def _save_command(key_source: str, value_source: Any, *, action: str = OWNER_MEMORY_SAVE, parser_rule: str = OWNER_MEMORY_EXPLICIT_RULE) -> OwnerMemoryCommand:
    key_source, alias_applied = _canonicalize_owner_key_source(key_source)
    if alias_applied:
        parser_rule = OWNER_MEMORY_WHISPER_ALIAS_RULE
    try:
        key = normalize_owner_fact_key(key_source)
        value = normalize_owner_fact_value(value_source)
    except OwnerMemoryValidationError as error:
        return _rejected_command(error.code, normalized_key=error.normalized_key, protected=error.protected)
    return OwnerMemoryCommand(True, action=action, normalized_key=key, display_key=owner_fact_display_name(key), value=value, parser_rule=parser_rule)


def _key_command(action: str, key_source: str, *, parser_rule: str = OWNER_MEMORY_EXPLICIT_RULE) -> OwnerMemoryCommand:
    key_source, alias_applied = _canonicalize_owner_key_source(key_source)
    if alias_applied:
        parser_rule = OWNER_MEMORY_WHISPER_ALIAS_RULE
    try:
        key = normalize_owner_fact_key(key_source)
    except OwnerMemoryValidationError as error:
        return _rejected_command(error.code, normalized_key=error.normalized_key, protected=error.protected)
    return OwnerMemoryCommand(True, action=action, normalized_key=key, display_key=owner_fact_display_name(key), parser_rule=parser_rule)


def _rejected_command(reason: str, *, normalized_key: str = "", protected: bool = False) -> OwnerMemoryCommand:
    return OwnerMemoryCommand(True, action=OWNER_MEMORY_REJECT, normalized_key=normalized_key, display_key=owner_fact_display_name(normalized_key), rejection_reason=reason, protected=protected)


def _normalize_source(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = normalized.translate(str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'}))
    return " ".join(normalized.strip().split())


def _normalize_command_punctuation(value: str) -> str:
    source = str(value or "")
    source = re.sub(r"\s*[,;:]\s*", " ", source)
    return " ".join(source.split())


def _strip_owner_address(value: str) -> str:
    return re.sub(r"^(?:(?:hello|hey|hi)\s+)?ares\s+", "", value, flags=re.IGNORECASE).strip()


def _normalize_alias_text(value: str) -> str:
    source = unicodedata.normalize("NFKC", str(value or "")).casefold()
    source = source.replace("\u2019", "'").replace("favourite", "favorite").replace("colour", "color")
    return " ".join(source.strip().split()).rstrip(" ?!.,;")


def _canonicalize_owner_key_source(value: str) -> tuple[str, bool]:
    source = " ".join(str(value or "").strip().split())
    alias = _WHISPER_KEY_ALIASES.get(source.casefold())
    return (alias, True) if alias else (source, False)


def _looks_like_owner_prefix(value: str) -> bool:
    lowered = _strip_owner_address(str(value or "")).casefold()
    if any(lowered == prefix or lowered.startswith(f"{prefix} ") for prefix in _OWNER_PREFIXES):
        return True
    if lowered in _LIST_PHRASES or lowered in _SPECIAL_RECALL_KEYS:
        return True
    if re.match(r"^remember(?:\s+that)?\s+i\s+(?:live|work)(?:\s|$)", lowered):
        return True
    if lowered.startswith("my ") and (" is now " in lowered or lowered.endswith(" remember that")):
        return True
    return bool(re.match(r"^remember(?:\s+that)?\s+modified\s+white\s+colou?r(?:\s|$)", lowered))


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in str(value or ""))


def _contains_token_sequence(tokens: Tuple[str, ...], sequence: Tuple[str, ...]) -> bool:
    size = len(sequence)
    return any(tokens[index:index + size] == sequence for index in range(len(tokens) - size + 1))
