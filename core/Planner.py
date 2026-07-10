import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

from core.DeviceAction import DANGER_SAFE, classify_device_action
from core.Intent import Intent


@dataclass(frozen=True)
class PlanStep:
    order: int
    target: str
    action: str
    input_text: str
    intent_name: str
    entities: Dict[str, Any] = field(default_factory=dict)
    can_execute: bool = True
    skip_reason: str = ""
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "target": self.target,
            "action": self.action,
            "input_text": self.input_text,
            "intent_name": self.intent_name,
            "entities": dict(self.entities),
            "can_execute": self.can_execute,
            "skip_reason": self.skip_reason,
            "description": self.description,
        }


@dataclass(frozen=True)
class Plan:
    raw_text: str
    intent_name: str
    steps: List[PlanStep] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def executable_steps(self) -> List[PlanStep]:
        return [step for step in self.steps if step.can_execute]

    def is_multi_step(self) -> bool:
        return len(self.executable_steps()) > 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "intent_name": self.intent_name,
            "steps": [step.to_dict() for step in self.steps],
            "errors": list(self.errors),
        }

    def format(self) -> str:
        if not self.steps:
            if self.errors:
                return "No executable plan.\n" + "\n".join(f"- {error}" for error in self.errors)
            return "No executable plan."

        lines = ["Execution plan:"]
        for step in self.steps:
            status = "ready" if step.can_execute else f"skipped: {step.skip_reason}"
            lines.append(f"{step.order}. {step.target}.{step.action} - {status}")
            if step.description:
                lines.append(f"   {step.description}")
        if self.errors:
            lines.append("Planning errors:")
            lines.extend(f"- {error}" for error in self.errors)
        return "\n".join(lines)


@dataclass(frozen=True)
class MultiStepPlan(Plan):
    """Marker plan for ordered requests that execute more than one step."""


