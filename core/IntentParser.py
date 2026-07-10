import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

from core.DeviceAction import DANGER_SAFE, classify_device_action
from core.Intent import Intent


@dataclass(frozen=True)
class ParsedText:
    raw_text: str
    normalized: str
    tokens: List[str]

    def is_exact(self, *phrases: str) -> bool:
        return self.normalized in {_normalize(phrase) for phrase in phrases}

    def starts_with(self, phrase: str) -> bool:
        prefix = _normalize(phrase)
        return self.normalized == prefix or self.normalized.startswith(f"{prefix} ")

    def after(self, phrase: str) -> str:
        if not self.starts_with(phrase):
            return ""
        return self.raw_text.strip()[len(phrase) :].strip(" :-.\t")


class IntentRule:
    def __init__(self, name: str, parser: Callable[[ParsedText], Optional[Intent]]):
        self.name = name
        self.parser = parser

    def parse(self, text: ParsedText) -> Optional[Intent]:
        return self.parser(text)


class IntentParser:
    def __init__(self, rules: Optional[Iterable[IntentRule]] = None):
        self.rules = list(rules) if rules is not None else [
            IntentRule("goal", self._parse_goal),
            IntentRule("note", self._parse_note),
            IntentRule("task", self._parse_task),
            IntentRule("device_action", self._parse_device_action),
            IntentRule("event_history", self._parse_event_history),
            IntentRule("voice_session", self._parse_voice_session),
            IntentRule("calculate", self._parse_calculate),
            IntentRule("memory_recall", self._parse_memory_recall),
            IntentRule("weather", self._parse_weather),
            IntentRule("market", self._parse_market),
            IntentRule("calendar", self._parse_calendar),
            IntentRule("time_date", self._parse_time_date),
        ]

    def parse(self, text: str) -> Intent:
        parsed = ParsedText(
            raw_text=(text or "").strip(),
            normalized=_normalize(text),
            tokens=_tokens(text),
        )

        if not parsed.normalized:
            return self._unknown(parsed.raw_text)

        for rule in self.rules:
            intent = rule.parse(parsed)
            if intent:
                return intent

        return self._unknown(parsed.raw_text)

    def _parse_goal(self, text: ParsedText) -> Optional[Intent]:
        if _is_next_goal_question(text.normalized):
            return self._intent("goal", 0.9, text.raw_text, action="next")

        if text.is_exact("list goals", "show goals", "list my goals", "show my goals"):
            return self._intent("goal", 0.96, text.raw_text, action="list")

        if text.starts_with("show goal"):
            goal_id = _first_word(text.after("show goal"))
            return self._intent("goal", 0.94, text.raw_text, action="show", goal_id=goal_id)

        if text.starts_with("complete goal"):
            goal_id = _first_word(text.after("complete goal"))
            return self._intent("goal", 0.94, text.raw_text, action="complete", goal_id=goal_id)

        if text.starts_with("pause goal"):
            goal_id = _first_word(text.after("pause goal"))
            return self._intent("goal", 0.94, text.raw_text, action="pause", goal_id=goal_id)

        if text.starts_with("delete goal"):
            goal_id = _first_word(text.after("delete goal"))
            return self._intent("goal", 0.94, text.raw_text, action="delete", goal_id=goal_id)

        if text.starts_with("add milestone to goal"):
            goal_id, milestone = _split_first_word(text.after("add milestone to goal"))
            return self._intent(
                "goal",
                0.94,
                text.raw_text,
                action="add_milestone",
                goal_id=goal_id,
                milestone=milestone,
            )

        if text.starts_with("add goal"):
            title, description, priority = _split_goal_fields(text.after("add goal"))
            return self._intent(
                "goal",
                0.94,
                text.raw_text,
                action="add",
                title=title,
                description=description,
                priority=priority,
            )

        return None

    def _parse_note(self, text: ParsedText) -> Optional[Intent]:
        if text.is_exact("list my notes", "show my notes"):
            return self._intent("note", 0.96, text.raw_text, action="list")

        notes_about_keyword = _notes_about_keyword(text.raw_text)
        if notes_about_keyword is not None:
            return self._intent("note", 0.9, text.raw_text, action="search", keyword=notes_about_keyword)

        if text.is_exact("delete all notes"):
            return self._intent("note", 0.96, text.raw_text, action="delete_all_request")

        if text.is_exact("confirm delete all notes", "delete all notes confirm", "delete all notes confirmed"):
            return self._intent("note", 0.96, text.raw_text, action="delete_all_confirm")

        if text.starts_with("search notes"):
            keyword = _clean_text(text.after("search notes"))
            return self._intent("note", 0.94, text.raw_text, action="search", keyword=keyword)

        if text.starts_with("delete note"):
            note_id = _first_word(text.after("delete note"))
            return self._intent("note", 0.94, text.raw_text, action="delete", note_id=note_id)

        for phrase in ("remember this", "save note", "take a note"):
            if text.starts_with(phrase):
                note_text = _clean_text(text.after(phrase))
                return self._intent("note", 0.94, text.raw_text, action="add", text=note_text)

        return None

    def _parse_task(self, text: ParsedText) -> Optional[Intent]:
        if text.is_exact("list tasks", "show tasks"):
            return self._intent("task", 0.96, text.raw_text, action="list")

        if text.is_exact("clear completed tasks"):
            return self._intent("task", 0.96, text.raw_text, action="clear_completed")

        if text.starts_with("mark task") and text.normalized.endswith(" done"):
            task_id = _first_word(text.after("mark task"))
            return self._intent("task", 0.94, text.raw_text, action="mark_done", task_id=task_id)

        if text.starts_with("delete task"):
            task_id = _first_word(text.after("delete task"))
            return self._intent("task", 0.94, text.raw_text, action="delete", task_id=task_id)

        for phrase in ("add task", "remind me to", "remind me about", "remember"):
            if text.starts_with(phrase):
                task_text, due = _split_due_text(_strip_leading_task_marker(text.after(phrase)))
                return self._intent("task", 0.9, text.raw_text, action="add", text=task_text, due=due)

        return None

    def _parse_device_action(self, text: ParsedText) -> Optional[Intent]:
        if text.is_exact("list apps", "show apps", "list available apps"):
            return self._intent(
                "device_action",
                0.96,
                text.raw_text,
                action="list",
                action_name="list_apps",
                parameters={},
                danger_classification=DANGER_SAFE,
            )

        if text.starts_with("open app"):
            app_id = _normalize_action_name(text.after("open app"))
            safety = classify_device_action("open_app")
            parameters = {"app_id": app_id} if app_id else {}
            return self._intent(
                "device_action",
                0.96,
                text.raw_text,
                action=safety.classification,
                action_name=safety.action_name,
                app_id=app_id,
                parameters=parameters,
                danger_classification=safety.classification,
                confirmation_required=safety.requires_confirmation,
                forbidden=safety.forbidden,
                reason=safety.reason,
            )

        dangerous_action_name = _dangerous_device_action_name(text.normalized)
        if dangerous_action_name:
            safety = classify_device_action(dangerous_action_name)
            return self._intent(
                "device_action",
                0.96,
                text.raw_text,
                action=safety.classification,
                action_name=safety.action_name,
                danger_classification=safety.classification,
                confirmation_required=safety.requires_confirmation,
                forbidden=safety.forbidden,
                reason=safety.reason,
            )

        if text.starts_with("echo"):
            message = _clean_text(text.after("echo"))
            if message:
                return self._intent(
                    "device_action",
                    0.94,
                    text.raw_text,
                    action="echo",
                    action_name="echo",
                    parameters={"message": message},
                    danger_classification=DANGER_SAFE,
                )

        if text.is_exact("list device actions", "show device actions", "list available device actions"):
            return self._intent(
                "device_action",
                0.96,
                text.raw_text,
                action="list",
                action_name="list_actions",
                parameters={},
                danger_classification=DANGER_SAFE,
            )

        if text.is_exact("system status", "device status"):
            return self._intent(
                "device_action",
                0.94,
                text.raw_text,
                action="status",
                action_name="system_status_mock",
                parameters={},
                danger_classification=DANGER_SAFE,
            )

        if text.starts_with("device action"):
            action_name = _normalize_action_name(text.after("device action"))
            if action_name:
                safety = classify_device_action(action_name)
                return self._intent(
                    "device_action",
                    0.76,
                    text.raw_text,
                    action="execute",
                    action_name=action_name,
                    parameters={},
                    danger_classification=safety.classification,
                    confirmation_required=safety.requires_confirmation,
                    forbidden=safety.forbidden,
                    reason=safety.reason,
                )

        return None

    def _parse_event_history(self, text: ParsedText) -> Optional[Intent]:
        if text.is_exact("what happened recently", "show recent events"):
            return self._intent("event_history", 0.96, text.raw_text, action="recent", query_type="recent")

        if text.is_exact("show critical events"):
            return self._intent(
                "event_history",
                0.96,
                text.raw_text,
                action="critical",
                query_type="critical",
                priority="critical",
            )

        return None

    def _parse_voice_session(self, text: ParsedText) -> Optional[Intent]:
        if _looks_like_voice_session_status(text.raw_text):
            return self._intent(
                "voice_session",
                0.96,
                text.raw_text,
                action="status",
                query_type="latest",
            )

        if not _looks_like_voice_session(text.raw_text):
            return None

        return self._intent(
            "voice_session",
            0.96,
            text.raw_text,
            action="start",
            max_turns=_voice_session_max_turns(text.raw_text),
        )

    def _parse_calculate(self, text: ParsedText) -> Optional[Intent]:
        for phrase in ("calculate", "calculator", "compute", "solve"):
            if text.starts_with(phrase):
                expression = _clean_text(text.after(phrase))
                return self._intent("calculate", 0.96, text.raw_text, action="calculate", expression=expression)

        for phrase in ("what is", "what's", "how much is"):
            if text.starts_with(phrase):
                expression = _clean_text(text.after(phrase))
                if _looks_like_arithmetic(expression):
                    return self._intent("calculate", 0.9, text.raw_text, action="calculate", expression=expression)

        if _looks_like_arithmetic(text.raw_text):
            return self._intent("calculate", 0.86, text.raw_text, action="calculate", expression=text.raw_text.strip())

        return None

    def _parse_memory_recall(self, text: ParsedText) -> Optional[Intent]:
        if text.is_exact("what is my name"):
            return self._intent("memory_recall", 0.96, text.raw_text, action="profile_fact", key="name")

        if text.is_exact("where do i live"):
            return self._intent("memory_recall", 0.96, text.raw_text, action="profile_fact", key="location")

        if text.is_exact("when is my birthday"):
            return self._intent("memory_recall", 0.96, text.raw_text, action="profile_fact", key="birthday")

        if text.is_exact("what is my birthday"):
            return self._intent("memory_recall", 0.96, text.raw_text, action="profile_fact", key="birthday")

        if text.starts_with("what is my favorite"):
            subject = _clean_text(text.after("what is my favorite").rstrip("?"))
            return self._intent("memory_recall", 0.95, text.raw_text, action="favorite", subject=subject)

        if text.starts_with("what did i tell you about"):
            topic = _clean_text(text.after("what did i tell you about").rstrip("?"))
            return self._intent("memory_recall", 0.82, text.raw_text, action="recall_topic", topic=topic)

        if text.starts_with("what do you remember about"):
            topic = _clean_text(text.after("what do you remember about").rstrip("?"))
            return self._intent("memory_recall", 0.8, text.raw_text, action="recall_topic", topic=topic)

        return None

    def _parse_weather(self, text: ParsedText) -> Optional[Intent]:
        if "weather" not in text.tokens and "forecast" not in text.tokens:
            return None

        period = _weather_period(text)
        location = _weather_location(text.raw_text)
        capability = "weather.forecast" if period == "tomorrow" else "weather.current"
        confidence = 0.94 if "weather" in text.tokens else 0.88

        return self._intent(
            "weather",
            confidence,
            text.raw_text,
            action="weather",
            location=location,
            period=period,
            adapter_name="mock_weather",
            capability=capability,
        )

    def _parse_market(self, text: ParsedText) -> Optional[Intent]:
        if "stock" not in text.tokens and "market" not in text.tokens:
            return None

        symbol = _market_symbol(text.raw_text)
        if not symbol:
            return None

        return self._intent(
            "market",
            0.94,
            text.raw_text,
            action="quote",
            symbol=symbol,
            adapter_name="mock_market",
            capability="market.quote",
        )

    def _parse_calendar(self, text: ParsedText) -> Optional[Intent]:
        calendar_tokens = {"calendar", "schedule"}
        has_calendar_word = bool(calendar_tokens & set(text.tokens))
        asks_anything = text.starts_with("do i have anything")
        if not has_calendar_word and not asks_anything:
            return None

        period = _calendar_period(text)
        return self._intent(
            "calendar",
            0.94,
            text.raw_text,
            action="list",
            period=period,
            adapter_name="mock_calendar",
            capability="calendar.events",
        )

    def _parse_time_date(self, text: ParsedText) -> Optional[Intent]:
        asks_time = any(token in text.tokens for token in ("time", "clock"))
        asks_date = any(token in text.tokens for token in ("date", "today")) or text.starts_with("what day")

        if asks_time and asks_date:
            return self._intent("time_date", 0.94, text.raw_text, query_type="time_date")

        if asks_time:
            return self._intent("time_date", 0.92, text.raw_text, query_type="time")

        if asks_date:
            return self._intent("time_date", 0.92, text.raw_text, query_type="date")

        return None

    def _intent(self, intent_name: str, confidence: float, raw_text: str, **entities) -> Intent:
        clean_entities = {
            key: value
            for key, value in entities.items()
            if value is not None
        }
        return Intent(
            intent_name=intent_name,
            confidence=max(0.0, min(1.0, float(confidence))),
            extracted_entities=clean_entities,
            raw_text=raw_text,
        )

    def _unknown(self, raw_text: str) -> Intent:
        return Intent(
            intent_name="unknown",
            confidence=0.0,
            extracted_entities={},
            raw_text=raw_text,
        )


