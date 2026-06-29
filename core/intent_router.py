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


class IntentRouter:
    def __init__(self):
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

        if not q:
            return "I did not hear anything."

        for intent in self.intents:
            if intent.matches(q):
                return intent.handle(q)

        return "I'm not sure how to answer that yet."
