from __future__ import annotations

from typing import Any, Dict, Iterable

from core.OwnerMemory import (
    OWNER_MEMORY_DELETE_ALL_CONFIRM,
    OWNER_MEMORY_DELETE_ALL_REQUEST,
    OWNER_MEMORY_FORGET,
    OWNER_MEMORY_LIST,
    OWNER_MEMORY_RECALL,
    OWNER_MEMORY_REJECT,
    OWNER_MEMORY_SAVE,
    OWNER_MEMORY_UPDATE,
    OwnerMemoryCommand,
    owner_memory_uses_explicit_store,
    parse_owner_memory_command,
)
from core.OwnerLongTermMemory import (
    DELETE_ALL_CONFIRMATION_PHRASE,
    MAX_SPOKEN_MEMORY_RESULTS,
    general_memory_clause,
)
from memory.owner_memory_contracts import (
    OWNER_MEMORY_ACTION_DELETE_ALL_CONFIRM,
    OWNER_MEMORY_ACTION_DELETE_ALL_REQUEST,
    OWNER_MEMORY_ACTION_FORGET,
    OWNER_MEMORY_ACTION_LIST,
    OWNER_MEMORY_ACTION_RECALL,
    OWNER_MEMORY_ACTION_REMEMBER,
    OWNER_MEMORY_ACTION_UPDATE,
    OwnerMemoryRequestV1,
)
from skills.base import Skill, SkillContext, SkillResponse


_ACTION_MAP = {
    OWNER_MEMORY_SAVE: OWNER_MEMORY_ACTION_REMEMBER,
    OWNER_MEMORY_UPDATE: OWNER_MEMORY_ACTION_UPDATE,
    OWNER_MEMORY_RECALL: OWNER_MEMORY_ACTION_RECALL,
    OWNER_MEMORY_FORGET: OWNER_MEMORY_ACTION_FORGET,
    OWNER_MEMORY_LIST: OWNER_MEMORY_ACTION_LIST,
    OWNER_MEMORY_DELETE_ALL_REQUEST: OWNER_MEMORY_ACTION_DELETE_ALL_REQUEST,
    OWNER_MEMORY_DELETE_ALL_CONFIRM: OWNER_MEMORY_ACTION_DELETE_ALL_CONFIRM,
}
_MAX_SPOKEN_LIST_FACTS = 5