class Planner:
    """Builds deterministic execution plans without executing any skills."""

    def __init__(
        self,
        tool_adapter_registry=None,
        profile_store=None,
        notes_store=None,
        tasks_store=None,
        goals_store=None,
    ):
        self.tool_adapter_registry = tool_adapter_registry
        self.profile_store = profile_store
        self.notes_store = notes_store
        self.tasks_store = tasks_store
        self.goals_store = goals_store

    def set_context_sources(
        self,
        profile_store=None,
        notes_store=None,
        tasks_store=None,
        goals_store=None,
    ) -> None:
        self.profile_store = profile_store
        self.notes_store = notes_store
        self.tasks_store = tasks_store
        self.goals_store = goals_store

    def plan(self, intent: Intent) -> Plan:
        raw_text = (intent.raw_text or "").strip()
        if not raw_text:
            return Plan(raw_text=raw_text, intent_name=intent.intent_name, errors=["No input to plan."])

        clauses = _split_clauses(raw_text)
        steps = []
        errors = []

        if len(clauses) > 1:
            for clause in clauses:
                step, error = self._plan_clause(clause, intent)
                if step:
                    if step.can_execute:
                        steps.append(step)
                    else:
                        errors.append(f"Skipped {step.target}.{step.action}: {step.skip_reason}")
                if error:
                    errors.append(error)
        else:
            step, error = self._plan_intent(intent)
            if step:
                if step.can_execute:
                    steps.append(step)
                else:
                    errors.append(f"Skipped {step.target}.{step.action}: {step.skip_reason}")
            elif error:
                errors.append(error)

        ordered_steps = [
            _renumber_step(step, index + 1)
            for index, step in enumerate(step for step in steps if step.can_execute)
        ]

        if not ordered_steps and not errors:
            errors.append("No executable steps found.")

        plan_type = MultiStepPlan if len(ordered_steps) > 1 else Plan
        return plan_type(
            raw_text=raw_text,
            intent_name=intent.intent_name,
            steps=ordered_steps,
            errors=errors,
        )

    def _plan_intent(self, intent: Intent):
        if intent.intent_name == "goal":
            return self._goal_step(intent.raw_text, dict(intent.extracted_entities)), None

        if intent.intent_name == "note":
            entities = dict(intent.extracted_entities)
            if entities.get("action") == "search":
                context_keyword = _note_search_keyword(intent.raw_text)
                if context_keyword is not None:
                    return self._note_search_step(intent.raw_text, context_keyword), None
            return self._note_step(intent.raw_text, entities), None

        if intent.intent_name == "task":
            return self._task_step(intent.raw_text, dict(intent.extracted_entities)), None

        if intent.intent_name == "device_action":
            return self._device_action_step(intent.raw_text, dict(intent.extracted_entities)), None

        if intent.intent_name == "calculate":
            return self._calculator_step(intent.raw_text, dict(intent.extracted_entities)), None

        if intent.intent_name == "weather":
            return self._weather_step(intent.raw_text, dict(intent.extracted_entities)), None

        if intent.intent_name == "market":
            return self._market_step(intent.raw_text, dict(intent.extracted_entities)), None

        if intent.intent_name == "calendar":
            return self._calendar_step(intent.raw_text, dict(intent.extracted_entities)), None

        if intent.intent_name == "event_history":
            return self._event_history_step(intent.raw_text, dict(intent.extracted_entities)), None

        memory_step = self._memory_step_from_text(intent.raw_text)
        if memory_step:
            return memory_step, None

        return None, f"No planner support for intent: {intent.intent_name}"

    def _plan_clause(self, clause: str, parent_intent: Intent):
        clean_clause = _clean_clause(clause)
        if not clean_clause:
            return None, "Skipped empty plan clause."

        task_step = self._task_step_from_text(clean_clause)
        if task_step:
            return task_step, None

        goal_step = self._goal_step_from_text(clean_clause)
        if goal_step:
            return goal_step, None

        memory_step = self._memory_step_from_text(clean_clause)
        if memory_step:
            return memory_step, None

        note_step = self._note_step_from_text(clean_clause)
        if note_step:
            return note_step, None

        calculator_step = self._calculator_step_from_text(clean_clause)
        if calculator_step:
            return calculator_step, None

        weather_step = self._weather_step_from_text(clean_clause)
        if weather_step:
            return weather_step, None

        market_step = self._market_step_from_text(clean_clause)
        if market_step:
            return market_step, None

        calendar_step = self._calendar_step_from_text(clean_clause)
        if calendar_step:
            return calendar_step, None

        event_history_step = self._event_history_step_from_text(clean_clause)
        if event_history_step:
            return event_history_step, None

        device_action_step = self._device_action_step_from_text(clean_clause)
        if device_action_step:
            return device_action_step, None

        return None, f"Skipped unsupported plan clause: {clean_clause}"

    def _goal_step(self, raw_text: str, entities: Dict[str, Any]) -> PlanStep:
        action = entities.get("action") or "add"
        clean_entities = dict(entities)

        if action == "next":
            return self._next_goal_step(raw_text)

        if action == "add":
            title = entities.get("title") or _strip_goal_prefix(raw_text)
            description = entities.get("description") or ""
            priority = entities.get("priority") or "normal"
            can_execute = bool(title)
            clean_entities.update({"title": title, "description": description, "priority": priority})
            command = f"add goal {title}" if title else raw_text
            if description:
                command = f"{command} description {description}"
            if priority and priority != "normal":
                command = f"{command} priority {priority}"
            description_text = f"Add goal: {title}" if title else "Add goal."
        elif action == "add_milestone":
            goal_id = entities.get("goal_id") or ""
            milestone = entities.get("milestone") or ""
            can_execute = bool(goal_id and milestone)
            clean_entities.update({"goal_id": goal_id, "milestone": milestone})
            command = f"add milestone to goal {goal_id} {milestone}" if goal_id or milestone else raw_text
            description_text = f"Add milestone to goal {goal_id}: {milestone}" if can_execute else "Add milestone."
        elif action in {"show", "complete", "pause", "delete"}:
            goal_id = entities.get("goal_id") or _first_word(_strip_prefix(raw_text, f"{action} goal"))
            can_execute = bool(goal_id)
            clean_entities["goal_id"] = goal_id
            command = f"{action} goal {goal_id}" if goal_id else raw_text
            description_text = f"{action.title()} goal: {goal_id}" if goal_id else "Run goal command."
        elif action == "list":
            can_execute = True
            command = "list goals"
            description_text = "List goals."
        else:
            can_execute = False
            command = raw_text
            description_text = "Run goal command."

        return PlanStep(
            order=0,
            target="goals",
            action=action,
            input_text=command,
            intent_name="goal",
            entities=clean_entities,
            can_execute=can_execute,
            skip_reason="" if can_execute else "Missing goal details.",
            description=description_text,
        )

    def _goal_step_from_text(self, text: str):
        lowered = text.lower()
        if _is_next_goal_question(lowered):
            return self._goal_step(text, {"action": "next"})

        if lowered.startswith("add milestone to goal"):
            goal_id, milestone = _split_first_word(_strip_prefix(text, "add milestone to goal"))
            return self._goal_step(
                text,
                {"action": "add_milestone", "goal_id": goal_id, "milestone": milestone},
            )

        for action in ("show", "complete", "pause", "delete"):
            prefix = f"{action} goal"
            if lowered.startswith(prefix):
                return self._goal_step(
                    text,
                    {"action": action, "goal_id": _first_word(_strip_prefix(text, prefix))},
                )

        if lowered in ("list goals", "show goals", "list my goals", "show my goals"):
            return self._goal_step(text, {"action": "list"})

        if lowered.startswith("add goal"):
            title, description, priority = _split_goal_fields(_strip_prefix(text, "add goal"))
            return self._goal_step(
                text,
                {
                    "action": "add",
                    "title": title,
                    "description": description,
                    "priority": priority,
                },
            )

        return None

    def _next_goal_step(self, raw_text: str) -> PlanStep:
        if not self.goals_store:
            return self._context_response_step(
                raw_text,
                "I do not have goal context available yet.",
                context_type="goals",
                reason="missing_goals_store",
            )

        goal = self._main_goal()
        if not goal:
            return self._context_response_step(
                raw_text,
                "I do not have any goals yet.",
                context_type="goals",
                reason="empty_goals",
            )

        task = self._next_task_for_goal(goal)
        if task:
            due = f" due {task.due}" if getattr(task, "due", None) else ""
            return self._context_response_step(
                raw_text,
                f'Next for your goal "{goal.title}": finish task {task.id}: {task.text}{due}.',
                context_type="goals",
                reason="related_task",
                extra_entities={
                    "goal_id": goal.id,
                    "goal_title": goal.title,
                    "task_id": task.id,
                    "task_text": task.text,
                    "task_due": getattr(task, "due", None),
                },
            )

        milestones = list(getattr(goal, "milestones", []) or [])
        if milestones:
            return self._context_response_step(
                raw_text,
                f'Next for your goal "{goal.title}": {milestones[0]}.',
                context_type="goals",
                reason="first_milestone",
                extra_entities={
                    "goal_id": goal.id,
                    "goal_title": goal.title,
                    "milestone": milestones[0],
                },
            )

        return self._context_response_step(
            raw_text,
            f'Next for your goal "{goal.title}": add a milestone or task for the next concrete step.',
            context_type="goals",
            reason="missing_next_step",
            extra_entities={"goal_id": goal.id, "goal_title": goal.title},
        )

    def _note_step(self, raw_text: str, entities: Dict[str, Any]) -> PlanStep:
        action = entities.get("action") or "add"
        if action == "delete_all_request":
            action = "delete_all_confirm"
            entities = {**entities, "action": action}
            raw_text = "confirm delete all notes"

        if action == "search":
            keyword = entities.get("keyword") or _strip_prefix(raw_text, "search notes")
            can_execute = bool(keyword)
            clean_entities = dict(entities)
            if keyword:
                clean_entities["keyword"] = keyword
            return PlanStep(
                order=0,
                target="notes",
                action="search",
                input_text=f"search notes {keyword}" if keyword else raw_text,
                intent_name="note",
                entities=clean_entities,
                can_execute=can_execute,
                skip_reason="" if can_execute else "Missing note search keyword.",
                description=f"Search notes for: {keyword}" if keyword else "Search notes.",
            )

        note_text = entities.get("text") or _strip_note_prefix(raw_text)
        can_execute = bool(action != "add" or note_text)
        return PlanStep(
            order=0,
            target="notes",
            action=action,
            input_text=raw_text if action != "add" else f"save note {note_text}",
            intent_name="note",
            entities={**entities, "text": note_text} if note_text else dict(entities),
            can_execute=can_execute,
            skip_reason="" if can_execute else "Missing note text.",
            description=f"Save note: {note_text}" if action == "add" and note_text else "Run note command.",
        )

    def _note_step_from_text(self, text: str):
        lowered = text.lower()
        search_keyword = _note_search_keyword(text)
        if search_keyword is not None:
            return self._note_search_step(text, search_keyword)

        if lowered == "delete all notes":
            return self._note_step(text, {"action": "delete_all_request"})

        if lowered.startswith("confirm delete all notes"):
            return self._note_step(text, {"action": "delete_all_confirm"})

        if lowered.startswith("delete note"):
            return self._note_step(
                text,
                {"action": "delete", "note_id": _first_word(_strip_prefix(text, "delete note"))},
            )

        if lowered.startswith("search notes"):
            return self._note_step(
                text,
                {"action": "search", "keyword": _strip_prefix(text, "search notes")},
            )

        if lowered.startswith(("remember this", "save note", "take a note")):
            note_text = _strip_note_prefix(text)
        elif lowered.startswith("remember "):
            note_text = _strip_prefix(text, "remember")
        else:
            return None

        return self._note_step(
            f"save note {note_text}",
            {"action": "add", "text": note_text},
        )

    def _note_search_step(self, raw_text: str, keyword: str) -> PlanStep:
        clean_keyword = _clean_clause(keyword)
        if not clean_keyword:
            return self._context_response_step(
                raw_text,
                "I need a note topic to search.",
                context_type="notes",
                reason="missing_note_keyword",
            )

        if not self.notes_store:
            return self._context_response_step(
                raw_text,
                "I do not have notes context available yet.",
                context_type="notes",
                reason="missing_notes_store",
                extra_entities={"keyword": clean_keyword},
            )

        matches = self.notes_store.search(clean_keyword)
        if not matches:
            return self._context_response_step(
                raw_text,
                f'I do not have notes about "{clean_keyword}" yet.',
                context_type="notes",
                reason="empty_note_search",
                extra_entities={"keyword": clean_keyword},
            )

        return self._note_step(
            f"search notes {clean_keyword}",
            {
                "action": "search",
                "keyword": clean_keyword,
                "context_source": "notes",
                "context_count": len(matches),
            },
        )

    def _task_step(self, raw_text: str, entities: Dict[str, Any]) -> PlanStep:
        action = entities.get("action") or "add"
        task_text = entities.get("text") or _strip_task_prefix(raw_text)
        due = entities.get("due")
        if not due:
            task_text, due = _split_due_text(task_text)

        if action == "add" and not entities.get("context_resolved"):
            context_step = self._contextual_task_step(raw_text, task_text, due)
            if context_step:
                return context_step

        can_execute = bool(action != "add" or task_text)
        command = f"add task {task_text}" if action == "add" else raw_text
        if action == "add" and due:
            command = f"{command} due {due}"

        clean_entities = dict(entities)
        if task_text:
            clean_entities["text"] = task_text
        if due:
            clean_entities["due"] = due

        return PlanStep(
            order=0,
            target="tasks",
            action=action,
            input_text=command,
            intent_name="task",
            entities=clean_entities,
            can_execute=can_execute,
            skip_reason="" if can_execute else "Missing task text.",
            description=_task_description(task_text, due) if action == "add" else "Run task command.",
        )

    def _task_step_from_text(self, text: str):
        lowered = text.lower()
        if lowered == "clear completed tasks":
            return self._task_step(text, {"action": "clear_completed"})
        if lowered.startswith("delete task"):
            return self._task_step(
                text,
                {"action": "delete", "task_id": _first_word(_strip_prefix(text, "delete task"))},
            )
        if lowered.startswith("mark task") and lowered.endswith(" done"):
            return self._task_step(
                text,
                {"action": "mark_done", "task_id": _first_word(_strip_prefix(text, "mark task"))},
            )
        if lowered.startswith("create a task"):
            task_text = _strip_prefix(text, "create a task")
        elif lowered.startswith("add task"):
            task_text = _strip_prefix(text, "add task")
        elif lowered.startswith("remind me to"):
            task_text = _strip_prefix(text, "remind me to")
        elif lowered.startswith("remind me about"):
            task_text = _strip_prefix(text, "remind me about")
        elif lowered.startswith("remember to"):
            task_text = _strip_prefix(text, "remember to")
        else:
            return None

        task_text, due = _split_due_text(task_text)
        if not task_text and due:
            task_text = "task"
        return self._task_step(
            text,
            {"action": "add", "text": task_text, "due": due},
        )

    def _contextual_task_step(self, raw_text: str, task_text: str, due: str):
        if _is_main_goal_reference(task_text):
            if not self.goals_store:
                return self._context_response_step(
                    raw_text,
                    "I do not have goal context available yet.",
                    context_type="goals",
                    reason="missing_goals_store",
                )

            goal = self._main_goal()
            if not goal:
                return self._context_response_step(
                    raw_text,
                    "I do not have any goals yet.",
                    context_type="goals",
                    reason="empty_goals",
                )

            return self._task_step(
                raw_text,
                {
                    "action": "add",
                    "text": f"Review goal: {goal.title}",
                    "due": due,
                    "context_resolved": True,
                    "context_source": "goals",
                    "context_goal_id": goal.id,
                    "context_goal_title": goal.title,
                },
            )

        favorite_subject = _favorite_subject(task_text)
        if favorite_subject is not None:
            if not self.profile_store:
                return self._context_response_step(
                    raw_text,
                    "I do not have profile context available yet.",
                    context_type="profile",
                    reason="missing_profile_store",
                    extra_entities={"subject": favorite_subject},
                )

            favorite = self.profile_store.get_favorite(favorite_subject)
            if not favorite:
                return self._context_response_step(
                    raw_text,
                    f'I do not know your favorite {favorite_subject} yet.',
                    context_type="profile",
                    reason="missing_favorite",
                    extra_entities={"subject": favorite_subject},
                )

            return self._task_step(
                raw_text,
                {
                    "action": "add",
                    "text": f"Review favorite {favorite_subject}: {favorite}",
                    "due": due,
                    "context_resolved": True,
                    "context_source": "profile",
                    "context_subject": favorite_subject,
                    "context_value": favorite,
                },
            )

        return None

    def _device_action_step(self, raw_text: str, entities: Dict[str, Any]) -> PlanStep:
        action = entities.get("action") or "execute"
        action_name = entities.get("action_name") or _device_action_name(raw_text)
        parameters = dict(entities.get("parameters") or {})
        if action_name == "open_app":
            app_id = _normalize_action_name(str(entities.get("app_id") or parameters.get("app_id") or ""))
            if app_id:
                parameters["app_id"] = app_id
                entities = {**entities, "app_id": app_id}
        safety = classify_device_action(action_name)
        classification = entities.get("danger_classification") or safety.classification
        reason = entities.get("reason") or ""
        can_execute = bool(action_name)
        skip_reason = "" if can_execute else "Missing device action name."
        if action_name == "open_app" and not parameters.get("app_id"):
            can_execute = False
            skip_reason = "Missing app id."

        clean_entities = {
            **entities,
            "action": action,
            "action_name": action_name,
            "parameters": parameters,
            "danger_classification": classification,
            "confirmation_required": classification == "confirmation_required",
            "forbidden": classification == "forbidden",
        }
        if reason:
            clean_entities["reason"] = reason

        return PlanStep(
            order=0,
            target="device_action",
            action=action_name or action,
            input_text=_device_action_command(action_name, parameters, raw_text),
            intent_name="device_action",
            entities=clean_entities,
            can_execute=can_execute,
            skip_reason=skip_reason,
            description=_device_action_description(action_name, parameters, classification),
        )

    def _device_action_step_from_text(self, text: str):
        entities = _device_action_entities(text)
        if not entities:
            return None
        return self._device_action_step(text, entities)

    def _calculator_step(self, raw_text: str, entities: Dict[str, Any]) -> PlanStep:
        expression = entities.get("expression") or _extract_expression(raw_text)
        can_execute = bool(expression)
        return PlanStep(
            order=0,
            target="calculator",
            action="calculate",
            input_text=f"calculate {expression}" if expression else raw_text,
            intent_name="calculate",
            entities={"action": "calculate", "expression": expression} if expression else dict(entities),
            can_execute=can_execute,
            skip_reason="" if can_execute else "Missing calculator expression.",
            description=f"Calculate expression: {expression}" if expression else "Calculate expression.",
        )

    def _calculator_step_from_text(self, text: str):
        expression = _extract_expression(text)
        if not expression:
            return None
        return self._calculator_step(
            text,
            {"action": "calculate", "expression": expression},
        )

    def _weather_step(self, raw_text: str, entities: Dict[str, Any]) -> PlanStep:
        location = entities.get("location") or _weather_location(raw_text)
        period = entities.get("period") or _weather_period(raw_text)
        capability = entities.get("capability") or ("weather.forecast" if period == "tomorrow" else "weather.current")
        clean_entities = {
            **entities,
            "action": "weather",
            "location": location,
            "period": period,
            "adapter_name": entities.get("adapter_name") or "mock_weather",
            "capability": capability,
        }
        command = "weather"
        if period == "tomorrow":
            command = "weather tomorrow"
        elif period == "today":
            command = "weather today"
        if location and location != "local":
            command = f"{command} in {location}"

        return PlanStep(
            order=0,
            target="weather",
            action="weather",
            input_text=command,
            intent_name="weather",
            entities=clean_entities,
            can_execute=True,
            description=f"Check mock weather for {location}.",
        )

    def _weather_step_from_text(self, text: str):
        if not _looks_like_weather(text):
            return None
        return self._weather_step(
            text,
            {
                "action": "weather",
                "location": _weather_location(text),
                "period": _weather_period(text),
            },
        )

    def _market_step(self, raw_text: str, entities: Dict[str, Any]) -> PlanStep:
        symbol = entities.get("symbol") or _market_symbol(raw_text)
        capability = entities.get("capability") or "market.quote"
        clean_entities = {
            **entities,
            "action": "quote",
            "symbol": symbol,
            "adapter_name": entities.get("adapter_name") or "mock_market",
            "capability": capability,
        }
        can_execute = bool(symbol)
        command = f"stock {symbol}" if symbol else raw_text

        return PlanStep(
            order=0,
            target="market",
            action="quote",
            input_text=command,
            intent_name="market",
            entities=clean_entities,
            can_execute=can_execute,
            skip_reason="" if can_execute else "Missing market symbol.",
            description=f"Check mock market quote for {symbol}." if symbol else "Check mock market quote.",
        )

    def _market_step_from_text(self, text: str):
        if not _looks_like_market(text):
            return None
        return self._market_step(
            text,
            {
                "action": "quote",
                "symbol": _market_symbol(text),
            },
        )

    def _calendar_step(self, raw_text: str, entities: Dict[str, Any]) -> PlanStep:
        period = entities.get("period") or _calendar_period(raw_text)
        clean_entities = {
            **entities,
            "action": "list",
            "period": period,
            "adapter_name": entities.get("adapter_name") or "mock_calendar",
            "capability": entities.get("capability") or "calendar.events",
        }

        return PlanStep(
            order=0,
            target="calendar",
            action="list",
            input_text=f"calendar {period}",
            intent_name="calendar",
            entities=clean_entities,
            can_execute=True,
            description=f"Check mock calendar for {period}.",
        )

    def _calendar_step_from_text(self, text: str):
        if not _looks_like_calendar(text):
            return None
        return self._calendar_step(
            text,
            {
                "action": "list",
                "period": _calendar_period(text),
            },
        )

    def _event_history_step(self, raw_text: str, entities: Dict[str, Any]) -> PlanStep:
        query_type = entities.get("query_type") or _event_history_query_type(raw_text) or "recent"
        clean_entities = {
            **entities,
            "action": query_type,
            "query_type": query_type,
        }
        if query_type == "critical":
            clean_entities["priority"] = "critical"
            command = "show critical events"
            description = "Show recent critical internal events."
        else:
            command = "show recent events"
            description = "Show recent internal events."

        return PlanStep(
            order=0,
            target="event_history",
            action=query_type,
            input_text=command,
            intent_name="event_history",
            entities=clean_entities,
            can_execute=True,
            description=description,
        )

    def _event_history_step_from_text(self, text: str):
        query_type = _event_history_query_type(text)
        if not query_type:
            return None
        entities = {"action": query_type, "query_type": query_type}
        if query_type == "critical":
            entities["priority"] = "critical"
        return self._event_history_step(text, entities)

    def _memory_step_from_text(self, text: str):
        content = _memory_content(text)
        if not content:
            return None
        return PlanStep(
            order=0,
            target="conversation_memory",
            action="remember",
            input_text=content,
            intent_name="conversation_memory",
            entities={"content": content},
            can_execute=True,
            description=f"Store long-term memory: {content}",
        )

    def _context_response_step(
        self,
        raw_text: str,
        response_text: str,
        context_type: str,
        reason: str,
        extra_entities: Dict[str, Any] = None,
    ) -> PlanStep:
        entities = {
            "text": response_text,
            "context_type": context_type,
            "reason": reason,
        }
        if extra_entities:
            entities.update(extra_entities)

        return PlanStep(
            order=0,
            target="planner_context",
            action="respond",
            input_text=response_text,
            intent_name="planner_context",
            entities=entities,
            can_execute=True,
            description="Return a context-aware planner response.",
        )

    def _main_goal(self):
        goals = self.goals_store.list()
        candidates = [goal for goal in goals if getattr(goal, "status", "active") == "active"]
        if not candidates:
            candidates = goals
        if not candidates:
            return None

        indexed = list(enumerate(candidates))
        indexed.sort(key=lambda item: (-_priority_score(getattr(item[1], "priority", "")), item[0]))
        return indexed[0][1]

    def _next_task_for_goal(self, goal):
        if not self.tasks_store:
            return None

        goal_tokens = {
            token
            for token in _tokens(getattr(goal, "title", ""))
            if len(token) >= 3
        }
        if not goal_tokens:
            return None

        for task in self.tasks_store.list():
            if getattr(task, "completed", False):
                continue
            task_tokens = set(_tokens(getattr(task, "text", "")))
            if goal_tokens & task_tokens:
                return task
        return None


