from __future__ import annotations

from typing import Any, Dict, Iterable

from core.OwnerMemory import (
    OWNER_MEMORY_CANCEL_DELETE,
    OWNER_MEMORY_CONFIRM_DELETE,
    OWNER_MEMORY_COUNT,
    OWNER_MEMORY_DELETE_ALL_CONFIRM,
    OWNER_MEMORY_DELETE_ALL_REQUEST,
    OWNER_MEMORY_FORGET,
    OWNER_MEMORY_FORGET_ALL_GENERAL,
    OWNER_MEMORY_FORGET_KEYED_FACT,
    OWNER_MEMORY_FORGET_SPECIFIC,
    OWNER_MEMORY_FORGET_TOPIC,
    OWNER_MEMORY_INSPECT,
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
    MAX_SPOKEN_MEMORY_RESULTS,
    general_memory_clause,
)
from memory.owner_memory_contracts import (
    OWNER_MEMORY_ACTION_CANCEL_DELETE,
    OWNER_MEMORY_ACTION_CONFIRM_DELETE,
    OWNER_MEMORY_ACTION_COUNT,
    OWNER_MEMORY_ACTION_DELETE_ALL_CONFIRM,
    OWNER_MEMORY_ACTION_DELETE_ALL_REQUEST,
    OWNER_MEMORY_ACTION_FORGET,
    OWNER_MEMORY_ACTION_FORGET_ALL_GENERAL,
    OWNER_MEMORY_ACTION_FORGET_KEYED_FACT,
    OWNER_MEMORY_ACTION_FORGET_SPECIFIC,
    OWNER_MEMORY_ACTION_FORGET_TOPIC,
    OWNER_MEMORY_ACTION_INSPECT,
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
    OWNER_MEMORY_INSPECT: OWNER_MEMORY_ACTION_INSPECT,
    OWNER_MEMORY_COUNT: OWNER_MEMORY_ACTION_COUNT,
    OWNER_MEMORY_FORGET_SPECIFIC: OWNER_MEMORY_ACTION_FORGET_SPECIFIC,
    OWNER_MEMORY_FORGET_TOPIC: OWNER_MEMORY_ACTION_FORGET_TOPIC,
    OWNER_MEMORY_FORGET_ALL_GENERAL: OWNER_MEMORY_ACTION_FORGET_ALL_GENERAL,
    OWNER_MEMORY_FORGET_KEYED_FACT: OWNER_MEMORY_ACTION_FORGET_KEYED_FACT,
    OWNER_MEMORY_CONFIRM_DELETE: OWNER_MEMORY_ACTION_CONFIRM_DELETE,
    OWNER_MEMORY_CANCEL_DELETE: OWNER_MEMORY_ACTION_CANCEL_DELETE,
    OWNER_MEMORY_LIST: OWNER_MEMORY_ACTION_LIST,
    OWNER_MEMORY_DELETE_ALL_REQUEST: OWNER_MEMORY_ACTION_DELETE_ALL_REQUEST,
    OWNER_MEMORY_DELETE_ALL_CONFIRM: OWNER_MEMORY_ACTION_DELETE_ALL_CONFIRM,
}
_MAX_SPOKEN_LIST_FACTS = 5


