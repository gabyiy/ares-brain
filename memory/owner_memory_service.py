from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from memory.owner_memory_contracts import (
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
    """Central Brain-facing API for explicit bounded owner facts."""

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
        if action != OWNER_MEMORY_ACTION_LIST and not key:
            return self._failure(normalized_request, "missing_fact_key", "Owner fact key is required.")
        if action in {OWNER_MEMORY_ACTION_REMEMBER, OWNER_MEMORY_ACTION_UPDATE} and normalized_request.value is None:
            return self._failure(normalized_request, "missing_fact_value", "Owner fact value is required.")

        if action in {OWNER_MEMORY_ACTION_REMEMBER, OWNER_MEMORY_ACTION_UPDATE}:
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
            store_result = self._store.list_facts(include_values=True)
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
            ],
            "metadata": {
                "schema_name": report.get("schema_name", ""),
                "detected_version": report.get("detected_version"),
                "target_version": report.get("current_target_version"),
                "fact_count": report.get("fact_count", 0),
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