def _split_clauses(text: str) -> List[str]:
    return [
        _clean_clause(part)
        for part in re.split(r"\s+(?:and|then)\s+", text or "", flags=re.IGNORECASE)
        if _clean_clause(part)
    ]


def _clean_clause(text: str) -> str:
    return (text or "").strip().strip(" .!?").strip()


def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _is_next_goal_question(text: str) -> bool:
    lowered = (text or "").lower().strip(" ?!.")
    return lowered in {
        "what should i do next for my goals",
        "what should i do next for my goal",
        "next for my goals",
        "next for my goal",
    }


def _is_main_goal_reference(text: str) -> bool:
    lowered = (text or "").lower().strip(" ?!.")
    if lowered in {"my goal", "my goals", "main goal", "my main goal"}:
        return True
    return bool(re.search(r"\bmy\s+main\s+goal\b|\bmain\s+goal\b", lowered))


def _favorite_subject(text: str):
    match = re.search(r"\bmy\s+favorite\s+(.+)$", text or "", flags=re.IGNORECASE)
    if not match:
        return None
    subject = _clean_clause(match.group(1))
    return subject or None


def _note_search_keyword(text: str):
    clean_text = _clean_clause(text)
    patterns = (
        r"^notes\s+about\s+(.+)$",
        r"^show\s+notes\s+about\s+(.+)$",
        r"^show\s+my\s+notes\s+about\s+(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, clean_text, flags=re.IGNORECASE)
        if match:
            return _clean_clause(match.group(1))
    return None


def _priority_score(priority: str) -> int:
    normalized = (priority or "").strip().lower()
    return {
        "critical": 4,
        "urgent": 4,
        "high": 3,
        "normal": 2,
        "medium": 2,
        "low": 1,
    }.get(normalized, 2)


def _renumber_step(step: PlanStep, order: int) -> PlanStep:
    return PlanStep(
        order=order,
        target=step.target,
        action=step.action,
        input_text=step.input_text,
        intent_name=step.intent_name,
        entities=dict(step.entities),
        can_execute=step.can_execute,
        skip_reason=step.skip_reason,
        description=step.description,
    )


def _strip_note_prefix(text: str) -> str:
    for prefix in ("remember this", "save note", "take a note", "remember"):
        if text.lower().startswith(prefix):
            return _strip_prefix(text, prefix)
    return _clean_clause(text)


def _strip_task_prefix(text: str) -> str:
    for prefix in ("create a task", "add task", "remind me to", "remember to", "remember"):
        if text.lower().startswith(prefix):
            return _strip_prefix(text, prefix)
    return _clean_clause(text)


def _strip_goal_prefix(text: str) -> str:
    for prefix in ("add goal", "goal"):
        if text.lower().startswith(prefix):
            return _strip_prefix(text, prefix)
    return _clean_clause(text)


def _strip_prefix(text: str, prefix: str) -> str:
    return _clean_clause(text[len(prefix) :].lstrip(" :-"))


def _first_word(text: str) -> str:
    clean_text = _clean_clause(text)
    return clean_text.split()[0] if clean_text.split() else ""


def _split_first_word(text: str):
    clean_text = _clean_clause(text)
    if not clean_text:
        return "", ""
    parts = clean_text.split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], _clean_clause(parts[1])


