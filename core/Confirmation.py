import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.Planner import PlanStep


CONFIRMATION_TTL_SECONDS = 300


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ConfirmationRequest:
    id: str
    step: PlanStep
    prompt: str
    action_label: str
    reason: str
    created_at: str
    expires_at: str

    @classmethod
    def create(cls, step: PlanStep, reason: str, ttl_seconds: int = CONFIRMATION_TTL_SECONDS):
        created_at = _utc_now()
        expires_at = created_at + timedelta(seconds=max(1, int(ttl_seconds)))
        action_label = confirmation_action_label(step)
        request_id = f"confirm-{uuid.uuid4().hex}"
        prompt = (
            f"Confirmation required to {action_label}. "
            "Reply yes or confirm to proceed, or no or cancel to stop. "
            f"Confirmation id: {request_id}"
        )
        return cls(
            id=request_id,
            step=step,
            prompt=prompt,
            action_label=action_label,
            reason=reason,
            created_at=_format_utc(created_at),
            expires_at=_format_utc(expires_at),
        )

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        current = now or _utc_now()
        expires_at = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        return current >= expires_at

    def to_dict(self):
        return {
            "id": self.id,
            "step": self.step.to_dict(),
            "prompt": self.prompt,
            "action_label": self.action_label,
            "reason": self.reason,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class ConfirmationDecision:
    confirmation_id: str
    decision: str
    accepted: bool
    raw_text: str
    message: str
    request: Optional[ConfirmationRequest] = None

    def to_dict(self):
        return {
            "confirmation_id": self.confirmation_id,
            "decision": self.decision,
            "accepted": self.accepted,
            "raw_text": self.raw_text,
            "message": self.message,
            "request": self.request.to_dict() if self.request else None,
        }


class ConfirmationManager:
    """Keeps one in-memory pending confirmation for the active runtime."""

    def __init__(self, ttl_seconds: int = CONFIRMATION_TTL_SECONDS):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._pending: Optional[ConfirmationRequest] = None

    def request(self, step: PlanStep, reason: str = "important_action") -> ConfirmationRequest:
        self._pending = ConfirmationRequest.create(step, reason=reason, ttl_seconds=self.ttl_seconds)
        return self._pending

    def pending(self) -> Optional[ConfirmationRequest]:
        if self._pending and self._pending.is_expired():
            self._pending = None
        return self._pending

    def clear(self) -> None:
        self._pending = None

    def parse_decision(self, text: str) -> Optional[str]:
        normalized = _normalize_decision(text)
        if normalized in {"yes", "confirm", "confirm it", "confirm action"}:
            return "confirm"
        if normalized in {"no", "cancel", "cancel it", "stop"}:
            return "cancel"
        return None

    def decide(self, text: str) -> Optional[ConfirmationDecision]:
        decision = self.parse_decision(text)
        if decision is None:
            return None

        request = self.pending()
        if not request:
            return ConfirmationDecision(
                confirmation_id="",
                decision="missing",
                accepted=False,
                raw_text=text,
                message="No pending confirmation to confirm or cancel.",
                request=None,
            )

        self._pending = None
        if decision == "cancel":
            return ConfirmationDecision(
                confirmation_id=request.id,
                decision="cancelled",
                accepted=False,
                raw_text=text,
                message=f"Cancelled: {request.action_label}.",
                request=request,
            )

        return ConfirmationDecision(
            confirmation_id=request.id,
            decision="confirmed",
            accepted=True,
            raw_text=text,
            message=f"Confirmed: {request.action_label}.",
            request=request,
        )


def requires_confirmation(step: PlanStep) -> bool:
    if bool(step.entities.get("confirmation_approved")):
        return False

    if (step.target, step.action) in {
        ("notes", "delete"),
        ("notes", "delete_all_request"),
        ("notes", "delete_all_confirm"),
        ("tasks", "delete"),
        ("tasks", "clear_completed"),
        ("goals", "delete"),
        ("goals", "pause"),
        ("goals", "complete"),
    }:
        return True

    if step.target == "tool_adapter":
        capability = str(step.entities.get("capability") or "").lower()
        action = str(step.action or "").lower()
        if bool(step.entities.get("requires_confirmation")):
            return True
        if bool(step.entities.get("write_action")):
            return True
        if capability.endswith(".write") or capability.endswith(".delete"):
            return True
        if action in {"create", "update", "write", "delete", "post", "put", "patch"}:
            return True

    if step.target == "device_action":
        action_name = str(step.entities.get("action_name") or step.action or "").strip().lower()
        if action_name in {"lock_pc", "sleep_pc"} and bool(step.entities.get("confirmation_required")):
            return True

    return False


def confirmation_action_label(step: PlanStep) -> str:
    if step.target == "notes":
        if step.action in {"delete_all_request", "delete_all_confirm"}:
            return "delete all notes"
        if step.action == "delete":
            return f"delete note {step.entities.get('note_id') or _last_token(step.input_text)}"

    if step.target == "tasks":
        if step.action == "clear_completed":
            return "clear completed tasks"
        if step.action == "delete":
            return f"delete task {step.entities.get('task_id') or _last_token(step.input_text)}"

    if step.target == "goals":
        if step.action in {"delete", "pause", "complete"}:
            return f"{step.action} goal {step.entities.get('goal_id') or _last_token(step.input_text)}"

    if step.target == "tool_adapter":
        return f"run external write action {step.action}"

    if step.target == "device_action" and (step.entities.get("action_name") or step.action) == "lock_pc":
        return "lock Windows session"

    if step.target == "device_action" and (step.entities.get("action_name") or step.action) == "sleep_pc":
        return "put Windows PC to sleep"

    return f"run {step.target}.{step.action}"


def mark_confirmation_approved(step: PlanStep, confirmation_id: str) -> PlanStep:
    entities = dict(step.entities)
    entities["confirmation_approved"] = True
    entities["confirmation_id"] = confirmation_id

    if step.target == "notes" and step.action == "delete_all_request":
        return PlanStep(
            order=step.order,
            target=step.target,
            action="delete_all_confirm",
            input_text="confirm delete all notes",
            intent_name=step.intent_name,
            entities=entities,
            can_execute=step.can_execute,
            skip_reason=step.skip_reason,
            description=step.description,
        )

    return PlanStep(
        order=step.order,
        target=step.target,
        action=step.action,
        input_text=step.input_text,
        intent_name=step.intent_name,
        entities=entities,
        can_execute=step.can_execute,
        skip_reason=step.skip_reason,
        description=step.description,
    )


def _normalize_decision(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _last_token(text: str) -> str:
    tokens = re.findall(r"\S+", text or "")
    return tokens[-1] if tokens else ""
