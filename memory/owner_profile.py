from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.OwnerMemory import (
    OwnerMemoryValidationError,
    normalize_owner_fact_key,
    normalize_owner_fact_value,
    owner_fact_display_name,
)
from events import get_global_bus
from memory.schema_migrations import (
    MigrationError,
    SCHEMA_OWNER_PROFILE,
    load_store_data,
    publish_migration_failure,
    save_store_data,
    utc_now,
)


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OWNER_PROFILE_PATH = (
    BASE_DIR / "data" / "memory" / "owner_profile.json"
).resolve()
OWNER_PROFILE_ID = "primary_owner"
OWNER_PROFILE_RESULT_CONTRACT = "ares.owner_profile.operation_result"
OWNER_PROFILE_RESULT_VERSION = "v1"

Loader = Callable[..., Any]
Saver = Callable[..., Any]


@dataclass(frozen=True)
class OwnerProfileResultV1:
    success: bool
    status: str
    operation: str
    normalized_key: str = ""
    display_key: str = ""
    value: str = ""
    changed: bool = False
    error_code: str = ""
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    contract_name: str = OWNER_PROFILE_RESULT_CONTRACT
    contract_version: str = OWNER_PROFILE_RESULT_VERSION

    def to_dict(self, *, include_value: bool = True) -> Dict[str, Any]:
        payload = {
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "success": self.success,
            "status": self.status,
            "operation": self.operation,
            "normalized_key": self.normalized_key,
            "display_key": self.display_key,
            "changed": self.changed,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": dict(self.metadata),
        }
        if include_value and self.value:
            payload["value"] = self.value
        return payload


