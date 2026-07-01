import re
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from memory.tasks import TaskRecord, TasksStore


def parse_due_text(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    reference = now or datetime.now().astimezone()
    clean_text = (text or "").strip().lower()
    if not clean_text:
        return None

    if clean_text == "today":
        return _end_of_day(reference)

    if clean_text == "tomorrow":
        return _end_of_day(reference + timedelta(days=1))

    if clean_text == "next week":
        return _end_of_day(reference + timedelta(days=7))

    relative_match = re.fullmatch(r"in\s+(\d+)\s+(minute|minutes|hour|hours)", clean_text)
    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2)
        if amount <= 0:
            return None
        delta = timedelta(minutes=amount) if unit.startswith("minute") else timedelta(hours=amount)
        return reference + delta

    time_match = re.fullmatch(r"at\s+([01]?\d|2[0-3]):([0-5]\d)", clean_text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        due_at = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due_at < reference:
            due_at = due_at + timedelta(days=1)
        return due_at

    return None


class ReminderScheduler:
    """Interprets task due text without sending notifications."""

    def __init__(self, tasks_store: TasksStore):
        self.tasks_store = tasks_store

    def parse_due_text(self, text: str, now: Optional[datetime] = None) -> Optional[datetime]:
        return parse_due_text(text, now=now)

    def due_tasks(self, now: datetime) -> List[TaskRecord]:
        current = _normalize_now(now)
        due = []
        for task, due_at in self._scheduled_tasks(current):
            comparable_due = _align_datetime(due_at, current)
            if comparable_due <= current:
                due.append(task)
        return due

    def upcoming_tasks(self, now: datetime, limit: int) -> List[TaskRecord]:
        if limit <= 0:
            return []

        current = _normalize_now(now)
        upcoming = []
        for task, due_at in self._scheduled_tasks(current):
            comparable_due = _align_datetime(due_at, current)
            if comparable_due > current:
                upcoming.append((comparable_due, task))

        upcoming.sort(key=lambda entry: (entry[0], entry[1].id))
        return [task for _due_at, task in upcoming[:limit]]

    def _scheduled_tasks(self, now: datetime) -> List[Tuple[TaskRecord, datetime]]:
        scheduled = []
        for task in self.tasks_store.list():
            if task.completed or not task.due:
                continue

            reference = _task_reference_time(task, now)
            due_at = parse_due_text(task.due, now=reference)
            if due_at is not None:
                scheduled.append((task, due_at))

        return scheduled


def _end_of_day(value: datetime) -> datetime:
    return value.replace(hour=23, minute=59, second=59, microsecond=0)


def _normalize_now(now: datetime) -> datetime:
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    return now


def _task_reference_time(task: TaskRecord, fallback: datetime) -> datetime:
    try:
        created_at = task.created_at.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(created_at)
    except ValueError:
        return fallback

    return _align_datetime(parsed, fallback)


def _align_datetime(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)

    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)

    if value.tzinfo is not None and reference.tzinfo is not None:
        return value.astimezone(reference.tzinfo)

    return value
