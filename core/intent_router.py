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
    def __init__(self, event_bus=None):
        self.events = event_bus or get_global_bus()

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
        self.events.publish("input.received", {"text": q}, source="intent_router")

        if not q:
            self.events.publish("intent.empty", {}, source="intent_router")
            return "I did not hear anything."

        for intent in self.intents:
            if intent.matches(q):
                intent_name = intent.__class__.__name__
                self.events.publish(
                    "intent.matched",
                    {"intent": intent_name, "text": q},
                    source="intent_router",
                )
                response = intent.handle(q)
                self.events.publish(
                    "intent.response",
                    {"intent": intent_name, "response": response},
                    source="intent_router",
                )
                return response

        self.events.publish("intent.unmatched", {"text": q}, source="intent_router")
        return "I'm not sure how to answer that yet."
