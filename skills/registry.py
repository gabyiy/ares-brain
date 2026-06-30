from typing import Dict, List, Optional

from skills.base import Skill


class SkillRegistry:
    def __init__(self):
        self._skills: Dict[str, Skill] = {}

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

    def matching(self, text: str, run_before_intents: Optional[bool] = None) -> List[Skill]:
        skills = [skill for skill in self.all() if skill.can_handle(text)]
        if run_before_intents is not None:
            skills = [
                skill
                for skill in skills
                if bool(getattr(skill, "run_before_intents", False)) == run_before_intents
            ]
        return skills

    def first_match(self, text: str, run_before_intents: Optional[bool] = None) -> Optional[Skill]:
        matches = self.matching(text, run_before_intents=run_before_intents)
        return matches[0] if matches else None
