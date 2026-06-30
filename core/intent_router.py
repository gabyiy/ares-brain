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
        self._publish_many(
            ("user_message_received", "input.received"),
            {"text": q},
        )

        if not q:
            response = "I did not hear anything."
            self.events.publish("intent.empty", {}, source="intent_router")
            self._publish_response(response=response, text=q)
            return response

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

        skill_response = self._handle_skill(q)
        if skill_response:
            return skill_response

        self.events.publish("intent.unmatched", {"text": q}, source="intent_router")
        response = "I'm not sure how to answer that yet."
        self._publish_response(response=response, text=q)
        return response

    def _publish_many(self, event_names, payload):
        for event_name in event_names:
            self.events.publish(event_name, dict(payload), source="intent_router")

    def _publish_response(self, response: str, text: str, intent=None):
        payload = {
            "intent": intent,
            "response": response,
            "text": text,
        }
        self._publish_many(("response_generated", "intent.response"), payload)

    def _handle_skill(self, text: str):
        if not self.skill_manager:
            return None

        response = self.skill_manager.handle(text)
        if not response:
            return None

        skill_name = getattr(response, "skill", "skill")
        response_text = getattr(response, "text", str(response))
        self._publish_many(
            ("intent_detected", "intent.matched"),
            {"intent": skill_name, "kind": "skill", "text": text},
        )
        self._publish_response(response=response_text, text=text, intent=skill_name)
        return response_text
