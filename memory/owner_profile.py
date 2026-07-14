from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

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
    StoreWriteLock,
    inspect_store,
    load_store_data,
    publish_migration_failure,
    save_store_data,
    utc_now,
)


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OWNER_PROFILE_PATH = (BASE_DIR / "data" / "memory" / "owner_profile.json").resolve()
OWNER_PROFILE_ID = "primary_owner"
OWNER_PROFILE_RESULT_CONTRACT = "ares.owner_profile.operation_result"
OWNER_PROFILE_RESULT_VERSION = "v1"
OWNER_PROFILE_SCHEMA_VERSION = 2
OWNER_FACT_SOURCE = "explicit_owner_statement"
MAX_OWNER_FACTS = 100
MAX_OWNER_PROFILE_BYTES = 65536
OWNER_PROFILE_BACKUP_RETENTION = 1

Loader = Callable[..., Any]
Saver = Callable[..., Any]


@dataclass(frozen=True)
class OwnerProfileResultV1:
    success: bool
    status: str
    operation: str
    normalized_key: str = ""
    display_key: str = ""
    value: Any = None
    previous_value: Any = None
    changed: bool = False
    facts: Tuple[Dict[str, Any], ...] = ()
    error_code: str = ""
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    contract_name: str = OWNER_PROFILE_RESULT_CONTRACT
    contract_version: str = OWNER_PROFILE_RESULT_VERSION

    def to_dict(self, *, include_value: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
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
        if include_value:
            if self.value is not None:
                payload["value"] = self.value
            if self.previous_value is not None:
                payload["previous_value"] = self.previous_value
            if self.facts:
                payload["facts"] = [dict(fact) for fact in self.facts]
        return payload


class OwnerProfileStore:
    """Versioned owner facts behind one canonical, atomically written profile."""

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
        self._transaction_path = self.path.with_name(f"{self.path.name}.owner_transaction")

    def save_fact(
        self,
        key: str,
        value: Any,
        *,
        display_key: str = "",
        source: str = OWNER_FACT_SOURCE,
    ) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        validation = self._validated_fact(key, value, operation="save")
        if isinstance(validation, OwnerProfileResultV1):
            return validation
        normalized_key, clean_value = validation
        clean_display = self._validated_display_key(display_key, normalized_key)
        if isinstance(clean_display, OwnerProfileResultV1):
            return clean_display
        if source != OWNER_FACT_SOURCE:
            return self._validation_failure(
                "save",
                OwnerMemoryValidationError("unsupported_source", "Owner fact source is not allowed."),
                normalized_key=normalized_key,
            )
        try:
            with StoreWriteLock(self._transaction_path):
                profile = self._load()
                facts = dict(profile.get("facts") or {})
                existed = normalized_key in facts
                if not existed and len(facts) >= MAX_OWNER_FACTS:
                    raise OwnerMemoryValidationError("fact_limit_reached", "Owner fact limit reached.")
                previous = dict(facts.get(normalized_key) or {})
                timestamp = self._timestamp_factory()
                facts[normalized_key] = {
                    "value": clean_value,
                    "display_key": clean_display,
                    "normalized_key": normalized_key,
                    "created_at": str(previous.get("created_at") or timestamp),
                    "updated_at": timestamp,
                    "source": OWNER_FACT_SOURCE,
                }
                profile = self._profile(facts)
                self._validate_profile_size(profile)
                self._save(profile)
        except OwnerMemoryValidationError as error:
            return self._validation_failure("save", error, normalized_key=normalized_key)
        except Exception as error:
            return self._failure("save", normalized_key, error, stage="write" if 'profile' in locals() else "read", file_existed_before=file_existed_before)

        status = "updated" if existed else "created"
        result = OwnerProfileResultV1(
            True,
            status,
            "update" if existed else "create",
            normalized_key=normalized_key,
            display_key=clean_display,
            value=clean_value,
            previous_value=previous.get("value") if existed else None,
            changed=True,
            metadata=self._operation_metadata(
                file_existed_before,
                value_length=_value_length(clean_value),
                fact_existed_before=existed,
                fact_count=len(facts),
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
            return self._failure("recall", normalized_key, error, stage="read", file_existed_before=file_existed_before)
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
        else:
            result = OwnerProfileResultV1(
                True,
                "recalled",
                "recall",
                normalized_key=normalized_key,
                display_key=str(fact.get("display_key") or owner_fact_display_name(normalized_key)),
                value=fact.get("value"),
                metadata=self._operation_metadata(
                    file_existed_before,
                    created_at=str(fact.get("created_at") or ""),
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
            with StoreWriteLock(self._transaction_path):
                profile = self._load()
                facts = dict(profile.get("facts") or {})
                previous = dict(facts.get(normalized_key) or {})
                if not previous:
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
                self._save(self._profile(facts))
        except Exception as error:
            return self._failure("forget", normalized_key, error, stage="write" if 'facts' in locals() else "read", file_existed_before=file_existed_before)
        result = OwnerProfileResultV1(
            True,
            "forgotten",
            "forget",
            normalized_key=normalized_key,
            display_key=str(previous.get("display_key") or owner_fact_display_name(normalized_key)),
            previous_value=previous.get("value"),
            changed=True,
            metadata=self._operation_metadata(file_existed_before, fact_count=len(facts)),
        )
        self._publish(result)
        return result

    def list_facts(self, *, include_values: bool = False) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        try:
            profile = self._load()
        except Exception as error:
            return self._failure("list", "", error, stage="read", file_existed_before=file_existed_before)
        facts_by_key = dict(profile.get("facts") or {})
        facts = tuple(
            {
                "normalized_key": key,
                "display_key": str(facts_by_key[key].get("display_key") or owner_fact_display_name(key)),
                **({"value": facts_by_key[key].get("value")} if include_values else {}),
                "created_at": str(facts_by_key[key].get("created_at") or ""),
                "updated_at": str(facts_by_key[key].get("updated_at") or ""),
                "source": str(facts_by_key[key].get("source") or ""),
            }
            for key in sorted(facts_by_key)
        )
        metadata: Dict[str, Any] = {
            "owner_id": OWNER_PROFILE_ID,
            "fact_keys": sorted(facts_by_key),
            "fact_count": len(facts_by_key),
        }
        if include_values:
            metadata["facts"] = {fact["normalized_key"]: dict(fact) for fact in facts}
        return OwnerProfileResultV1(True, "listed", "list", facts=facts, metadata=metadata)

    def inspection_report(self, *, include_values: bool = False) -> Dict[str, Any]:
        report = inspect_store(self.path, SCHEMA_OWNER_PROFILE).to_dict()
        facts = self.list_facts(include_values=include_values)
        report.update(
            {
                "profile_path": str(self.path),
                "owner_id": OWNER_PROFILE_ID,
                "fact_count": int(facts.metadata.get("fact_count", 0)) if facts.success else 0,
                "facts": [dict(fact) for fact in facts.facts] if facts.success else [],
                "storage_error": facts.error_code if not facts.success else "",
                "last_backup": self._latest_backup_path(),
            }
        )
        return report

    def _load(self) -> Dict[str, Any]:
        try:
            profile = self._loader(
                self.path,
                SCHEMA_OWNER_PROFILE,
                {"owner_id": OWNER_PROFILE_ID, "facts": {}},
                backup_retention=OWNER_PROFILE_BACKUP_RETENTION,
            )
        except MigrationError as error:
            publish_migration_failure(self.events, SCHEMA_OWNER_PROFILE, self.path, error)
            raise
        facts = dict(profile.get("facts") or {})
        try:
            for key, raw_entry in facts.items():
                entry = dict(raw_entry)
                if normalize_owner_fact_key(key) != key or entry.get("normalized_key") != key:
                    raise OwnerMemoryValidationError("noncanonical_key", "Owner profile contains a noncanonical key.", normalized_key=key)
                if normalize_owner_fact_value(entry.get("value")) != entry.get("value"):
                    raise OwnerMemoryValidationError("noncanonical_value", "Owner profile contains a noncanonical value.", normalized_key=key)
        except OwnerMemoryValidationError as error:
            raise MigrationError(
                "Owner profile data failed bounded fact validation",
                schema_name=SCHEMA_OWNER_PROFILE,
                path=self.path,
                status="invalid_store_data",
                details={"error_code": error.code},
            ) from error
        return {"owner_id": OWNER_PROFILE_ID, "facts": facts}

    def _save(self, profile: Dict[str, Any]) -> None:
        self._saver(
            self.path,
            SCHEMA_OWNER_PROFILE,
            profile,
            metadata={"owner_id": OWNER_PROFILE_ID, "purpose": "explicit_owner_facts"},
            backup_retention=OWNER_PROFILE_BACKUP_RETENTION,
        )

    def _profile(self, facts: Dict[str, Any]) -> Dict[str, Any]:
        return {"owner_id": OWNER_PROFILE_ID, "facts": {key: facts[key] for key in sorted(facts)}}

    def _validate_profile_size(self, profile: Dict[str, Any]) -> None:
        size = len(json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        if size > MAX_OWNER_PROFILE_BYTES:
            raise OwnerMemoryValidationError("profile_size_limit_reached", "Owner profile size limit reached.")

    def _validated_display_key(self, display_key: str, normalized_key: str) -> str | OwnerProfileResultV1:
        source = unicodedata.normalize("NFKC", str(display_key or owner_fact_display_name(normalized_key)))
        clean = " ".join(source.strip().split()).rstrip(" ?!.,;").strip()
        if not clean or len(clean) > 120 or any(unicodedata.category(character) == "Cc" for character in clean):
            return self._validation_failure(
                "save",
                OwnerMemoryValidationError("invalid_display_key", "Owner fact display key is invalid."),
                normalized_key=normalized_key,
            )
        return clean

    def _operation_metadata(self, file_existed_before: bool, **metadata: Any) -> Dict[str, Any]:
        return {
            "owner_id": OWNER_PROFILE_ID,
            "profile_path": str(self.path),
            "file_existed_before": bool(file_existed_before),
            "schema_version": OWNER_PROFILE_SCHEMA_VERSION,
            **metadata,
        }

    def _validated_key(self, key: str, *, operation: str) -> str | OwnerProfileResultV1:
        try:
            return normalize_owner_fact_key(key)
        except OwnerMemoryValidationError as error:
            return self._validation_failure(operation, error)

    def _validated_fact(self, key: str, value: Any, *, operation: str) -> tuple[str, Any] | OwnerProfileResultV1:
        key_result = self._validated_key(key, operation=operation)
        if isinstance(key_result, OwnerProfileResultV1):
            return key_result
        try:
            clean_value = normalize_owner_fact_value(value)
        except OwnerMemoryValidationError as error:
            return self._validation_failure(operation, error, normalized_key=key_result)
        return key_result, clean_value

    def _validation_failure(self, operation: str, error: OwnerMemoryValidationError, *, normalized_key: str = "") -> OwnerProfileResultV1:
        key = error.normalized_key or normalized_key
        result = OwnerProfileResultV1(
            False,
            "rejected",
            operation,
            normalized_key=key,
            display_key=owner_fact_display_name(key),
            error_code=error.code,
            error_message=error.message,
            metadata={**self._operation_metadata(self.path.exists()), "protected": error.protected, "value_redacted": True},
        )
        self._publish(result)
        return result

    def _failure(self, operation: str, normalized_key: str, error: Exception, *, stage: str, file_existed_before: bool) -> OwnerProfileResultV1:
        code = error.status if isinstance(error, MigrationError) else f"{stage}_failed"
        result = OwnerProfileResultV1(
            False,
            "storage_failed",
            operation,
            normalized_key=normalized_key,
            display_key=owner_fact_display_name(normalized_key),
            error_code=code,
            error_message=f"Owner profile {stage} failed safely.",
            metadata={**self._operation_metadata(file_existed_before), "error_type": type(error).__name__, "value_redacted": True},
        )
        self._publish(result)
        return result

    def _publish(self, result: OwnerProfileResultV1) -> None:
        if self.events:
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

    def _latest_backup_path(self) -> str:
        backup_dir = self.path.parent / ".migration_backups"
        backups = sorted(backup_dir.glob(f"{self.path.name}.*.bak"), key=lambda item: item.stat().st_mtime, reverse=True) if backup_dir.exists() else []
        return str(backups[0]) if backups else ""


def resolve_owner_profile_path(path: Optional[Path | str] = None) -> Path:
    configured = str(path) if path is not None else os.environ.get("ARES_OWNER_PROFILE_PATH", "").strip()
    candidate = Path(configured).expanduser() if configured else DEFAULT_OWNER_PROFILE_PATH
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    return candidate.resolve()


def _value_length(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    return len(str(value))
