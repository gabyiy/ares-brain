import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional

from core.Confirmation import ConfirmationManager, mark_confirmation_approved, requires_confirmation
from core.Intent import Intent
from core.Planner import Plan, PlanStep
from core.ToolAdapter import ToolRequest


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class StepResult:
    order: int
    target: str
    action: str
    input_text: str
    start_time: str
    end_time: str
    duration: float
    success: bool
    returned_data: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    recoverable: bool = True
    redact_operational_events: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "target": self.target,
            "action": self.action,
            "input_text": self.input_text,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": self.duration,
            "success": self.success,
            "failure": not self.success,
            "returned_data": dict(self.returned_data),
            "error_message": self.error_message,
            "recoverable": self.recoverable,
        }


@dataclass(frozen=True)
class ExecutionResult:
    plan: Plan
    step_results: List[StepResult] = field(default_factory=list)
    success: bool = False
    stopped: bool = False
    start_time: str = ""
    end_time: str = ""
    duration: float = 0.0
    error_message: str = ""
    rollback_attempted: bool = False
    rollback_error: str = ""
    pending_confirmation: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "step_results": [result.to_dict() for result in self.step_results],
            "success": self.success,
            "stopped": self.stopped,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": self.duration,
            "error_message": self.error_message,
            "rollback_attempted": self.rollback_attempted,
            "rollback_error": self.rollback_error,
            "pending_confirmation": dict(self.pending_confirmation),
        }

    def to_event_dict(self) -> Dict[str, Any]:
        return _execution_event_payload(self)

    def format(self) -> str:
        if not self.step_results:
            if self.error_message:
                return f"Execution failed: {self.error_message}"
            return "No execution results are available yet."

        lines = ["Execution results:"]
        for result in self.step_results:
            status = "ok" if result.success else "failed"
            text = result.returned_data.get("text") or result.error_message or "No response."
            lines.append(f"{result.order}. {result.target}.{result.action} - {status}: {text}")
        if self.stopped and self.error_message:
            lines.append(f"Stopped: {self.error_message}")
        return "\n".join(lines)

    def format_response_text(self) -> str:
        if not self.step_results:
            return self.format()

        if len(self.step_results) == 1:
            result = self.step_results[0]
            return result.returned_data.get("text") or result.error_message or self.format()

        has_success = any(result.success for result in self.step_results)
        has_failure = any(not result.success for result in self.step_results)
        heading = "Partial results:" if has_success and has_failure else "Plan results:"
        lines = [heading]
        for index, result in enumerate(self.step_results, start=1):
            text = result.returned_data.get("text") or result.error_message or "No response."
            lines.append(f"{index}. {text}")
        return "\n".join(lines)


class RollbackHook:
    """No-op rollback extension point for future reversible local actions."""

    def rollback(self, completed_steps: List[StepResult], failed_step: StepResult, context: Any) -> None:
        return None


