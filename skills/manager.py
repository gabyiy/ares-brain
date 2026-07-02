from typing import Optional

from core.ConversationContext import ConversationContextManager
from core.ExecutionPipeline import ExecutionPipeline
from core.Intent import Intent
from core.IntentParser import IntentParser
from core.Planner import Plan
from core.ToolAdapter import MockCalendarAdapter, MockMarketAdapter, MockWeatherAdapter, ToolAdapterRegistry
from core.ToolChain import ToolChain
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
        goals_store=None,
        tool_adapter_registry=None,
        conversation_context=None,
        intent_parser=None,
    ):
        self.registry = registry or SkillRegistry()
        self.event_bus = event_bus or get_global_bus()
        self.memory_store = memory_store
        self.profile_store = profile_store
        self.notes_store = notes_store
        self.tasks_store = tasks_store
        self.goals_store = goals_store
        self.tool_adapter_registry = tool_adapter_registry or ToolAdapterRegistry(
            [MockWeatherAdapter(), MockMarketAdapter(), MockCalendarAdapter()]
        )
        self.conversation_context = conversation_context or ConversationContextManager()
        self.intent_parser = intent_parser or IntentParser()
        self.registry.selector.planner.tool_adapter_registry = self.tool_adapter_registry
        self.last_plan = None
        self.last_execution = None
        self.execution_pipeline = ExecutionPipeline(
            skill_resolver=self.registry.get,
            event_bus=self.event_bus,
            memory_store=self.memory_store,
            tool_adapter_registry=self.tool_adapter_registry,
            context_builder=self._context_with_intent,
        )
        self.tool_chain = ToolChain(
            execution_pipeline=self.execution_pipeline,
            event_bus=self.event_bus,
        )

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
            if selection:
                self._publish_skill_detected(selection, intent, plan)
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
        self._publish_skill_detected(selection, intent, plan)

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

    def format_last_execution(self) -> str:
        if not self.last_execution:
            return "No execution is available yet."
        return self.last_execution.format()

    def format_last_chain(self) -> str:
        return self.tool_chain.format_last()

    def format_chain_history(self) -> str:
        return self.tool_chain.format_history()

    def create_context(self) -> SkillContext:
        return SkillContext(
            event_bus=self.event_bus,
            memory_store=self.memory_store,
            profile_store=self.profile_store,
            notes_store=self.notes_store,
            tasks_store=self.tasks_store,
            goals_store=self.goals_store,
            tool_adapter_registry=self.tool_adapter_registry,
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
            goals_store=context.goals_store,
            tool_adapter_registry=context.tool_adapter_registry,
            conversation_context=context.conversation_context,
            metadata=metadata,
        )

    def _should_execute_plan(self, plan, selection) -> bool:
        if not isinstance(plan, Plan):
            return False

        return bool(plan.executable_steps())

    def _execute_plan(self, plan: Plan, context: SkillContext) -> SkillResponse:
        self.event_bus.publish(
            "planner.plan_created",
            plan.to_dict(),
            source="skill_manager",
        )
        chain = self.tool_chain.execute(plan, context)
        execution = chain.execution
        self.last_execution = execution

        response_skill = "planner"
        if execution and len(execution.step_results) == 1:
            response_skill = execution.step_results[0].returned_data.get("skill") or "planner"
        elif not execution:
            response_skill = "tool_chain"

        return SkillResponse(
            text=chain.format_response_text(),
            skill=response_skill,
            metadata={
                "plan": plan.to_dict(),
                "chain": chain.to_dict(),
                "execution": execution.to_dict() if execution else None,
                "results": [result.to_dict() for result in execution.step_results] if execution else [],
            },
        )

    def _publish_skill_detected(self, selection, intent: Intent, plan) -> None:
        self.event_bus.publish(
            "skill.detected",
            {
                "skill": selection.skill.name,
                "text": intent.raw_text,
                "intent": intent.intent_name,
                "entities": dict(intent.extracted_entities),
                "confidence": selection.confidence,
                "reason": selection.reason,
                "plan": plan.to_dict() if plan else None,
            },
            source="skill_manager",
        )