def _split_goal_fields(text: str):
    remaining = _clean_clause(text)
    priority = "normal"
    description = ""

    priority_match = re.search(r"\s+priority\s+([a-z0-9_-]+)\s*$", remaining, flags=re.IGNORECASE)
    if priority_match:
        priority = priority_match.group(1).strip()
        remaining = _clean_clause(remaining[: priority_match.start()])

    description_match = re.search(r"\s+description\s+(.+)$", remaining, flags=re.IGNORECASE)
    if description_match:
        description = _clean_clause(description_match.group(1))
        remaining = _clean_clause(remaining[: description_match.start()])

    return remaining, description, priority


def _extract_expression(text: str) -> str:
    clean_text = _clean_clause(text)
    lowered = clean_text.lower()
    for prefix in ("calculate", "calculator", "compute", "solve", "what is", "what's", "how much is"):
        if lowered.startswith(prefix):
            candidate = _strip_prefix(clean_text, prefix)
            return candidate if _looks_like_arithmetic(candidate) else ""

    return clean_text if _looks_like_arithmetic(clean_text) else ""


def _looks_like_arithmetic(text: str) -> bool:
    return bool(re.search(r"\d", text or "") and re.search(r"[+\-*/^]", text or ""))


def _memory_content(text: str) -> str:
    clean_text = _clean_clause(text)
    lowered = clean_text.lower()
    if not lowered.startswith("remember "):
        return ""

    content = _strip_prefix(clean_text, "remember")
    content_lowered = content.lower()
    memory_markers = (
        "i like ",
        "i love ",
        "i prefer ",
        "i am ",
        "i live ",
        "i own ",
        "my name ",
        "my birthday ",
        "my favorite ",
    )
    if content_lowered.startswith(memory_markers):
        return content
    return ""


