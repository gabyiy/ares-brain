import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

from skills.base import Skill


@dataclass(frozen=True)
class ToolSelection:
    skill: Skill
    confidence: float
    reason: str


class ToolSelector:
    """
    Score local skills for a user text request.

    The selector is intentionally generic: local skills such as CalculatorSkill
    and future skills such as NotesSkill can participate by defining triggers
    and optional selection attributes on the Skill class.
    """

    def __init__(self, min_confidence: float = 0.25):
        self.min_confidence = float(min_confidence)

    def select(
        self,
        text: str,
        skills: Iterable[Skill],
        run_before_intents: Optional[bool] = None,
    ) -> Optional[ToolSelection]:
        candidates = []
        for index, skill in enumerate(skills):
            if run_before_intents is not None:
                if bool(getattr(skill, "run_before_intents", False)) != run_before_intents:
                    continue

            confidence, reason = self.score(text, skill)
            if confidence >= self.min_confidence:
                candidates.append((confidence, self._priority(skill), -index, reason, skill))

        if not candidates:
            return None

        confidence, _priority, _index, reason, skill = max(candidates)
        return ToolSelection(skill=skill, confidence=confidence, reason=reason)

    def matching(
        self,
        text: str,
        skills: Iterable[Skill],
        run_before_intents: Optional[bool] = None,
    ) -> List[ToolSelection]:
        matches = []
        for skill in skills:
            if run_before_intents is not None:
                if bool(getattr(skill, "run_before_intents", False)) != run_before_intents:
                    continue

            confidence, reason = self.score(text, skill)
            if confidence >= self.min_confidence:
                matches.append(ToolSelection(skill=skill, confidence=confidence, reason=reason))

        return sorted(
            matches,
            key=lambda match: (match.confidence, self._priority(match.skill)),
            reverse=True,
        )

    def score(self, text: str, skill: Skill):
        normalized_text = self._normalize(text)
        if not normalized_text:
            return 0.0, "empty input"

        best_score = 0.0
        best_reason = "no trigger match"

        for trigger in self._selection_triggers(skill):
            normalized_trigger = self._normalize(trigger)
            if not normalized_trigger:
                continue

            score, reason = self._score_trigger(normalized_text, normalized_trigger)
            if score > best_score:
                best_score = score
                best_reason = reason

        if best_score <= 0.0:
            if skill.can_handle(text):
                score = min(1.0, 0.6 + self._priority(skill))
                return score, "skill can_handle match"
            return 0.0, best_reason

        score = min(1.0, best_score + self._priority(skill))
        return score, best_reason

    def _score_trigger(self, normalized_text: str, normalized_trigger: str):
        if normalized_text == normalized_trigger:
            return 1.0, "exact trigger match"

        if normalized_trigger in normalized_text:
            length_bonus = min(0.15, len(normalized_trigger) / 100.0)
            return 0.75 + length_bonus, "trigger phrase contained in text"

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

    def _priority(self, skill: Skill) -> float:
        return float(getattr(skill, "selection_priority", 0.0) or 0.0)

    def _normalize(self, value: str) -> str:
        return " ".join(self._tokens(value))

    def _tokens(self, value: str):
        return re.findall(r"[a-z0-9]+", (value or "").lower())
