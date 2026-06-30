from typing import Optional

from core.ConversationContext import ConversationContextManager
from core.Intent import Intent
from core.IntentParser import IntentParser
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
        notes_store=None,
        tasks_store=None,
        conversation_context=None,
        intent_parser=None,
    ):
        self.registry = registry or SkillRegistry()
        self.event_bus = event_bus or get_global_bus()
        self.memory_store = memory_store
        self.profile_store = profile_store
        self.notes_store = notes_store
        self.tasks_store = tasks_store
        self.conversation_context = conversation_context or ConversationContextManager()
        self.intent_parser = intent_parser or IntentParser()

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

    def detect(self, text, run_before_intents: Optional[bool] = None):
        intent = self.parse_intent(text)
        return self.registry.first_match(intent, run_before_intents=run_before_intents)

    def select(self, text, run_before_intents: Optional[bool] = None):
        intent = self.parse_intent(text)
        return self.registry.select(intent, run_before_intents=run_before_intents)

    def parse_intent(self, text) -> Intent:
        if isinstance(text, Intent):
            return text
        return self.intent_parser.parse(str(text or ""))

    def handle(
        self,
        text,
        context: Optional[SkillContext] = None,
        run_before_intents: Optional[bool] = None,
    ):
        intent = self.parse_intent(text)
        selection = self.registry.select(intent, run_before_intents=run_before_intents)
        if not selection:
            return None
        skill = selection.skill

        context = self._context_with_intent(context or self.create_context(), intent)
        self.event_bus.publish(
            "skill.detected",
            {
                "skill": skill.name,
                "text": intent.raw_text,
                "intent": intent.intent_name,
                "entities": dict(intent.extracted_entities),
                "confidence": selection.confidence,
                "reason": selection.reason,
            },
            source="skill_manager",
        )

        response = skill.handle(intent.raw_text, context)
        if isinstance(response, str):
            response = SkillResponse(text=response, skill=skill.name)

        self.conversation_context.record_turn(
            user_message=intent.raw_text,
            assistant_response=response.text,
            detected_skill=skill.name,
        )

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
            notes_store=self.notes_store,
            tasks_store=self.tasks_store,
            conversation_context=self.conversation_context,
        )

    def _context_with_intent(self, context: SkillContext, intent: Intent) -> SkillContext:
        metadata = dict(context.metadata)
        metadata["intent"] = intent
        metadata["entities"] = dict(intent.extracted_entities)
        return SkillContext(
            event_bus=context.event_bus,
            memory_store=context.memory_store,
            profile_store=context.profile_store,
            notes_store=context.notes_store,
            tasks_store=context.tasks_store,
            conversation_context=context.conversation_context,
            metadata=metadata,
        )