class OwnerProfileStore:
    """Explicit bounded owner facts backed by the shared atomic schema writer."""

    def __init__(
        self,
        path: Optional[Path | str] = None,
        event_bus: Any = None,
        *,
        loader: Loader = load_store_data,
        saver: Saver = save_store_data,
        timestamp_factory: Callable[[], str] = utc_now,
    ):
        self.path = resolve_owner_profile_path(path)
        self.events = event_bus if event_bus is not None else get_global_bus()
        self._loader = loader
        self._saver = saver
        self._timestamp_factory = timestamp_factory

    def save_fact(self, key: str, value: str) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        validation = self._validated_fact(key, value, operation="save")
        if isinstance(validation, OwnerProfileResultV1):
            return validation
        normalized_key, clean_value = validation
        try:
            profile = self._load()
        except Exception as error:
            return self._failure(
                "save",
                normalized_key,
                error,
                stage="read",
                file_existed_before=file_existed_before,
            )

        facts = dict(profile.get("facts") or {})
        existed = normalized_key in facts
        facts[normalized_key] = {
            "value": clean_value,
            "updated_at": self._timestamp_factory(),
        }
        profile = {
            "owner_id": OWNER_PROFILE_ID,
            "facts": {fact_key: facts[fact_key] for fact_key in sorted(facts)},
        }
        try:
            self._save(profile)
        except Exception as error:
            return self._failure(
                "save",
                normalized_key,
                error,
                stage="write",
                file_existed_before=file_existed_before,
            )

        status = "updated" if existed else "created"
        result = OwnerProfileResultV1(
            True,
            status,
            "update" if existed else "create",
            normalized_key=normalized_key,
            display_key=owner_fact_display_name(normalized_key),
            changed=True,
            metadata=self._operation_metadata(
                file_existed_before,
                value_length=len(clean_value),
                fact_existed_before=existed,
            ),
        )
        self._publish(result)
        return result

    def recall_fact(self, key: str) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        validation = self._validated_key(key, operation="recall")
        if isinstance(validation, OwnerProfileResultV1):
            return validation
        normalized_key = validation
        try:
            profile = self._load()
        except Exception as error:
            return self._failure(
                "recall",
                normalized_key,
                error,
                stage="read",
                file_existed_before=file_existed_before,
            )

        fact = dict(profile.get("facts", {}).get(normalized_key) or {})
        if not fact:
            result = OwnerProfileResultV1(
                True,
                "missing",
                "miss",
                normalized_key=normalized_key,
                display_key=owner_fact_display_name(normalized_key),
                metadata=self._operation_metadata(file_existed_before),
            )
            self._publish(result)
            return result

        result = OwnerProfileResultV1(
            True,
            "recalled",
            "recall",
            normalized_key=normalized_key,
            display_key=owner_fact_display_name(normalized_key),
            value=str(fact.get("value") or ""),
            metadata=self._operation_metadata(
                file_existed_before,
                updated_at=str(fact.get("updated_at") or ""),
            ),
        )
        self._publish(result)
        return result

    def forget_fact(self, key: str) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        validation = self._validated_key(key, operation="forget")
        if isinstance(validation, OwnerProfileResultV1):
            return validation
        normalized_key = validation
        try:
            profile = self._load()
        except Exception as error:
            return self._failure(
                "forget",
                normalized_key,
                error,
                stage="read",
                file_existed_before=file_existed_before,
            )

        facts = dict(profile.get("facts") or {})
        if normalized_key not in facts:
            result = OwnerProfileResultV1(
                True,
                "missing",
                "miss",
                normalized_key=normalized_key,
                display_key=owner_fact_display_name(normalized_key),
                metadata=self._operation_metadata(file_existed_before),
            )
            self._publish(result)
            return result

        facts.pop(normalized_key)
        profile = {
            "owner_id": OWNER_PROFILE_ID,
            "facts": {fact_key: facts[fact_key] for fact_key in sorted(facts)},
        }
        try:
            self._save(profile)
        except Exception as error:
            return self._failure(
                "forget",
                normalized_key,
                error,
                stage="write",
                file_existed_before=file_existed_before,
            )

        result = OwnerProfileResultV1(
            True,
            "forgotten",
            "forget",
            normalized_key=normalized_key,
            display_key=owner_fact_display_name(normalized_key),
            changed=True,
            metadata=self._operation_metadata(file_existed_before),
        )
        self._publish(result)
        return result

    def list_facts(self, *, include_values: bool = False) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        try:
            profile = self._load()
        except Exception as error:
            return self._failure(
                "list",
                "",
                error,
                stage="read",
                file_existed_before=file_existed_before,
            )
        facts = dict(profile.get("facts") or {})
        metadata: Dict[str, Any] = {
            "owner_id": OWNER_PROFILE_ID,
            "fact_keys": sorted(facts),
            "fact_count": len(facts),
        }
        if include_values:
            metadata["facts"] = {
                key: {
                    "value": str(dict(facts[key]).get("value") or ""),
                    "updated_at": str(dict(facts[key]).get("updated_at") or ""),
                }
                for key in sorted(facts)
            }
        return OwnerProfileResultV1(True, "listed", "list", metadata=metadata)

    def _load(self) -> Dict[str, Any]:
        try:
            profile = self._loader(
                self.path,
                SCHEMA_OWNER_PROFILE,
                {"owner_id": OWNER_PROFILE_ID, "facts": {}},
            )
        except MigrationError as error:
            publish_migration_failure(self.events, SCHEMA_OWNER_PROFILE, self.path, error)
            raise
        facts = dict(profile.get("facts") or {})
        try:
            for key, entry in facts.items():
                if normalize_owner_fact_key(key) != key:
                    raise OwnerMemoryValidationError(
                        "noncanonical_key",
                        "Owner profile contains a noncanonical key.",
                        normalized_key=key,
                    )
                value = str(dict(entry).get("value") or "")
                if normalize_owner_fact_value(value) != value:
                    raise OwnerMemoryValidationError(
                        "noncanonical_value",
                        "Owner profile contains a noncanonical value.",
                        normalized_key=key,
                    )
        except OwnerMemoryValidationError as error:
            raise MigrationError(
                "Owner profile data failed bounded fact validation",
                schema_name=SCHEMA_OWNER_PROFILE,
                path=self.path,
                status="invalid_store_data",
                details={"error_code": error.code},
            ) from error
        return {
            "owner_id": OWNER_PROFILE_ID,
            "facts": facts,
        }

    def _save(self, profile: Dict[str, Any]) -> None:
        self._saver(
            self.path,
            SCHEMA_OWNER_PROFILE,
            profile,
            metadata={"owner_id": OWNER_PROFILE_ID, "purpose": "explicit_owner_facts"},
        )

    def _operation_metadata(
        self,
        file_existed_before: bool,
        **metadata: Any,
    ) -> Dict[str, Any]:
        return {
            "owner_id": OWNER_PROFILE_ID,
            "profile_path": str(self.path),
            "file_existed_before": bool(file_existed_before),
            **metadata,
        }

    def _validated_key(
        self,
        key: str,
        *,
        operation: str,
    ) -> str | OwnerProfileResultV1:
        try:
            return normalize_owner_fact_key(key)
        except OwnerMemoryValidationError as error:
            return self._validation_failure(operation, error)

    def _validated_fact(
        self,
        key: str,
        value: str,
        *,
        operation: str,
    ) -> tuple[str, str] | OwnerProfileResultV1:
        key_result = self._validated_key(key, operation=operation)
        if isinstance(key_result, OwnerProfileResultV1):
            return key_result
        try:
            clean_value = normalize_owner_fact_value(value)
        except OwnerMemoryValidationError as error:
            return self._validation_failure(operation, error, normalized_key=key_result)
        return key_result, clean_value

    def _validation_failure(
        self,
        operation: str,
        error: OwnerMemoryValidationError,
        *,
        normalized_key: str = "",
    ) -> OwnerProfileResultV1:
        key = error.normalized_key or normalized_key
        result = OwnerProfileResultV1(
            False,
            "rejected",
            operation,
            normalized_key=key,
            display_key=owner_fact_display_name(key),
            error_code=error.code,
            error_message=error.message,
            metadata={
                **self._operation_metadata(self.path.exists()),
                "protected": error.protected,
                "value_redacted": True,
            },
        )
        self._publish(result)
        return result

    def _failure(
        self,
        operation: str,
        normalized_key: str,
        error: Exception,
        *,
        stage: str,
        file_existed_before: bool,
    ) -> OwnerProfileResultV1:
        if isinstance(error, MigrationError):
            code = error.status
        elif isinstance(error, OSError):
            code = f"{stage}_failed"
        else:
            code = f"{stage}_failed"
        result = OwnerProfileResultV1(
            False,
            "storage_failed",
            operation,
            normalized_key=normalized_key,
            display_key=owner_fact_display_name(normalized_key),
            error_code=code,
            error_message=f"Owner profile {stage} failed safely.",
            metadata={
                **self._operation_metadata(file_existed_before),
                "error_type": type(error).__name__,
                "value_redacted": True,
            },
        )
        self._publish(result)
        return result

    def _publish(self, result: OwnerProfileResultV1) -> None:
        if not self.events:
            return
        self.events.publish(
            "owner_profile.operation",
            {
                "operation": result.operation,
                "status": result.status,
                "success": result.success,
                "normalized_key": result.normalized_key,
                "changed": result.changed,
                "error_code": result.error_code,
                "value_redacted": True,
            },
            source="memory.owner_profile",
        )


def resolve_owner_profile_path(path: Optional[Path | str] = None) -> Path:
    configured = (
        str(path)
        if path is not None
        else os.environ.get("ARES_OWNER_PROFILE_PATH", "").strip()
    )
    candidate = Path(configured).expanduser() if configured else DEFAULT_OWNER_PROFILE_PATH
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    return candidate.resolve()