def _split_due_text(text: str):
    clean_text = _clean_clause(text)
    if not clean_text:
        return "", None

    due_match = re.search(
        r"\b(?:due|for)\s+(today|tomorrow|next week|in \d+ minutes?|in \d+ hours?|at [0-2]?\d:[0-5]\d)\b",
        clean_text,
        flags=re.IGNORECASE,
    )
    if due_match:
        due = due_match.group(1)
        task_text = _clean_clause(clean_text[: due_match.start()] + clean_text[due_match.end() :])
        return task_text, due.lower()

    suffix_match = re.search(
        r"\b(today|tomorrow|next week|in \d+ minutes?|in \d+ hours?|at [0-2]?\d:[0-5]\d)$",
        clean_text,
        flags=re.IGNORECASE,
    )
    if suffix_match:
        due = suffix_match.group(1)
        task_text = _clean_clause(clean_text[: suffix_match.start()])
        return task_text, due.lower()

    return clean_text, None


def _task_description(task_text: str, due: str) -> str:
    if due:
        return f"Add task: {task_text} due {due}"
    return f"Add task: {task_text}"


def _looks_like_weather(text: str) -> bool:
    lowered = (text or "").lower()
    return bool(re.search(r"\b(weather|forecast)\b", lowered))


def _weather_period(text: str) -> str:
    lowered = (text or "").lower()
    if re.search(r"\btomorrow\b", lowered):
        return "tomorrow"
    return "today"