class ExecutionPipeline:
    """Executes planner steps sequentially through injected local capabilities."""

    def __init__(
        self,
        skill_resolver: Callable[[str], Any],
        event_bus: Any = None,
        memory_store: Any = None,
        tool_adapter_registry: Any = None,
        rollback_hook: Optional[RollbackHook] = None,
        logger: Optional[logging.Logger] = None,
        context_builder: Optional[Callable[[Any, Intent], Any]] = None,
        confirmation_manager: Optional[ConfirmationManager] = None,
    ):
        self.skill_resolver = skill_resolver
        self.event_bus = event_bus
        self.memory_store = memory_store
        self.tool_adapter_registry = tool_adapter_registry
        self.rollback_hook = rollback_hook or RollbackHook()
        self.logger = logger or logging.getLogger("ares.execution")
        self.context_builder = context_builder or _context_with_intent
        self.confirmation_manager = confirmation_manager or ConfirmationManager()

    def execute(self, plan: Plan, context: Any = None) -> ExecutionResult:
        start_time = _utc_now()
        started_at = perf_counter()
        step_results = []
        stopped = False
        error_message = ""
        rollback_attempted = False
        rollback_error = ""
        pending_confirmation = {}

        self.logger.info("Execution started for intent %s", plan.intent_name)
        self._publish("execution.started", {"plan": plan.to_dict(), "start_time": start_time})

        for step in sorted(plan.steps, key=lambda item: item.order):
            result = self._execute_step(step, context)
            step_results.append(result)

            confirmation = _result_confirmation(result)
            if confirmation:
                stopped = True
                error_message = "Confirmation required."
                pending_confirmation = confirmation
                break

            if result.success or result.recoverable:
                continue

            stopped = True
            error_message = result.error_message
            rollback_attempted, rollback_error = self._rollback(step_results[:-1], result, context)
            break

        end_time = _utc_now()
        duration = round(perf_counter() - started_at, 6)
        success = bool(step_results) and all(result.success for result in step_results) and not stopped
        if not step_results and plan.errors:
            error_message = "; ".join(plan.errors)

        execution = ExecutionResult(
            plan=plan,
            step_results=step_results,
            success=success,
            stopped=stopped,
            start_time=start_time,
            end_time=end_time,
            duration=duration,
            error_message=error_message,
            rollback_attempted=rollback_attempted,
            rollback_error=rollback_error,
            pending_confirmation=pending_confirmation,
        )
        self._publish("execution.completed", _execution_event_payload(execution))
        self.logger.info("Execution completed: success=%s stopped=%s", execution.success, execution.stopped)
        return execution

    def execute_confirmed(self, request, context: Any = None) -> ExecutionResult:
        confirmed_step = mark_confirmation_approved(request.step, request.id)
        plan = Plan(
            raw_text=request.action_label,
            intent_name=confirmed_step.intent_name,
            steps=[confirmed_step],
        )
        return self.execute(plan, context)

    def _execute_step(self, step: PlanStep, context: Any) -> StepResult:
        start_time = _utc_now()
        started_at = perf_counter()
        self.logger.info("Execution step started: %s.%s", step.target, step.action)
        self._publish("execution.step_started", {"step": step.to_dict(), "start_time": start_time})

        if not step.can_execute:
            return self._finish_step(
                step=step,
                start_time=start_time,
                started_at=started_at,
                success=False,
                returned_data={},
                error_message=step.skip_reason or "Step cannot execute.",
                recoverable=True,
            )

        if requires_confirmation(step):
            return self._execute_confirmation_step(step, start_time, started_at)

        if step.target == "conversation_memory":
            return self._execute_memory_step(step, context, start_time, started_at)

        if step.target == "planner_context":
            return self._execute_planner_context_step(step, context, start_time, started_at)

        if step.target == "tool_adapter":
            return self._execute_tool_adapter_step(step, context, start_time, started_at)

        skill = self.skill_resolver(step.target)
        if not skill:
            return self._finish_step(
                step=step,
                start_time=start_time,
                started_at=started_at,
                success=False,
                returned_data={"missing_skill": step.target},
                error_message=f"Skill is not available: {step.target}",
                recoverable=False,
            )

        intent = Intent(
            intent_name=step.intent_name,
            confidence=1.0,
            extracted_entities=dict(step.entities),
            raw_text=step.input_text,
        )
        step_context = self.context_builder(context, intent)

        try:
            response = skill.handle(step.input_text, step_context)
        except Exception as error:
            self.logger.exception("Execution step raised an unrecoverable error: %s.%s", step.target, step.action)
            return self._finish_step(
                step=step,
                start_time=start_time,
                started_at=started_at,
                success=False,
                returned_data={},
                error_message=f"{type(error).__name__}: {error}",
                recoverable=False,
            )

        returned_data = _response_to_data(response, default_skill=skill.name)
        error_message = _response_error(returned_data)
        return self._finish_step(
            step=step,
            start_time=start_time,
            started_at=started_at,
            success=not bool(error_message),
            returned_data=returned_data,
            error_message=error_message,
            recoverable=not bool(returned_data.get("metadata", {}).get("unrecoverable")),
        )

    def _execute_memory_step(
        self,
        step: PlanStep,
        context: Any,
        start_time: str,
        started_at: float,
    ) -> StepResult:
        memory_store = getattr(context, "memory_store", None) or self.memory_store
        if not memory_store:
            return self._finish_step(
                step=step,
                start_time=start_time,
                started_at=started_at,
                success=False,
                returned_data={},
                error_message="Memory storage is not available.",
                recoverable=False,
            )

        content = (step.entities.get("content") or step.input_text or "").strip()
        if not content:
            return self._finish_step(
                step=step,
                start_time=start_time,
                started_at=started_at,
                success=False,
                returned_data={},
                error_message="Missing memory content.",
                recoverable=True,
            )

        try:
            memory = memory_store.remember(
                content=content,
                category="conversation_memory",
                importance=0.85,
                tags=["conversation", "planner"],
                long_term=True,
                metadata={"plan_step": step.to_dict()},
                source="core.execution_pipeline",
            )
        except Exception as error:
            self.logger.exception("Execution memory step raised an unrecoverable error")
            return self._finish_step(
                step=step,
                start_time=start_time,
                started_at=started_at,
                success=False,
                returned_data={},
                error_message=f"{type(error).__name__}: {error}",
                recoverable=False,
            )

        returned_data = {
            "text": f"Stored memory: {content}",
            "skill": "conversation_memory",
            "metadata": {"memory_id": memory.id, "long_term": memory.long_term},
        }
        return self._finish_step(
            step=step,
            start_time=start_time,
            started_at=started_at,
            success=True,
            returned_data=returned_data,
            error_message="",
            recoverable=True,
        )

    def _execute_confirmation_step(
        self,
        step: PlanStep,
        start_time: str,
        started_at: float,
    ) -> StepResult:
        request = self.confirmation_manager.request(step)
        returned_data = {
            "text": request.prompt,
            "skill": "confirmation",
            "metadata": {
                "confirmation_required": True,
                "confirmation": request.to_dict(),
            },
        }
        self._publish("confirmation.requested", request.to_dict())
        return self._finish_step(
            step=step,
            start_time=start_time,
            started_at=started_at,
            success=False,
            returned_data=returned_data,
            error_message="Confirmation required.",
            recoverable=True,
        )

    def _execute_planner_context_step(
        self,
        step: PlanStep,
        context: Any,
        start_time: str,
        started_at: float,
    ) -> StepResult:
        response_text = str(step.entities.get("text") or step.input_text or "").strip()
        if not response_text:
            return self._finish_step(
                step=step,
                start_time=start_time,
                started_at=started_at,
                success=False,
                returned_data={},
                error_message="Missing planner context response.",
                recoverable=True,
            )

        return self._finish_step(
            step=step,
            start_time=start_time,
            started_at=started_at,
            success=True,
            returned_data={
                "text": response_text,
                "skill": "planner_context",
                "metadata": {
                    "context_type": step.entities.get("context_type"),
                    "reason": step.entities.get("reason"),
                },
            },
            error_message="",
            recoverable=True,
        )

    def _execute_tool_adapter_step(
        self,
        step: PlanStep,
        context: Any,
        start_time: str,
        started_at: float,
    ) -> StepResult:
        registry = getattr(context, "tool_adapter_registry", None) or self.tool_adapter_registry
        if not registry:
            return self._finish_step(
                step=step,
                start_time=start_time,
                started_at=started_at,
                success=False,
                returned_data={},
                error_message="Tool adapter registry is not available.",
                recoverable=False,
            )

        request = ToolRequest(
            adapter_name=str(step.entities.get("adapter_name") or step.action or "").strip(),
            capability=str(step.entities.get("capability") or "").strip(),
            query=str(step.entities.get("query") or "").strip(),
            parameters=dict(step.entities.get("parameters") or {}),
            raw_text=step.input_text,
        )
        if not request.adapter_name:
            return self._finish_step(
                step=step,
                start_time=start_time,
                started_at=started_at,
                success=False,
                returned_data={},
                error_message="Missing tool adapter name.",
                recoverable=True,
            )
        if not request.capability:
            return self._finish_step(
                step=step,
                start_time=start_time,
                started_at=started_at,
                success=False,
                returned_data={},
                error_message="Missing tool adapter capability.",
                recoverable=True,
            )

        response = registry.execute(request)
        returned_data = {
            "text": response.text,
            "skill": "tool_adapter",
            "data": dict(response.data),
            "metadata": {
                **dict(response.metadata),
                "adapter_name": response.adapter_name,
                "capability": response.capability,
                "request": request.to_dict(),
            },
        }
        return self._finish_step(
            step=step,
            start_time=start_time,
            started_at=started_at,
            success=response.success,
            returned_data=returned_data,
            error_message=response.error_message,
            recoverable=not bool(response.metadata.get("unrecoverable")),
        )

    def _finish_step(
        self,
        step: PlanStep,
        start_time: str,
        started_at: float,
        success: bool,
        returned_data: Dict[str, Any],
        error_message: str,
        recoverable: bool,
    ) -> StepResult:
        result = StepResult(
            order=step.order,
            target=step.target,
            action=step.action,
            input_text=step.input_text,
            start_time=start_time,
            end_time=_utc_now(),
            duration=round(perf_counter() - started_at, 6),
            success=success,
            returned_data=returned_data,
            error_message=error_message,
            recoverable=recoverable,
            redact_operational_events=step.redact_operational_events,
        )
        event_name = "execution.step_completed" if success else "execution.step_failed"
        self._publish(event_name, _step_event_payload(result))

        if success:
            self.logger.info("Execution step completed: %s.%s", step.target, step.action)
        elif recoverable:
            self.logger.warning("Execution step failed recoverably: %s.%s - %s", step.target, step.action, error_message)
        else:
            self.logger.error("Execution step failed unrecoverably: %s.%s - %s", step.target, step.action, error_message)

        return result

    def _rollback(self, completed_steps: List[StepResult], failed_step: StepResult, context: Any):
        self._publish(
            "execution.rollback_requested",
            {
                "completed_steps": [_step_event_payload(step) for step in completed_steps],
                "failed_step": _step_event_payload(failed_step),
            },
        )
        try:
            self.rollback_hook.rollback(completed_steps, failed_step, context)
        except Exception as error:
            self.logger.exception("Execution rollback hook failed")
            message = f"{type(error).__name__}: {error}"
            self._publish(
                "execution.rollback_failed",
                {"error": message, "failed_step": _step_event_payload(failed_step)},
            )
            return True, message
        return True, ""

    def _publish(self, name: str, payload: Dict[str, Any]) -> None:
        if self.event_bus:
            self.event_bus.publish(name, payload, source="execution_pipeline")


