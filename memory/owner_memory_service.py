from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from core.OwnerLongTermMemory import (
    MAX_GENERAL_MEMORIES,
    MAX_MEMORY_RETRIEVAL_RESULTS,
    extract_memory_topics,
    general_memory_clause,
)
from core.OwnerMemory import owner_fact_display_name
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
    OwnerMemoryResultV1,
)
from memory.owner_profile import OwnerProfileResultV1, OwnerProfileStore
from memory.pending_owner_memory import (
    PENDING_OPERATION_FORGET_ALL_GENERAL,
    PENDING_OPERATION_FORGET_KEYED_FACT,
    PENDING_OPERATION_FORGET_SPECIFIC,
    PENDING_OPERATION_FORGET_TOPIC,
    PENDING_TARGET_GENERAL_MEMORIES,
    PENDING_TARGET_GENERAL_MEMORY,
    PENDING_TARGET_KEYED_FACT,
    PendingOwnerMemoryAction,
    PendingOwnerMemoryActionStore,
)


class OwnerMemoryService:
    """Central Brain-facing API for explicit bounded owner facts and memories."""

    def __init__(
        self,
        profile_path: Optional[Path | str] = None,
        event_bus: Any = None,
        *,
        store: Optional[OwnerProfileStore] = None,
        pending_path: Optional[Path | str] = None,
        pending_store: Optional[PendingOwnerMemoryActionStore] = None,
    ):
        self._store = store or OwnerProfileStore(profile_path, event_bus=event_bus)
        if pending_store is not None:
            self._pending_store = pending_store
        else:
            isolated_pending = pending_path
            if isolated_pending is None and (profile_path is not None or store is not None):
                isolated_pending = self._store.path.with_name("pending_owner_memory_action.json")
            self._pending_store = PendingOwnerMemoryActionStore(isolated_pending)

    @property
    def profile_path(self) -> Path:
        return self._store.path

    @property
    def pending_path(self) -> Path:
        return self._pending_store.path

    def execute(self, request: OwnerMemoryRequestV1 | Mapping[str, Any]) -> OwnerMemoryResultV1:
        normalized_request = self._normalize_request(request)
        if isinstance(normalized_request, OwnerMemoryResultV1):
            return normalized_request

        action = normalized_request.action
        key = normalized_request.normalized_key
        memory_kind = normalized_request.memory_kind or "fact"
        general = memory_kind == "general"
        pending_action = action in {OWNER_MEMORY_ACTION_CONFIRM_DELETE, OWNER_MEMORY_ACTION_CANCEL_DELETE}
        keyless_actions = {
            OWNER_MEMORY_ACTION_LIST,
            OWNER_MEMORY_ACTION_COUNT,
            OWNER_MEMORY_ACTION_DELETE_ALL_REQUEST,
            OWNER_MEMORY_ACTION_DELETE_ALL_CONFIRM,
            OWNER_MEMORY_ACTION_FORGET_ALL_GENERAL,
            OWNER_MEMORY_ACTION_CONFIRM_DELETE,
            OWNER_MEMORY_ACTION_CANCEL_DELETE,
        }
        if not general and not pending_action and action not in keyless_actions and not key:
            return self._failure(normalized_request, "missing_fact_key", "Owner fact key is required.")
        if general and action in {
            OWNER_MEMORY_ACTION_REMEMBER,
            OWNER_MEMORY_ACTION_UPDATE,
            OWNER_MEMORY_ACTION_RECALL,
            OWNER_MEMORY_ACTION_INSPECT,
            OWNER_MEMORY_ACTION_FORGET,
            OWNER_MEMORY_ACTION_FORGET_SPECIFIC,
            OWNER_MEMORY_ACTION_FORGET_TOPIC,
        }:
            if not normalized_request.explicit:
                return self._failure(normalized_request, "explicit_memory_trigger_required", "General owner memory requires an explicit request.")
            if normalized_request.persistence != "long_term":
                return self._failure(normalized_request, "invalid_memory_persistence", "General owner memory persistence must be long_term.")
        if general and action in {OWNER_MEMORY_ACTION_REMEMBER, OWNER_MEMORY_ACTION_UPDATE} and not normalized_request.memory:
            return self._failure(normalized_request, "missing_general_memory", "Structured general owner memory is required.")
        if not general and action in {OWNER_MEMORY_ACTION_REMEMBER, OWNER_MEMORY_ACTION_UPDATE} and normalized_request.value is None:
            return self._failure(normalized_request, "missing_fact_value", "Owner fact value is required.")

        if general and action in {OWNER_MEMORY_ACTION_REMEMBER, OWNER_MEMORY_ACTION_UPDATE}:
            return self._store_result(
                normalized_request,
                self._store.save_memory(
                    normalized_request.memory,
                    replace_query=normalized_request.query,
                    force_update=action == OWNER_MEMORY_ACTION_UPDATE,
                ),
            )
        if general and action in {OWNER_MEMORY_ACTION_RECALL, OWNER_MEMORY_ACTION_INSPECT}:
            return self._store_result(
                normalized_request,
                self._store.recall_memories(normalized_request.query),
            )
        if action == OWNER_MEMORY_ACTION_LIST:
            return self._list(normalized_request)
        if action == OWNER_MEMORY_ACTION_COUNT:
            return self._count(normalized_request)
        if action in {OWNER_MEMORY_ACTION_FORGET_SPECIFIC} or (general and action == OWNER_MEMORY_ACTION_FORGET):
            return self._request_specific_delete(normalized_request)
        if action == OWNER_MEMORY_ACTION_FORGET_TOPIC:
            return self._request_topic_delete(normalized_request)
        if action in {OWNER_MEMORY_ACTION_FORGET_ALL_GENERAL, OWNER_MEMORY_ACTION_DELETE_ALL_REQUEST}:
            return self._request_all_general_delete(normalized_request)
        if action in {OWNER_MEMORY_ACTION_FORGET_KEYED_FACT} or (not general and action == OWNER_MEMORY_ACTION_FORGET):
            return self._request_keyed_fact_delete(normalized_request)
        if action in {OWNER_MEMORY_ACTION_CONFIRM_DELETE, OWNER_MEMORY_ACTION_DELETE_ALL_CONFIRM}:
            return self._confirm_delete(normalized_request)
        if action == OWNER_MEMORY_ACTION_CANCEL_DELETE:
            return self._cancel_delete(normalized_request)
        if action in {OWNER_MEMORY_ACTION_REMEMBER, OWNER_MEMORY_ACTION_UPDATE}:
            return self._store_result(
                normalized_request,
                self._store.save_fact(
                    key,
                    normalized_request.value,
                    display_key=normalized_request.display_key,
                ),
            )
        if action == OWNER_MEMORY_ACTION_RECALL:
            return self._store_result(normalized_request, self._store.recall_fact(key))
        return self._failure(normalized_request, "unsupported_owner_memory_action", "Owner memory action is unsupported.")

    def inspect(self, *, include_values: bool = False) -> Dict[str, Any]:
        report = self._store.inspection_report(include_values=include_values)
        pending = self._pending_store.inspection_report()
        report.update(
            {
                "pending_state_path": str(self.pending_path),
                "pending_action_status": pending.get("status", "missing"),
                "pending_action": pending.get("pending_action"),
                "pending_validation_state": pending.get("validation_state", "missing"),
                "pending_error": pending.get("error_code", ""),
            }
        )
        return report

    def has_pending_action(self) -> bool:
        return self._pending_store.read().status == "active"

    def has_pending_state(self) -> bool:
        """Return whether transient confirmation state exists, even if invalid or expired."""

        return self.pending_path.exists()

    def health_check(self) -> Dict[str, Any]:
        report = self.inspect(include_values=False)
        valid = str(report.get("validation_state") or "") in {"valid", "missing_default"}
        pending_valid = str(report.get("pending_validation_state") or "") in {"valid", "missing"}
        healthy = valid and pending_valid
        return {
            "component_name": "owner_memory",
            "component_type": "service",
            "status": "healthy" if healthy else "failed",
            "healthy": healthy,
            "available": healthy,
            "degraded": False,
            "capabilities": [
                "memory.owner_fact.save",
                "memory.owner_fact.update",
                "memory.owner_fact.recall",
                "memory.owner_fact.forget",
                "memory.owner_fact.list",
                "memory.owner.general.remember",
                "memory.owner.general.recall",
                "memory.owner.general.inspect",
                "memory.owner.general.count",
                "memory.owner.general.forget",
                "memory.owner.general.list",
                "memory.owner.deletion.confirm",
                "memory.owner.deletion.cancel",
            ],
            "metadata": {
                "schema_name": report.get("schema_name", ""),
                "detected_version": report.get("detected_version"),
                "target_version": report.get("current_target_version"),
                "fact_count": report.get("fact_count", 0),
                "memory_count": report.get("memory_count", 0),
                "pending_status": report.get("pending_action_status", "missing"),
            },
        }

    def _normalize_request(
        self,
        request: OwnerMemoryRequestV1 | Mapping[str, Any],
    ) -> OwnerMemoryRequestV1 | OwnerMemoryResultV1:
        try:
            return request if isinstance(request, OwnerMemoryRequestV1) else OwnerMemoryRequestV1.from_dict(request)
        except (TypeError, ValueError) as error:
            return OwnerMemoryResultV1(
                False,
                "rejected",
                "",
                error_code="invalid_owner_memory_contract",
                error_message="Owner memory request contract is invalid.",
                metadata={"error_type": type(error).__name__, "value_redacted": True},
            )

    def _list(self, request: OwnerMemoryRequestV1) -> OwnerMemoryResultV1:
        scope = str(request.query.get("scope") or "all")
        facts_result = self._store.list_facts(include_values=True)
        memories_result = self._list_general_for_query(request.query)
        if scope in {"all", "facts"} and not facts_result.success:
            return self._store_result(request, facts_result)
        if scope in {"all", "general"} and not memories_result.success:
            return self._store_result(request, memories_result)
        facts = facts_result.facts if scope in {"all", "facts"} else ()
        memories = memories_result.memories if scope in {"all", "general"} else ()
        return OwnerMemoryResultV1(
            True,
            "listed",
            request.action,
            facts=facts,
            memories=memories,
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            metadata=self._metadata(
                fact_count=len(facts_result.facts),
                memory_count=len(memories_result.memories),
                list_scope=scope,
                response_style=str(request.query.get("response_style") or "combined_list"),
            ),
        )

    def _count(self, request: OwnerMemoryRequestV1) -> OwnerMemoryResultV1:
        scope = str(request.query.get("scope") or "all")
        facts_result = self._store.list_facts(include_values=False)
        memories_result = self._list_general_for_query(request.query)
        if not facts_result.success:
            return self._store_result(request, facts_result)
        if not memories_result.success:
            return self._store_result(request, memories_result)
        return OwnerMemoryResultV1(
            True,
            "counted",
            request.action,
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            metadata=self._metadata(
                count_scope=scope,
                fact_count=len(facts_result.facts),
                memory_count=len(memories_result.memories),
            ),
        )

    def _list_general_for_query(self, query: Mapping[str, Any]) -> OwnerProfileResultV1:
        memory_type = str(query.get("memory_type") or "")
        if memory_type:
            return self._store.recall_memories(
                {
                    "memory_type": memory_type,
                    "match_all": True,
                    "response_style": str(query.get("response_style") or "type_list"),
                    "display_query": str(query.get("display_query") or memory_type),
                },
                limit=MAX_GENERAL_MEMORIES,
            )
        return self._store.list_memories(include_inactive=False)

    def _request_specific_delete(self, request: OwnerMemoryRequestV1) -> OwnerMemoryResultV1:
        preview = self._store.recall_memories(request.query, limit=MAX_GENERAL_MEMORIES)
        if not preview.success:
            return self._store_result(request, preview)
        matches = tuple(preview.memories)
        if not matches:
            return self._selection_result(request, "missing_match", (), candidate_count=0)
        if len(matches) != 1:
            return self._selection_result(
                request,
                "ambiguous",
                matches[:MAX_MEMORY_RETRIEVAL_RESULTS],
                candidate_count=len(matches),
            )
        memory = dict(matches[0])
        return self._create_pending(
            request,
            operation=PENDING_OPERATION_FORGET_SPECIFIC,
            target_kind=PENDING_TARGET_GENERAL_MEMORY,
            target_ids=(str(memory.get("memory_id") or ""),),
            topic=str(request.query.get("display_query") or ""),
            summary=general_memory_clause(memory),
            memories=(memory,),
        )

    def _request_topic_delete(self, request: OwnerMemoryRequestV1) -> OwnerMemoryResultV1:
        preview = self._store.recall_memories(request.query, limit=MAX_GENERAL_MEMORIES)
        if not preview.success:
            return self._store_result(request, preview)
        matches = tuple(preview.memories)
        if not matches:
            return self._selection_result(request, "missing_match", (), candidate_count=0)
        topic = str(request.query.get("display_query") or "that topic")
        return self._create_pending(
            request,
            operation=PENDING_OPERATION_FORGET_TOPIC,
            target_kind=PENDING_TARGET_GENERAL_MEMORIES,
            target_ids=tuple(str(memory.get("memory_id") or "") for memory in matches),
            topic=topic,
            summary=f"{len(matches)} general memories about {topic}",
            memories=matches[:MAX_MEMORY_RETRIEVAL_RESULTS],
            related_keyed_facts=self._related_keyed_facts(request.query),
        )

    def _request_all_general_delete(self, request: OwnerMemoryRequestV1) -> OwnerMemoryResultV1:
        memories_result = self._store.list_memories(include_inactive=False)
        if not memories_result.success:
            return self._store_result(request, memories_result)
        memories = tuple(memories_result.memories)
        if not memories:
            return self._selection_result(request, "missing_match", (), candidate_count=0)
        facts_result = self._store.list_facts(include_values=False)
        if not facts_result.success:
            return self._store_result(request, facts_result)
        return self._create_pending(
            request,
            operation=PENDING_OPERATION_FORGET_ALL_GENERAL,
            target_kind=PENDING_TARGET_GENERAL_MEMORIES,
            target_ids=tuple(str(memory.get("memory_id") or "") for memory in memories),
            topic="general long-term memories",
            summary=f"all {len(memories)} general long-term memories",
            memories=memories[:MAX_MEMORY_RETRIEVAL_RESULTS],
            preserved_fact_keys=tuple(str(fact.get("normalized_key") or "") for fact in facts_result.facts),
        )

    def _request_keyed_fact_delete(self, request: OwnerMemoryRequestV1) -> OwnerMemoryResultV1:
        fact = self._store.recall_fact(request.normalized_key)
        if not fact.success:
            return self._store_result(request, fact)
        if fact.status == "missing":
            return self._selection_result(request, "missing_match", (), candidate_count=0)
        display = fact.display_key or owner_fact_display_name(request.normalized_key)
        return self._create_pending(
            request,
            operation=PENDING_OPERATION_FORGET_KEYED_FACT,
            target_kind=PENDING_TARGET_KEYED_FACT,
            target_key=request.normalized_key,
            target_revision=self._fact_revision(
                request.normalized_key,
                fact.value,
                str(fact.metadata.get("updated_at") or ""),
            ),
            summary=f"{display} is {fact.value}",
            facts=(
                {
                    "normalized_key": request.normalized_key,
                    "display_key": display,
                    "value": fact.value,
                },
            ),
        )

    def _create_pending(
        self,
        request: OwnerMemoryRequestV1,
        *,
        operation: str,
        target_kind: str,
        target_ids: Sequence[str] = (),
        target_key: str = "",
        target_revision: str = "",
        topic: str = "",
        summary: str,
        memories: Sequence[Mapping[str, Any]] = (),
        facts: Sequence[Mapping[str, Any]] = (),
        related_keyed_facts: Sequence[str] = (),
        preserved_fact_keys: Sequence[str] = (),
    ) -> OwnerMemoryResultV1:
        candidate_count = len(target_ids) if target_ids else 1
        pending = self._pending_store.create(
            operation=operation,
            target_kind=target_kind,
            target_ids=target_ids,
            target_key=target_key,
            target_revision=target_revision,
            topic=topic,
            candidate_count=candidate_count,
            summary=summary,
            normalized_request=str(request.metadata.get("normalized_request") or request.action),
        )
        if not pending.success or pending.action is None:
            return self._failure(request, pending.error_code or "pending_write_failed", pending.error_message or "Deletion confirmation could not be created.")
        return OwnerMemoryResultV1(
            True,
            "confirmation_required",
            request.action,
            normalized_key=target_key,
            display_key=owner_fact_display_name(target_key) if target_key else "",
            value=(facts[0].get("value") if facts else None),
            facts=tuple(dict(fact) for fact in facts),
            memory=dict(memories[0]) if memories else {},
            memories=tuple(dict(memory) for memory in memories),
            confirmation_required=True,
            pending_action=pending.action.to_dict(include_targets=False),
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            metadata=self._metadata(
                pending_state_path=str(self.pending_path),
                pending_operation=operation,
                target_kind=target_kind,
                candidate_count=candidate_count,
                topic=topic,
                summary=summary,
                expires_at=pending.action.expires_at,
                related_keyed_facts=list(related_keyed_facts),
                preserved_fact_keys=list(preserved_fact_keys),
                profile_changed=False,
            ),
        )

    def _selection_result(
        self,
        request: OwnerMemoryRequestV1,
        status: str,
        memories: Sequence[Mapping[str, Any]],
        *,
        candidate_count: int,
    ) -> OwnerMemoryResultV1:
        return OwnerMemoryResultV1(
            True,
            status,
            request.action,
            normalized_key=request.normalized_key,
            display_key=request.display_key,
            memory=dict(memories[0]) if memories else {},
            memories=tuple(dict(memory) for memory in memories),
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            metadata=self._metadata(
                candidate_count=candidate_count,
                display_query=str(request.query.get("display_query") or ""),
                profile_changed=False,
            ),
        )

    def _confirm_delete(self, request: OwnerMemoryRequestV1) -> OwnerMemoryResultV1:
        pending = self._pending_store.read(cleanup_invalid=True, cleanup_expired=True)
        if pending.status == "missing":
            return self._pending_result(request, "missing_pending")
        if pending.status == "expired":
            return self._pending_result(request, "expired", pending.action)
        if pending.status == "invalid" or not pending.success or pending.action is None:
            return self._pending_result(request, "invalid_pending")
        action = pending.action
        if action.operation == PENDING_OPERATION_FORGET_KEYED_FACT:
            current = self._store.recall_fact(action.target_key)
            if not current.success:
                return self._store_result(request, current, pending_action=action)
            if current.status == "missing":
                self._pending_store.clear()
                return self._pending_result(request, "target_already_missing", action)
            current_revision = self._fact_revision(
                action.target_key,
                current.value,
                str(current.metadata.get("updated_at") or ""),
            )
            if current_revision != action.target_revision:
                self._pending_store.clear()
                return self._pending_result(request, "target_changed", action)
            store_result = self._store.forget_fact(action.target_key)
            success_status = "deleted_keyed_fact"
        else:
            store_result = self._store.forget_memories_by_ids(list(action.target_ids))
            success_status = {
                PENDING_OPERATION_FORGET_SPECIFIC: "deleted_specific",
                PENDING_OPERATION_FORGET_TOPIC: "deleted_topic",
                PENDING_OPERATION_FORGET_ALL_GENERAL: "deleted_all_general",
            }[action.operation]
        if not store_result.success:
            return self._store_result(request, store_result, pending_action=action)
        cleared = self._pending_store.clear()
        status = success_status if store_result.status == "forgotten" else "target_already_missing"
        result = self._store_result(request, store_result, status=status, pending_action=action)
        return OwnerMemoryResultV1(
            **{
                **result.__dict__,
                "metadata": {
                    **dict(result.metadata),
                    "pending_cleanup_status": cleared.status,
                    "pending_operation": action.operation,
                    "candidate_count": action.candidate_count,
                    "topic": action.topic,
                    "summary": action.summary,
                },
            }
        )

    def _cancel_delete(self, request: OwnerMemoryRequestV1) -> OwnerMemoryResultV1:
        pending = self._pending_store.read(cleanup_invalid=True, cleanup_expired=True)
        if pending.status == "missing":
            return self._pending_result(request, "missing_pending")
        if pending.status == "expired":
            return self._pending_result(request, "expired", pending.action)
        if pending.status == "invalid" or not pending.success or pending.action is None:
            return self._pending_result(request, "invalid_pending")
        cleared = self._pending_store.clear()
        if not cleared.success:
            return self._failure(request, cleared.error_code or "pending_clear_failed", cleared.error_message)
        return self._pending_result(request, "cancelled", pending.action)

    def _pending_result(
        self,
        request: OwnerMemoryRequestV1,
        status: str,
        action: Optional[PendingOwnerMemoryAction] = None,
    ) -> OwnerMemoryResultV1:
        return OwnerMemoryResultV1(
            True,
            status,
            request.action,
            pending_action=action.to_dict(include_targets=False) if action else {},
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            metadata=self._metadata(
                pending_state_path=str(self.pending_path),
                pending_operation=action.operation if action else "",
                candidate_count=action.candidate_count if action else 0,
                topic=action.topic if action else "",
                summary=action.summary if action else "",
                profile_changed=False,
            ),
        )

    def _related_keyed_facts(self, query: Mapping[str, Any]) -> tuple[str, ...]:
        requested = set(query.get("topics") or ()) | set(query.get("tokens") or ())
        if not requested:
            return ()
        facts = self._store.list_facts(include_values=True)
        if not facts.success:
            return ()
        related = []
        for fact in facts.facts:
            tokens = set(
                extract_memory_topics(
                    str(fact.get("normalized_key") or ""),
                    str(fact.get("display_key") or ""),
                    str(fact.get("value") or ""),
                )
            )
            if requested & tokens:
                related.append(str(fact.get("normalized_key") or ""))
        return tuple(related)

    def _store_result(
        self,
        request: OwnerMemoryRequestV1,
        store_result: OwnerProfileResultV1,
        *,
        status: str = "",
        pending_action: Optional[PendingOwnerMemoryAction] = None,
    ) -> OwnerMemoryResultV1:
        metadata = {
            **dict(store_result.metadata),
            "storage_contract": store_result.contract_name,
            "storage_contract_version": store_result.contract_version,
            "value_redacted": True,
            "pending_state_path": str(self.pending_path),
        }
        return OwnerMemoryResultV1(
            store_result.success,
            status or store_result.status,
            request.action,
            normalized_key=store_result.normalized_key,
            display_key=store_result.display_key,
            value=store_result.value,
            previous_value=store_result.previous_value,
            changed=store_result.changed,
            facts=store_result.facts,
            memory=store_result.memory,
            memories=store_result.memories,
            confirmation_required=store_result.confirmation_required,
            pending_action=pending_action.to_dict(include_targets=False) if pending_action else {},
            error_code=store_result.error_code,
            error_message=store_result.error_message,
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            metadata=metadata,
        )

    def _metadata(self, **metadata: Any) -> Dict[str, Any]:
        return {
            "profile_path": str(self.profile_path),
            "pending_state_path": str(self.pending_path),
            "value_redacted": True,
            "memory_content_redacted": True,
            **metadata,
        }

    @staticmethod
    def _fact_revision(key: str, value: Any, updated_at: str) -> str:
        material = json.dumps(
            {"key": str(key or ""), "value": value, "updated_at": str(updated_at or "")},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

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
            metadata=self._metadata(),
        )
