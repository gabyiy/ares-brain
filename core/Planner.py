import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

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


class Planner:
    """Builds deterministic execution plans without executing any skills."""

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

        return Plan(
            raw_text=raw_text,
            intent_name=intent.intent_name,
            steps=ordered_steps,
            errors=errors,
        )

    def _plan_intent(self, intent: Intent):
        if intent.intent_name == "note":
            return self._note_step(intent.raw_text, dict(intent.extracted_entities)), None

        if intent.intent_name == "task":
            return self._task_step(intent.raw_text, dict(intent.extracted_entities)), None

        if intent.intent_name == "calculate":
            return self._calculator_step(intent.raw_text, dict(intent.extracted_entities)), None

        memory_step = self._memory_step_from_text(intent.raw_text)
        if memory_step:
            return memory_step, None

        return None, f"No planner support for intent: {intent.intent_name}"

    def _plan_clause(self, clause: str, parent_intent: Intent):
        clean_clause = _clean_clause(clause)
        if not clean_clause:
            return None, "Skipped empty plan clause."

        calculator_step = self._calculator_step_from_text(clean_clause)
        if calculator_step:
            return calculator_step, None

        memory_step = self._memory_step_from_text(clean_clause)
        if memory_step:
            return memory_step, None

        task_step = self._task_step_from_text(clean_clause)
        if task_step:
            return task_step, None

        note_step = self._note_step_from_text(clean_clause)
        if note_step:
            return note_step, None

        return None, f"Skipped unsupported plan clause: {clean_clause}"

    def _note_step(self, raw_text: str, entities: Dict[str, Any]) -> PlanStep:
        action = entities.get("action") or "add"
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

    def _task_step(self, raw_text: str, entities: Dict[str, Any]) -> PlanStep:
        action = entities.get("action") or "add"
        task_text = entities.get("text") or _strip_task_prefix(raw_text)
        due = entities.get("due")
        if not due:
            task_text, due = _split_due_text(task_text)

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
        if lowered.startswith("create a task"):
            task_text = _strip_prefix(text, "create a task")
        elif lowered.startswith("add task"):
            task_text = _strip_prefix(text, "add task")
        elif lowered.startswith("remind me to"):
            task_text = _strip_prefix(text, "remind me to")
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


def _split_clauses(text: str) -> List[str]:
    return [
        _clean_clause(part)
        for part in re.split(r"\s+(?:and|then)\s+", text or "", flags=re.IGNORECASE)
        if _clean_clause(part)
    ]


def _clean_clause(text: str) -> str:
    return (text or "").strip().strip(" .!?").strip()


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


def _strip_prefix(text: str, prefix: str) -> str:
    return _clean_clause(text[len(prefix) :].lstrip(" :-"))


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