def _response_to_data(response: Any, default_skill: str) -> Dict[str, Any]:
    if isinstance(response, str):
        return {"text": response, "skill": default_skill, "metadata": {}}
    return {
        "text": getattr(response, "text", ""),
        "skill": getattr(response, "skill", default_skill),
        "metadata": dict(getattr(response, "metadata", {}) or {}),
    }


def _step_event_payload(result: StepResult) -> Dict[str, Any]:
    if not result.redact_operational_events:
        return result.to_dict()
    metadata = dict(result.returned_data.get("metadata", {}) or {})
    return {
        "order": result.order,
        "target": result.target,
        "action": result.action,
        "input_text": "[REDACTED]",
        "start_time": result.start_time,
        "end_time": result.end_time,
        "duration": result.duration,
        "success": result.success,
        "failure": not result.success,
        "returned_data": {
            "skill": result.returned_data.get("skill", result.target),
            "metadata": {
                "redacted": True,
                "memory_action": metadata.get("memory_action", result.action),
                "normalized_fact_key": metadata.get("normalized_fact_key", ""),
                "storage_status": metadata.get("storage_status", ""),
            },
        },
        "error_message": result.error_message,
        "recoverable": result.recoverable,
    }


def _execution_event_payload(execution: ExecutionResult) -> Dict[str, Any]:
    payload = execution.to_dict()
    payload["step_results"] = [
        _step_event_payload(result)
        for result in execution.step_results
    ]
    return payload


