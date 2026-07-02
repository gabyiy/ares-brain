import re

from core.ToolAdapter import ToolRequest
from skills.base import Skill, SkillContext, SkillResponse


class WeatherSkill(Skill):
    name = "weather"
    description = "Answers weather requests through the offline mock weather adapter."
    version = "0.1"
    intent_names = ("weather",)
    run_before_intents = True
    triggers = (
        "weather",
        "weather today",
        "weather tomorrow",
        "weather in",
        "forecast",
    )
    selection_keywords = (
        "weather",
        "forecast",
    )
    selection_priority = 0.1

    def can_handle(self, text: str) -> bool:
        return self._parse(text)["action"] == "weather"

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        registry = getattr(context, "tool_adapter_registry", None)
        if not registry:
            return self._response(
                "Weather adapter registry is not available.",
                error="missing_tool_adapter_registry",
            )

        parsed = self._parse_from_context(text, context)
        location = parsed["location"]
        period = parsed["period"]
        capability = parsed["capability"]
        adapter_name = parsed["adapter_name"]
        request = ToolRequest(
            adapter_name=adapter_name,
            capability=capability,
            query=location,
            parameters={
                "location": location,
                "period": period,
            },
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
            action="weather",
            adapter_name=adapter_response.adapter_name,
            capability=adapter_response.capability,
            location=location,
            period=period,
            data=dict(adapter_response.data),
            adapter_metadata=dict(adapter_response.metadata),
        )

    def _parse_from_context(self, text: str, context: SkillContext):
        intent = context.metadata.get("intent") if context.metadata else None
        if getattr(intent, "intent_name", None) == "weather":
            entities = dict(getattr(intent, "extracted_entities", {}) or {})
            return self._normalize_parse_result(
                {
                    "action": entities.get("action"),
                    "location": entities.get("location"),
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
                "action": "weather" if _looks_like_weather(text) else None,
                "location": _weather_location(text),
                "period": _weather_period(text),
                "adapter_name": "mock_weather",
                "capability": None,
            },
            fallback_text=text,
        )

    def _normalize_parse_result(self, parsed, fallback_text: str):
        period = parsed.get("period") or _weather_period(fallback_text)
        location = parsed.get("location") or _weather_location(fallback_text)
        capability = parsed.get("capability") or (
            "weather.forecast" if period == "tomorrow" else "weather.current"
        )
        return {
            "action": parsed.get("action") or ("weather" if _looks_like_weather(fallback_text) else None),
            "location": location,
            "period": period,
            "adapter_name": parsed.get("adapter_name") or "mock_weather",
            "capability": capability,
        }

    def _response(self, text: str, **metadata) -> SkillResponse:
        return SkillResponse(text=text, skill=self.name, metadata=metadata)


def _looks_like_weather(text: str) -> bool:
    return bool(re.search(r"\b(weather|forecast)\b", text or "", flags=re.IGNORECASE))


def _weather_period(text: str) -> str:
    lowered = (text or "").lower()
    if re.search(r"\btomorrow\b", lowered):
        return "tomorrow"
    return "today"


def _weather_location(text: str) -> str:
    match = re.search(r"\bin\s+(.+)$", text or "", flags=re.IGNORECASE)
    if not match:
        return "local"
    location = match.group(1).strip().strip(" ?!.:-").strip()
    return location or "local"
