from importlib import import_module

from memory.v1 import MemoryRecord, MemoryStore, get_default_memory_store, recall, remember
from memory.goals import GoalRecord, GoalsStore
from memory.notes import NoteRecord, NotesStore
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
    OWNER_MEMORY_CONTRACT_VERSION,
    OWNER_MEMORY_REQUEST_CONTRACT,
    OWNER_MEMORY_RESULT_CONTRACT,
    OwnerMemoryRequestV1,
    OwnerMemoryResultV1,
)
from memory.pending_owner_memory import (
    DEFAULT_PENDING_OWNER_MEMORY_ACTION_PATH,
    PENDING_OWNER_MEMORY_SCHEMA,
    PENDING_OWNER_MEMORY_SCHEMA_VERSION,
    PENDING_OWNER_MEMORY_TTL_SECONDS,
    PendingOwnerMemoryAction,
    PendingOwnerMemoryActionStore,
    PendingOwnerMemoryStateResult,
    resolve_pending_owner_memory_path,
)
from memory.profile import ProfileFact, UserProfileStore, detect_profile_facts
from memory.reminder_scheduler import ReminderScheduler, parse_due_text
from memory.schema_migrations import (
    DEFAULT_MIGRATION_REGISTRY,
    SCHEMA_EVENT_HISTORY,
    SCHEMA_GOALS,
    SCHEMA_MEMORY_LONG,
    SCHEMA_MEMORY_SHORT,
    SCHEMA_NOTES,
    SCHEMA_OWNER_PROFILE,
    SCHEMA_TASKS,
    SCHEMA_USER_PROFILE,
    MigrationError,
    MigrationRegistry,
    SchemaEnvelope,
    inspect_store,
)
from memory.tasks import TaskRecord, TasksStore


# These modules depend on core, which imports the event-history store. Loading
# them lazily keeps `events -> memory.schema_migrations` import-order independent.
_LAZY_EXPORTS = {
    "DEFAULT_OWNER_PROFILE_PATH": ("memory.owner_profile", "DEFAULT_OWNER_PROFILE_PATH"),
    "MAX_OWNER_FACTS": ("memory.owner_profile", "MAX_OWNER_FACTS"),
    "MAX_OWNER_PROFILE_BYTES": ("memory.owner_profile", "MAX_OWNER_PROFILE_BYTES"),
    "OWNER_PROFILE_ID": ("memory.owner_profile", "OWNER_PROFILE_ID"),
    "OWNER_PROFILE_SCHEMA_VERSION": ("memory.owner_profile", "OWNER_PROFILE_SCHEMA_VERSION"),
    "OwnerProfileResultV1": ("memory.owner_profile", "OwnerProfileResultV1"),
    "OwnerProfileStore": ("memory.owner_profile", "OwnerProfileStore"),
    "resolve_owner_profile_path": ("memory.owner_profile", "resolve_owner_profile_path"),
    "OwnerMemoryService": ("memory.owner_memory_service", "OwnerMemoryService"),
}


def __getattr__(name):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))

__all__ = [
    "DEFAULT_MIGRATION_REGISTRY",
    "GoalRecord",
    "GoalsStore",
    "MemoryRecord",
    "MemoryStore",
    "MigrationError",
    "MigrationRegistry",
    "NoteRecord",
    "NotesStore",
    "DEFAULT_OWNER_PROFILE_PATH",
    "MAX_OWNER_FACTS",
    "MAX_OWNER_PROFILE_BYTES",
    "OWNER_MEMORY_ACTION_CANCEL_DELETE",
    "OWNER_MEMORY_ACTION_CONFIRM_DELETE",
    "OWNER_MEMORY_ACTION_COUNT",
    "OWNER_MEMORY_ACTION_DELETE_ALL_CONFIRM",
    "OWNER_MEMORY_ACTION_DELETE_ALL_REQUEST",
    "OWNER_MEMORY_ACTION_FORGET",
    "OWNER_MEMORY_ACTION_FORGET_ALL_GENERAL",
    "OWNER_MEMORY_ACTION_FORGET_KEYED_FACT",
    "OWNER_MEMORY_ACTION_FORGET_SPECIFIC",
    "OWNER_MEMORY_ACTION_FORGET_TOPIC",
    "OWNER_MEMORY_ACTION_INSPECT",
    "OWNER_MEMORY_ACTION_LIST",
    "OWNER_MEMORY_ACTION_RECALL",
    "OWNER_MEMORY_ACTION_REMEMBER",
    "OWNER_MEMORY_ACTION_UPDATE",
    "OWNER_MEMORY_CONTRACT_VERSION",
    "OWNER_MEMORY_REQUEST_CONTRACT",
    "OWNER_MEMORY_RESULT_CONTRACT",
    "OWNER_PROFILE_ID",
    "OWNER_PROFILE_SCHEMA_VERSION",
    "OwnerMemoryRequestV1",
    "OwnerMemoryResultV1",
    "OwnerMemoryService",
    "DEFAULT_PENDING_OWNER_MEMORY_ACTION_PATH",
    "PENDING_OWNER_MEMORY_SCHEMA",
    "PENDING_OWNER_MEMORY_SCHEMA_VERSION",
    "PENDING_OWNER_MEMORY_TTL_SECONDS",
    "PendingOwnerMemoryAction",
    "PendingOwnerMemoryActionStore",
    "PendingOwnerMemoryStateResult",
    "OwnerProfileResultV1",
    "OwnerProfileStore",
    "resolve_owner_profile_path",
    "resolve_pending_owner_memory_path",
    "ProfileFact",
    "ReminderScheduler",
    "SCHEMA_EVENT_HISTORY",
    "SCHEMA_GOALS",
    "SCHEMA_MEMORY_LONG",
    "SCHEMA_MEMORY_SHORT",
    "SCHEMA_NOTES",
    "SCHEMA_OWNER_PROFILE",
    "SCHEMA_TASKS",
    "SCHEMA_USER_PROFILE",
    "SchemaEnvelope",
    "TaskRecord",
    "TasksStore",
    "UserProfileStore",
    "detect_profile_facts",
    "get_default_memory_store",
    "inspect_store",
    "parse_due_text",
    "recall",
    "remember",
]
