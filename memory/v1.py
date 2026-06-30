import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from events import get_global_bus


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SHORT_MEMORY_FILE = DATA_DIR / "memories_short.json"
LONG_MEMORY_FILE = DATA_DIR / "memories_long.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp_importance(value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.5
    return max(0.0, min(1.0, numeric))


def _clean_tags(tags: Optional[Iterable[str]]) -> List[str]:
    if not tags:
        return []
    return sorted({str(tag).strip().lower() for tag in tags if str(tag).strip()})


def _stable_legacy_id(entry: Dict[str, Any], index: int, long_term: bool) -> str:
    raw = "|".join(
        [
            str(entry.get("timestamp", "")),
            str(entry.get("category", "")),
            str(entry.get("content", "")),
            str(index),
            "long" if long_term else "short",
        ]
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"legacy-{digest}"


@dataclass
class MemoryRecord:
    id: str
    timestamp: str
    category: str
    content: str
    importance: float = 0.5
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    source: str = "memory.v1"
    long_term: bool = False

    @classmethod
    def create(
        cls,
        content: str,
        category: str = "general",
        importance: float = 0.5,
        tags: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "memory.v1",
        long_term: bool = False,
    ):
        clean_importance = _clamp_importance(importance)
        return cls(
            id=f"mem-{uuid.uuid4().hex}",
            timestamp=_utc_now(),
            category=(category or "general").strip() or "general",
            content=(content or "").strip(),
            importance=clean_importance,
            tags=_clean_tags(tags),
            metadata=dict(metadata or {}),
            source=(source or "memory.v1").strip() or "memory.v1",
            long_term=bool(long_term),
        )

    @classmethod
    def from_dict(cls, entry: Dict[str, Any], index: int = 0, long_term: bool = False):
        return cls(
            id=str(entry.get("id") or _stable_legacy_id(entry, index, long_term)),
            timestamp=str(entry.get("timestamp") or entry.get("created_at") or _utc_now()),
            category=str(entry.get("category") or "general").strip() or "general",
            content=str(entry.get("content") or entry.get("text") or "").strip(),
            importance=_clamp_importance(entry.get("importance", 0.5)),
            tags=_clean_tags(entry.get("tags", [])),
            metadata=dict(entry.get("metadata") or {}),
            source=str(entry.get("source") or "legacy").strip() or "legacy",
            long_term=bool(entry.get("long_term", long_term)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "category": self.category,
            "content": self.content,
            "importance": self.importance,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "source": self.source,
            "long_term": self.long_term,
        }


class MemoryStore:
    """
    Versioned memory interface backed by the existing short/long JSON files.
    """

    def __init__(
        self,
        short_path: Optional[Path] = None,
        long_path: Optional[Path] = None,
        event_bus=None,
    ):
        self.short_path = Path(short_path) if short_path else SHORT_MEMORY_FILE
        self.long_path = Path(long_path) if long_path else LONG_MEMORY_FILE
        self.events = event_bus if event_bus is not None else get_global_bus()

    def remember(
        self,
        content: str,
        category: str = "general",
        importance: float = 0.5,
        tags: Optional[Iterable[str]] = None,
        long_term: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "memory.v1",
    ) -> MemoryRecord:
        record = MemoryRecord.create(
            content=content,
            category=category,
            importance=importance,
            tags=tags,
            metadata=metadata,
            source=source,
            long_term=long_term or _clamp_importance(importance) >= 0.85,
        )

        if not record.content:
            raise ValueError("Memory content is required")

        target_path = self.long_path if record.long_term else self.short_path
        records = self._load_file(target_path, long_term=record.long_term)
        records.append(record)
        self._save_file(target_path, records)
        self._publish(
            "memory.recorded",
            {
                "id": record.id,
                "category": record.category,
                "importance": record.importance,
                "long_term": record.long_term,
            },
        )
        return record

    def recall(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        long_term: Optional[bool] = None,
        min_importance: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> List[MemoryRecord]:
        records = self._load_records(long_term=long_term)

        if category:
            wanted_category = category.strip().lower()
            records = [
                record
                for record in records
                if record.category.strip().lower() == wanted_category
            ]

        wanted_tags = set(_clean_tags(tags))
        if wanted_tags:
            records = [
                record
                for record in records
                if wanted_tags.issubset(set(record.tags))
            ]

        if min_importance is not None:
            threshold = _clamp_importance(min_importance)
            records = [record for record in records if record.importance >= threshold]

        if query:
            needle = query.strip().lower()
            records = [
                record
                for record in records
                if needle in " ".join(
                    [record.category, record.content, " ".join(record.tags)]
                ).lower()
            ]

        records.sort(key=lambda record: record.timestamp)

        if limit is not None:
            records = records[-max(0, int(limit)) :]

        return records

    def promote(self, memory_id: str) -> Optional[MemoryRecord]:
        memory_id = (memory_id or "").strip()
        if not memory_id:
            raise ValueError("Memory id is required")

        short_records = self._load_file(self.short_path, long_term=False)
        long_records = self._load_file(self.long_path, long_term=True)

        for index, record in enumerate(short_records):
            if record.id == memory_id:
                promoted = short_records.pop(index)
                promoted.long_term = True
                promoted.metadata = dict(promoted.metadata)
                promoted.metadata["promoted_at"] = _utc_now()
                long_records.append(promoted)
                self._save_file(self.short_path, short_records)
                self._save_file(self.long_path, long_records)
                self._publish("memory.promoted", {"id": promoted.id})
                return promoted

        return None

    def clear(self, long_term: Optional[bool] = None) -> None:
        if long_term in (False, None):
            self._save_file(self.short_path, [])
        if long_term in (True, None):
            self._save_file(self.long_path, [])
        self._publish("memory.cleared", {"long_term": long_term})

    def stats(self) -> Dict[str, int]:
        short_count = len(self._load_file(self.short_path, long_term=False))
        long_count = len(self._load_file(self.long_path, long_term=True))
        return {
            "short_term": short_count,
            "long_term": long_count,
            "total": short_count + long_count,
        }

    def _load_records(self, long_term: Optional[bool] = None) -> List[MemoryRecord]:
        records = []
        if long_term in (False, None):
            records.extend(self._load_file(self.short_path, long_term=False))
        if long_term in (True, None):
            records.extend(self._load_file(self.long_path, long_term=True))
        return records

    def _load_file(self, path: Path, long_term: bool) -> List[MemoryRecord]:
        if not path.exists():
            return []

        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return []

        if not isinstance(data, list):
            return []

        return [
            MemoryRecord.from_dict(entry, index=index, long_term=long_term)
            for index, entry in enumerate(data)
            if isinstance(entry, dict)
        ]

    def _save_file(self, path: Path, records: List[MemoryRecord]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [record.to_dict() for record in records]
        temp_path = path.with_suffix(path.suffix + ".tmp")

        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        temp_path.replace(path)

    def _publish(self, name: str, payload: Dict[str, Any]) -> None:
        if not self.events:
            return
        self.events.publish(name, payload, source="memory.v1")


_DEFAULT_STORE = None


def get_default_memory_store() -> MemoryStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = MemoryStore()
    return _DEFAULT_STORE


def remember(*args, **kwargs) -> MemoryRecord:
    return get_default_memory_store().remember(*args, **kwargs)


def recall(*args, **kwargs) -> List[MemoryRecord]:
    return get_default_memory_store().recall(*args, **kwargs)
