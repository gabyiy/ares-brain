import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from events import get_global_bus


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_GOALS_PATH = DATA_DIR / "goals.json"
VALID_GOAL_STATUSES = {"active", "completed", "paused"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _goals_path_from_env() -> Optional[Path]:
    path = os.environ.get("ARES_GOALS_PATH", "").strip()
    return Path(path) if path else None


@dataclass(frozen=True)
class GoalRecord:
    id: str
    title: str
    description: str
    created_at: str
    status: str
    priority: str
    milestones: List[str]

    @classmethod
    def create(cls, title: str, description: str = "", priority: str = "normal"):
        clean_title = (title or "").strip()
        clean_description = (description or "").strip()
        clean_priority = (priority or "").strip() or "normal"
        if not clean_title:
            raise ValueError("Goal title is required")
        return cls(
            id=f"goal-{uuid.uuid4().hex}",
            title=clean_title,
            description=clean_description,
            created_at=_utc_now(),
            status="active",
            priority=clean_priority,
            milestones=[],
        )

    @classmethod
    def from_dict(cls, entry: Dict[str, Any]):
        status = str(entry.get("status") or "active").strip().lower()
        if status not in VALID_GOAL_STATUSES:
            status = "active"

        raw_milestones = entry.get("milestones") or []
        milestones = [
            str(milestone).strip()
            for milestone in raw_milestones
            if str(milestone).strip()
        ] if isinstance(raw_milestones, list) else []

        return cls(
            id=str(entry.get("id") or f"goal-{uuid.uuid4().hex}"),
            title=str(entry.get("title") or "").strip(),
            description=str(entry.get("description") or "").strip(),
            created_at=str(entry.get("created_at") or _utc_now()),
            status=status,
            priority=str(entry.get("priority") or "normal").strip() or "normal",
            milestones=milestones,
        )

    def with_status(self, status: str):
        clean_status = (status or "").strip().lower()
        if clean_status not in VALID_GOAL_STATUSES:
            raise ValueError(f"Invalid goal status: {status}")
        return replace(self, status=clean_status)

    def with_milestone(self, milestone: str):
        clean_milestone = (milestone or "").strip()
        if not clean_milestone:
            raise ValueError("Milestone text is required")
        return replace(self, milestones=[*self.milestones, clean_milestone])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "created_at": self.created_at,
            "status": self.status,
            "priority": self.priority,
            "milestones": list(self.milestones),
        }


class GoalsStore:
    """Persistent store for long-term user goals only."""

    def __init__(self, path: Optional[Path] = None, event_bus=None):
        self.path = Path(path) if path else (_goals_path_from_env() or DEFAULT_GOALS_PATH)
        self.events = event_bus if event_bus is not None else get_global_bus()

    def add(self, title: str, description: str = "", priority: str = "normal") -> GoalRecord:
        goal = GoalRecord.create(title, description=description, priority=priority)
        goals = self.list()
        goals.append(goal)
        self._save(goals)
        self._publish("goals.recorded", {"id": goal.id, "priority": goal.priority})
        return goal

    def list(self) -> List[GoalRecord]:
        return self._load()

    def get(self, goal_id: str) -> Optional[GoalRecord]:
        clean_id = (goal_id or "").strip()
        if not clean_id:
            raise ValueError("Goal id is required")
        return next((goal for goal in self.list() if goal.id == clean_id), None)

    def complete(self, goal_id: str) -> Optional[GoalRecord]:
        return self._update_status(goal_id, "completed", "goals.completed")

    def pause(self, goal_id: str) -> Optional[GoalRecord]:
        return self._update_status(goal_id, "paused", "goals.paused")

    def delete(self, goal_id: str) -> Optional[GoalRecord]:
        clean_id = (goal_id or "").strip()
        if not clean_id:
            raise ValueError("Goal id is required")

        goals = self.list()
        remaining = []
        deleted = None

        for goal in goals:
            if goal.id == clean_id and deleted is None:
                deleted = goal
            else:
                remaining.append(goal)

        if deleted:
            self._save(remaining)
            self._publish("goals.deleted", {"id": deleted.id})

        return deleted

    def add_milestone(self, goal_id: str, milestone: str) -> Optional[GoalRecord]:
        clean_id = (goal_id or "").strip()
        if not clean_id:
            raise ValueError("Goal id is required")

        goals = self.list()
        updated_goal = None
        updated = []

        for goal in goals:
            if goal.id == clean_id and updated_goal is None:
                updated_goal = goal.with_milestone(milestone)
                updated.append(updated_goal)
            else:
                updated.append(goal)

        if updated_goal:
            self._save(updated)
            self._publish("goals.milestone_added", {"id": updated_goal.id, "count": len(updated_goal.milestones)})

        return updated_goal

    def _update_status(self, goal_id: str, status: str, event_name: str) -> Optional[GoalRecord]:
        clean_id = (goal_id or "").strip()
        if not clean_id:
            raise ValueError("Goal id is required")

        goals = self.list()
        updated_goal = None
        updated = []

        for goal in goals:
            if goal.id == clean_id and updated_goal is None:
                updated_goal = goal.with_status(status)
                updated.append(updated_goal)
            else:
                updated.append(goal)

        if updated_goal:
            self._save(updated)
            self._publish(event_name, {"id": updated_goal.id})

        return updated_goal

    def _load(self) -> List[GoalRecord]:
        if not self.path.exists():
            return []

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return []

        if not isinstance(data, list):
            return []

        goals = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            goal = GoalRecord.from_dict(entry)
            if goal.id and goal.title:
                goals.append(goal)
        return goals

    def _save(self, goals: List[GoalRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = [goal.to_dict() for goal in goals]

        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        temp_path.replace(self.path)

    def _publish(self, name: str, payload: Dict[str, Any]) -> None:
        if self.events:
            self.events.publish(name, payload, source="memory.goals")
