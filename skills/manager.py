from typing import Optional

from events import get_global_bus
from skills.base import Skill, SkillContext, SkillResponse
from skills.plugin import SkillPlugin
from skills.registry import SkillRegistry


class SkillManager:
    def __init__(
        self,
        registry: Optional[SkillRegistry] = None,
        event_bus=None,
        memory_store=None,
        profile_store=None,
    ):
        self.registry = registry or SkillRegistry()
        self.event_bus = event_bus or get_global_bus()
        self.memory_store = memory_store
        self.profile_store = profile_store

    def register(self, skill: Skill) -> Skill:
        registered = self.registry.register(skill)
        self.event_bus.publish(
            "skill.registered",
            {"skill": registered.name},
            source="skill_manager",
        )
        return registered

    def register_plugin(self, plugin: SkillPlugin) -> None:
        plugin.register(self.registry)
        self.event_bus.publish(
            "skill.plugin_registered",
            {"plugin": plugin.name, "skills": [skill.name for skill in plugin.skills]},
            source="skill_manager",
        )

    def detect(self, text: str, run_before_intents: Optional[bool] = None):
        return self.registry.first_match(text, run_before_intents=run_before_intents)

    def select(self, text: str, run_before_intents: Optional[bool] = None):
        return self.registry.select(text, run_before_intents=run_before_intents)

    def handle(
        self,
        text: str,
        context: Optional[SkillContext] = None,
        run_before_intents: Optional[bool] = None,
    ):
        selection = self.select(text, run_before_intents=run_before_intents)
        if not selection:
            return None
        skill = selection.skill

        context = context or self.create_context()
        self.event_bus.publish(
            "skill.detected",
            {
                "skill": skill.name,
                "text": text,
                "confidence": selection.confidence,
                "reason": selection.reason,
            },
            source="skill_manager",
        )

        response = skill.handle(text, context)
        if isinstance(response, str):
            response = SkillResponse(text=response, skill=skill.name)

        self.event_bus.publish(
            "skill.response_generated",
            {"skill": skill.name, "response": response.text},
            source="skill_manager",
        )
        return response

    def create_context(self) -> SkillContext:
        return SkillContext(
            event_bus=self.event_bus,
            memory_store=self.memory_store,
            profile_store=self.profile_store,
        )
