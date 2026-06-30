from dataclasses import dataclass, field
from typing import Iterable, Tuple

from skills.base import Skill


@dataclass(frozen=True)
class SkillPlugin:
    name: str
    version: str = "0.1"
    description: str = ""
    skills: Tuple[Skill, ...] = field(default_factory=tuple)

    @classmethod
    def create(
        cls,
        name: str,
        skills: Iterable[Skill],
        version: str = "0.1",
        description: str = "",
    ):
        return cls(
            name=name,
            version=version,
            description=description,
            skills=tuple(skills),
        )

    def register(self, registry) -> None:
        for skill in self.skills:
            registry.register(skill)