def _response_error(returned_data: Dict[str, Any]) -> str:
    metadata = dict(returned_data.get("metadata", {}) or {})
    if metadata.get("error"):
        return str(metadata["error"])
    if metadata.get("missing_skill"):
        return str(metadata["missing_skill"])
    if metadata.get("missing"):
        return str(returned_data.get("text") or "Requested item was not found.")
    return ""


def _result_confirmation(result: StepResult) -> Dict[str, Any]:
    metadata = dict(result.returned_data.get("metadata", {}) or {})
    if not metadata.get("confirmation_required"):
        return {}
    confirmation = metadata.get("confirmation") or {}
    return dict(confirmation) if isinstance(confirmation, dict) else {}


def _context_with_intent(context: Any, intent: Intent) -> Any:
    if context is None:
        return context

    metadata = dict(getattr(context, "metadata", {}) or {})
    metadata["intent"] = intent
    metadata["entities"] = dict(intent.extracted_entities)

    return type(context)(
        event_bus=getattr(context, "event_bus", None),
        memory_store=getattr(context, "memory_store", None),
        profile_store=getattr(context, "profile_store", None),
        owner_profile_store=getattr(context, "owner_profile_store", None),
        notes_store=getattr(context, "notes_store", None),
        tasks_store=getattr(context, "tasks_store", None),
        goals_store=getattr(context, "goals_store", None),
        tool_adapter_registry=getattr(context, "tool_adapter_registry", None),
        device_action_adapter=getattr(context, "device_action_adapter", None),
        core_service=getattr(context, "core_service", None),
        event_history_store=getattr(context, "event_history_store", None),
        conversation_context=getattr(context, "conversation_context", None),
        metadata=metadata,
    )
