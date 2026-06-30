from typing import Dict, List, Optional

from skills.base import Skill
from skills.selector import ToolSelector


class SkillRegistry:
    def __init__(self, selector: Optional[ToolSelector] = None):
        self._skills: Dict[str, Skill] = {}
        self.selector = selector or ToolSelector()

    def register(self, skill: Skill) -> Skill:
        if not isinstance(skill, Skill):
            raise TypeError("Registered object must implement Skill")

        name = (skill.name or "").strip()
        if not name:
            raise ValueError("Skill name is required")

        if name in self._skills:
            raise ValueError(f"Skill already registered: {name}")

        self._skills[name] = skill
        return skill

    def unregister(self, name: str) -> Optional[Skill]:
        return self._skills.pop((name or "").strip(), None)

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get((name or "").strip())

    def all(self) -> List[Skill]:
        return list(self._skills.values())

    def matching(self, text, run_before_intents: Optional[bool] = None) -> List[Skill]:
        selections = self.selector.matching(
            text,
            self.all(),
            run_before_intents=run_before_intents,
        )
        return [selection.skill for selection in selections]

    def first_match(self, text, run_before_intents: Optional[bool] = None) -> Optional[Skill]:
        selection = self.select(text, run_before_intents=run_before_intents)
        return selection.skill if selection else None

    def select(self, text, run_before_intents: Optional[bool] = None):
        return self.selector.select(
            text,
            self.all(),
            run_before_intents=run_before_intents,
        )
