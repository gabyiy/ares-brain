import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from events import get_global_bus
from memory.schema_migrations import (
    MigrationError,
    SCHEMA_NOTES,
    load_store_data,
    publish_migration_failure,
    save_store_data,
)


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_NOTES_PATH = DATA_DIR / "notes.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _notes_path_from_env() -> Optional[Path]:
    path = os.environ.get("ARES_NOTES_PATH", "").strip()
    return Path(path) if path else None


@dataclass(frozen=True)
class NoteRecord:
    id: str
    timestamp: str
    text: str

    @classmethod
    def create(cls, text: str):
        clean_text = (text or "").strip()
        if not clean_text:
            raise ValueError("Note text is required")
        return cls(
            id=f"note-{uuid.uuid4().hex}",
            timestamp=_utc_now(),
            text=clean_text,
        )

    @classmethod
    def from_dict(cls, entry: Dict[str, Any]):
        return cls(
            id=str(entry.get("id") or f"note-{uuid.uuid4().hex}"),
            timestamp=str(entry.get("timestamp") or _utc_now()),
            text=str(entry.get("text") or "").strip(),
        )

    def to_dict(self) -> Dict[str, str]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "text": self.text,
        }


class NotesStore:
    """Persistent store for user-created notes only."""

    def __init__(self, path: Optional[Path] = None, event_bus=None):
        self.path = Path(path) if path else (_notes_path_from_env() or DEFAULT_NOTES_PATH)
        self.events = event_bus if event_bus is not None else get_global_bus()

    def add(self, text: str) -> NoteRecord:
        note = NoteRecord.create(text)
        notes = self.list()
        notes.append(note)
        self._save(notes)
        self._publish("notes.recorded", {"id": note.id})
        return note

    def list(self) -> List[NoteRecord]:
        return self._load()

    def search(self, keyword: str) -> List[NoteRecord]:
        clean_keyword = (keyword or "").strip()
        if not clean_keyword:
            raise ValueError("Search keyword is required")

        needle = clean_keyword.lower()
        return [note for note in self.list() if needle in note.text.lower()]

    def delete(self, note_id: str) -> Optional[NoteRecord]:
        clean_id = (note_id or "").strip()
        if not clean_id:
            raise ValueError("Note id is required")

        notes = self.list()
        remaining = []
        deleted = None

        for note in notes:
            if note.id == clean_id and deleted is None:
                deleted = note
            else:
                remaining.append(note)

        if deleted:
            self._save(remaining)
            self._publish("notes.deleted", {"id": deleted.id})

        return deleted

    def clear(self) -> int:
        notes = self.list()
        self._save([])
        count = len(notes)
        self._publish("notes.cleared", {"count": count})
        return count

    def _load(self) -> List[NoteRecord]:
        try:
            data = load_store_data(self.path, SCHEMA_NOTES, [])
        except MigrationError as error:
            publish_migration_failure(self.events, SCHEMA_NOTES, self.path, error)
            raise

        notes = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            note = NoteRecord.from_dict(entry)
            if note.id and note.text:
                notes.append(note)
        return notes

    def _save(self, notes: List[NoteRecord]) -> None:
        payload = [note.to_dict() for note in notes]
        save_store_data(self.path, SCHEMA_NOTES, payload)

    def _publish(self, name: str, payload: Dict[str, Any]) -> None:
        if self.events:
            self.events.publish(name, payload, source="memory.notes")
