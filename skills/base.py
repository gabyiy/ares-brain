from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Tuple


@dataclass(frozen=True)
class SkillContext:
    event_bus: Any = None
    memory_store: Any = None
    profile_store: Any = None
    notes_store: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def publish(self, event_name: str, payload: Dict[str, Any]):
        if self.event_bus:
            self.event_bus.publish(event_name, payload, source="skill")


@dataclass(frozen=True)
class SkillResponse:
    text: str
    skill: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class Skill(ABC):
    name = ""
    description = ""
    version = "0.1"
    triggers: Tuple[str, ...] = ()
    run_before_intents = False
    selection_keywords: Tuple[str, ...] = ()
    selection_priority = 0.0

    def metadata(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "triggers": list(self.triggers),
            "run_before_intents": self.run_before_intents,
            "selection_keywords": list(self.selection_keywords),
            "selection_priority": self.selection_priority,
        }

    def can_handle(self, text: str) -> bool:
        low = (text or "").lower()
        return any(trigger in low for trigger in self._clean_triggers())

    @abstractmethod
    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        raise NotImplementedError

    def _clean_triggers(self) -> Iterable[str]:
        return [trigger.lower().strip() for trigger in self.triggers if trigger.strip()]
