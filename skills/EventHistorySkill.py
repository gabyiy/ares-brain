from events.EventHistoryStore import EventHistoryStore
from skills.base import Skill, SkillContext, SkillResponse


class EventHistorySkill(Skill):
    name = "event_history"
    description = "Reads recent internal event history."
    version = "0.1"
    intent_names = ("event_history",)
    triggers = (
        "what happened recently",
        "show recent events",
        "show critical events",
        "recent events",
        "critical events",
    )
    selection_keywords = (
        "recent events",
        "critical events",
        "what happened recently",
    )
    selection_priority = 0.1

    def can_handle(self, text: str) -> bool:
        return _query_type(text) in {"recent", "critical"}

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        store = getattr(context, "event_history_store", None) or EventHistoryStore()
        query_type = self._query_type_from_context(text, context)

        if query_type == "critical":
            records = store.recent(priority="critical", limit=10)
            if not records:
                return self._response(
                    "No critical events found.",
                    query_type=query_type,
                    count=0,
                )
            return self._response(
                _format_records("Critical events:", records),
                query_type=query_type,
                count=len(records),
            )

        records = store.recent(limit=10)
        if not records:
            return self._response(
                "No event history is available yet.",
                query_type=query_type,
                count=0,
            )

        return self._response(
            _format_records("Recent events:", records),
            query_type=query_type,
            count=len(records),
        )

    def _query_type_from_context(self, text: str, context: SkillContext) -> str:
        intent = context.metadata.get("intent") if context.metadata else None
        if getattr(intent, "intent_name", None) == "event_history":
            entities = dict(getattr(intent, "extracted_entities", {}) or {})
            query_type = str(entities.get("query_type") or "").strip().lower()
            if query_type in {"recent", "critical"}:
                return query_type
        return _query_type(text) or "recent"

    def _response(self, text: str, **metadata) -> SkillResponse:
        return SkillResponse(text=text, skill=self.name, metadata=metadata)


def _query_type(text: str) -> str:
    lowered = " ".join((text or "").lower().strip().split())
    if lowered in {"show critical events", "critical events"}:
        return "critical"
    if lowered in {"what happened recently", "show recent events", "recent events"}:
        return "recent"
    return ""


def _format_records(heading: str, records) -> str:
    lines = [heading]
    for record in records:
        lines.append(
            f"- {record.timestamp} [{record.priority}] {record.source}.{record.type}: {record.decision}"
        )
    return "\n".join(lines)
