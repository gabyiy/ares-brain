from dataclasses import dataclass, field
from typing import Any, Dict


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
            "extracted_entities": dict(self.extracted_entities),
            "raw_text": self.raw_text,
        }