def _normalize(value: str) -> str:
    return " ".join(_tokens(value))


def _tokens(value: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", (value or "").lower())


def _clean_text(value: str) -> str:
    return (value or "").strip().strip(" ?!.").strip(":- ").strip()


def _is_next_goal_question(value: str) -> bool:
    normalized = _normalize(value).strip()
    return normalized in {
        "what should i do next for my goals",
        "what should i do next for my goal",
        "next for my goals",
        "next for my goal",
    }


def _notes_about_keyword(value: str):
    clean = _clean_text(value)
    patterns = (
        r"^notes\s+about\s+(.+)$",
        r"^show\s+notes\s+about\s+(.+)$",
        r"^show\s+my\s+notes\s+about\s+(.+)$",
        r"\bnotes\s+about\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, clean, flags=re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))
    return None


def _first_word(value: str) -> str:
    return _clean_text(value).split()[0] if _clean_text(value).split() else ""


def _split_first_word(value: str):
    clean = _clean_text(value)
    if not clean:
        return "", ""
    parts = clean.split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], _clean_text(parts[1])


def _split_goal_fields(value: str):
    remaining = _clean_text(value)
    priority = "normal"
    description = ""

    priority_match = re.search(r"\s+priority\s+([a-z0-9_-]+)\s*$", remaining, flags=re.IGNORECASE)
    if priority_match:
        priority = priority_match.group(1).strip()
        remaining = _clean_text(remaining[: priority_match.start()])

    description_match = re.search(r"\s+description\s+(.+)$", remaining, flags=re.IGNORECASE)
    if description_match:
        description = _clean_text(description_match.group(1))
        remaining = _clean_text(remaining[: description_match.start()])

    return remaining, description, priority


def _looks_like_arithmetic(value: str) -> bool:
    text = (value or "").strip().lower()
    has_number = any(char.isdigit() for char in text)
    has_operator = any(operator in text for operator in ("+", "-", "*", "/", "^"))
    has_word_operator = any(word in text.split() for word in ("plus", "minus", "times"))
    allowed = all(char.isdigit() or char in " .()+-*/^" for char in text)
    return has_number and (has_operator or has_word_operator) and (allowed or has_word_operator)


def _split_due_text(value: str):
    text = _clean_text(value)
    if not text:
        return "", None

    due_marker = " due "
    lowered = text.lower()
    if due_marker in lowered:
        index = lowered.index(due_marker)
        task_text = _clean_text(text[:index])
        due = _clean_text(text[index + len(due_marker) :])
        return task_text, due or None

    for suffix in _DUE_SUFFIXES:
        if lowered == suffix:
            return "", suffix
        if lowered.endswith(f" {suffix}"):
            task_text = _clean_text(text[: -len(suffix)])
            return task_text, suffix

    return text, None


def _weather_period(text: ParsedText) -> str:
    if "tomorrow" in text.tokens:
        return "tomorrow"
    if "today" in text.tokens:
        return "today"
    return "today"


def _weather_location(value: str) -> str:
    clean = _clean_text(value)
    match = re.search(r"\bin\s+(.+)$", clean, flags=re.IGNORECASE)
    if not match:
        return "local"
    location = _clean_text(match.group(1))
    return location or "local"


def _market_symbol(value: str) -> str:
    clean = _clean_text(value)
    patterns = (
        r"^stock\s+(.+)$",
        r"^market\s+price\s+for\s+(.+)$",
        r"^market\s+quote\s+for\s+(.+)$",
        r"^(.+?)\s+stock$",
    )
    for pattern in patterns:
        match = re.match(pattern, clean, flags=re.IGNORECASE)
        if match:
            return _normalize_market_symbol(match.group(1))
    return ""


def _normalize_market_symbol(value: str) -> str:
    symbol = _clean_text(value)
    symbol = re.sub(r"^(?:the|a|an)\s+", "", symbol, flags=re.IGNORECASE)
    symbol = symbol.replace("$", "").strip()
    return symbol.upper()


def _dangerous_device_action_name(normalized_text: str) -> str:
    if normalized_text in {"lock", "lock pc", "lock computer", "lock session", "lock windows", "lock windows session"}:
        return "lock_pc"
    if normalized_text in {"sleep", "sleep pc", "sleep computer", "sleep session", "sleep windows", "sleep windows pc"}:
        return "sleep_pc"
    if normalized_text in {"shutdown", "restart"}:
        return normalized_text
    if normalized_text.startswith("run command"):
        return "run_command"
    if normalized_text.startswith("open app"):
        return "open_app"
    if normalized_text == "delete" or normalized_text.startswith("delete "):
        return "delete"
    if "arbitrary shell" in normalized_text or normalized_text.startswith("shell "):
        return "arbitrary_shell"
    return ""


def _normalize_action_name(value: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _looks_like_voice_session(text: str) -> bool:
    normalized = _normalize(text)
    phrases = {
        "start voice session",
        "start mock voice",
        "run voice test",
    }
    return any(normalized == phrase or normalized.startswith(f"{phrase} ") for phrase in phrases)


def _looks_like_voice_session_status(text: str) -> bool:
    normalized = _normalize(text)
    phrases = {
        "what happened in voice session",
        "show last voice session",
        "voice session status",
    }
    return normalized in phrases


def _voice_session_max_turns(text: str):
    match = re.search(r"\b(?:max\s+turns?|for)\s+(\d+)(?:\s+turns?)?\b", text or "", flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _calendar_period(text: ParsedText) -> str:
    if "tomorrow" in text.tokens:
        return "tomorrow"
    return "today"


def _strip_leading_task_marker(value: str) -> str:
    text = _clean_text(value)
    lowered = text.lower()
    if lowered == "to":
        return ""
    if lowered.startswith("to "):
        return text[3:].strip()
    return text


_DUE_SUFFIXES = (
    "tomorrow morning",
    "tomorrow afternoon",
    "tomorrow evening",
    "next week",
    "next month",
    "tomorrow",
    "tonight",
    "today",
)
