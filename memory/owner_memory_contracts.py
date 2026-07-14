from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Tuple


OWNER_MEMORY_REQUEST_CONTRACT = "ares.owner_memory.request"
OWNER_MEMORY_RESULT_CONTRACT = "ares.owner_memory.result"
OWNER_MEMORY_CONTRACT_VERSION = "v1"

OWNER_MEMORY_ACTION_REMEMBER = "remember"
OWNER_MEMORY_ACTION_UPDATE = "update"
OWNER_MEMORY_ACTION_RECALL = "recall"
OWNER_MEMORY_ACTION_FORGET = "forget"
OWNER_MEMORY_ACTION_LIST = "list"
OWNER_MEMORY_ACTIONS = {
    OWNER_MEMORY_ACTION_REMEMBER,
    OWNER_MEMORY_ACTION_UPDATE,
    OWNER_MEMORY_ACTION_RECALL,
    OWNER_MEMORY_ACTION_FORGET,
    OWNER_MEMORY_ACTION_LIST,
}


def owner_memory_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class OwnerMemoryRequestV1:
    action: str
    normalized_key: str = ""
    display_key: str = ""
    value: Any = None
    correlation_id: str = ""
    session_id: str = ""
    created_at: str = field(default_factory=owner_memory_timestamp)
    metadata: Dict[str, Any] = field(default_factory=dict)
    contract_name: str = OWNER_MEMORY_REQUEST_CONTRACT
    contract_version: str = OWNER_MEMORY_CONTRACT_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "action": self.action,
            "normalized_key": self.normalized_key,
            "display_key": self.display_key,
            "value": self.value,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OwnerMemoryRequestV1":
        if not isinstance(payload, Mapping):
            raise ValueError("Owner memory request must be an object")
        required = {"contract_name", "contract_version", "action"}
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError(f"Owner memory request is missing: {', '.join(missing)}")
        if payload.get("contract_name") != OWNER_MEMORY_REQUEST_CONTRACT:
            raise ValueError("Wrong owner memory request contract")
        if payload.get("contract_version") != OWNER_MEMORY_CONTRACT_VERSION:
            raise ValueError("Unsupported owner memory request version")
        action = str(payload.get("action") or "")
        if action not in OWNER_MEMORY_ACTIONS:
            raise ValueError("Unsupported owner memory action")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("Owner memory request metadata must be an object")
        return cls(
            action=action,
            normalized_key=str(payload.get("normalized_key") or ""),
            display_key=str(payload.get("display_key") or ""),
            value=payload.get("value"),
            correlation_id=str(payload.get("correlation_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            created_at=str(payload.get("created_at") or owner_memory_timestamp()),
            metadata=dict(metadata),
        )


@dataclass(frozen=True)
class OwnerMemoryResultV1:
    success: bool
    status: str
    action: str
    normalized_key: str = ""
    display_key: str = ""
    value: Any = None
    previous_value: Any = None
    changed: bool = False
    facts: Tuple[Dict[str, Any], ...] = ()
    error_code: str = ""
    error_message: str = ""
    correlation_id: str = ""
    session_id: str = ""
    created_at: str = field(default_factory=owner_memory_timestamp)
    metadata: Dict[str, Any] = field(default_factory=dict)
    contract_name: str = OWNER_MEMORY_RESULT_CONTRACT
    contract_version: str = OWNER_MEMORY_CONTRACT_VERSION

    def to_dict(self, *, include_values: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "success": self.success,
            "status": self.status,
            "action": self.action,
            "normalized_key": self.normalized_key,
            "display_key": self.display_key,
            "changed": self.changed,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": dict(self.metadata),
        }
        if include_values:
            if self.value is not None:
                payload["value"] = self.value
            if self.previous_value is not None:
                payload["previous_value"] = self.previous_value
            if self.facts:
                payload["facts"] = [dict(fact) for fact in self.facts]
        elif self.facts:
            payload["fact_count"] = len(self.facts)
        return payload