def _weather_location(text: str) -> str:
    match = re.search(r"\bin\s+(.+)$", text or "", flags=re.IGNORECASE)
    if not match:
        return "local"
    location = _clean_clause(match.group(1))
    return location or "local"


def _looks_like_market(text: str) -> bool:
    lowered = (text or "").lower()
    return bool(re.search(r"\b(stock|market)\b", lowered))


def _market_symbol(text: str) -> str:
    clean_text = _clean_clause(text)
    patterns = (
        r"^stock\s+(.+)$",
        r"^market\s+price\s+for\s+(.+)$",
        r"^market\s+quote\s+for\s+(.+)$",
        r"^(.+?)\s+stock$",
    )
    for pattern in patterns:
        match = re.match(pattern, clean_text, flags=re.IGNORECASE)
        if match:
            return _normalize_market_symbol(match.group(1))
    return ""


def _normalize_market_symbol(text: str) -> str:
    symbol = _clean_clause(text)
    symbol = re.sub(r"^(?:the|a|an)\s+", "", symbol, flags=re.IGNORECASE)
    symbol = symbol.replace("$", "").strip()
    return symbol.upper()


def _device_action_entities(text: str):
    clean_text = _clean_clause(text)
    lowered = " ".join(_tokens(clean_text))
    if not lowered:
        return None

    if lowered in {"list apps", "show apps", "list available apps"}:
        return {
            "action": "list",
            "action_name": "list_apps",
            "parameters": {},
            "danger_classification": DANGER_SAFE,
        }

    if lowered.startswith("open app"):
        app_id = _normalize_action_name(clean_text[len("open app") :])
        safety = classify_device_action("open_app")
        return {
            "action": safety.classification,
            "action_name": safety.action_name,
            "app_id": app_id,
            "parameters": {"app_id": app_id} if app_id else {},
            "danger_classification": safety.classification,
            "confirmation_required": safety.requires_confirmation,
            "forbidden": safety.forbidden,
            "reason": safety.reason,
        }

    dangerous_action_name = _dangerous_device_action_name(lowered)
    if dangerous_action_name:
        safety = classify_device_action(dangerous_action_name)
        return {
            "action": safety.classification,
            "action_name": safety.action_name,
            "parameters": {},
            "danger_classification": safety.classification,
            "confirmation_required": safety.requires_confirmation,
            "forbidden": safety.forbidden,
            "reason": safety.reason,
        }

    if lowered.startswith("echo "):
        message = _clean_clause(clean_text[len("echo") :])
        if not message:
            return None
        return {
            "action": "echo",
            "action_name": "echo",
            "parameters": {"message": message},
            "danger_classification": DANGER_SAFE,
        }

    if lowered in {"list device actions", "show device actions", "list available device actions"}:
        return {
            "action": "list",
            "action_name": "list_actions",
            "parameters": {},
            "danger_classification": DANGER_SAFE,
        }

    if lowered in {"system status", "device status"}:
        return {
            "action": "status",
            "action_name": "system_status_mock",
            "parameters": {},
            "danger_classification": DANGER_SAFE,
        }

    if lowered.startswith("device action "):
        action_name = _normalize_action_name(lowered[len("device action ") :])
        safety = classify_device_action(action_name)
        return {
            "action": "execute",
            "action_name": action_name,
            "parameters": {},
            "danger_classification": safety.classification,
            "confirmation_required": safety.requires_confirmation,
            "forbidden": safety.forbidden,
            "reason": safety.reason,
        }

    return None


