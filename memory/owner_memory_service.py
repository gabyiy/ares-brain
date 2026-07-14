from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from memory.owner_memory_contracts import (
    OWNER_MEMORY_ACTION_DELETE_ALL_CONFIRM,
    OWNER_MEMORY_ACTION_DELETE_ALL_REQUEST,
    OWNER_MEMORY_ACTION_FORGET,
    OWNER_MEMORY_ACTION_LIST,
    OWNER_MEMORY_ACTION_RECALL,
    OWNER_MEMORY_ACTION_REMEMBER,
    OWNER_MEMORY_ACTION_UPDATE,
    OwnerMemoryRequestV1,
    OwnerMemoryResultV1,
)
from memory.owner_profile import OwnerProfileStore


class OwnerMemoryService:
    """Central Brain-facing API for explicit bounded owner facts and memories."""

    def __init__(
        self,
        profile_path: Optional[Path | str] = None,
        event_bus: Any = None,
        *,
        store: Optional[OwnerProfileStore] = None,
    ):
        self._store = store or OwnerProfileStore(profile_path, event_bus=event_bus)

    @property
    def profile_path(self) -> Path:
        return self._store.path

    def execute(self, request: OwnerMemoryRequestV1 | Mapping[str, Any]) -> OwnerMemoryResultV1:
        try:
            normalized_request = (
                request
                if isinstance(request, OwnerMemoryRequestV1)
                else OwnerMemoryRequestV1.from_dict(request)
            )
        except (TypeError, ValueError) as error:
            return OwnerMemoryResultV1(
                False,
                "rejected",
                "",
                error_code="invalid_owner_memory_contract",
                error_message="Owner memory request contract is invalid.",
                metadata={"error_type": type(error).__name__, "value_redacted": True},
            )

        action = normalized_request.action
        key = normalized_request.normalized_key
        memory_kind = normalized_request.memory_kind or "fact"
        general = memory_kind == "general"
        if not general and action not in {
            OWNER_MEMORY_ACTION_LIST,
            OWNER_MEMORY_ACTION_DELETE_ALL_REQUEST,
            OWNER_MEMORY_ACTION_DELETE_ALL_CONFIRM,
        } and not key:
            return self._failure(normalized_request, "missing_fact_key", "Owner fact key is required.")
        if general and not normalized_request.explicit:
            return self._failure(normalized_request, "explicit_memory_trigger_required", "General owner memory requires an explicit request.")
        if general and normalized_request.persistence != "long_term":
            return self._failure(normalized_request, "invalid_memory_persistence", "General owner memory persistence must be long_term.")
        if general and action in {OWNER_MEMORY_ACTION_REMEMBER, OWNER_MEMORY_ACTION_UPDATE} and not normalized_request.memory:
            return self._failure(normalized_request, "missing_general_memory", "Structured general owner memory is required.")
        if not general and action in {OWNER_MEMORY_ACTION_REMEMBER, OWNER_MEMORY_ACTION_UPDATE} and normalized_request.value is None:
            return self._failure(normalized_request, "missing_fact_value", "Owner fact value is required.")

        if general and action in {OWNER_MEMORY_ACTION_REMEMBER, OWNER_MEMORY_ACTION_UPDATE}:
            store_result = self._store.save_memory(
                normalized_request.memory,
                replace_query=normalized_request.query,
                force_update=action == OWNER_MEMORY_ACTION_UPDATE,
            )
        elif general and action == OWNER_MEMORY_ACTION_RECALL:
            store_result = self._store.recall_memories(normalized_request.query)
        elif general and action == OWNER_MEMORY_ACTION_FORGET:
            store_result = self._store.forget_memories(normalized_request.query)
        elif action == OWNER_MEMORY_ACTION_DELETE_ALL_REQUEST:
            store_result = self._store.request_delete_all()
        elif action == OWNER_MEMORY_ACTION_DELETE_ALL_CONFIRM:
            store_result = self._store.confirm_delete_all()
        elif action in {OWNER_MEMORY_ACTION_REMEMBER, OWNER_MEMORY_ACTION_UPDATE}:
            store_result = self._store.save_fact(
                key,
                normalized_request.value,
                display_key=normalized_request.display_key,
            )
        elif action == OWNER_MEMORY_ACTION_RECALL:
            store_result = self._store.recall_fact(key)
        elif action == OWNER_MEMORY_ACTION_FORGET:
            store_result = self._store.forget_fact(key)
        elif action == OWNER_MEMORY_ACTION_LIST:
            facts_result = self._store.list_facts(include_values=True)
            memories_result = self._store.list_memories(include_inactive=False)
            if not facts_result.success:
                store_result = facts_result
            elif not memories_result.success:
                store_result = memories_result
            else:
                return OwnerMemoryResultV1(
                    True,
                    "listed",
                    action,
                    facts=facts_result.facts,
                    memories=memories_result.memories,
                    correlation_id=normalized_request.correlation_id,
                    session_id=normalized_request.session_id,
                    metadata={
                        "profile_path": str(self.profile_path),
                        "fact_count": len(facts_result.facts),
                        "memory_count": len(memories_result.memories),
                        "value_redacted": True,
                        "memory_content_redacted": True,
                    },
                )
        else:
            return self._failure(normalized_request, "unsupported_owner_memory_action", "Owner memory action is unsupported.")

        metadata = {
            **dict(store_result.metadata),
            "storage_contract": store_result.contract_name,
            "storage_contract_version": store_result.contract_version,
            "value_redacted": True,
        }
        return OwnerMemoryResultV1(
            store_result.success,
            store_result.status,
            action,
            normalized_key=store_result.normalized_key,
            display_key=store_result.display_key,
            value=store_result.value,
            previous_value=store_result.previous_value,
            changed=store_result.changed,
            facts=store_result.facts,
            memory=store_result.memory,
            memories=store_result.memories,
            confirmation_required=store_result.confirmation_required,
            error_code=store_result.error_code,
            error_message=store_result.error_message,
            correlation_id=normalized_request.correlation_id,
            session_id=normalized_request.session_id,
            metadata=metadata,
        )

    def inspect(self, *, include_values: bool = False) -> Dict[str, Any]:
        return self._store.inspection_report(include_values=include_values)

    def health_check(self) -> Dict[str, Any]:
        report = self.inspect(include_values=False)
        valid = str(report.get("validation_state") or "") in {"valid", "missing_default"}
        return {
            "component_name": "owner_memory",
            "component_type": "service",
            "status": "healthy" if valid else "failed",
            "healthy": valid,
            "available": valid,
            "degraded": False,
            "capabilities": [
                "memory.owner_fact.save",
                "memory.owner_fact.update",
                "memory.owner_fact.recall",
                "memory.owner_fact.forget",
                "memory.owner_fact.list",
                "memory.owner.general.remember",
                "memory.owner.general.recall",
                "memory.owner.general.forget",
                "memory.owner.general.list",
            ],
            "metadata": {
                "schema_name": report.get("schema_name", ""),
                "detected_version": report.get("detected_version"),
                "target_version": report.get("current_target_version"),
                "fact_count": report.get("fact_count", 0),
                "memory_count": report.get("memory_count", 0),
            },
        }

    def _failure(self, request: OwnerMemoryRequestV1, code: str, message: str) -> OwnerMemoryResultV1:
        return OwnerMemoryResultV1(
            False,
            "rejected",
            request.action,
            normalized_key=request.normalized_key,
            display_key=request.display_key,
            error_code=code,
            error_message=message,
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            metadata={"profile_path": str(self.profile_path), "value_redacted": True},
        )
