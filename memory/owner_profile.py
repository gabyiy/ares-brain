from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from core.OwnerLongTermMemory import (
    GENERAL_MEMORY_PERSISTENCE,
    GENERAL_MEMORY_SOURCE,
    MAX_GENERAL_MEMORIES,
    MAX_GENERAL_MEMORY_HISTORY,
    MAX_MEMORY_RETRIEVAL_RESULTS,
    GeneralMemoryValidationError,
    general_memory_id,
    likely_duplicate_memory,
    normalize_general_memory_record,
    normalize_memory_query,
    score_general_memory,
)
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
    load_store_envelope,
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
OWNER_PROFILE_SCHEMA_VERSION = 3
OWNER_FACT_SOURCE = "explicit_owner_statement"
MAX_OWNER_FACTS = 100
MAX_OWNER_PROFILE_BYTES = 65536
OWNER_PROFILE_BACKUP_RETENTION = 1
OWNER_DELETE_CONFIRMATION_TTL_SECONDS = 300

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
    memory: Dict[str, Any] = field(default_factory=dict)
    memories: Tuple[Dict[str, Any], ...] = ()
    confirmation_required: bool = False
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
            "confirmation_required": self.confirmation_required,
        }
        if include_value:
            if self.value is not None:
                payload["value"] = self.value
            if self.previous_value is not None:
                payload["previous_value"] = self.previous_value
            if self.facts:
                payload["facts"] = [dict(fact) for fact in self.facts]
            if self.memory:
                payload["memory"] = dict(self.memory)
            if self.memories:
                payload["memories"] = [dict(memory) for memory in self.memories]
        return payload