def _device_action_name(text: str) -> str:
    entities = _device_action_entities(text)
    if not entities:
        return ""
    return str(entities.get("action_name") or "")


def _device_action_command(action_name: str, parameters: Dict[str, Any], fallback_text: str) -> str:
    if action_name == "echo":
        return f"echo {parameters.get('message') or ''}".strip()
    if action_name == "list_actions":
        return "list device actions"
    if action_name == "list_apps":
        return "list apps"
    if action_name == "open_app":
        app_id = parameters.get("app_id") or ""
        return f"open app {app_id}".strip()
    if action_name == "system_status_mock":
        return "system status"
    if action_name == "lock_pc":
        return "lock pc"
    if action_name == "sleep_pc":
        return "sleep pc"
    return fallback_text


def _device_action_description(action_name: str, parameters: Dict[str, Any], classification: str) -> str:
    if classification == "confirmation_required":
        return f"Require confirmation for device action: {action_name}."
    if classification == "forbidden":
        return f"Reject forbidden device action: {action_name}."
    if action_name == "echo":
        return f"Run safe echo action: {parameters.get('message') or ''}"
    if action_name == "list_actions":
        return "List safe local device actions."
    if action_name == "list_apps":
        return "List allowlisted local apps."
    if action_name == "open_app":
        app_id = parameters.get("app_id") or "requested app"
        return f"Require confirmation to open allowlisted Windows app: {app_id}."
    if action_name == "system_status_mock":
        return "Return mock system status."
    return f"Run safe device action: {action_name}."


