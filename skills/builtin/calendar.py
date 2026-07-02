import re

from core.ToolAdapter import ToolRequest
from skills.base import Skill, SkillContext, SkillResponse


class CalendarSkill(Skill):
    name = "calendar"
    description = "Answers calendar requests through the offline mock calendar adapter."
    version = "0.1"
    intent_names = ("calendar",)
    run_before_intents = True
    triggers = (
        "calendar",
        "schedule",
        "do i have anything",
    )
    selection_keywords = (
        "calendar",
        "schedule",
        "events",
    )
    selection_priority = 0.1

    def can_handle(self, text: str) -> bool:
        return self._parse(text)["action"] == "list"

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        registry = getattr(context, "tool_adapter_registry", None)
        if not registry:
            return self._response(
                "Calendar adapter registry is not available.",
                error="missing_tool_adapter_registry",
            )

        parsed = self._parse_from_context(text, context)
        period = parsed["period"]
        adapter_name = parsed["adapter_name"]
        capability = parsed["capability"]
        request = ToolRequest(
            adapter_name=adapter_name,
            capability=capability,
            query=period,
            parameters={"period": period},
            raw_text=text,
        )

        adapter_response = registry.execute(request)
        if not adapter_response.success:
            return self._response(
                adapter_response.text or adapter_response.error_message,
                error=adapter_response.error_message,
                adapter_name=adapter_response.adapter_name,
                capability=adapter_response.capability,
            )

        return self._response(
            adapter_response.text,
            action="list",
            adapter_name=adapter_response.adapter_name,
            capability=adapter_response.capability,
            period=period,
            data=dict(adapter_response.data),
            adapter_metadata=dict(adapter_response.metadata),
        )

    def _parse_from_context(self, text: str, context: SkillContext):
        intent = context.metadata.get("intent") if context.metadata else None
        if getattr(intent, "intent_name", None) == "calendar":
            entities = dict(getattr(intent, "extracted_entities", {}) or {})
            return self._normalize_parse_result(
                {
                    "action": entities.get("action"),
                    "period": entities.get("period"),
                    "adapter_name": entities.get("adapter_name"),
                    "capability": entities.get("capability"),
                },
                fallback_text=text,
            )
        return self._parse(text)

    def _parse(self, text: str):
        return self._normalize_parse_result(
            {
                "action": "list" if _looks_like_calendar(text) else None,
                "period": _calendar_period(text),
                "adapter_name": "mock_calendar",
                "capability": "calendar.events",
            },
            fallback_text=text,
        )

    def _normalize_parse_result(self, parsed, fallback_text: str):
        period = parsed.get("period") or _calendar_period(fallback_text)
        return {
            "action": parsed.get("action") or ("list" if _looks_like_calendar(fallback_text) else None),
            "period": period,
            "adapter_name": parsed.get("adapter_name") or "mock_calendar",
            "capability": parsed.get("capability") or "calendar.events",
        }

    def _response(self, text: str, **metadata) -> SkillResponse:
        return SkillResponse(text=text, skill=self.name, metadata=metadata)


def _looks_like_calendar(text: str) -> bool:
    lowered = (text or "").lower()
    if re.search(r"\b(calendar|schedule)\b", lowered):
        return True
    return lowered.startswith("do i have anything")


def _calendar_period(text: str) -> str:
    lowered = (text or "").lower()
    if re.search(r"\btomorrow\b", lowered):
        return "tomorrow"
    return "today"
