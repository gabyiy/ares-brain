from network.http_client import RateLimitedHttpClient
from network.cache import TTLCache
from network.providers.weather import WeatherProvider
from network.providers.news import NewsProvider
from network.providers.wikipedia import WikipediaProvider
from network.providers.knowledge import KnowledgeProvider
from network.providers.stocks import StockProvider

from core.intents.greeting import GreetingIntent
from core.intents.goodbye import GoodbyeIntent
from core.intents.weather import WeatherIntent
from core.intents.news import NewsIntent
from core.intents.knowledge import KnowledgeIntent
from core.intents.stocks import StockIntent
from events import get_global_bus
from core.OwnerMemory import owner_memory_uses_explicit_store, parse_owner_memory_command


class IntentRouter:
    def __init__(self, event_bus=None, skill_manager=None):
        self.events = event_bus or get_global_bus()
        self.skill_manager = skill_manager

        self.http = RateLimitedHttpClient(min_delay=1.5, timeout=10)
        self.cache = TTLCache()

        self.weather_provider = WeatherProvider(self.http, self.cache)
        self.news_provider = NewsProvider(self.http, self.cache)
        self.wikipedia_provider = WikipediaProvider(self.http, self.cache)
        self.knowledge_provider = KnowledgeProvider(self.wikipedia_provider)
        self.stock_provider = StockProvider(self.http, self.cache)

        self.intents = [
            GreetingIntent(),
            GoodbyeIntent(),
            WeatherIntent(self.weather_provider),
            NewsIntent(self.news_provider),
            KnowledgeIntent(self.knowledge_provider),
            StockIntent(self.stock_provider),
        ]

    def handle(self, text: str) -> str:
        q = (text or "").strip()
        redact_operational = self._redact_owner_memory_events(q)
        self._publish_many(
            ("user_message_received", "input.received"),
            self._safe_text_payload(q, redact_operational),
        )

        if not q:
            response = "I did not hear anything."
            self.events.publish("intent.empty", {}, source="intent_router")
            self._publish_response(response=response, text=q)
            return response

        priority_skill_response = self._handle_skill(
            q,
            run_before_intents=True,
            redact_operational=redact_operational,
        )
        if priority_skill_response:
            return priority_skill_response

        for intent in self.intents:
            if intent.matches(q):
                intent_name = intent.__class__.__name__
                self._publish_many(
                    ("intent_detected", "intent.matched"),
                    {"intent": intent_name, "text": q},
                )
                response = intent.handle(q)
                self._publish_response(response=response, text=q, intent=intent_name)
                return response

        skill_response = self._handle_skill(
            q,
            run_before_intents=False,
            redact_operational=redact_operational,
        )
        if skill_response:
            return skill_response

        self.events.publish("intent.unmatched", {"text": q}, source="intent_router")
        response = "I'm not sure how to answer that yet."
        self._publish_response(response=response, text=q)
        return response

    def _publish_many(self, event_names, payload):
        for event_name in event_names:
            self.events.publish(event_name, dict(payload), source="intent_router")

    def _publish_response(self, response: str, text: str, intent=None, redact=False):
        payload = {
            "intent": intent,
            "response": "[REDACTED]" if redact else response,
            **self._safe_text_payload(text, redact),
        }
        self._publish_many(("response_generated", "intent.response"), payload)

    def _handle_skill(
        self,
        text: str,
        run_before_intents=False,
        redact_operational=False,
    ):
        if not self.skill_manager:
            return None

        response = self.skill_manager.handle(text, run_before_intents=run_before_intents)
        if not response:
            return None

        skill_name = getattr(response, "skill", "skill")
        response_text = getattr(response, "text", str(response))
        self._publish_many(
            ("intent_detected", "intent.matched"),
            {
                "intent": skill_name,
                "kind": "skill",
                **self._safe_text_payload(text, redact_operational),
            },
        )
        self._publish_response(
            response=response_text,
            text=text,
            intent=skill_name,
            redact=redact_operational,
        )
        return response_text

    def _redact_owner_memory_events(self, text: str) -> bool:
        return owner_memory_uses_explicit_store(parse_owner_memory_command(text))

    @staticmethod
    def _safe_text_payload(text: str, redact: bool):
        if not redact:
            return {"text": text}
        return {
            "text": "[REDACTED]",
            "text_length": len(text),
            "value_redacted": True,
        }