def _dangerous_device_action_name(normalized_text: str) -> str:
    if normalized_text in {"lock", "lock pc", "lock computer", "lock session", "lock windows", "lock windows session"}:
        return "lock_pc"
    if normalized_text in {"sleep", "sleep pc", "sleep computer", "sleep session", "sleep windows", "sleep windows pc"}:
        return "sleep_pc"
    if normalized_text in {"shutdown", "restart"}:
        return normalized_text
    if normalized_text.startswith("run command"):
        return "run_command"
    if normalized_text.startswith("open app"):
        return "open_app"
    if normalized_text == "delete" or normalized_text.startswith("delete "):
        return "delete"
    if "arbitrary shell" in normalized_text or normalized_text.startswith("shell "):
        return "arbitrary_shell"
    return ""


def _normalize_action_name(value: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _looks_like_calendar(text: str) -> bool:
    lowered = (text or "").lower()
    if re.search(r"\b(calendar|schedule)\b", lowered):
        return True
    return lowered.startswith("do i have anything")


def _event_history_query_type(text: str) -> str:
    lowered = " ".join(_tokens(text or ""))
    if lowered in {"show critical events", "critical events"}:
        return "critical"
    if lowered in {"what happened recently", "show recent events", "recent events"}:
        return "recent"
    return ""


def _calendar_period(text: str) -> str:
    lowered = (text or "").lower()
    if re.search(r"\btomorrow\b", lowered):
        return "tomorrow"
    return "today"