class OwnerProfileStore:
    """Versioned owner facts and memories in one atomically written profile."""

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
                profile = self._profile(
                    facts,
                    memories=list(profile.get("memories") or []),
                    pending_delete_all=profile.get("pending_delete_all"),
                )
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
                self._save(
                    self._profile(
                        facts,
                        memories=list(profile.get("memories") or []),
                        pending_delete_all=profile.get("pending_delete_all"),
                    )
                )
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

    def save_memory(
        self,
        record: Mapping[str, Any],
        *,
        replace_query: Optional[Mapping[str, Any]] = None,
        force_update: bool = False,
    ) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        try:
            normalized = normalize_general_memory_record(record)
            embedded_replace = normalized.pop("replacement_query", None)
            clean_replace = normalize_memory_query(replace_query or embedded_replace or {}) if (replace_query or embedded_replace) else {}
        except (GeneralMemoryValidationError, OwnerMemoryValidationError) as error:
            return self._general_validation_failure("save_memory", error)

        try:
            with StoreWriteLock(self._transaction_path):
                profile = self._load()
                memories = [dict(memory) for memory in profile.get("memories") or []]
                duplicate_index = next(
                    (
                        index
                        for index, existing in enumerate(memories)
                        if existing.get("status") == "active" and likely_duplicate_memory(existing, normalized)
                    ),
                    None,
                )
                timestamp = self._timestamp_factory()
                if duplicate_index is not None and not force_update and not clean_replace:
                    existing = dict(memories[duplicate_index])
                    existing["updated_at"] = timestamp
                    memories[duplicate_index] = existing
                    profile = self._profile(
                        dict(profile.get("facts") or {}),
                        memories=self._trim_memory_history(memories),
                        pending_delete_all=profile.get("pending_delete_all"),
                    )
                    self._validate_profile_size(profile)
                    self._save(profile)
                    result = OwnerProfileResultV1(
                        True,
                        "duplicate",
                        "remember_memory",
                        memory=existing,
                        changed=True,
                        metadata=self._general_operation_metadata(
                            file_existed_before,
                            active_memory_count=self._active_memory_count(memories),
                            duplicate=True,
                        ),
                    )
                    self._publish(result)
                    return result

                memory_id = general_memory_id(normalized)
                matching_replacements = self._matching_memories(memories, clean_replace, limit=MAX_GENERAL_MEMORIES) if clean_replace else []
                replaced = []
                for index, previous in matching_replacements:
                    if previous.get("memory_id") == memory_id:
                        continue
                    superseded = dict(memories[index])
                    superseded.update(
                        {
                            "status": "superseded",
                            "updated_at": timestamp,
                            "superseded_at": timestamp,
                            "replaced_by": memory_id,
                        }
                    )
                    memories[index] = superseded
                    replaced.append(dict(previous))

                existing_same_id = next(
                    (index for index, memory in enumerate(memories) if memory.get("memory_id") == memory_id),
                    None,
                )
                if existing_same_id is None and self._active_memory_count(memories) >= MAX_GENERAL_MEMORIES:
                    raise GeneralMemoryValidationError("memory_limit_reached", "Long-term owner memory limit reached.")

                stored = {
                    **normalized,
                    "memory_id": memory_id,
                    "created_at": (
                        str(memories[existing_same_id].get("created_at") or timestamp)
                        if existing_same_id is not None
                        else timestamp
                    ),
                    "updated_at": timestamp,
                    "status": "active",
                }
                if existing_same_id is not None:
                    memories[existing_same_id] = stored
                else:
                    memories.append(stored)
                memories = self._trim_memory_history(memories)
                profile = self._profile(
                    dict(profile.get("facts") or {}),
                    memories=memories,
                    pending_delete_all=profile.get("pending_delete_all"),
                )
                self._validate_profile_size(profile)
                self._save(profile)
        except (GeneralMemoryValidationError, OwnerMemoryValidationError) as error:
            return self._general_validation_failure("save_memory", error)
        except Exception as error:
            return self._failure("save_memory", "", error, stage="write" if 'profile' in locals() else "read", file_existed_before=file_existed_before)

        status = "updated" if replaced or force_update else "created"
        result = OwnerProfileResultV1(
            True,
            status,
            "update_memory" if status == "updated" else "remember_memory",
            memory=stored,
            memories=tuple(replaced),
            changed=True,
            metadata=self._general_operation_metadata(
                file_existed_before,
                active_memory_count=self._active_memory_count(memories),
                replaced_count=len(replaced),
            ),
        )
        self._publish(result)
        return result

    def recall_memories(
        self,
        query: Mapping[str, Any],
        *,
        limit: int = MAX_MEMORY_RETRIEVAL_RESULTS,
    ) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        try:
            clean_query = normalize_memory_query(query)
            profile = self._load()
            matches = self._matching_memories(
                [dict(memory) for memory in profile.get("memories") or []],
                clean_query,
                limit=max(1, min(MAX_GENERAL_MEMORIES, int(limit))),
            )
        except GeneralMemoryValidationError as error:
            return self._general_validation_failure("recall_memory", error)
        except Exception as error:
            return self._failure("recall_memory", "", error, stage="read", file_existed_before=file_existed_before)
        memories = tuple(dict(memory) for _, memory in matches)
        result = OwnerProfileResultV1(
            True,
            "recalled" if memories else "missing",
            "recall_memory" if memories else "miss",
            memory=dict(memories[0]) if memories else {},
            memories=memories,
            metadata=self._general_operation_metadata(
                file_existed_before,
                result_count=len(memories),
                query_type=str(clean_query.get("memory_type") or ""),
                query_topics=list(clean_query.get("topics") or []),
                response_style=str(clean_query.get("response_style") or "topic"),
                display_query=str(clean_query.get("display_query") or ""),
            ),
        )
        self._publish(result)
        return result

    def forget_memories(self, query: Mapping[str, Any]) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        try:
            clean_query = normalize_memory_query(query)
            with StoreWriteLock(self._transaction_path):
                profile = self._load()
                memories = [dict(memory) for memory in profile.get("memories") or []]
                matches = self._matching_memories(memories, clean_query, limit=MAX_GENERAL_MEMORIES)
                if not matches:
                    result = OwnerProfileResultV1(
                        True,
                        "missing",
                        "miss",
                        metadata=self._general_operation_metadata(
                            file_existed_before,
                            result_count=0,
                            display_query=str(clean_query.get("display_query") or ""),
                        ),
                    )
                    self._publish(result)
                    return result
                timestamp = self._timestamp_factory()
                forgotten = []
                for index, previous in matches:
                    updated = dict(memories[index])
                    updated.update({"status": "forgotten", "updated_at": timestamp})
                    memories[index] = updated
                    forgotten.append(updated)
                memories = self._trim_memory_history(memories)
                profile = self._profile(
                    dict(profile.get("facts") or {}),
                    memories=memories,
                    pending_delete_all=profile.get("pending_delete_all"),
                )
                self._validate_profile_size(profile)
                self._save(profile)
        except GeneralMemoryValidationError as error:
            return self._general_validation_failure("forget_memory", error)
        except Exception as error:
            return self._failure("forget_memory", "", error, stage="write" if 'profile' in locals() else "read", file_existed_before=file_existed_before)
        result = OwnerProfileResultV1(
            True,
            "forgotten",
            "forget_memory",
            memory=dict(forgotten[0]),
            memories=tuple(forgotten),
            changed=True,
            metadata=self._general_operation_metadata(
                file_existed_before,
                forgotten_count=len(forgotten),
                active_memory_count=self._active_memory_count(memories),
                display_query=str(clean_query.get("display_query") or ""),
            ),
        )
        self._publish(result)
        return result

    def forget_memories_by_ids(self, memory_ids: Tuple[str, ...] | list[str]) -> OwnerProfileResultV1:
        """Forget only the exact active records selected during confirmation preview."""

        file_existed_before = self.path.exists()
        target_ids = tuple(dict.fromkeys(str(item or "") for item in memory_ids))
        if (
            not target_ids
            or len(target_ids) > MAX_GENERAL_MEMORIES
            or any(not item.startswith("mem-") or len(item) != 20 for item in target_ids)
        ):
            return self._general_validation_failure(
                "forget_memory_ids",
                GeneralMemoryValidationError(
                    "invalid_memory_targets",
                    "Confirmed owner-memory targets are invalid.",
                ),
            )
        try:
            with StoreWriteLock(self._transaction_path):
                profile = self._load()
                memories = [dict(memory) for memory in profile.get("memories") or []]
                target_set = set(target_ids)
                matches = [
                    (index, memory)
                    for index, memory in enumerate(memories)
                    if memory.get("status") == "active" and memory.get("memory_id") in target_set
                ]
                if not matches:
                    result = OwnerProfileResultV1(
                        True,
                        "missing",
                        "miss",
                        metadata=self._general_operation_metadata(
                            file_existed_before,
                            requested_count=len(target_ids),
                            forgotten_count=0,
                        ),
                    )
                    self._publish(result)
                    return result
                timestamp = self._timestamp_factory()
                forgotten = []
                for index, previous in matches:
                    updated = dict(previous)
                    updated.update({"status": "forgotten", "updated_at": timestamp})
                    memories[index] = updated
                    forgotten.append(updated)
                memories = self._trim_memory_history(memories)
                profile = self._profile(
                    dict(profile.get("facts") or {}),
                    memories=memories,
                    pending_delete_all=profile.get("pending_delete_all"),
                )
                self._validate_profile_size(profile)
                self._save(profile)
        except Exception as error:
            return self._failure(
                "forget_memory_ids",
                "",
                error,
                stage="write" if "profile" in locals() else "read",
                file_existed_before=file_existed_before,
            )
        result = OwnerProfileResultV1(
            True,
            "forgotten",
            "forget_memory_ids",
            memory=dict(forgotten[0]),
            memories=tuple(forgotten),
            changed=True,
            metadata=self._general_operation_metadata(
                file_existed_before,
                requested_count=len(target_ids),
                forgotten_count=len(forgotten),
                active_memory_count=self._active_memory_count(memories),
            ),
        )
        self._publish(result)
        return result

    def list_memories(self, *, include_inactive: bool = False) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        try:
            profile = self._load()
        except Exception as error:
            return self._failure("list_memories", "", error, stage="read", file_existed_before=file_existed_before)
        all_memories = [dict(memory) for memory in profile.get("memories") or []]
        selected = [memory for memory in all_memories if include_inactive or memory.get("status") == "active"]
        selected.sort(key=lambda memory: (str(memory.get("created_at") or ""), str(memory.get("memory_id") or "")))
        return OwnerProfileResultV1(
            True,
            "listed",
            "list_memories",
            memories=tuple(selected),
            metadata=self._general_operation_metadata(
                file_existed_before,
                active_memory_count=self._active_memory_count(all_memories),
                stored_memory_count=len(all_memories),
                pending_delete_all=bool(profile.get("pending_delete_all")),
            ),
        )

    def request_delete_all(self) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        try:
            with StoreWriteLock(self._transaction_path):
                profile = self._load()
                requested_at = self._timestamp_factory()
                expires_at = _add_seconds(requested_at, OWNER_DELETE_CONFIRMATION_TTL_SECONDS)
                profile = self._profile(
                    dict(profile.get("facts") or {}),
                    memories=list(profile.get("memories") or []),
                    pending_delete_all={"requested_at": requested_at, "expires_at": expires_at},
                )
                self._save(profile)
        except Exception as error:
            return self._failure("delete_all_request", "", error, stage="write" if 'profile' in locals() else "read", file_existed_before=file_existed_before)
        result = OwnerProfileResultV1(
            True,
            "confirmation_required",
            "delete_all_request",
            confirmation_required=True,
            changed=True,
            metadata=self._general_operation_metadata(file_existed_before, expires_at=expires_at),
        )
        self._publish(result)
        return result

    def confirm_delete_all(self) -> OwnerProfileResultV1:
        file_existed_before = self.path.exists()
        try:
            with StoreWriteLock(self._transaction_path):
                profile = self._load()
                pending = dict(profile.get("pending_delete_all") or {})
                if not pending or _timestamp_expired(str(pending.get("expires_at") or "")):
                    if pending:
                        self._save(
                            self._profile(
                                dict(profile.get("facts") or {}),
                                memories=list(profile.get("memories") or []),
                                pending_delete_all=None,
                            )
                        )
                    result = OwnerProfileResultV1(
                        True,
                        "missing_confirmation",
                        "delete_all_confirm",
                        metadata=self._general_operation_metadata(file_existed_before),
                    )
                    self._publish(result)
                    return result
                facts = dict(profile.get("facts") or {})
                memories = [dict(memory) for memory in profile.get("memories") or []]
                deleted_fact_count = 0
                deleted_memory_count = self._active_memory_count(memories)
                timestamp = self._timestamp_factory()
                for memory in memories:
                    if memory.get("status") == "active":
                        memory.update({"status": "forgotten", "updated_at": timestamp})
                self._save(
                    self._profile(
                        facts,
                        memories=self._trim_memory_history(memories),
                        pending_delete_all=None,
                    )
                )
        except Exception as error:
            return self._failure("delete_all_confirm", "", error, stage="write" if 'profile' in locals() else "read", file_existed_before=file_existed_before)
        result = OwnerProfileResultV1(
            True,
            "deleted_all",
            "delete_all_confirm",
            changed=True,
            metadata=self._general_operation_metadata(
                file_existed_before,
                deleted_fact_count=deleted_fact_count,
                deleted_memory_count=deleted_memory_count,
            ),
        )
        self._publish(result)
        return result

    def inspection_report(self, *, include_values: bool = False) -> Dict[str, Any]:
        report = inspect_store(self.path, SCHEMA_OWNER_PROFILE).to_dict()
        facts_output: list[Dict[str, Any]] = []
        memories_output: list[Dict[str, Any]] = []
        pending_delete_all = False
        storage_error = ""
        if report.get("validation_state") != "invalid":
            try:
                loaded = load_store_envelope(
                    self.path,
                    SCHEMA_OWNER_PROFILE,
                    {
                        "owner_id": OWNER_PROFILE_ID,
                        "facts": {},
                        "memories": [],
                        "pending_delete_all": None,
                    },
                    write_migration=False,
                    backup_retention=OWNER_PROFILE_BACKUP_RETENTION,
                )
                profile = dict((loaded.envelope.data if loaded.envelope else {}) or {})
                facts_by_key = dict(profile.get("facts") or {})
                facts_output = [
                    {
                        "normalized_key": key,
                        "display_key": str(facts_by_key[key].get("display_key") or owner_fact_display_name(key)),
                        **({"value": facts_by_key[key].get("value")} if include_values else {}),
                        "created_at": str(facts_by_key[key].get("created_at") or ""),
                        "updated_at": str(facts_by_key[key].get("updated_at") or ""),
                        "source": str(facts_by_key[key].get("source") or ""),
                    }
                    for key in sorted(facts_by_key)
                ]
                memories_output = [dict(memory) for memory in list(profile.get("memories") or [])]
                memories_output.sort(key=lambda memory: (str(memory.get("created_at") or ""), str(memory.get("memory_id") or "")))
                pending_delete_all = bool(profile.get("pending_delete_all"))
            except Exception as error:
                storage_error = error.status if isinstance(error, MigrationError) else "inspection_load_failed"
        report.update(
            {
                "profile_path": str(self.path),
                "owner_id": OWNER_PROFILE_ID,
                "fact_count": len(facts_output),
                "facts": facts_output,
                "memory_count": sum(1 for memory in memories_output if memory.get("status") == "active"),
                "stored_memory_count": len(memories_output),
                "memories": memories_output,
                "pending_delete_all": pending_delete_all,
                "storage_error": storage_error or str(report.get("error_message") or "") if report.get("validation_state") == "invalid" else storage_error,
                "last_backup": self._latest_backup_path(),
            }
        )
        return report

    def _load(self) -> Dict[str, Any]:
        try:
            profile = self._loader(
                self.path,
                SCHEMA_OWNER_PROFILE,
                {
                    "owner_id": OWNER_PROFILE_ID,
                    "facts": {},
                    "memories": [],
                    "pending_delete_all": None,
                },
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
        return {
            "owner_id": OWNER_PROFILE_ID,
            "facts": facts,
            "memories": [dict(memory) for memory in list(profile.get("memories") or [])],
            "pending_delete_all": profile.get("pending_delete_all"),
        }

    def _save(self, profile: Dict[str, Any]) -> None:
        self._saver(
            self.path,
            SCHEMA_OWNER_PROFILE,
            profile,
            metadata={"owner_id": OWNER_PROFILE_ID, "purpose": "explicit_owner_memory"},
            backup_retention=OWNER_PROFILE_BACKUP_RETENTION,
        )

    def _profile(
        self,
        facts: Dict[str, Any],
        *,
        memories: Optional[list[Dict[str, Any]]] = None,
        pending_delete_all: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "owner_id": OWNER_PROFILE_ID,
            "facts": {key: facts[key] for key in sorted(facts)},
            "memories": [dict(memory) for memory in list(memories or [])],
            "pending_delete_all": dict(pending_delete_all) if pending_delete_all else None,
        }

    def _validate_profile_size(self, profile: Dict[str, Any]) -> None:
        size = len(json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        if size > MAX_OWNER_PROFILE_BYTES:
            raise OwnerMemoryValidationError("profile_size_limit_reached", "Owner profile size limit reached.")

    def _matching_memories(
        self,
        memories: list[Dict[str, Any]],
        query: Mapping[str, Any],
        *,
        limit: int,
    ) -> list[tuple[int, Dict[str, Any]]]:
        clean_query = normalize_memory_query(query)
        candidates = []
        for index, memory in enumerate(memories):
            score = score_general_memory(memory, clean_query)
            if score >= 0:
                candidates.append((score, index, dict(memory)))
        candidates.sort(
            key=lambda item: (
                item[0],
                str(item[2].get("updated_at") or ""),
                str(item[2].get("memory_id") or ""),
            ),
            reverse=True,
        )
        return [(index, memory) for _, index, memory in candidates[: max(1, int(limit))]]

    @staticmethod
    def _active_memory_count(memories: list[Dict[str, Any]]) -> int:
        return sum(1 for memory in memories if memory.get("status") == "active")

    @staticmethod
    def _trim_memory_history(memories: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        active = [dict(memory) for memory in memories if memory.get("status") == "active"]
        inactive = [dict(memory) for memory in memories if memory.get("status") != "active"]
        inactive.sort(
            key=lambda memory: (
                str(memory.get("updated_at") or ""),
                str(memory.get("memory_id") or ""),
            ),
            reverse=True,
        )
        combined = active + inactive[:MAX_GENERAL_MEMORY_HISTORY]
        combined.sort(key=lambda memory: (str(memory.get("created_at") or ""), str(memory.get("memory_id") or "")))
        return combined

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

    def _general_operation_metadata(self, file_existed_before: bool, **metadata: Any) -> Dict[str, Any]:
        return {
            **self._operation_metadata(file_existed_before),
            "memory_content_redacted": True,
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

    def _general_validation_failure(
        self,
        operation: str,
        error: GeneralMemoryValidationError | OwnerMemoryValidationError,
    ) -> OwnerProfileResultV1:
        result = OwnerProfileResultV1(
            False,
            "rejected",
            operation,
            error_code=error.code,
            error_message=error.message,
            metadata={
                **self._general_operation_metadata(self.path.exists()),
                "protected": error.protected,
                "value_redacted": True,
            },
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
                    "memory_id": str(result.memory.get("memory_id") or ""),
                    "memory_type": str(result.memory.get("memory_type") or ""),
                    "memory_count": len(result.memories),
                    "changed": result.changed,
                    "error_code": result.error_code,
                    "value_redacted": True,
                    "memory_content_redacted": True,
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


def _parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _add_seconds(value: str, seconds: int) -> str:
    return (_parse_utc_timestamp(value) + timedelta(seconds=max(1, int(seconds)))).isoformat().replace("+00:00", "Z")


def _timestamp_expired(value: str) -> bool:
    try:
        return datetime.now(timezone.utc) >= _parse_utc_timestamp(value)
    except (TypeError, ValueError):
        return True