class OwnerMemorySkill(Skill):
    """Routes explicit owner facts and memories through the central service."""

    name = "owner_memory"
    description = "Explicitly manages bounded owner facts and long-term memories."
    version = "2.0"
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
        "memory.owner_fact.list",
        "memory.owner.general.remember",
        "memory.owner.general.recall",
        "memory.owner.general.forget",
        "memory.owner.general.list",
    )

    def can_handle(self, text: str) -> bool:
        return owner_memory_uses_explicit_store(parse_owner_memory_command(text))

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        command = self._command_from_context(text, context)
        if not owner_memory_uses_explicit_store(command):
            return self._response(
                "I need an explicit owner-memory command.",
                command,
                storage_status="rejected",
                rejection_reason="unrecognized_owner_memory_command",
            )
        if command.action == OWNER_MEMORY_REJECT:
            if command.clarification_reason == "temporary_memory_requires_clarification":
                message = "That sounds temporary. Should I save it as a temporary note instead?"
            elif command.protected:
                message = "I cannot store or recall protected information such as passwords, tokens, API keys, or private keys."
            else:
                message = "I could not safely understand that owner-memory request."
            return self._response(
                message,
                command,
                storage_status="rejected",
                rejection_reason=command.rejection_reason,
                redact_transcript=command.protected,
            )

        core_service = context.core_service
        execute = getattr(core_service, "execute_owner_memory", None)
        if not callable(execute):
            return self._response(
                "Owner memory storage is not available.",
                command,
                storage_status="storage_failed",
                error="missing_owner_memory_service",
            )

        request = OwnerMemoryRequestV1(
            action=_ACTION_MAP.get(command.action, ""),
            normalized_key=command.normalized_key,
            display_key=command.display_key,
            value=command.value if command.action in {OWNER_MEMORY_SAVE, OWNER_MEMORY_UPDATE} else None,
            memory_kind=command.memory_kind,
            memory=dict(command.memory),
            query=dict(command.query),
            persistence=command.persistence,
            explicit=command.explicit,
            correlation_id=str(context.metadata.get("correlation_id") or ""),
            session_id=str(context.metadata.get("session_id") or ""),
            metadata={
                "source": "explicit_owner_statement",
                "value_redacted": True,
                "memory_content_redacted": True,
            },
        )
        result = execute(request)

        if not result.success:
            return self._response(
                self._safe_failure_text(result.error_code),
                command,
                result=result,
                storage_status=result.status,
                storage_result=result.to_dict(include_values=False),
                error=result.error_code or "owner_profile_operation_failed",
            )

        if result.status == "missing" and command.action == OWNER_MEMORY_RECALL:
            legacy_value = self._legacy_recall(command.normalized_key, context)
            if legacy_value is not None:
                return self._response(
                    self._recall_text(command.normalized_key, command.display_key, legacy_value),
                    command,
                    result=result,
                    storage_status="legacy_recalled",
                    operation_result="legacy_recall",
                )

        response_text = self._result_text(command, result)
        if not response_text:
            return self._response(
                "I could not update owner memory safely.",
                command,
                result=result,
                storage_status=result.status,
                storage_result=result.to_dict(include_values=False),
                error="unexpected_owner_profile_result",
            )
        return self._response(
            response_text,
            command,
            result=result,
            storage_status=result.status,
            storage_result=result.to_dict(include_values=False),
            operation_result=result.action,
        )

    def _result_text(self, command: OwnerMemoryCommand, result: Any) -> str:
        if command.action == OWNER_MEMORY_DELETE_ALL_REQUEST and result.status == "confirmation_required":
            return (
                "Deleting all long-term owner memory requires confirmation. "
                f"Say: {DELETE_ALL_CONFIRMATION_PHRASE.capitalize()}."
            )
        if command.action == OWNER_MEMORY_DELETE_ALL_CONFIRM:
            if result.status == "deleted_all":
                return "I deleted all your long-term owner memory."
            if result.status == "missing_confirmation":
                return "There is no active request to delete all long-term owner memory."
        if command.memory_kind == "general":
            return self._general_result_text(command, result)
        key = result.normalized_key or command.normalized_key
        display_key = result.display_key or command.display_key
        if result.status == "created":
            return self._remember_text(key, display_key, command.value)
        if result.status == "updated":
            if command.action == OWNER_MEMORY_UPDATE and result.previous_value is not None:
                return self._updated_from_text(key, display_key, result.previous_value, command.value)
            return self._updated_text(key, display_key, command.value)
        if result.status == "recalled":
            return self._recall_text(key, display_key, result.value)
        if result.status == "forgotten":
            return f"I forgot your {display_key}."
        if result.status == "missing":
            if command.action == OWNER_MEMORY_FORGET:
                return f"I was not storing your {display_key}."
            return f"I do not know your {display_key} yet."
        if result.status == "listed":
            return self._list_text(result.facts, result.memories)
        return ""

    def _general_result_text(self, command: OwnerMemoryCommand, result: Any) -> str:
        memory = dict(result.memory or command.memory or {})
        clause = general_memory_clause(memory) if memory else ""
        if result.status == "created":
            return f"I will remember that {clause}."
        if result.status == "duplicate":
            return f"I already remember that {clause}."
        if result.status == "updated":
            return f"I updated that memory to: {self._capitalize_clause(clause)}."
        if result.status == "recalled":
            memories = list(result.memories or (() if not memory else (memory,)))
            style = str(result.metadata.get("response_style") or command.query.get("response_style") or "topic")
            clauses = [general_memory_clause(item) for item in memories[:MAX_SPOKEN_MEMORY_RESULTS]]
            if style == "assertion":
                return f"Yes. You told me that {clauses[0]}."
            if style in {"preference_list", "type_list"}:
                return f"{self._capitalize_clause(self._join_phrases(clauses))}."
            if len(clauses) == 1:
                return f"You told me that {clauses[0]}."
            return f"You told me that {self._join_phrases(clauses)}."
        if result.status == "forgotten":
            count = len(result.memories or ())
            if count <= 1 and clause:
                return f"I forgot that {clause}."
            display = str(command.query.get("display_query") or "that topic")
            return f"I forgot {count} long-term memories about {display}."
        if result.status == "missing":
            display = str(command.query.get("display_query") or "that topic")
            if command.action == OWNER_MEMORY_FORGET:
                return f"I was not storing an active long-term memory about {display}."
            if command.query.get("memory_type"):
                return f"I do not have any saved {display} yet."
            return f"I do not have an active long-term memory about {display}."
        return ""

    def _remember_text(self, key: str, display_key: str, value: Any) -> str:
        if key == "city":
            return f"I will remember that you live in {self._format_value(value)}."
        return f"I will remember that your {display_key} is {self._format_value(value)}."

    def _updated_text(self, key: str, display_key: str, value: Any) -> str:
        if key == "city":
            return f"I updated where you live to {self._format_value(value)}."
        return f"I updated your {display_key} to {self._format_value(value)}."

    def _updated_from_text(self, key: str, display_key: str, previous: Any, value: Any) -> str:
        if key == "city":
            return f"I updated where you live from {self._format_value(previous)} to {self._format_value(value)}."
        return f"I updated your {display_key} from {self._format_value(previous)} to {self._format_value(value)}."

    def _recall_text(self, key: str, display_key: str, value: Any) -> str:
        if key == "city":
            return f"You live in {self._format_value(value)}."
        return f"Your {display_key} is {self._format_value(value)}."

    def _list_text(
        self,
        facts: Iterable[Dict[str, Any]],
        memories: Iterable[Dict[str, Any]] = (),
    ) -> str:
        facts_list = list(facts)
        memories_list = list(memories)
        if not facts_list and not memories_list:
            return "I do not have any saved facts about you yet."
        all_phrases = [self._fact_clause(fact) for fact in facts_list]
        all_phrases.extend(general_memory_clause(memory) for memory in memories_list)
        phrases = all_phrases[:_MAX_SPOKEN_LIST_FACTS]
        joined = self._join_phrases(phrases)
        remaining = len(all_phrases) - len(phrases)
        suffix = f" I have {remaining} additional saved fact{'s' if remaining != 1 else ''}." if remaining else ""
        return f"I remember that {joined}.{suffix}".strip()

    def _fact_clause(self, fact: Dict[str, Any]) -> str:
        key = str(fact.get("normalized_key") or "")
        display = str(fact.get("display_key") or key.replace("_", " "))
        value = self._format_value(fact.get("value"))
        return f"you live in {value}" if key == "city" else f"your {display} is {value}"

    def _legacy_recall(self, key: str, context: SkillContext) -> Any:
        profile = context.profile_store
        if profile is None:
            return None
        if key == "city":
            return profile.get_value("location")
        if key.startswith("favorite_"):
            return profile.get_favorite(key[len("favorite_"):])
        return profile.get_value(key)

    def _command_from_context(self, text: str, context: SkillContext) -> OwnerMemoryCommand:
        intent = context.metadata.get("intent") if context.metadata else None
        if getattr(intent, "intent_name", None) != "owner_memory":
            return parse_owner_memory_command(text)
        entities = dict(getattr(intent, "extracted_entities", {}) or {})
        return OwnerMemoryCommand(
            recognized=True,
            action=str(entities.get("action") or OWNER_MEMORY_REJECT),
            normalized_key=str(entities.get("normalized_key") or ""),
            display_key=str(entities.get("display_key") or ""),
            value=entities.get("value", ""),
            rejection_reason=str(entities.get("rejection_reason") or ""),
            protected=bool(entities.get("protected")),
            parser_rule=str(entities.get("parser_rule") or "owner_memory_explicit_v1"),
            memory_kind=str(entities.get("memory_kind") or "fact"),
            memory=dict(entities.get("memory") or {}),
            query=dict(entities.get("query") or {}),
            persistence=str(entities.get("persistence") or ""),
            explicit=bool(entities.get("explicit")),
            extracted_memory_phrase=str(entities.get("extracted_memory_phrase") or ""),
            fact_text=str(entities.get("fact_text") or ""),
            clarification_reason=str(entities.get("clarification_reason") or ""),
            confirmation_required=bool(entities.get("confirmation_required")),
        )

    def _response(self, text: str, command: OwnerMemoryCommand, *, result: Any = None, **metadata: Any) -> SkillResponse:
        safe_metadata: Dict[str, Any] = {
            "memory_action": command.action or OWNER_MEMORY_REJECT,
            "normalized_fact_key": command.normalized_key,
            "parser_rule": command.parser_rule,
            "protected_key_rejected": command.protected,
            "owner_memory_diagnostics": self._diagnostics(command, result=result, fallback_status=str(metadata.get("storage_status") or "")),
            **metadata,
        }
        return SkillResponse(text=text, skill=self.name, metadata=safe_metadata)

    def _diagnostics(self, command: OwnerMemoryCommand, *, result: Any, fallback_status: str) -> Dict[str, Any]:
        result_metadata = dict(getattr(result, "metadata", {}) or {})
        return {
            "action": command.action or OWNER_MEMORY_REJECT,
            "normalized_key": command.normalized_key,
            "extracted_value": "[REDACTED]" if command.protected else command.value,
            "parser_rule": command.parser_rule,
            "memory_kind": command.memory_kind,
            "memory_type": str(command.memory.get("memory_type") or ""),
            "persistence": command.persistence,
            "profile_path": str(result_metadata.get("profile_path") or ""),
            "file_existed_before": result_metadata.get("file_existed_before"),
            "operation_result": str(getattr(result, "status", "") or fallback_status),
            "rejection_reason": str(command.rejection_reason or getattr(result, "error_code", "") or ""),
            "extracted_memory_phrase": command.extracted_memory_phrase,
            "fact_text_length": len(command.fact_text),
            "memory_id": str(getattr(result, "memory", {}).get("memory_id") or ""),
        }

    def _safe_failure_text(self, error_code: str) -> str:
        if error_code in {"fact_limit_reached", "memory_limit_reached", "profile_size_limit_reached"}:
            return "Owner memory has reached its safe storage limit."
        if error_code == "general_memory_too_long":
            return "That long-term memory is too long to store safely."
        return "I could not update owner memory safely."

    @staticmethod
    def _capitalize_clause(value: str) -> str:
        return value[:1].upper() + value[1:] if value else value

    @staticmethod
    def _format_value(value: Any) -> str:
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, list):
            return OwnerMemorySkill._join_phrases([str(item) for item in value])
        return str(value)

    @staticmethod
    def _join_phrases(values: Iterable[str]) -> str:
        items = list(values)
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return f"{', '.join(items[:-1])}, and {items[-1]}"
