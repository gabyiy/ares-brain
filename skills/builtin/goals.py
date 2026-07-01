import re

from skills.base import Skill, SkillContext, SkillResponse


class GoalsSkill(Skill):
    name = "goals"
    description = "Stores and manages long-term local user goals."
    version = "0.1"
    intent_names = ("goal",)
    run_before_intents = True
    triggers = (
        "add goal",
        "list goals",
        "show goal",
        "complete goal",
        "pause goal",
        "delete goal",
        "add milestone to goal",
    )
    selection_keywords = (
        "goal",
        "goals",
        "milestone",
        "milestones",
    )
    selection_priority = 0.1

    def can_handle(self, text: str) -> bool:
        return self._parse(text)["action"] is not None

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        store = getattr(context, "goals_store", None)
        if not store:
            return self._response(
                "Goal storage is not available.",
                error="missing_goals_store",
            )

        parsed = self._parse_from_context(text, context)
        action = parsed["action"]

        if action == "add":
            title = parsed.get("title", "")
            if not title:
                return self._response("I need a goal title to save.", error="empty_goal")
            goal = store.add(
                title=title,
                description=parsed.get("description", ""),
                priority=parsed.get("priority", "normal"),
            )
            return self._response(
                f"Saved goal {goal.id}: {goal.title}",
                action="add",
                goal_id=goal.id,
                priority=goal.priority,
            )

        if action == "list":
            return self._format_goals(store.list(), "Your goals:", empty="You do not have any goals yet.")

        if action == "show":
            goal_id = parsed.get("goal_id", "")
            if not goal_id:
                return self._response("I need a goal id.", error="empty_goal_id")
            goal = store.get(goal_id)
            if not goal:
                return self._response(f"I could not find goal {goal_id}.", action="show", missing=True)
            return self._response(self._format_goal(goal), action="show", goal_id=goal_id)

        if action == "complete":
            return self._update_goal_status(store.complete, parsed.get("goal_id", ""), "Completed", "complete")

        if action == "pause":
            return self._update_goal_status(store.pause, parsed.get("goal_id", ""), "Paused", "pause")

        if action == "delete":
            goal_id = parsed.get("goal_id", "")
            if not goal_id:
                return self._response("I need a goal id.", error="empty_goal_id")
            deleted = store.delete(goal_id)
            if not deleted:
                return self._response(f"I could not find goal {goal_id}.", action="delete", missing=True)
            return self._response(f"Deleted goal {goal_id}.", action="delete", goal_id=goal_id)

        if action == "add_milestone":
            goal_id = parsed.get("goal_id", "")
            milestone = parsed.get("milestone", "")
            if not goal_id:
                return self._response("I need a goal id.", error="empty_goal_id")
            if not milestone:
                return self._response("I need milestone text to save.", error="empty_milestone")
            goal = store.add_milestone(goal_id, milestone)
            if not goal:
                return self._response(f"I could not find goal {goal_id}.", action="add_milestone", missing=True)
            return self._response(
                f"Added milestone to goal {goal_id}: {milestone}",
                action="add_milestone",
                goal_id=goal_id,
                milestone=milestone,
            )

        return self._response("I do not know how to handle that goal request.", error="unknown_goals_action")

    def _parse(self, text: str):
        raw = (text or "").strip()
        low = raw.lower().strip()

        if low in ("list goals", "show goals"):
            return {"action": "list"}

        add_milestone_match = re.match(
            r"^add\s+milestone\s+to\s+goal\s+(\S+)\s*(.*)$",
            raw,
            flags=re.IGNORECASE,
        )
        if add_milestone_match:
            return {
                "action": "add_milestone",
                "goal_id": add_milestone_match.group(1).strip(),
                "milestone": self._clean_goal_text(add_milestone_match.group(2)),
            }

        for action in ("show", "complete", "pause", "delete"):
            match = re.match(rf"^{action}\s+goal\s+(\S+)$", raw, flags=re.IGNORECASE)
            if match:
                return {"action": action, "goal_id": match.group(1).strip()}

        add_match = re.match(r"^add\s+goal\s*(.*)$", raw, flags=re.IGNORECASE)
        if add_match:
            title, description, priority = self._split_goal_fields(add_match.group(1))
            return {
                "action": "add",
                "title": title,
                "description": description,
                "priority": priority,
            }

        return {"action": None}

    def _parse_from_context(self, text: str, context: SkillContext):
        intent = context.metadata.get("intent") if context.metadata else None
        if getattr(intent, "intent_name", None) == "goal":
            entities = dict(getattr(intent, "extracted_entities", {}) or {})
            return {
                "action": entities.get("action"),
                "title": entities.get("title", ""),
                "description": entities.get("description", ""),
                "priority": entities.get("priority", "normal"),
                "goal_id": entities.get("goal_id", ""),
                "milestone": entities.get("milestone", ""),
            }
        return self._parse(text)

    def _split_goal_fields(self, text: str):
        remaining = self._clean_goal_text(text)
        priority = "normal"
        description = ""

        priority_match = re.search(r"\s+priority\s+([a-z0-9_-]+)\s*$", remaining, flags=re.IGNORECASE)
        if priority_match:
            priority = priority_match.group(1).strip()
            remaining = self._clean_goal_text(remaining[: priority_match.start()])

        description_match = re.search(r"\s+description\s+(.+)$", remaining, flags=re.IGNORECASE)
        if description_match:
            description = self._clean_goal_text(description_match.group(1))
            remaining = self._clean_goal_text(remaining[: description_match.start()])

        return remaining, description, priority

    def _update_goal_status(self, updater, goal_id: str, label: str, action: str) -> SkillResponse:
        if not goal_id:
            return self._response("I need a goal id.", error="empty_goal_id")
        goal = updater(goal_id)
        if not goal:
            return self._response(f"I could not find goal {goal_id}.", action=action, missing=True)
        return self._response(f"{label} goal {goal_id}.", action=action, goal_id=goal_id)

    def _format_goals(self, goals, heading: str, empty: str, **metadata) -> SkillResponse:
        if not goals:
            return self._response(empty, count=0, **metadata)

        lines = [heading]
        for goal in goals:
            lines.append(f"- {goal.id} [{goal.status}, priority {goal.priority}]: {goal.title}")

        return self._response("\n".join(lines), count=len(goals), **metadata)

    def _format_goal(self, goal) -> str:
        lines = [
            f"Goal {goal.id}: {goal.title}",
            f"Status: {goal.status}",
            f"Priority: {goal.priority}",
        ]
        if goal.description:
            lines.append(f"Description: {goal.description}")
        if goal.milestones:
            lines.append("Milestones:")
            lines.extend(f"- {milestone}" for milestone in goal.milestones)
        else:
            lines.append("Milestones: none")
        return "\n".join(lines)

    def _clean_goal_text(self, text: str) -> str:
        return (text or "").strip().lstrip(":-. ").strip()

    def _response(self, text: str, **metadata) -> SkillResponse:
        return SkillResponse(text=text, skill=self.name, metadata=metadata)
