import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from events import get_global_bus
from memory.schema_migrations import (
    MigrationError,
    SCHEMA_TASKS,
    load_store_data,
    publish_migration_failure,
    save_store_data,
)


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_TASKS_PATH = DATA_DIR / "tasks.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tasks_path_from_env() -> Optional[Path]:
    path = os.environ.get("ARES_TASKS_PATH", "").strip()
    return Path(path) if path else None


@dataclass(frozen=True)
class TaskRecord:
    id: str
    text: str
    created_at: str
    due: Optional[str] = None
    completed: bool = False

    @classmethod
    def create(cls, text: str, due: Optional[str] = None):
        clean_text = (text or "").strip()
        clean_due = (due or "").strip() or None
        if not clean_text:
            raise ValueError("Task text is required")
        return cls(
            id=f"task-{uuid.uuid4().hex}",
            text=clean_text,
            created_at=_utc_now(),
            due=clean_due,
            completed=False,
        )

    @classmethod
    def from_dict(cls, entry: Dict[str, Any]):
        clean_due = entry.get("due")
        if clean_due is not None:
            clean_due = str(clean_due).strip() or None
        return cls(
            id=str(entry.get("id") or f"task-{uuid.uuid4().hex}"),
            text=str(entry.get("text") or "").strip(),
            created_at=str(entry.get("created_at") or entry.get("timestamp") or _utc_now()),
            due=clean_due,
            completed=bool(entry.get("completed", False)),
        )

    def complete(self):
        return replace(self, completed=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "created_at": self.created_at,
            "due": self.due,
            "completed": self.completed,
        }


class TasksStore:
    """Persistent store for offline reminders/tasks only."""

    def __init__(self, path: Optional[Path] = None, event_bus=None):
        self.path = Path(path) if path else (_tasks_path_from_env() or DEFAULT_TASKS_PATH)
        self.events = event_bus if event_bus is not None else get_global_bus()

    def add(self, text: str, due: Optional[str] = None) -> TaskRecord:
        task = TaskRecord.create(text, due=due)
        tasks = self.list()
        tasks.append(task)
        self._save(tasks)
        self._publish("tasks.recorded", {"id": task.id, "due": task.due})
        return task

    def list(self) -> List[TaskRecord]:
        return self._load()

    def mark_done(self, task_id: str) -> Optional[TaskRecord]:
        clean_id = (task_id or "").strip()
        if not clean_id:
            raise ValueError("Task id is required")

        tasks = self.list()
        updated_task = None
        updated = []

        for task in tasks:
            if task.id == clean_id and updated_task is None:
                updated_task = task.complete()
                updated.append(updated_task)
            else:
                updated.append(task)

        if updated_task:
            self._save(updated)
            self._publish("tasks.completed", {"id": updated_task.id})

        return updated_task

    def delete(self, task_id: str) -> Optional[TaskRecord]:
        clean_id = (task_id or "").strip()
        if not clean_id:
            raise ValueError("Task id is required")

        tasks = self.list()
        remaining = []
        deleted = None

        for task in tasks:
            if task.id == clean_id and deleted is None:
                deleted = task
            else:
                remaining.append(task)

        if deleted:
            self._save(remaining)
            self._publish("tasks.deleted", {"id": deleted.id})

        return deleted

    def clear_completed(self) -> int:
        tasks = self.list()
        remaining = [task for task in tasks if not task.completed]
        count = len(tasks) - len(remaining)
        if count:
            self._save(remaining)
        self._publish("tasks.completed_cleared", {"count": count})
        return count

    def _load(self) -> List[TaskRecord]:
        try:
            data = load_store_data(self.path, SCHEMA_TASKS, [])
        except MigrationError as error:
            publish_migration_failure(self.events, SCHEMA_TASKS, self.path, error)
            raise

        tasks = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            task = TaskRecord.from_dict(entry)
            if task.id and task.text:
                tasks.append(task)
        return tasks

    def _save(self, tasks: List[TaskRecord]) -> None:
        payload = [task.to_dict() for task in tasks]
        save_store_data(self.path, SCHEMA_TASKS, payload)

    def _publish(self, name: str, payload: Dict[str, Any]) -> None:
        if self.events:
            self.events.publish(name, payload, source="memory.tasks")
