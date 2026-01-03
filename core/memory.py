from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
import json
import uuid
from typing import List, Optional, Dict, Any


BASE_DIR = Path(__file__).resolve().parents[1]
MEMORY_DIR = BASE_DIR / "data" / "memory"
MEMORY_FILE = MEMORY_DIR / "long_term.json"


@dataclass
class Memory:
    id: str
    kind: str              # "system", "user", "experience", etc.
    content: str           # what happened / what was learned
    timestamp: str         # ISO time string
    importance: float = 1.0
    tags: Optional[List[str]] = None


class MemoryManager:
    def __init__(self, path: Path = MEMORY_FILE) -> None:
        self.path = path
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self._data = {"memories": []}  # type: Dict[str, Any]
        self._load()

    # ---------- internal helpers ----------

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                # if file is corrupted, keep what we have in RAM
                pass

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ---------- public API ----------

    def add_memory(
        self,
        kind: str,
        content: str,
        importance: float = 1.0,
        tags: Optional[List[str]] = None,
        timestamp: Optional[str] = None,
    ) -> Memory:
        """Store a new memory and return it."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()

        mem = Memory(
            id=str(uuid.uuid4()),
            kind=kind,
            content=content,
            timestamp=timestamp,
            importance=importance,
            tags=tags or [],
        )
        self._data.setdefault("memories", []).append(asdict(mem))
        self._save()
        return mem

    def get_all(self) -> List[Memory]:
        return [Memory(**m) for m in self._data.get("memories", [])]

    def get_birth_memory(self) -> Optional[Memory]:
        """First memory ARES has."""
        memories = self.get_all()
        if not memories:
            return None
        return sorted(memories, key=lambda m: m.timestamp)[0]

    def get_stats(self) -> Dict[str, Any]:
        mems = self.get_all()
        return {
            "total_memories": len(mems),
            "by_kind": self._count_by("kind", mems),
        }

    @staticmethod
    def _count_by(field: str, mems: List[Memory]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for m in mems:
            key = getattr(m, field)
            counts[key] = counts.get(key, 0) + 1
        return counts

    # ---------- initialization helpers ----------

    def ensure_birth_memory(self) -> Memory:
        """Create 'ARES was born' memory if none exists."""
        birth = self.get_birth_memory()
        if birth is not None:
            return birth

        return self.add_memory(
            kind="system",
            content="ARES was first activated and came online.",
            importance=10.0,
            tags=["birth", "system", "identity"],
        )

