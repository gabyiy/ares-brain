import re

from core.ToolAdapter import ToolRequest
from skills.base import Skill, SkillContext, SkillResponse


class MarketSkill(Skill):
    name = "market"
    description = "Answers market quote requests through the offline mock market adapter."
    version = "0.1"
    intent_names = ("market",)
    run_before_intents = True
    triggers = (
        "stock",
        "market price",
        "market quote",
    )
    selection_keywords = (
        "stock",
        "market",
        "quote",
        "price",
    )
    selection_priority = 0.1

    def can_handle(self, text: str) -> bool:
        return self._parse(text)["action"] == "quote"

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        registry = getattr(context, "tool_adapter_registry", None)
        if not registry:
            return self._response(
                "Market adapter registry is not available.",
                error="missing_tool_adapter_registry",
            )

        parsed = self._parse_from_context(text, context)
        symbol = parsed["symbol"]
        adapter_name = parsed["adapter_name"]
        capability = parsed["capability"]
        request = ToolRequest(
            adapter_name=adapter_name,
            capability=capability,
            query=symbol,
            parameters={"symbol": symbol},
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
            action="quote",
            adapter_name=adapter_response.adapter_name,
            capability=adapter_response.capability,
            symbol=symbol,
            data=dict(adapter_response.data),
            adapter_metadata=dict(adapter_response.metadata),
        )

    def _parse_from_context(self, text: str, context: SkillContext):
        intent = context.metadata.get("intent") if context.metadata else None
        if getattr(intent, "intent_name", None) == "market":
            entities = dict(getattr(intent, "extracted_entities", {}) or {})
            return self._normalize_parse_result(
                {
                    "action": entities.get("action"),
                    "symbol": entities.get("symbol"),
                    "adapter_name": entities.get("adapter_name"),
                    "capability": entities.get("capability"),
                },
                fallback_text=text,
            )
        return self._parse(text)

    def _parse(self, text: str):
        return self._normalize_parse_result(
            {
                "action": "quote" if _looks_like_market(text) else None,
                "symbol": _market_symbol(text),
                "adapter_name": "mock_market",
                "capability": "market.quote",
            },
            fallback_text=text,
        )

    def _normalize_parse_result(self, parsed, fallback_text: str):
        symbol = parsed.get("symbol") or _market_symbol(fallback_text)
        return {
            "action": parsed.get("action") or ("quote" if _looks_like_market(fallback_text) else None),
            "symbol": symbol,
            "adapter_name": parsed.get("adapter_name") or "mock_market",
            "capability": parsed.get("capability") or "market.quote",
        }

    def _response(self, text: str, **metadata) -> SkillResponse:
        return SkillResponse(text=text, skill=self.name, metadata=metadata)


def _looks_like_market(text: str) -> bool:
    return bool(re.search(r"\b(stock|market)\b", text or "", flags=re.IGNORECASE))


def _market_symbol(text: str) -> str:
    clean_text = (text or "").strip().strip(" ?!.:-").strip()
    patterns = (
        r"^stock\s+(.+)$",
        r"^market\s+price\s+for\s+(.+)$",
        r"^market\s+quote\s+for\s+(.+)$",
        r"^(.+?)\s+stock$",
    )
    for pattern in patterns:
        match = re.match(pattern, clean_text, flags=re.IGNORECASE)
        if match:
            return _normalize_market_symbol(match.group(1))
    return ""


def _normalize_market_symbol(text: str) -> str:
    symbol = (text or "").strip().strip(" ?!.:-").strip()
    symbol = re.sub(r"^(?:the|a|an)\s+", "", symbol, flags=re.IGNORECASE)
    symbol = symbol.replace("$", "").strip()
    return symbol.upper()