class OwnerMemorySkill(Skill):
    """Routes explicit owner facts and memories through the central service."""

    name = "owner_memory"
    description = "Explicitly manages bounded owner facts and long-term memories."
    version = "3.0"
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
        "memory.owner.general.inspect",
        "memory.owner.general.count",
        "memory.owner.deletion.confirm",
        "memory.owner.deletion.cancel",
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
                "normalized_request": command.routing_text or command.safe_raw_text,
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
        if result.status == "confirmation_required":
            return self._confirmation_text(command, result)
        if result.status == "ambiguous":
            count = int(result.metadata.get("candidate_count") or len(result.memories or ()))
            display = str(command.query.get("display_query") or "that topic")
            return (
                f"I found {self._spoken_count(count)} memories matching {display}. "
                f"Please tell me which one to remove, or say forget everything about {display}."
            )
        if result.status == "missing_match":
            if command.memory_kind == "fact" and command.display_key:
                return f"I was not storing your {command.display_key}."
            return "I could not find a saved memory matching that."
        if result.status == "counted":
            return self._count_text(command, result)
        if result.status == "listed":
            return self._list_text(
                result.facts,
                result.memories,
                scope=str(result.metadata.get("list_scope") or command.query.get("scope") or "all"),
            )
        if result.status == "deleted_specific":
            clause = general_memory_clause(dict(result.memory or {}))
            return f"I deleted the memory that {clause}."
        if result.status == "deleted_topic":
            count = len(result.memories or ()) or int(result.metadata.get("candidate_count") or 0)
            topic = str(result.metadata.get("topic") or command.query.get("display_query") or "that topic")
            return f"I deleted {self._spoken_count(count)} general memories about {topic}."
        if result.status == "deleted_all_general":
            count = len(result.memories or ()) or int(result.metadata.get("candidate_count") or 0)
            return f"I deleted {self._spoken_count(count)} general long-term memories. Your keyed facts were preserved."
        if result.status == "deleted_keyed_fact":
            display = result.display_key or command.display_key or "saved"
            return f"I deleted your {display.replace(' ', '-') } fact."
        if result.status == "target_already_missing":
            return "That saved target no longer exists. I did not delete anything else."
        if result.status == "target_changed":
            return "That saved fact changed after the deletion request. Please ask me again before deleting it."
        if result.status == "cancelled":
            return "Deletion cancelled. I kept the memory."
        if result.status == "expired":
            return "That deletion request expired. Please ask me again."
        if result.status == "invalid_pending":
            return "I could not verify that deletion request, so I did not delete anything."
        if result.status == "missing_pending":
            return "There is no active owner-memory deletion request."
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
        if result.status == "missing":
            if command.action in {OWNER_MEMORY_FORGET, OWNER_MEMORY_FORGET_KEYED_FACT}:
                return f"I was not storing your {display_key}."
            return f"I do not know your {display_key} yet."
        return ""

    def _confirmation_text(self, command: OwnerMemoryCommand, result: Any) -> str:
        metadata = dict(result.metadata or {})
        operation = str(metadata.get("pending_operation") or result.pending_action.get("operation") or "")
        count = int(metadata.get("candidate_count") or result.pending_action.get("candidate_count") or 1)
        if operation == "forget_specific":
            memory = dict(result.memory or {})
            return f"I found the memory that {general_memory_clause(memory)}. Should I delete it?"
        if operation == "forget_topic":
            topic = str(metadata.get("topic") or command.query.get("display_query") or "that topic")
            related = [str(item).replace("_", "-") for item in metadata.get("related_keyed_facts") or ()]
            preserved = f" Your keyed {self._join_phrases(related)} fact will remain." if len(related) == 1 else ""
            return (
                f"I found {self._spoken_count(count)} general memories about {topic}."
                f"{preserved} Should I delete {self._delete_count_phrase(count)}?"
            )
        if operation == "forget_all_general":
            fact_keys = [str(item).replace("_", " ") for item in metadata.get("preserved_fact_keys") or ()]
            if fact_keys:
                preserved = (
                    " This will preserve your keyed facts, including "
                    f"{self._join_phrases(fact_keys[:_MAX_SPOKEN_LIST_FACTS])}."
                )
            else:
                preserved = " Your keyed facts will remain."
            return (
                f"You currently have {self._spoken_count(count)} general long-term memories."
                f"{preserved} Say confirm delete all general memories to continue."
            )
        if operation == "forget_keyed_fact":
            display = result.display_key or command.display_key or "saved fact"
            return f"Your saved {display} is {self._format_value(result.value)}. Should I delete that fact?"
        return "I found a matching saved item. Should I delete it?"

    def _count_text(self, command: OwnerMemoryCommand, result: Any) -> str:
        scope = str(result.metadata.get("count_scope") or command.query.get("scope") or "all")
        facts = int(result.metadata.get("fact_count") or 0)
        memories = int(result.metadata.get("memory_count") or 0)
        if scope == "facts":
            return f"I have {self._spoken_count(facts)} keyed {self._plural('fact', facts)} about you."
        if scope == "preferences":
            return f"I have {self._spoken_count(memories)} saved {self._plural('preference', memories)} about you."
        if scope == "general":
            return f"I have {self._spoken_count(memories)} active general long-term {self._plural('memory', memories)} about you."
        return (
            f"I have {self._spoken_count(facts)} keyed {self._plural('fact', facts)} and "
            f"{self._spoken_count(memories)} active general {self._plural('memory', memories)} about you."
        )

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
        if result.status == "missing":
            display = str(command.query.get("display_query") or "that topic")
            if command.action in {OWNER_MEMORY_FORGET, OWNER_MEMORY_FORGET_SPECIFIC, OWNER_MEMORY_FORGET_TOPIC}:
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
        *,
        scope: str = "all",
    ) -> str:
        facts_list = list(facts)
        memories_list = list(memories)
        if not facts_list and not memories_list:
            if scope == "general":
                return "I do not have any active general long-term memories about you."
            if scope == "facts":
                return "I do not have any saved keyed facts about you yet."
            return "I do not have any saved facts or general memories about you yet."
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
            normalized_memory_trigger=str(entities.get("normalized_memory_trigger") or ""),
            routing_reason=str(entities.get("routing_reason") or ""),
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
        pending_action = dict(getattr(result, "pending_action", {}) or {})
        pending_operation = str(result_metadata.get("pending_operation") or pending_action.get("operation") or "")
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
            "extracted_fact_text": "[REDACTED]" if command.protected else command.fact_text,
            "fact_text_length": len(command.fact_text),
            "normalized_memory_trigger": command.normalized_memory_trigger,
            "routing_reason": command.routing_reason,
            "memory_id": str(getattr(result, "memory", {}).get("memory_id") or ""),
            "pending_state_path": str(result_metadata.get("pending_state_path") or ""),
            "pending_operation": pending_operation,
            "pending_status": str(getattr(result, "status", "") or "") if pending_operation else "",
            "pending_candidate_count": int(result_metadata.get("candidate_count") or pending_action.get("candidate_count") or 0),
            "pending_expires_at": str(result_metadata.get("expires_at") or pending_action.get("expires_at") or ""),
            "pending_topic": str(result_metadata.get("topic") or pending_action.get("topic") or ""),
        }

    def _safe_failure_text(self, error_code: str) -> str:
        if error_code in {"fact_limit_reached", "memory_limit_reached", "profile_size_limit_reached"}:
            return "Owner memory has reached its safe storage limit."
        if error_code == "general_memory_too_long":
            return "That long-term memory is too long to store safely."
        if error_code in {"invalid_pending_state", "pending_write_failed", "pending_clear_failed"}:
            return "I could not verify the deletion request, so I did not delete anything."
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

    @staticmethod
    def _spoken_count(value: int) -> str:
        words = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
        count = max(0, int(value))
        return words[count] if count < len(words) else str(count)

    @classmethod
    def _delete_count_phrase(cls, value: int) -> str:
        count = max(0, int(value))
        if count == 1:
            return "it"
        if count == 2:
            return "both"
        return f"all {cls._spoken_count(count)}"

    @staticmethod
    def _plural(noun: str, value: int) -> str:
        if int(value) == 1:
            return noun
        return "memories" if noun == "memory" else f"{noun}s"
