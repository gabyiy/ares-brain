from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4


SCHEMA_VERSION_1 = 1
SCHEMA_VERSION_2 = 2
SCHEMA_VERSION_3 = 3

SCHEMA_USER_PROFILE = "ares.user_profile"
SCHEMA_OWNER_PROFILE = "ares.owner_profile"
SCHEMA_GOALS = "ares.goals"
SCHEMA_NOTES = "ares.notes"
SCHEMA_TASKS = "ares.tasks"
SCHEMA_MEMORY_SHORT = "ares.memory.short"
SCHEMA_MEMORY_LONG = "ares.memory.long"
SCHEMA_EVENT_HISTORY = "ares.event_history"
SCHEMA_TEST_FIXTURE = "ares.test_fixture"

_ENVELOPE_FIELDS = {
    "schema_name",
    "schema_version",
    "created_at",
    "updated_at",
    "data",
}
_OPTIONAL_ENVELOPE_FIELDS = {"metadata"}
LOCK_METADATA_SCHEMA = "ares.file_lock"
LOCK_METADATA_VERSION = 1
DEFAULT_STALE_LOCK_SECONDS = 30.0
_WRITE_LOCKS: Dict[str, str] = {}
_WRITE_LOCKS_GUARD = RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MigrationError(Exception):
    """Structured persistence migration failure."""

    def __init__(
        self,
        message: str,
        *,
        schema_name: str = "",
        path: Optional[Path] = None,
        status: str = "migration_failed",
        source_version: Optional[int] = None,
        target_version: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.schema_name = schema_name
        self.path = Path(path) if path is not None else None
        self.status = status
        self.source_version = source_version
        self.target_version = target_version
        self.details = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": False,
            "status": self.status,
            "schema_name": self.schema_name,
            "path": str(self.path) if self.path else "",
            "source_version": self.source_version,
            "target_version": self.target_version,
            "error_message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class SchemaEnvelope:
    schema_name: str
    schema_version: int
    created_at: str
    updated_at: str
    data: Any
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clean_schema = str(self.schema_name or "").strip()
        if not clean_schema:
            raise ValueError("schema_name is required")
        object.__setattr__(self, "schema_name", clean_schema)
        object.__setattr__(self, "schema_version", _normalize_version(self.schema_version))
        object.__setattr__(self, "created_at", str(self.created_at or utc_now()))
        object.__setattr__(self, "updated_at", str(self.updated_at or utc_now()))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @classmethod
    def create(
        cls,
        schema_name: str,
        schema_version: int,
        data: Any,
        *,
        created_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "SchemaEnvelope":
        timestamp = utc_now()
        return cls(
            schema_name=schema_name,
            schema_version=schema_version,
            created_at=created_at or timestamp,
            updated_at=timestamp,
            data=data,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SchemaEnvelope":
        if not isinstance(payload, dict):
            raise ValueError("Schema envelope must be a JSON object")
        missing = sorted(_ENVELOPE_FIELDS - set(payload))
        if missing:
            raise ValueError(f"Missing envelope fields: {', '.join(missing)}")
        unknown = sorted(set(payload) - (_ENVELOPE_FIELDS | _OPTIONAL_ENVELOPE_FIELDS))
        if unknown:
            raise ValueError(f"Unknown envelope fields: {', '.join(unknown)}")
        return cls(
            schema_name=payload["schema_name"],
            schema_version=payload["schema_version"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            data=payload["data"],
            metadata=dict(payload.get("metadata") or {}),
        )

    def with_data(self, data: Any, version: int, metadata: Optional[Dict[str, Any]] = None) -> "SchemaEnvelope":
        return SchemaEnvelope(
            schema_name=self.schema_name,
            schema_version=version,
            created_at=self.created_at,
            updated_at=utc_now(),
            data=data,
            metadata=dict(metadata if metadata is not None else self.metadata),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "data": self.data,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MigrationResult:
    success: bool
    status: str
    schema_name: str
    path: str = ""
    source_version: Optional[int] = None
    target_version: Optional[int] = None
    migration_path: List[Tuple[int, int]] = field(default_factory=list)
    dry_run: bool = False
    migration_needed: bool = False
    backup_path: str = ""
    changed: bool = False
    error_message: str = ""
    report: Dict[str, Any] = field(default_factory=dict)
    envelope: Optional[SchemaEnvelope] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "schema_name": self.schema_name,
            "path": self.path,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "migration_path": [list(edge) for edge in self.migration_path],
            "dry_run": self.dry_run,
            "migration_needed": self.migration_needed,
            "backup_path": self.backup_path,
            "changed": self.changed,
            "error_message": self.error_message,
            "report": dict(self.report),
        }


@dataclass(frozen=True)
class StoreInspectionReport:
    store_path: str
    schema_name: str
    detected_version: Optional[int]
    current_target_version: Optional[int]
    migration_needed: bool
    migration_path: List[Tuple[int, int]]
    last_backup: str
    validation_state: str
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "store_path": self.store_path,
            "schema_name": self.schema_name,
            "detected_version": self.detected_version,
            "current_target_version": self.current_target_version,
            "migration_needed": self.migration_needed,
            "migration_path": [list(edge) for edge in self.migration_path],
            "last_backup": self.last_backup,
            "validation_state": self.validation_state,
            "error_message": self.error_message,
        }


MigrationFunction = Callable[[SchemaEnvelope], SchemaEnvelope]
LegacyImporter = Callable[[Any, str], SchemaEnvelope]
DataValidator = Callable[[Any], None]


@dataclass(frozen=True)
class SchemaDefinition:
    schema_name: str
    current_version: int
    validate_data: DataValidator
    legacy_importer: Optional[LegacyImporter] = None
    validators: Dict[int, DataValidator] = field(default_factory=dict)


class MigrationRegistry:
    def __init__(self):
        self._schemas: Dict[str, SchemaDefinition] = {}
        self._migrations: Dict[str, Dict[Tuple[int, int], MigrationFunction]] = {}

    def register_schema(
        self,
        schema_name: str,
        current_version: int,
        validate_data: DataValidator,
        legacy_importer: Optional[LegacyImporter] = None,
        validators: Optional[Dict[int, DataValidator]] = None,
    ) -> SchemaDefinition:
        clean_name = _clean_schema_name(schema_name)
        clean_version = _normalize_version(current_version)
        if clean_name in self._schemas:
            raise ValueError(f"Schema already registered: {clean_name}")
        definition = SchemaDefinition(
            schema_name=clean_name,
            current_version=clean_version,
            validate_data=validate_data,
            legacy_importer=legacy_importer,
            validators={_normalize_version(version): validator for version, validator in dict(validators or {}).items()},
        )
        self._schemas[clean_name] = definition
        self._migrations.setdefault(clean_name, {})
        return definition

    def register_migration(
        self,
        schema_name: str,
        from_version: int,
        to_version: int,
        migrate: MigrationFunction,
    ) -> None:
        clean_name = _clean_schema_name(schema_name)
        if clean_name not in self._schemas:
            raise ValueError(f"Cannot register migration for unknown schema: {clean_name}")
        source = _normalize_version(from_version)
        target = _normalize_version(to_version)
        if source == target:
            raise ValueError("Migration source and target versions must differ")
        edge = (source, target)
        migrations = self._migrations.setdefault(clean_name, {})
        if edge in migrations:
            raise ValueError(f"Duplicate migration edge for {clean_name}: {source}->{target}")
        migrations[edge] = migrate
        if self._has_path(clean_name, target, source):
            migrations.pop(edge, None)
            raise ValueError(f"Migration edge creates a cycle for {clean_name}: {source}->{target}")

    def known_schema_names(self) -> List[str]:
        return sorted(self._schemas)

    def supported_versions(self, schema_name: str) -> List[int]:
        clean_name = _clean_schema_name(schema_name)
        self._require_schema(clean_name)
        versions = {self._schemas[clean_name].current_version}
        for source, target in self._migrations.get(clean_name, {}):
            versions.add(source)
            versions.add(target)
        return sorted(versions)

    def current_version(self, schema_name: str) -> int:
        return self._require_schema(schema_name).current_version

    def migration_path(self, schema_name: str, from_version: int, to_version: Optional[int] = None) -> List[Tuple[int, int]]:
        clean_name = _clean_schema_name(schema_name)
        target = self.current_version(clean_name) if to_version is None else _normalize_version(to_version)
        source = _normalize_version(from_version)
        if source == target:
            return []
        graph: Dict[int, List[int]] = {}
        for edge_source, edge_target in self._migrations.get(clean_name, {}):
            graph.setdefault(edge_source, []).append(edge_target)
        queue: List[Tuple[int, List[Tuple[int, int]]]] = [(source, [])]
        visited = {source}
        while queue:
            version, path = queue.pop(0)
            for next_version in sorted(graph.get(version, [])):
                if next_version in visited:
                    continue
                next_path = [*path, (version, next_version)]
                if next_version == target:
                    return next_path
                visited.add(next_version)
                queue.append((next_version, next_path))
        raise MigrationError(
            f"Missing migration path for {clean_name}: {source}->{target}",
            schema_name=clean_name,
            status="missing_migration_path",
            source_version=source,
            target_version=target,
        )

    def can_migrate_to_current(self, schema_name: str, from_version: int) -> bool:
        try:
            self.migration_path(schema_name, from_version)
            return True
        except MigrationError:
            return False

    def registered_migrations(self, schema_name: str) -> List[Tuple[int, int]]:
        clean_name = _clean_schema_name(schema_name)
        self._require_schema(clean_name)
        return sorted(self._migrations.get(clean_name, {}))

    def validate_envelope(self, envelope: SchemaEnvelope) -> None:
        definition = self._require_schema(envelope.schema_name)
        if envelope.schema_version > definition.current_version:
            raise MigrationError(
                f"Unsupported future schema version: {envelope.schema_version}",
                schema_name=envelope.schema_name,
                status="future_schema_version",
                source_version=envelope.schema_version,
                target_version=definition.current_version,
            )
        validator = definition.validators.get(envelope.schema_version, definition.validate_data)
        validator(envelope.data)

    def import_legacy(self, schema_name: str, payload: Any, path: str = "") -> SchemaEnvelope:
        definition = self._require_schema(schema_name)
        if definition.legacy_importer is None:
            raise MigrationError(
                f"No legacy importer registered for {schema_name}",
                schema_name=schema_name,
                path=Path(path) if path else None,
                status="legacy_import_unsupported",
            )
        envelope = definition.legacy_importer(payload, path)
        if envelope.schema_name != definition.schema_name:
            raise MigrationError(
                "Legacy importer returned wrong schema",
                schema_name=definition.schema_name,
                status="wrong_schema_name",
            )
        if envelope.schema_version != SCHEMA_VERSION_1:
            raise MigrationError(
                "Legacy importer must return schema version 1",
                schema_name=definition.schema_name,
                source_version=envelope.schema_version,
                target_version=SCHEMA_VERSION_1,
                status="invalid_legacy_import_version",
            )
        definition.validate_data(envelope.data)
        return envelope

    def migrate(
        self,
        envelope: SchemaEnvelope,
        *,
        dry_run: bool = False,
        target_version: Optional[int] = None,
    ) -> MigrationResult:
        definition = self._require_schema(envelope.schema_name)
        target = definition.current_version if target_version is None else _normalize_version(target_version)
        if envelope.schema_version > definition.current_version:
            self.validate_envelope(envelope)
        if envelope.schema_version > target:
            raise MigrationError(
                "Downgrades are not allowed",
                schema_name=envelope.schema_name,
                status="downgrade_rejected",
                source_version=envelope.schema_version,
                target_version=target,
            )
        self.validate_envelope(envelope)
        path = self.migration_path(envelope.schema_name, envelope.schema_version, target)
        current = envelope
        for source, destination in path:
            migrate = self._migrations[envelope.schema_name][(source, destination)]
            if current.schema_version != source:
                raise MigrationError(
                    "Migration path version mismatch",
                    schema_name=envelope.schema_name,
                    status="migration_path_mismatch",
                    source_version=current.schema_version,
                    target_version=destination,
                )
            candidate = migrate(current)
            if not isinstance(candidate, SchemaEnvelope):
                raise MigrationError(
                    "Migration function returned invalid envelope",
                    schema_name=envelope.schema_name,
                    status="invalid_migration_result",
                    source_version=source,
                    target_version=destination,
                )
            if candidate.schema_name != envelope.schema_name or candidate.schema_version != destination:
                raise MigrationError(
                    "Migration function returned wrong schema or version",
                    schema_name=envelope.schema_name,
                    status="invalid_migration_result",
                    source_version=source,
                    target_version=destination,
                )
            self.validate_envelope(candidate)
            current = candidate
        return MigrationResult(
            success=True,
            status="dry_run" if dry_run else "migrated" if path else "current",
            schema_name=envelope.schema_name,
            source_version=envelope.schema_version,
            target_version=target,
            migration_path=path,
            dry_run=dry_run,
            migration_needed=bool(path),
            changed=bool(path),
            envelope=current,
        )

    def _require_schema(self, schema_name: str) -> SchemaDefinition:
        clean_name = _clean_schema_name(schema_name)
        if clean_name not in self._schemas:
            raise ValueError(f"Unknown schema: {clean_name}")
        return self._schemas[clean_name]

    def _has_path(self, schema_name: str, source: int, target: int) -> bool:
        try:
            self.migration_path(schema_name, source, target)
            return True
        except MigrationError:
            return False


@dataclass(frozen=True)
class StoreLockInspection:
    store_path: str
    lock_path: str
    lock_exists: bool
    metadata_format: str = ""
    metadata_valid: bool = False
    owner_pid: int = 0
    owner_hostname: str = ""
    owner_token: str = ""
    owner_kind: str = ""
    created_at: str = ""
    lock_age_seconds: float = 0.0
    owner_process_state: str = "unknown"
    expired: bool = False
    safe_recovery_possible: bool = False
    metadata_fingerprint: str = ""
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "store_path": self.store_path,
            "lock_path": self.lock_path,
            "lock_exists": self.lock_exists,
            "metadata_format": self.metadata_format,
            "metadata_valid": self.metadata_valid,
            "owner_pid": self.owner_pid,
            "owner_hostname": self.owner_hostname,
            "owner_token": self.owner_token,
            "owner_kind": self.owner_kind,
            "created_at": self.created_at,
            "lock_age_seconds": self.lock_age_seconds,
            "owner_process_state": self.owner_process_state,
            "expired": self.expired,
            "safe_recovery_possible": self.safe_recovery_possible,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class StoreLockRecoveryResult:
    success: bool
    status: str
    recovered: bool
    inspection: StoreLockInspection
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "recovered": self.recovered,
            "inspection": self.inspection.to_dict(),
            "error_message": self.error_message,
        }


def store_lock_path(path: Path) -> Path:
    target = Path(path)
    return target.with_suffix(target.suffix + ".lock")


def inspect_store_lock(
    path: Path,
    *,
    stale_after_seconds: float = DEFAULT_STALE_LOCK_SECONDS,
    now: Optional[Callable[[], datetime]] = None,
    process_alive: Optional[Callable[[int], Optional[bool]]] = None,
    hostname: Optional[str] = None,
) -> StoreLockInspection:
    target = Path(path)
    lock_path = store_lock_path(target)
    stale_seconds = _validated_stale_lock_seconds(stale_after_seconds)
    if not lock_path.exists():
        return StoreLockInspection(
            store_path=str(target),
            lock_path=str(lock_path),
            lock_exists=False,
            owner_process_state="not_present",
        )

    try:
        raw = lock_path.read_bytes()
    except FileNotFoundError:
        return StoreLockInspection(
            store_path=str(target),
            lock_path=str(lock_path),
            lock_exists=False,
            owner_process_state="not_present",
        )
    except OSError as error:
        return StoreLockInspection(
            store_path=str(target),
            lock_path=str(lock_path),
            lock_exists=True,
            error_message=f"lock_read_failed:{type(error).__name__}:{str(error)[:120]}",
        )

    fingerprint = hashlib.sha256(raw).hexdigest()
    current_time = _validated_utc_now((now or (lambda: datetime.now(timezone.utc)))())
    current_hostname = str(hostname or socket.gethostname()).strip()
    text = raw.decode("utf-8", errors="replace").strip()
    metadata_format = "json_v1"
    metadata_valid = False
    owner_pid = 0
    owner_hostname = ""
    owner_token = ""
    owner_kind = ""
    created_at = ""
    created_datetime: Optional[datetime] = None
    error_message = ""

    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("lock metadata must be a JSON object")
        if payload.get("schema_name") != LOCK_METADATA_SCHEMA:
            raise ValueError("unsupported lock metadata schema")
        if payload.get("schema_version") != LOCK_METADATA_VERSION:
            raise ValueError("unsupported lock metadata version")
        pid = payload.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("lock owner pid is invalid")
        owner_pid = pid
        owner_hostname = _validated_lock_text(payload.get("hostname"), "hostname", 255)
        owner_token = _validated_lock_text(payload.get("owner_token"), "owner_token", 128)
        owner_kind = _validated_lock_text(payload.get("owner_kind"), "owner_kind", 64)
        created_at = _validated_lock_text(payload.get("created_at"), "created_at", 64)
        created_datetime = _parse_utc_timestamp(created_at)
        metadata_valid = True
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        try:
            created_datetime = _parse_utc_timestamp(text)
            created_at = text
            metadata_format = "legacy_timestamp_v0"
            metadata_valid = True
            owner_kind = "legacy_store_write"
        except ValueError:
            metadata_format = "malformed"
            error_message = f"lock_metadata_malformed:{str(error)[:120]}"

    age_seconds = (
        max(0.0, (current_time - created_datetime).total_seconds())
        if created_datetime is not None
        else 0.0
    )
    expired = bool(created_datetime is not None and age_seconds >= stale_seconds)
    owner_state = "unknown"
    safe_recovery = False
    if metadata_format == "legacy_timestamp_v0" and created_datetime is not None:
        boot_time = _local_boot_time()
        if boot_time is not None and created_datetime < boot_time:
            owner_state = "dead_prior_boot"
            safe_recovery = expired
        else:
            owner_state = "legacy_owner_unknown"
            error_message = "legacy_lock_owner_cannot_be_proven_dead_on_this_boot"
    elif metadata_valid:
        if owner_hostname.casefold() != current_hostname.casefold():
            owner_state = "remote_or_different_host"
        else:
            checker = process_alive or _process_alive
            alive = checker(owner_pid)
            owner_state = "alive" if alive is True else "dead" if alive is False else "unknown"
            safe_recovery = alive is False and expired

    return StoreLockInspection(
        store_path=str(target),
        lock_path=str(lock_path),
        lock_exists=True,
        metadata_format=metadata_format,
        metadata_valid=metadata_valid,
        owner_pid=owner_pid,
        owner_hostname=owner_hostname,
        owner_token=owner_token,
        owner_kind=owner_kind,
        created_at=created_at,
        lock_age_seconds=round(age_seconds, 6),
        owner_process_state=owner_state,
        expired=expired,
        safe_recovery_possible=safe_recovery,
        metadata_fingerprint=fingerprint,
        error_message=error_message,
    )


def recover_store_lock(
    path: Path,
    *,
    stale_after_seconds: float = DEFAULT_STALE_LOCK_SECONDS,
    now: Optional[Callable[[], datetime]] = None,
    process_alive: Optional[Callable[[int], Optional[bool]]] = None,
    hostname: Optional[str] = None,
) -> StoreLockRecoveryResult:
    inspection = inspect_store_lock(
        path,
        stale_after_seconds=stale_after_seconds,
        now=now,
        process_alive=process_alive,
        hostname=hostname,
    )
    if not inspection.lock_exists:
        return StoreLockRecoveryResult(True, "lock_not_present", False, inspection)
    if not inspection.safe_recovery_possible:
        return StoreLockRecoveryResult(
            False,
            "lock_not_safely_recoverable",
            False,
            inspection,
            inspection.error_message or f"lock owner is {inspection.owner_process_state}",
        )

    lock_path = Path(inspection.lock_path)
    recovery_path = lock_path.with_name(
        f".{lock_path.name}.recover-{uuid4().hex}"
    )
    try:
        os.replace(str(lock_path), str(recovery_path))
    except FileNotFoundError:
        refreshed = inspect_store_lock(path, stale_after_seconds=stale_after_seconds)
        return StoreLockRecoveryResult(True, "lock_disappeared", False, refreshed)
    except OSError as error:
        return StoreLockRecoveryResult(
            False,
            "lock_recovery_failed",
            False,
            inspection,
            f"{type(error).__name__}:{str(error)[:120]}",
        )

    try:
        moved_fingerprint = hashlib.sha256(recovery_path.read_bytes()).hexdigest()
        if moved_fingerprint != inspection.metadata_fingerprint:
            if not lock_path.exists():
                os.replace(str(recovery_path), str(lock_path))
            return StoreLockRecoveryResult(
                False,
                "lock_changed_during_recovery",
                False,
                inspection,
                "lock metadata changed before atomic recovery",
            )
        recovery_path.unlink()
    except OSError as error:
        if recovery_path.exists() and not lock_path.exists():
            try:
                os.replace(str(recovery_path), str(lock_path))
            except OSError:
                pass
        return StoreLockRecoveryResult(
            False,
            "lock_recovery_cleanup_failed",
            False,
            inspection,
            f"{type(error).__name__}:{str(error)[:120]}",
        )
    refreshed = inspect_store_lock(path, stale_after_seconds=stale_after_seconds)
    return StoreLockRecoveryResult(True, "recovered_dead_owner_lock", True, refreshed)


class StoreWriteLock:
    def __init__(
        self,
        path: Path,
        *,
        recover_if_owner_dead: bool = False,
        stale_after_seconds: float = DEFAULT_STALE_LOCK_SECONDS,
        owner_kind: str = "store_write",
        process_alive: Optional[Callable[[int], Optional[bool]]] = None,
        hostname: Optional[str] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.path = Path(path)
        self.lock_path = store_lock_path(self.path)
        if not isinstance(recover_if_owner_dead, bool):
            raise ValueError("recover_if_owner_dead must be a boolean")
        self.recover_if_owner_dead = recover_if_owner_dead
        self.stale_after_seconds = _validated_stale_lock_seconds(stale_after_seconds)
        self.owner_kind = _validated_lock_text(owner_kind, "owner_kind", 64)
        self._process_alive = process_alive
        self._hostname = str(hostname or socket.gethostname()).strip()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._owner_token = uuid4().hex
        self._acquired = False
        self._key = ""

    @property
    def owner_token(self) -> str:
        return self._owner_token

    def __enter__(self):
        key = str(self.lock_path.resolve())
        with _WRITE_LOCKS_GUARD:
            if key in _WRITE_LOCKS:
                raise self._locked_error("lock_owned_in_current_process")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._create_lock_file()
        except FileExistsError as error:
            if self.recover_if_owner_dead:
                recovered = recover_store_lock(
                    self.path,
                    stale_after_seconds=self.stale_after_seconds,
                    now=self._now,
                    process_alive=self._process_alive,
                    hostname=self._hostname,
                )
                if recovered.recovered:
                    try:
                        self._create_lock_file()
                    except FileExistsError as retry_error:
                        raise self._locked_error("lock_reacquired_during_recovery") from retry_error
                else:
                    raise self._locked_error(recovered.status) from error
            else:
                raise self._locked_error("lock_file_exists") from error
        with _WRITE_LOCKS_GUARD:
            _WRITE_LOCKS[key] = self._owner_token
        self._key = key
        self._acquired = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._acquired:
            with _WRITE_LOCKS_GUARD:
                if _WRITE_LOCKS.get(self._key) == self._owner_token:
                    _WRITE_LOCKS.pop(self._key, None)
            self._unlink_if_owned()
        self._acquired = False
        return False

    def _create_lock_file(self) -> None:
        descriptor = os.open(
            str(self.lock_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        payload = {
            "schema_name": LOCK_METADATA_SCHEMA,
            "schema_version": LOCK_METADATA_VERSION,
            "pid": os.getpid(),
            "hostname": self._hostname,
            "owner_token": self._owner_token,
            "owner_kind": self.owner_kind,
            "created_at": _format_utc_timestamp(self._now()),
        }
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
            raise

    def _unlink_if_owned(self) -> None:
        try:
            payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
            return
        if payload.get("owner_token") != self._owner_token:
            return
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def _locked_error(self, reason: str) -> MigrationError:
        inspection = inspect_store_lock(
            self.path,
            stale_after_seconds=self.stale_after_seconds,
            now=self._now,
            process_alive=self._process_alive,
            hostname=self._hostname,
        )
        return MigrationError(
            f"Store is locked: {self.path}",
            path=self.path,
            status="store_locked",
            details={"reason": reason, "lock": inspection.to_dict()},
        )


def _validated_stale_lock_seconds(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("stale_after_seconds must be numeric")
    seconds = float(value)
    if not 1.0 <= seconds <= 86400.0:
        raise ValueError("stale_after_seconds must be between 1 and 86400")
    return seconds


def _validated_lock_text(value: Any, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(character) < 32 for character in text):
        raise ValueError(f"lock {label} is invalid")
    return text


def _parse_utc_timestamp(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("lock timestamp is missing")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("lock timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("lock timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validated_utc_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("lock clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    return _validated_utc_now(value).isoformat().replace("+00:00", "Z")


def _process_alive(pid: int) -> Optional[bool]:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _local_boot_time() -> Optional[datetime]:
    path = Path("/proc/stat")
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return datetime.fromtimestamp(int(line.split()[1]), timezone.utc)
    except (OSError, ValueError, IndexError):
        return None
    return None


def load_store_envelope(
    path: Path,
    schema_name: str,
    default_data: Any,
    *,
    registry: Optional[MigrationRegistry] = None,
    write_migration: bool = True,
    dry_run: bool = False,
    backup_retention: int = 5,
) -> MigrationResult:
    registry = registry or DEFAULT_MIGRATION_REGISTRY
    store_path = Path(path)
    target_version = registry.current_version(schema_name)
    if not store_path.exists():
        envelope = SchemaEnvelope.create(schema_name, target_version, _copy_json_data(default_data))
        registry.validate_envelope(envelope)
        return MigrationResult(
            success=True,
            status="default",
            schema_name=schema_name,
            path=str(store_path),
            source_version=None,
            target_version=target_version,
            envelope=envelope,
        )

    payload = _read_json_file(store_path, schema_name)
    is_enveloped = _looks_like_envelope(payload)
    if is_enveloped:
        envelope = SchemaEnvelope.from_dict(payload)
        if envelope.schema_name != schema_name:
            raise MigrationError(
                f"Wrong schema name: {envelope.schema_name}",
                schema_name=schema_name,
                path=store_path,
                status="wrong_schema_name",
                source_version=envelope.schema_version,
                target_version=target_version,
            )
    elif _looks_partially_enveloped(payload):
        raise MigrationError(
            "Malformed schema envelope",
            schema_name=schema_name,
            path=store_path,
            status="malformed_envelope",
            target_version=target_version,
        )
    else:
        envelope = registry.import_legacy(schema_name, payload, str(store_path))

    result = registry.migrate(envelope, dry_run=dry_run)
    migrated_envelope = result.envelope or envelope
    needs_write = (not is_enveloped) or envelope.schema_version != target_version
    if needs_write and not dry_run and write_migration:
        reason = "legacy_import" if not is_enveloped else "migration"
        backup_path = _backup_file(store_path, schema_name, envelope.schema_version, target_version, reason, backup_retention)
        try:
            with StoreWriteLock(store_path):
                _atomic_write_envelope(store_path, migrated_envelope, registry)
        except Exception:
            _cleanup_temp(store_path)
            raise
        result = MigrationResult(
            success=True,
            status="imported" if not is_enveloped and not result.migration_path else "migrated",
            schema_name=schema_name,
            path=str(store_path),
            source_version=envelope.schema_version,
            target_version=target_version,
            migration_path=result.migration_path,
            dry_run=False,
            migration_needed=True,
            backup_path=str(backup_path),
            changed=True,
            report={"legacy_import": not is_enveloped},
            envelope=migrated_envelope,
        )
    else:
        result = MigrationResult(
            success=True,
            status=result.status,
            schema_name=schema_name,
            path=str(store_path),
            source_version=envelope.schema_version,
            target_version=target_version,
            migration_path=result.migration_path,
            dry_run=dry_run,
            migration_needed=bool(result.migration_path or envelope.schema_version != target_version or not is_enveloped),
            changed=False,
            report={"legacy_import": not is_enveloped},
            envelope=migrated_envelope,
        )
    return result


def load_store_data(path: Path, schema_name: str, default_data: Any, **kwargs) -> Any:
    result = load_store_envelope(path, schema_name, default_data, **kwargs)
    if result.envelope is None:
        raise MigrationError("Store load produced no envelope", schema_name=schema_name, path=Path(path))
    return result.envelope.data


def save_store_data(
    path: Path,
    schema_name: str,
    data: Any,
    *,
    registry: Optional[MigrationRegistry] = None,
    metadata: Optional[Dict[str, Any]] = None,
    backup_retention: int = 5,
) -> MigrationResult:
    registry = registry or DEFAULT_MIGRATION_REGISTRY
    store_path = Path(path)
    target_version = registry.current_version(schema_name)
    created_at = None
    existing_metadata: Dict[str, Any] = {}
    source_version = None
    backup_path = ""
    if store_path.exists():
        existing_payload = _read_json_file(store_path, schema_name)
        if _looks_like_envelope(existing_payload):
            existing = SchemaEnvelope.from_dict(existing_payload)
            if existing.schema_name == schema_name:
                created_at = existing.created_at
                existing_metadata = dict(existing.metadata)
                source_version = existing.schema_version
        backup_path = str(_backup_file(store_path, schema_name, source_version, target_version, "write", backup_retention))

    merged_metadata = {**existing_metadata, **dict(metadata or {})}
    envelope = SchemaEnvelope.create(schema_name, target_version, _copy_json_data(data), created_at=created_at, metadata=merged_metadata)
    registry.validate_envelope(envelope)
    try:
        with StoreWriteLock(store_path):
            _atomic_write_envelope(store_path, envelope, registry)
    except Exception:
        _cleanup_temp(store_path)
        raise
    return MigrationResult(
        success=True,
        status="written",
        schema_name=schema_name,
        path=str(store_path),
        source_version=source_version,
        target_version=target_version,
        backup_path=backup_path,
        changed=True,
        envelope=envelope,
    )


def inspect_store(
    path: Path,
    schema_name: str,
    *,
    registry: Optional[MigrationRegistry] = None,
) -> StoreInspectionReport:
    registry = registry or DEFAULT_MIGRATION_REGISTRY
    store_path = Path(path)
    target_version = registry.current_version(schema_name)
    last_backup = _latest_backup(store_path)
    if not store_path.exists():
        return StoreInspectionReport(
            store_path=str(store_path),
            schema_name=schema_name,
            detected_version=None,
            current_target_version=target_version,
            migration_needed=False,
            migration_path=[],
            last_backup=str(last_backup) if last_backup else "",
            validation_state="missing_default",
        )
    try:
        payload = _read_json_file(store_path, schema_name)
        if _looks_like_envelope(payload):
            envelope = SchemaEnvelope.from_dict(payload)
        elif _looks_partially_enveloped(payload):
            raise MigrationError("Malformed schema envelope", schema_name=schema_name, path=store_path, status="malformed_envelope")
        else:
            envelope = registry.import_legacy(schema_name, payload, str(store_path))
        if envelope.schema_name != schema_name:
            raise MigrationError("Wrong schema name", schema_name=schema_name, path=store_path, status="wrong_schema_name")
        registry.validate_envelope(envelope)
        path_edges = registry.migration_path(schema_name, envelope.schema_version, target_version)
        return StoreInspectionReport(
            store_path=str(store_path),
            schema_name=schema_name,
            detected_version=envelope.schema_version,
            current_target_version=target_version,
            migration_needed=bool(path_edges),
            migration_path=path_edges,
            last_backup=str(last_backup) if last_backup else "",
            validation_state="valid",
        )
    except Exception as error:
        return StoreInspectionReport(
            store_path=str(store_path),
            schema_name=schema_name,
            detected_version=None,
            current_target_version=target_version,
            migration_needed=False,
            migration_path=[],
            last_backup=str(last_backup) if last_backup else "",
            validation_state="invalid",
            error_message=str(error),
        )


def publish_migration_failure(event_bus: Any, schema_name: str, path: Path, error: Exception) -> None:
    if not event_bus:
        return
    publish = getattr(event_bus, "publish", None)
    if not callable(publish):
        return
    payload = {
        "schema_name": schema_name,
        "path": str(path),
        "error": str(error),
    }
    if isinstance(error, MigrationError):
        payload.update(error.to_dict())
    publish("storage.migration_failed", payload, source="memory.schema_migrations")


def record_migration_failure(event_history_store: Any, schema_name: str, path: Path, error: Exception) -> Any:
    add = getattr(event_history_store, "add", None)
    if not callable(add):
        return None
    event = {
        "source": "memory.schema_migrations",
        "type": "storage.migration_failed",
        "priority": "high",
        "payload": {
            "schema_name": schema_name,
            "path": str(path),
        },
        "timestamp": utc_now(),
    }
    result = {
        "success": False,
        "decision": "recorded",
        "text": "Migration failure recorded.",
        "data": error.to_dict() if isinstance(error, MigrationError) else {"error_message": str(error)},
        "error_message": str(error),
    }
    return add(event, result)


def _read_json_file(path: Path, schema_name: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise MigrationError(
            "Invalid JSON; preserving original store",
            schema_name=schema_name,
            path=Path(path),
            status="invalid_json",
            details={"line": error.lineno, "column": error.colno},
        ) from error
    except OSError as error:
        raise MigrationError(
            f"Unable to read store: {error}",
            schema_name=schema_name,
            path=Path(path),
            status="read_failed",
        ) from error


def _atomic_write_envelope(path: Path, envelope: SchemaEnvelope, registry: MigrationRegistry) -> None:
    registry.validate_envelope(envelope)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(envelope.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        temp_path.replace(path)
        final_payload = _read_json_file(path, envelope.schema_name)
        final_envelope = SchemaEnvelope.from_dict(final_payload)
        registry.validate_envelope(final_envelope)
    except Exception:
        _cleanup_temp(path)
        raise


def _backup_file(
    path: Path,
    schema_name: str,
    source_version: Optional[int],
    target_version: Optional[int],
    reason: str,
    retention: int,
) -> Path:
    path = Path(path)
    backup_dir = path.parent / ".migration_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = backup_dir / f"{path.name}.{stamp}.{reason}.bak"
    counter = 1
    while candidate.exists():
        candidate = backup_dir / f"{path.name}.{stamp}.{counter}.{reason}.bak"
        counter += 1
    shutil.copy2(path, candidate)
    metadata = {
        "source_path": str(path),
        "backup_path": str(candidate),
        "schema_name": schema_name,
        "source_schema_version": source_version,
        "target_schema_version": target_version,
        "migration_timestamp": utc_now(),
        "reason": reason,
    }
    candidate.with_suffix(candidate.suffix + ".meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _enforce_backup_retention(backup_dir, path.name, max(0, int(retention)))
    return candidate


def _enforce_backup_retention(backup_dir: Path, file_name: str, retention: int) -> None:
    if retention <= 0:
        return
    backups = sorted(
        backup_dir.glob(f"{file_name}.*.bak"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for backup in backups[retention:]:
        try:
            backup.unlink()
            meta = backup.with_suffix(backup.suffix + ".meta.json")
            if meta.exists():
                meta.unlink()
        except OSError:
            continue


def _latest_backup(path: Path) -> Optional[Path]:
    backup_dir = Path(path).parent / ".migration_backups"
    if not backup_dir.exists():
        return None
    backups = sorted(
        backup_dir.glob(f"{Path(path).name}.*.bak"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return backups[0] if backups else None


def _cleanup_temp(path: Path) -> None:
    temp_path = Path(path).with_suffix(Path(path).suffix + ".tmp")
    try:
        if temp_path.exists():
            temp_path.unlink()
    except OSError:
        pass


def _looks_like_envelope(payload: Any) -> bool:
    return isinstance(payload, dict) and _ENVELOPE_FIELDS.issubset(set(payload))


def _looks_partially_enveloped(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(set(payload) & _ENVELOPE_FIELDS) and not _looks_like_envelope(payload)


def _clean_schema_name(schema_name: str) -> str:
    clean = str(schema_name or "").strip()
    if not clean:
        raise ValueError("schema_name is required")
    return clean


def _normalize_version(version: Any) -> int:
    if isinstance(version, bool):
        raise ValueError("schema version must be an integer")
    try:
        normalized = int(version)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Malformed schema version: {version}") from error
    if normalized < 1:
        raise ValueError("schema version must be >= 1")
    return normalized


def _copy_json_data(data: Any) -> Any:
    return json.loads(json.dumps(data, ensure_ascii=False))


def _validate_list_data(data: Any, *, item_label: str) -> None:
    if not isinstance(data, list):
        raise MigrationError(f"{item_label} data must be a list", status="invalid_store_data")
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise MigrationError(f"{item_label} entry {index} must be an object", status="invalid_store_data")


def _validate_profile_data(data: Any) -> None:
    if not isinstance(data, dict):
        raise MigrationError("Profile data must be an object", schema_name=SCHEMA_USER_PROFILE, status="invalid_store_data")
    facts = data.get("facts")
    if facts is None:
        raise MigrationError("Profile data requires facts", schema_name=SCHEMA_USER_PROFILE, status="invalid_store_data")
    if not isinstance(facts, dict):
        raise MigrationError("Profile facts must be an object", schema_name=SCHEMA_USER_PROFILE, status="invalid_store_data")


def _validate_owner_profile_v1_data(data: Any) -> None:
    if not isinstance(data, dict):
        raise MigrationError(
            "Owner profile data must be an object",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )
    if set(data) != {"owner_id", "facts"}:
        raise MigrationError(
            "Owner profile contains unsupported durable fields",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )
    if data.get("owner_id") != "primary_owner":
        raise MigrationError(
            "Owner profile requires primary_owner",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )
    facts = data.get("facts")
    if not isinstance(facts, dict):
        raise MigrationError(
            "Owner profile facts must be an object",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )
    for key, entry in facts.items():
        if (
            not isinstance(key, str)
            or len(key) > 64
            or not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", key)
        ):
            raise MigrationError(
                "Owner profile contains an invalid fact key",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
        if not isinstance(entry, dict):
            raise MigrationError(
                "Owner profile fact must be an object",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
        if set(entry) != {"value", "updated_at"}:
            raise MigrationError(
                "Owner profile fact contains unsupported durable fields",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
        value = entry.get("value")
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise MigrationError(
                "Owner profile fact requires a value",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
        if not isinstance(entry.get("updated_at"), str) or not entry.get("updated_at"):
            raise MigrationError(
                "Owner profile fact requires updated_at",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )


def _validate_owner_profile_v2_data(data: Any) -> None:
    if not isinstance(data, dict):
        raise MigrationError(
            "Owner profile data must be an object",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )
    if set(data) != {"owner_id", "facts"} or data.get("owner_id") != "primary_owner":
        raise MigrationError(
            "Owner profile requires owner_id and facts",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )
    facts = data.get("facts")
    if not isinstance(facts, dict) or len(facts) > 100:
        raise MigrationError(
            "Owner profile facts must be a bounded object",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )
    for key, entry in facts.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", key) or len(key) > 64:
            raise MigrationError(
                "Owner profile contains an invalid fact key",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
        if not isinstance(entry, dict) or set(entry) != {
            "value",
            "display_key",
            "normalized_key",
            "created_at",
            "updated_at",
            "source",
        }:
            raise MigrationError(
                "Owner profile fact has an invalid v2 shape",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
        if entry.get("normalized_key") != key:
            raise MigrationError(
                "Owner profile fact key does not match its normalized key",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
        display_key = entry.get("display_key")
        if not isinstance(display_key, str) or not display_key or len(display_key) > 120:
            raise MigrationError(
                "Owner profile fact requires a bounded display key",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
        _validate_owner_profile_value(entry.get("value"))
        for timestamp_name in ("created_at", "updated_at"):
            if not isinstance(entry.get(timestamp_name), str) or not entry.get(timestamp_name):
                raise MigrationError(
                    f"Owner profile fact requires {timestamp_name}",
                    schema_name=SCHEMA_OWNER_PROFILE,
                    status="invalid_store_data",
                )
        if entry.get("source") != "explicit_owner_statement":
            raise MigrationError(
                "Owner profile fact has an unsupported source",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
    serialized_size = len(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if serialized_size > 65536:
        raise MigrationError(
            "Owner profile exceeds its bounded serialized size",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )


def _validate_owner_profile_value(value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) <= 10**15:
            return
    elif isinstance(value, float):
        if value == value and value not in (float("inf"), float("-inf")) and abs(value) <= 10**15:
            return
    elif isinstance(value, str):
        if value and len(value) <= 256 and not any(ord(character) < 32 or ord(character) == 127 for character in value):
            return
    elif isinstance(value, list) and 0 < len(value) <= 10:
        for item in value:
            if isinstance(item, (list, dict)) or item is None:
                break
            try:
                _validate_owner_profile_value(item)
            except MigrationError:
                break
        else:
            return
    raise MigrationError(
        "Owner profile fact has an unsupported or unbounded value",
        schema_name=SCHEMA_OWNER_PROFILE,
        status="invalid_store_data",
    )


def _owner_profile_migration_v1_to_v2(envelope: SchemaEnvelope) -> SchemaEnvelope:
    _validate_owner_profile_v1_data(envelope.data)
    migrated_facts: Dict[str, Any] = {}
    for key, raw_entry in sorted(dict(envelope.data.get("facts") or {}).items()):
        entry = dict(raw_entry)
        timestamp = str(entry.get("updated_at") or envelope.updated_at)
        migrated_facts[key] = {
            "value": entry.get("value"),
            "display_key": key.replace("_", " "),
            "normalized_key": key,
            "created_at": timestamp,
            "updated_at": timestamp,
            "source": "explicit_owner_statement",
        }
    metadata = dict(envelope.metadata)
    metadata["owner_profile_migrated_from"] = 1
    return envelope.with_data(
        {"owner_id": "primary_owner", "facts": migrated_facts},
        SCHEMA_VERSION_2,
        metadata=metadata,
    )


def _validate_owner_profile_data(data: Any) -> None:
    if not isinstance(data, dict) or set(data) != {
        "owner_id",
        "facts",
        "memories",
        "pending_delete_all",
    }:
        raise MigrationError(
            "Owner profile v3 requires owner_id, facts, memories, and pending_delete_all",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )
    _validate_owner_profile_v2_data(
        {"owner_id": data.get("owner_id"), "facts": data.get("facts")}
    )
    memories = data.get("memories")
    if not isinstance(memories, list) or len(memories) > 120:
        raise MigrationError(
            "Owner profile memories must be a bounded list",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )
    memory_ids = set()
    active_count = 0
    for index, entry in enumerate(memories):
        _validate_owner_memory_entry(entry, index)
        memory_id = entry["memory_id"]
        if memory_id in memory_ids:
            raise MigrationError(
                "Owner profile contains duplicate memory ids",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
        memory_ids.add(memory_id)
        if entry["status"] == "active":
            active_count += 1
    if active_count > 100:
        raise MigrationError(
            "Owner profile active memory limit exceeded",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )
    pending = data.get("pending_delete_all")
    if pending is not None:
        if not isinstance(pending, dict) or set(pending) != {"requested_at", "expires_at"}:
            raise MigrationError(
                "Owner profile pending deletion marker is invalid",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
        if not all(isinstance(pending.get(name), str) and pending.get(name) for name in ("requested_at", "expires_at")):
            raise MigrationError(
                "Owner profile pending deletion timestamps are invalid",
                schema_name=SCHEMA_OWNER_PROFILE,
                status="invalid_store_data",
            )
    serialized_size = len(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if serialized_size > 65536:
        raise MigrationError(
            "Owner profile exceeds its bounded serialized size",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )


def _validate_owner_memory_entry(entry: Any, index: int) -> None:
    required = {
        "memory_id",
        "memory_type",
        "subject",
        "predicate",
        "object",
        "canonical_text",
        "owner_spoken_text",
        "topics",
        "persistence",
        "source",
        "confidence",
        "created_at",
        "updated_at",
        "status",
    }
    optional = {"superseded_at", "replaced_by"}
    if not isinstance(entry, dict) or not required.issubset(entry) or set(entry) - required - optional:
        raise MigrationError(
            f"Owner memory entry {index} has an invalid shape",
            schema_name=SCHEMA_OWNER_PROFILE,
            status="invalid_store_data",
        )
    if not isinstance(entry["memory_id"], str) or not re.fullmatch(r"mem-[a-f0-9]{16}", entry["memory_id"]):
        raise MigrationError("Owner memory id is invalid", schema_name=SCHEMA_OWNER_PROFILE, status="invalid_store_data")
    if entry["memory_type"] not in {
        "preference",
        "dislike",
        "routine",
        "personal_fact",
        "relationship",
        "possession",
        "goal",
        "biographical_fact",
        "instruction_preference",
    }:
        raise MigrationError("Owner memory type is invalid", schema_name=SCHEMA_OWNER_PROFILE, status="invalid_store_data")
    for name, maximum in (
        ("subject", 120),
        ("predicate", 120),
        ("object", 320),
        ("owner_spoken_text", 320),
        ("canonical_text", 360),
        ("created_at", 80),
        ("updated_at", 80),
    ):
        value = entry.get(name)
        if not isinstance(value, str) or not value or len(value) > maximum or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise MigrationError(f"Owner memory {name} is invalid", schema_name=SCHEMA_OWNER_PROFILE, status="invalid_store_data")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", entry["predicate"]):
        raise MigrationError("Owner memory predicate is invalid", schema_name=SCHEMA_OWNER_PROFILE, status="invalid_store_data")
    topics = entry.get("topics")
    if not isinstance(topics, list) or len(topics) > 8 or len(set(topics)) != len(topics):
        raise MigrationError("Owner memory topics are invalid", schema_name=SCHEMA_OWNER_PROFILE, status="invalid_store_data")
    if any(not isinstance(topic, str) or not topic or len(topic) > 48 or not re.fullmatch(r"[a-z0-9]+", topic) for topic in topics):
        raise MigrationError("Owner memory topic is invalid", schema_name=SCHEMA_OWNER_PROFILE, status="invalid_store_data")
    if entry.get("persistence") != "long_term" or entry.get("source") != "explicit_owner_statement" or entry.get("confidence") != 1.0:
        raise MigrationError("Owner memory metadata is invalid", schema_name=SCHEMA_OWNER_PROFILE, status="invalid_store_data")
    if entry.get("status") not in {"active", "superseded", "forgotten"}:
        raise MigrationError("Owner memory status is invalid", schema_name=SCHEMA_OWNER_PROFILE, status="invalid_store_data")
    for optional_name in optional:
        if optional_name in entry and (not isinstance(entry[optional_name], str) or not entry[optional_name]):
            raise MigrationError(f"Owner memory {optional_name} is invalid", schema_name=SCHEMA_OWNER_PROFILE, status="invalid_store_data")


def _owner_profile_migration_v2_to_v3(envelope: SchemaEnvelope) -> SchemaEnvelope:
    _validate_owner_profile_v2_data(envelope.data)
    metadata = dict(envelope.metadata)
    metadata["owner_profile_migrated_from"] = 2
    metadata["purpose"] = "explicit_owner_memory"
    return envelope.with_data(
        {
            "owner_id": "primary_owner",
            "facts": _copy_json_data(dict(envelope.data.get("facts") or {})),
            "memories": [],
            "pending_delete_all": None,
        },
        SCHEMA_VERSION_3,
        metadata=metadata,
    )


def _validate_notes_data(data: Any) -> None:
    _validate_list_data(data, item_label="Notes")
    for index, entry in enumerate(data):
        if "text" not in entry:
            raise MigrationError(f"Note entry {index} requires text", schema_name=SCHEMA_NOTES, status="invalid_store_data")


def _validate_tasks_data(data: Any) -> None:
    _validate_list_data(data, item_label="Tasks")
    for index, entry in enumerate(data):
        if "text" not in entry:
            raise MigrationError(f"Task entry {index} requires text", schema_name=SCHEMA_TASKS, status="invalid_store_data")


def _validate_goals_data(data: Any) -> None:
    _validate_list_data(data, item_label="Goals")
    for index, entry in enumerate(data):
        if "title" not in entry:
            raise MigrationError(f"Goal entry {index} requires title", schema_name=SCHEMA_GOALS, status="invalid_store_data")


def _validate_memory_data(data: Any) -> None:
    _validate_list_data(data, item_label="Memory")
    for index, entry in enumerate(data):
        if "content" not in entry and "text" not in entry:
            raise MigrationError(f"Memory entry {index} requires content", status="invalid_store_data")


def _validate_event_history_data(data: Any) -> None:
    _validate_list_data(data, item_label="Event history")
    for index, entry in enumerate(data):
        if not (("event" in entry and "result" in entry) or ("source" in entry and ("type" in entry or "name" in entry))):
            raise MigrationError(f"Event history entry {index} is not recognized", schema_name=SCHEMA_EVENT_HISTORY, status="invalid_store_data")


def _validate_test_fixture_v1(data: Any) -> None:
    if not isinstance(data, dict) or "items" not in data:
        raise MigrationError("Test fixture v1 requires items", schema_name=SCHEMA_TEST_FIXTURE, status="invalid_store_data")


def _validate_test_fixture_v2(data: Any) -> None:
    if not isinstance(data, dict) or "entries" not in data:
        raise MigrationError("Test fixture v2 requires entries", schema_name=SCHEMA_TEST_FIXTURE, status="invalid_store_data")


def _legacy_profile(payload: Any, path: str) -> SchemaEnvelope:
    if not isinstance(payload, dict) or "facts" not in payload or not isinstance(payload.get("facts"), dict):
        raise MigrationError("Unrecognized legacy profile format", schema_name=SCHEMA_USER_PROFILE, path=Path(path), status="legacy_import_rejected")
    data = _copy_json_data(payload)
    data.setdefault("version", 1)
    return SchemaEnvelope.create(
        SCHEMA_USER_PROFILE,
        SCHEMA_VERSION_1,
        data,
        metadata={"legacy_imported": True, "source_format": "profile_dict"},
    )


def _legacy_list(schema_name: str, payload: Any, path: str, validator: DataValidator, source_format: str) -> SchemaEnvelope:
    if not isinstance(payload, list):
        raise MigrationError("Unrecognized legacy list format", schema_name=schema_name, path=Path(path), status="legacy_import_rejected")
    data = _copy_json_data(payload)
    validator(data)
    return SchemaEnvelope.create(
        schema_name,
        SCHEMA_VERSION_1,
        data,
        metadata={"legacy_imported": True, "source_format": source_format},
    )


def _legacy_notes(payload: Any, path: str) -> SchemaEnvelope:
    return _legacy_list(SCHEMA_NOTES, payload, path, _validate_notes_data, "notes_list")


def _legacy_tasks(payload: Any, path: str) -> SchemaEnvelope:
    return _legacy_list(SCHEMA_TASKS, payload, path, _validate_tasks_data, "tasks_list")


def _legacy_goals(payload: Any, path: str) -> SchemaEnvelope:
    return _legacy_list(SCHEMA_GOALS, payload, path, _validate_goals_data, "goals_list")


def _legacy_memory(schema_name: str) -> LegacyImporter:
    def importer(payload: Any, path: str) -> SchemaEnvelope:
        return _legacy_list(schema_name, payload, path, _validate_memory_data, "memory_list")

    return importer


def _legacy_event_history(payload: Any, path: str) -> SchemaEnvelope:
    return _legacy_list(SCHEMA_EVENT_HISTORY, payload, path, _validate_event_history_data, "event_history_list")


def _test_fixture_migration_v1_to_v2(envelope: SchemaEnvelope) -> SchemaEnvelope:
    _validate_test_fixture_v1(envelope.data)
    data = {
        "entries": list(envelope.data.get("items") or []),
        "migrated_from": 1,
    }
    metadata = dict(envelope.metadata)
    metadata["test_fixture_migrated"] = True
    return envelope.with_data(data, SCHEMA_VERSION_2, metadata=metadata)


def _build_default_registry() -> MigrationRegistry:
    registry = MigrationRegistry()
    registry.register_schema(SCHEMA_USER_PROFILE, SCHEMA_VERSION_1, _validate_profile_data, _legacy_profile)
    registry.register_schema(
        SCHEMA_OWNER_PROFILE,
        SCHEMA_VERSION_3,
        _validate_owner_profile_data,
        validators={
            SCHEMA_VERSION_1: _validate_owner_profile_v1_data,
            SCHEMA_VERSION_2: _validate_owner_profile_v2_data,
            SCHEMA_VERSION_3: _validate_owner_profile_data,
        },
    )
    registry.register_schema(SCHEMA_GOALS, SCHEMA_VERSION_1, _validate_goals_data, _legacy_goals)
    registry.register_schema(SCHEMA_NOTES, SCHEMA_VERSION_1, _validate_notes_data, _legacy_notes)
    registry.register_schema(SCHEMA_TASKS, SCHEMA_VERSION_1, _validate_tasks_data, _legacy_tasks)
    registry.register_schema(SCHEMA_MEMORY_SHORT, SCHEMA_VERSION_1, _validate_memory_data, _legacy_memory(SCHEMA_MEMORY_SHORT))
    registry.register_schema(SCHEMA_MEMORY_LONG, SCHEMA_VERSION_1, _validate_memory_data, _legacy_memory(SCHEMA_MEMORY_LONG))
    registry.register_schema(SCHEMA_EVENT_HISTORY, SCHEMA_VERSION_1, _validate_event_history_data, _legacy_event_history)
    registry.register_schema(
        SCHEMA_TEST_FIXTURE,
        SCHEMA_VERSION_2,
        _validate_test_fixture_v2,
        validators={
            SCHEMA_VERSION_1: _validate_test_fixture_v1,
            SCHEMA_VERSION_2: _validate_test_fixture_v2,
        },
    )
    registry.register_migration(SCHEMA_TEST_FIXTURE, SCHEMA_VERSION_1, SCHEMA_VERSION_2, _test_fixture_migration_v1_to_v2)
    registry.register_migration(
        SCHEMA_OWNER_PROFILE,
        SCHEMA_VERSION_1,
        SCHEMA_VERSION_2,
        _owner_profile_migration_v1_to_v2,
    )
    registry.register_migration(
        SCHEMA_OWNER_PROFILE,
        SCHEMA_VERSION_2,
        SCHEMA_VERSION_3,
        _owner_profile_migration_v2_to_v3,
    )
    return registry


DEFAULT_MIGRATION_REGISTRY = _build_default_registry()
