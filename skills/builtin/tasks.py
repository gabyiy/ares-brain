import re

from skills.base import Skill, SkillContext, SkillResponse


class TasksSkill(Skill):
    name = "tasks"
    description = "Manages offline reminders and tasks."
    version = "0.1"
    run_before_intents = True
    triggers = (
        "add task",
        "remind me to",
        "list tasks",
        "show tasks",
        "mark task",
        "delete task",
        "clear completed tasks",
    )
    selection_keywords = (
        "task",
        "tasks",
        "reminder",
        "remind",
        "todo",
    )
    selection_priority = 0.09

    def can_handle(self, text: str) -> bool:
        return self._parse(text)["action"] is not None

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        store = getattr(context, "tasks_store", None)
        if not store:
            return self._response(
                "Task storage is not available.",
                error="missing_tasks_store",
            )

        parsed = self._parse(text)
        action = parsed["action"]

        if action == "add":
            task_text = parsed["text"]
            if not task_text:
                return self._response("I need task text to save.", error="empty_task")
            task = store.add(task_text, due=parsed.get("due"))
            return self._response(
                self._format_saved(task),
                action="add",
                task_id=task.id,
                due=task.due,
            )

        if action == "list":
            return self._format_tasks(store.list(), "Your tasks:", empty="You do not have any tasks yet.")

        if action == "mark_done":
            task_id = parsed["task_id"]
            task = store.mark_done(task_id)
            if not task:
                return self._response(f"I could not find task {task_id}.", action="mark_done", missing=True)
            return self._response(f"Marked task {task_id} done.", action="mark_done", task_id=task_id)

        if action == "delete":
            task_id = parsed["task_id"]
            deleted = store.delete(task_id)
            if not deleted:
                return self._response(f"I could not find task {task_id}.", action="delete", missing=True)
            return self._response(f"Deleted task {task_id}.", action="delete", task_id=task_id)

        if action == "clear_completed":
            count = store.clear_completed()
            return self._response(f"Cleared {count} completed tasks.", action="clear_completed", count=count)

        return self._response("I do not know how to handle that task request.", error="unknown_tasks_action")

    def _parse(self, text: str):
        raw = (text or "").strip()
        low = raw.lower().strip()

        if low in ("list tasks", "show tasks"):
            return {"action": "list"}

        if low == "clear completed tasks":
            return {"action": "clear_completed"}

        done_match = re.match(r"^mark\s+task\s+(\S+)\s+done$", raw, flags=re.IGNORECASE)
        if done_match:
            return {"action": "mark_done", "task_id": done_match.group(1).strip()}

        delete_match = re.match(r"^delete\s+task\s+(\S+)$", raw, flags=re.IGNORECASE)
        if delete_match:
            return {"action": "delete", "task_id": delete_match.group(1).strip()}

        for pattern in (
            r"^add\s+task\s*(.*)$",
            r"^remind\s+me\s+to\s*(.*)$",
        ):
            match = re.match(pattern, raw, flags=re.IGNORECASE)
            if match:
                task_text, due = self._split_due_text(match.group(1))
                return {"action": "add", "text": task_text, "due": due}

        return {"action": None}

    def _split_due_text(self, text: str):
        clean_text = self._clean_task_text(text)
        if not clean_text:
            return "", None

        due_match = re.match(r"^(.+?)\s+due\s+(.+)$", clean_text, flags=re.IGNORECASE)
        if due_match:
            task_text = self._clean_task_text(due_match.group(1))
            due = self._clean_task_text(due_match.group(2))
            return task_text, due or None

        return clean_text, None

    def _clean_task_text(self, text: str) -> str:
        return (text or "").strip().lstrip(":-. ").strip()

    def _format_saved(self, task) -> str:
        if task.due:
            return f"Saved task {task.id}: {task.text} (due {task.due})"
        return f"Saved task {task.id}: {task.text}"

    def _format_tasks(self, tasks, heading: str, empty: str, **metadata) -> SkillResponse:
        if not tasks:
            return self._response(empty, count=0, **metadata)

        lines = [heading]
        for task in tasks:
            status = "done" if task.completed else "open"
            due = f" due {task.due}" if task.due else ""
            lines.append(f"- {task.id} [{status}]: {task.text}{due}")

        return self._response("\n".join(lines), count=len(tasks), **metadata)

    def _response(self, text: str, **metadata) -> SkillResponse:
        return SkillResponse(text=text, skill=self.name, metadata=metadata)
