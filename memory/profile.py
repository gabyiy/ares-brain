import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from events import get_global_bus
from memory.schema_migrations import (
    MigrationError,
    SCHEMA_USER_PROFILE,
    load_store_data,
    publish_migration_failure,
    save_store_data,
)


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_PROFILE_PATH = DATA_DIR / "user_profile.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_value(value: str) -> str:
    return (value or "").strip().strip(".!?").strip()


def _profile_path_from_env() -> Optional[Path]:
    path = os.environ.get("ARES_USER_PROFILE_PATH", "").strip()
    return Path(path) if path else None


@dataclass(frozen=True)
class ProfileFact:
    key: str
    value: Any
    label: str
    source_text: str


class UserProfileStore:
    def __init__(self, path: Optional[Path] = None, event_bus=None):
        self.path = Path(path) if path else (_profile_path_from_env() or DEFAULT_PROFILE_PATH)
        self.events = event_bus if event_bus is not None else get_global_bus()

    def learn_from_text(self, text: str) -> List[ProfileFact]:
        facts = detect_profile_facts(text)
        for fact in facts:
            if fact.key == "owned_items":
                self.add_owned_item(fact.value, source_text=fact.source_text)
            else:
                self.set_fact(fact.key, fact.value, fact.label, fact.source_text)
        return facts

    def set_fact(self, key: str, value: str, label: str, source_text: str = "") -> None:
        profile = self._load()
        profile.setdefault("facts", {})
        profile["facts"][key] = {
            "value": value,
            "label": label,
            "source_text": source_text,
            "updated_at": _utc_now(),
        }
        self._save(profile)
        self._publish("profile.fact_saved", {"key": key, "label": label})

    def add_owned_item(self, value: str, source_text: str = "") -> None:
        profile = self._load()
        profile.setdefault("facts", {})
        items = profile["facts"].setdefault("owned_items", {
            "value": [],
            "label": "owned items",
            "source_text": "",
            "updated_at": _utc_now(),
        })

        existing = list(items.get("value") or [])
        if value not in existing:
            existing.append(value)

        items["value"] = existing
        items["source_text"] = source_text
        items["updated_at"] = _utc_now()
        self._save(profile)
        self._publish("profile.fact_saved", {"key": "owned_items", "label": "owned items"})

    def get_fact(self, key: str):
        return self._load().get("facts", {}).get(key)

    def get_value(self, key: str):
        fact = self.get_fact(key)
        if not fact:
            return None
        return fact.get("value")

    def get_favorite(self, subject: str):
        key = f"favorite_{_normalize_key_part(subject)}"
        return self.get_value(key)

    def _load(self) -> Dict[str, Any]:
        try:
            data = load_store_data(
                self.path,
                SCHEMA_USER_PROFILE,
                {"version": 1, "facts": {}},
            )
        except MigrationError as error:
            publish_migration_failure(self.events, SCHEMA_USER_PROFILE, self.path, error)
            raise

        data.setdefault("version", 1)
        data.setdefault("facts", {})
        return data

    def _save(self, profile: Dict[str, Any]) -> None:
        save_store_data(self.path, SCHEMA_USER_PROFILE, profile)

    def _publish(self, name: str, payload: Dict[str, Any]) -> None:
        if self.events:
            self.events.publish(name, payload, source="memory.profile")


def detect_profile_facts(text: str) -> List[ProfileFact]:
    source = (text or "").strip()
    if not source:
        return []

    patterns = [
        (
            r"^my name is\s+(.+)$",
            lambda match: ProfileFact("name", _clean_value(match.group(1)), "name", source),
        ),
        (
            r"^i live in\s+(.+)$",
            lambda match: ProfileFact("location", _clean_value(match.group(1)), "location", source),
        ),
        (
            r"^my birthday is\s+(.+)$",
            lambda match: ProfileFact("birthday", _clean_value(match.group(1)), "birthday", source),
        ),
        (
            r"^my favorite\s+(.+?)\s+is\s+(.+)$",
            lambda match: ProfileFact(
                f"favorite_{_normalize_key_part(match.group(1))}",
                _clean_value(match.group(2)),
                f"favorite {match.group(1).strip()}",
                source,
            ),
        ),
        (
            r"^i own\s+(.+)$",
            lambda match: ProfileFact("owned_items", _clean_value(match.group(1)), "owned items", source),
        ),
    ]

    facts = []
    for pattern, factory in patterns:
        match = re.match(pattern, source, flags=re.IGNORECASE)
        if match:
            fact = factory(match)
            if fact.value:
                facts.append(fact)
    return facts


def _normalize_key_part(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", (value or "").lower())
    return "_".join(words)
