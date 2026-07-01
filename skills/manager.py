from typing import Optional

from core.ConversationContext import ConversationContextManager
from core.Intent import Intent
from core.IntentParser import IntentParser
from core.Planner import Plan
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
        self.last_plan = None

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
        plan = selection.plan if selection else getattr(self.registry.selector, "last_plan", None)
        self.last_plan = plan

        if self._should_execute_plan(plan, selection):
            response = self._execute_plan(plan, context or self.create_context())
            self.conversation_context.record_turn(
                user_message=intent.raw_text,
                assistant_response=response.text,
                detected_skill=response.skill,
            )
            self.event_bus.publish(
                "skill.response_generated",
                {"skill": response.skill, "response": response.text},
                source="skill_manager",
            )
            return response

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
                "plan": plan.to_dict() if plan else None,
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

    def format_last_plan(self) -> str:
        if not self.last_plan:
            return "No plan is available yet."
        return self.last_plan.format()

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

    def _should_execute_plan(self, plan, selection) -> bool:
        if not isinstance(plan, Plan):
            return False

        steps = plan.executable_steps()
        if not steps:
            return False

        if selection is None:
            return True

        if len(steps) > 1:
            return True

        return steps[0].target != selection.skill.name

    def _execute_plan(self, plan: Plan, context: SkillContext) -> SkillResponse:
        responses = []
        results = []

        self.event_bus.publish(
            "planner.plan_created",
            plan.to_dict(),
            source="skill_manager",
        )

        for step in plan.executable_steps():
            if step.target == "conversation_memory":
                response = self._execute_memory_step(step)
            else:
                response = self._execute_skill_step(step, context)

            responses.append(response)
            results.append(
                {
                    "step": step.to_dict(),
                    "response": response.text,
                    "skill": response.skill,
                    "metadata": dict(response.metadata),
                }
            )

        lines = ["Plan results:"]
        for index, response in enumerate(responses, start=1):
            lines.append(f"{index}. {response.text}")

        return SkillResponse(
            text="\n".join(lines),
            skill="planner",
            metadata={"plan": plan.to_dict(), "results": results},
        )

    def _execute_skill_step(self, step, context: SkillContext) -> SkillResponse:
        skill = self.registry.get(step.target)
        if not skill:
            return SkillResponse(
                text=f"Skipped {step.target}.{step.action}: skill is not available.",
                skill="planner",
                metadata={"missing_skill": step.target},
            )

        intent = Intent(
            intent_name=step.intent_name,
            confidence=1.0,
            extracted_entities=dict(step.entities),
            raw_text=step.input_text,
        )
        step_context = self._context_with_intent(context, intent)
        response = skill.handle(step.input_text, step_context)
        if isinstance(response, str):
            return SkillResponse(text=response, skill=skill.name)
        return response

    def _execute_memory_step(self, step) -> SkillResponse:
        if not self.memory_store:
            return SkillResponse(
                text="Memory storage is not available.",
                skill="planner",
                metadata={"error": "missing_memory_store"},
            )

        content = (step.entities.get("content") or step.input_text or "").strip()
        if not content:
            return SkillResponse(
                text="Skipped memory step: missing memory content.",
                skill="planner",
                metadata={"error": "missing_memory_content"},
            )

        memory = self.memory_store.remember(
            content=content,
            category="conversation_memory",
            importance=0.85,
            tags=["conversation", "planner"],
            long_term=True,
            metadata={"plan_step": step.to_dict()},
            source="skills.manager.planner",
        )
        return SkillResponse(
            text=f"Stored memory: {content}",
            skill="planner",
            metadata={"memory_id": memory.id, "long_term": memory.long_term},
        )
