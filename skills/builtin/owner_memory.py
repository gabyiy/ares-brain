from __future__ import annotations

from typing import Any, Dict

from core.OwnerMemory import (
    OWNER_MEMORY_FORGET,
    OWNER_MEMORY_RECALL,
    OWNER_MEMORY_REJECT,
    OWNER_MEMORY_SAVE,
    OWNER_MEMORY_UPDATE,
    OwnerMemoryCommand,
    owner_memory_uses_explicit_store,
    parse_owner_memory_command,
)
from skills.base import Skill, SkillContext, SkillResponse


class OwnerMemorySkill(Skill):
    """Stores only explicit, bounded owner facts in the injected profile store."""

    name = "owner_memory"
    description = "Explicitly saves, recalls, updates, or forgets bounded owner facts."
    version = "1.0"
    intent_names = ("owner_memory",)
    run_before_intents = True
    redact_operational_events = True
    triggers = ("owner memory",)
    selection_keywords = ("owner_memory",)
    selection_priority = 0.2
    capabilities = (
        "memory.owner_fact.save",
        "memory.owner_fact.update",
        "memory.owner_fact.recall",
        "memory.owner_fact.forget",
    )

    def can_handle(self, text: str) -> bool:
        return owner_memory_uses_explicit_store(parse_owner_memory_command(text))

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        command = self._command_from_context(text, context)
        store = context.owner_profile_store
        if not owner_memory_uses_explicit_store(command):
            return self._response(
                "I need an explicit owner-memory command.",
                command,
                store=store,
                storage_status="rejected",
                rejection_reason="unrecognized_owner_memory_command",
            )
        if command.action == OWNER_MEMORY_REJECT:
            message = (
                "I cannot store or recall protected information such as passwords, "
                "tokens, API keys, or private keys."
                if command.protected
                else "I could not safely understand that owner-memory request."
            )
            return self._response(
                message,
                command,
                store=store,
                storage_status="rejected",
                rejection_reason=command.rejection_reason,
                redact_transcript=command.protected,
            )

        if store is None:
            return self._response(
                "Owner memory storage is not available.",
                command,
                store=store,
                storage_status="storage_failed",
                error="missing_owner_profile_store",
            )

        if command.action in {OWNER_MEMORY_SAVE, OWNER_MEMORY_UPDATE}:
            result = store.save_fact(command.normalized_key, command.value)
        elif command.action == OWNER_MEMORY_RECALL:
            result = store.recall_fact(command.normalized_key)
        elif command.action == OWNER_MEMORY_FORGET:
            result = store.forget_fact(command.normalized_key)
        else:
            return self._response(
                "I could not safely understand that owner-memory request.",
                command,
                store=store,
                storage_status="rejected",
                rejection_reason="unsupported_owner_memory_action",
            )

        if not result.success:
            return self._response(
                "I could not update owner memory safely.",
                command,
                store=store,
                result=result,
                storage_status=result.status,
                storage_result=result.to_dict(include_value=False),
                error=result.error_code or "owner_profile_operation_failed",
            )

        display_key = result.display_key or command.display_key
        if result.status == "created":
            text = f"I will remember that your {display_key} is {command.value}."
        elif result.status == "updated":
            text = f"I updated your {display_key} to {command.value}."
        elif result.status == "recalled":
            text = f"Your {display_key} is {result.value}."
        elif result.status == "forgotten":
            text = f"I forgot your {display_key}."
        elif result.status == "missing":
            text = f"I do not know your {display_key} yet."
        else:
            return self._response(
                "I could not update owner memory safely.",
                command,
                store=store,
                result=result,
                storage_status=result.status,
                storage_result=result.to_dict(include_value=False),
                error="unexpected_owner_profile_result",
            )

        return self._response(
            text,
            command,
            store=store,
            result=result,
            storage_status=result.status,
            storage_result=result.to_dict(include_value=False),
            operation_result=result.operation,
        )

    def _command_from_context(
        self,
        text: str,
        context: SkillContext,
    ) -> OwnerMemoryCommand:
        intent = context.metadata.get("intent") if context.metadata else None
        if getattr(intent, "intent_name", None) != "owner_memory":
            return parse_owner_memory_command(text)
        entities = dict(getattr(intent, "extracted_entities", {}) or {})
        return OwnerMemoryCommand(
            recognized=True,
            action=str(entities.get("action") or OWNER_MEMORY_REJECT),
            normalized_key=str(entities.get("normalized_key") or ""),
            display_key=str(entities.get("display_key") or ""),
            value=str(entities.get("value") or ""),
            rejection_reason=str(entities.get("rejection_reason") or ""),
            protected=bool(entities.get("protected")),
            parser_rule=str(entities.get("parser_rule") or "owner_memory_explicit_v1"),
        )

    def _response(
        self,
        text: str,
        command: OwnerMemoryCommand,
        *,
        store: Any = None,
        result: Any = None,
        **metadata: Any,
    ) -> SkillResponse:
        safe_metadata: Dict[str, Any] = {
            "memory_action": command.action or OWNER_MEMORY_REJECT,
            "normalized_fact_key": command.normalized_key,
            "parser_rule": command.parser_rule,
            "protected_key_rejected": command.protected,
            "owner_memory_diagnostics": self._diagnostics(
                command,
                store=store,
                result=result,
                fallback_status=str(metadata.get("storage_status") or ""),
            ),
            **metadata,
        }
        return SkillResponse(text=text, skill=self.name, metadata=safe_metadata)

    def _diagnostics(
        self,
        command: OwnerMemoryCommand,
        *,
        store: Any,
        result: Any,
        fallback_status: str,
    ) -> Dict[str, Any]:
        result_metadata = dict(getattr(result, "metadata", {}) or {})
        path = str(
            result_metadata.get("profile_path")
            or getattr(store, "path", "")
            or ""
        )
        existed = result_metadata.get("file_existed_before")
        if existed is None and path:
            exists = getattr(getattr(store, "path", None), "exists", None)
            existed = bool(exists()) if callable(exists) else False
        return {
            "action": command.action or OWNER_MEMORY_REJECT,
            "normalized_key": command.normalized_key,
            "extracted_value": "[REDACTED]" if command.protected else command.value,
            "parser_rule": command.parser_rule,
            "profile_path": path,
            "file_existed_before": existed,
            "operation_result": str(
                getattr(result, "status", "") or fallback_status
            ),
            "rejection_reason": str(
                command.rejection_reason
                or getattr(result, "error_code", "")
                or ""
            ),
        }
