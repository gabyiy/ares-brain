import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

from core.Intent import Intent
from core.IntentParser import IntentParser
from core.Planner import Plan, Planner
from skills.base import Skill


@dataclass(frozen=True)
class ToolSelection:
    skill: Skill
    confidence: float
    reason: str
    plan: Plan


class ToolSelector:
    """
    Score local skills for a user text request.

    The selector is intentionally generic: local skills such as CalculatorSkill
    and future skills such as NotesSkill can participate by defining triggers
    and optional selection attributes on the Skill class.
    """

    def __init__(self, min_confidence: float = 0.25):
        self.min_confidence = float(min_confidence)
        self.intent_parser = IntentParser()
        self.planner = Planner()
        self.last_plan = None

    def select(
        self,
        text,
        skills: Iterable[Skill],
        run_before_intents: Optional[bool] = None,
    ) -> Optional[ToolSelection]:
        intent = self._coerce_intent(text)
        plan = self.planner.plan(intent)
        self.last_plan = plan
        candidates = []
        for index, skill in enumerate(skills):
            if run_before_intents is not None:
                if bool(getattr(skill, "run_before_intents", False)) != run_before_intents:
                    continue

            confidence, reason = self.score(intent, skill)
            if confidence >= self.min_confidence:
                candidates.append((confidence, self._priority(skill), -index, reason, skill))

        if not candidates:
            return None

        confidence, _priority, _index, reason, skill = max(candidates)
        return ToolSelection(skill=skill, confidence=confidence, reason=reason, plan=plan)

    def matching(
        self,
        text,
        skills: Iterable[Skill],
        run_before_intents: Optional[bool] = None,
    ) -> List[ToolSelection]:
        intent = self._coerce_intent(text)
        plan = self.planner.plan(intent)
        self.last_plan = plan
        matches = []
        for skill in skills:
            if run_before_intents is not None:
                if bool(getattr(skill, "run_before_intents", False)) != run_before_intents:
                    continue

            confidence, reason = self.score(intent, skill)
            if confidence >= self.min_confidence:
                matches.append(ToolSelection(skill=skill, confidence=confidence, reason=reason, plan=plan))

        return sorted(
            matches,
            key=lambda match: (match.confidence, self._priority(match.skill)),
            reverse=True,
        )

    def score(self, text, skill: Skill):
        intent = self._coerce_intent(text)
        intent_score, intent_reason = self._score_intent(intent, skill)
        if intent_score > 0.0:
            return intent_score, intent_reason

        normalized_text = self._normalize(intent.raw_text)
        if not normalized_text:
            return 0.0, "empty input"

        best_score = 0.0
        best_reason = "no trigger match"

        for trigger in self._selection_triggers(skill):
            normalized_trigger = self._normalize(trigger)
            if not normalized_trigger:
                continue

            score, reason = self._score_trigger(
                normalized_text,
                normalized_trigger,
                allow_token_overlap=intent.intent_name != "unknown",
            )
            if score > best_score:
                best_score = score
                best_reason = reason

        if best_score <= 0.0:
            if intent.intent_name == "unknown" and skill.can_handle(intent.raw_text):
                score = min(1.0, 0.6 + self._priority(skill))
                return score, "skill can_handle match"
            return 0.0, best_reason

        score = min(1.0, best_score + self._priority(skill))
        return score, best_reason

    def _score_trigger(
        self,
        normalized_text: str,
        normalized_trigger: str,
        allow_token_overlap: bool = True,
    ):
        if normalized_text == normalized_trigger:
            return 1.0, "exact trigger match"

        if normalized_trigger in normalized_text:
            length_bonus = min(0.15, len(normalized_trigger) / 100.0)
            return 0.75 + length_bonus, "trigger phrase contained in text"

        if not allow_token_overlap:
            return 0.0, "token overlap disabled for unknown intent"

        text_tokens = set(self._tokens(normalized_text))
        trigger_tokens = set(self._tokens(normalized_trigger))
        if not text_tokens or not trigger_tokens:
            return 0.0, "no comparable tokens"

        overlap = len(text_tokens & trigger_tokens) / len(trigger_tokens)
        if overlap:
            return 0.2 + (0.45 * overlap), "trigger token overlap"

        return 0.0, "no trigger match"

    def _selection_triggers(self, skill: Skill):
        triggers = list(getattr(skill, "triggers", ()) or ())
        triggers.extend(getattr(skill, "selection_keywords", ()) or ())
        return triggers

    def _score_intent(self, intent: Intent, skill: Skill):
        if intent.intent_name == "unknown":
            return 0.0, "unknown structured intent"

        normalized_intent_name = self._normalize_intent_name(intent.intent_name)
        if normalized_intent_name in self._skill_intents(skill):
            score = min(1.0, intent.confidence + self._priority(skill))
            return score, f"structured intent match: {intent.intent_name}"

        return 0.0, f"structured intent did not match: {intent.intent_name}"

    def _skill_intents(self, skill: Skill):
        names = set(getattr(skill, "intent_names", ()) or ())
        if not names:
            names.add(getattr(skill, "name", "") or "")
        intents = set()
        for name in names:
            normalized = self._normalize_intent_name(name)
            if normalized:
                intents.add(normalized)
        return intents

    def _priority(self, skill: Skill) -> float:
        return float(getattr(skill, "selection_priority", 0.0) or 0.0)

    def _normalize(self, value: str) -> str:
        return " ".join(self._tokens(value))

    def _normalize_intent_name(self, value: str) -> str:
        return "_".join(self._tokens(value))

    def _tokens(self, value: str):
        return re.findall(r"[a-z0-9]+", (value or "").lower())

    def _coerce_intent(self, value) -> Intent:
        if isinstance(value, Intent):
            return value
        return self.intent_parser.parse(str(value or ""))
