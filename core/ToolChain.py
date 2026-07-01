from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.ExecutionPipeline import ExecutionResult
from core.Planner import Plan, PlanStep


MAX_CHAIN_DEPTH = 5


@dataclass(frozen=True)
class ToolChainTraceStep:
    order: int
    target: str
    action: str
    input_text: str
    status: str = "pending"
    success: Optional[bool] = None
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "target": self.target,
            "action": self.action,
            "input_text": self.input_text,
            "status": self.status,
            "success": self.success,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class ToolChainResult:
    plan: Plan
    trace: List[ToolChainTraceStep] = field(default_factory=list)
    execution: Optional[ExecutionResult] = None
    success: bool = False
    errors: List[str] = field(default_factory=list)
    max_depth: int = MAX_CHAIN_DEPTH

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "trace": [step.to_dict() for step in self.trace],
            "execution": self.execution.to_dict() if self.execution else None,
            "success": self.success,
            "errors": list(self.errors),
            "max_depth": self.max_depth,
        }

    def format(self) -> str:
        if not self.trace:
            if self.errors:
                return "Tool chain failed:\n" + "\n".join(f"- {error}" for error in self.errors)
            return "No tool chain is available yet."

        lines = ["Tool chain:"]
        for step in self.trace:
            status = step.status
            if step.success is True:
                status = "ok"
            elif step.success is False:
                status = "failed"
            lines.append(f"{step.order}. {step.target}.{step.action} - {status}")
            if step.error_message:
                lines.append(f"   {step.error_message}")
        if self.errors:
            lines.append("Chain errors:")
            lines.extend(f"- {error}" for error in self.errors)
        return "\n".join(lines)

    def format_response_text(self) -> str:
        if self.execution:
            return self.execution.format_response_text()
        return self.format()


class ToolChain:
    """Validates, traces, and executes bounded local tool chains."""

    def __init__(
        self,
        execution_pipeline,
        max_depth: int = MAX_CHAIN_DEPTH,
        event_bus: Any = None,
        history_limit: int = 20,
    ):
        self.execution_pipeline = execution_pipeline
        self.max_depth = max(1, int(max_depth))
        self.event_bus = event_bus
        self.history_limit = max(1, int(history_limit))
        self.last_result: Optional[ToolChainResult] = None
        self._history: List[ToolChainResult] = []

    def execute(self, plan: Plan, context: Any = None) -> ToolChainResult:
        trace = self._build_trace(plan)
        errors = self._validate(plan)
        self._publish(
            "tool_chain.started",
            {
                "plan": plan.to_dict(),
                "trace": [step.to_dict() for step in trace],
                "max_depth": self.max_depth,
            },
        )

        if errors:
            result = ToolChainResult(
                plan=plan,
                trace=[_with_status(step, "rejected") for step in trace],
                execution=None,
                success=False,
                errors=errors,
                max_depth=self.max_depth,
            )
            self._remember(result)
            self._publish("tool_chain.rejected", result.to_dict())
            return result

        execution = self.execution_pipeline.execute(plan, context)
        result = ToolChainResult(
            plan=plan,
            trace=self._trace_from_execution(trace, execution),
            execution=execution,
            success=execution.success,
            errors=[],
            max_depth=self.max_depth,
        )
        self._remember(result)
        self._publish("tool_chain.completed", result.to_dict())
        return result

    def format_last(self) -> str:
        if not self.last_result:
            return "No tool chain is available yet."
        return self.last_result.format()

    def format_history(self, limit: int = 5) -> str:
        if not self._history:
            return "No tool chain history is available yet."

        recent = self._history[-max(1, int(limit)) :]
        lines = ["Tool chain history:"]
        for index, result in enumerate(recent, start=1):
            status = "ok" if result.success else "failed"
            lines.append(f"{index}. {status} - {len(result.trace)} steps - {result.plan.raw_text}")
        return "\n".join(lines)

    def history(self, limit: Optional[int] = None) -> List[ToolChainResult]:
        if limit is None:
            return list(self._history)
        return list(self._history[-max(0, int(limit)) :])

    def _validate(self, plan: Plan) -> List[str]:
        steps = sorted(plan.executable_steps(), key=lambda step: step.order)
        errors = []

        if len(steps) > self.max_depth:
            errors.append(f"Tool chain exceeds max depth {self.max_depth}: {len(steps)} steps.")

        seen = {}
        for step in steps:
            signature = _step_signature(step)
            if signature in seen:
                errors.append(
                    f"Loop detected: step {seen[signature]} and step {step.order} repeat "
                    f"{step.target}.{step.action}."
                )
                break
            seen[signature] = step.order

        return errors

    def _build_trace(self, plan: Plan) -> List[ToolChainTraceStep]:
        return [
            ToolChainTraceStep(
                order=step.order,
                target=step.target,
                action=step.action,
                input_text=step.input_text,
            )
            for step in sorted(plan.executable_steps(), key=lambda item: item.order)
        ]

    def _trace_from_execution(
        self,
        trace: List[ToolChainTraceStep],
        execution: ExecutionResult,
    ) -> List[ToolChainTraceStep]:
        results = {result.order: result for result in execution.step_results}
        updated = []
        for step in trace:
            result = results.get(step.order)
            if not result:
                updated.append(_with_status(step, "not_run"))
                continue
            updated.append(
                ToolChainTraceStep(
                    order=step.order,
                    target=step.target,
                    action=step.action,
                    input_text=step.input_text,
                    status="ok" if result.success else "failed",
                    success=result.success,
                    error_message=result.error_message,
                )
            )
        return updated

    def _remember(self, result: ToolChainResult) -> None:
        self.last_result = result
        self._history.append(result)
        self._history = self._history[-self.history_limit :]

    def _publish(self, name: str, payload: Dict[str, Any]) -> None:
        if self.event_bus:
            self.event_bus.publish(name, payload, source="tool_chain")


def _with_status(step: ToolChainTraceStep, status: str) -> ToolChainTraceStep:
    return ToolChainTraceStep(
        order=step.order,
        target=step.target,
        action=step.action,
        input_text=step.input_text,
        status=status,
        success=False if status in ("failed", "rejected") else step.success,
        error_message=step.error_message,
    )


def _step_signature(step: PlanStep) -> str:
    normalized_input = " ".join((step.input_text or "").lower().split())
    return f"{step.target}:{step.action}:{normalized_input}"
