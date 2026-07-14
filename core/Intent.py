from dataclasses import dataclass, field
from typing import Any, Dict


SENSITIVE_ENTITY_FIELDS = "_sensitive_fields"


def redact_sensitive_entities(entities: Dict[str, Any]) -> Dict[str, Any]:
    values = dict(entities or {})
    sensitive = {
        str(name)
        for name in list(values.pop(SENSITIVE_ENTITY_FIELDS, []) or [])
        if str(name)
    }
    for name in sensitive:
        if name in values:
            values[name] = "[REDACTED]"
    return values


@dataclass(frozen=True)
class Intent:
    intent_name: str
    confidence: float
    extracted_entities: Dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent_name": self.intent_name,
            "confidence": self.confidence,
            "extracted_entities": redact_sensitive_entities(self.extracted_entities),
            "raw_text": self.raw_text,
        }
