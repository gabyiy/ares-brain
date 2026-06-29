from network.http_client import RateLimitedHttpClient
from network.cache import TTLCache
from network.providers.weather import WeatherProvider
from network.providers.news import NewsProvider

from core.intents.greeting import GreetingIntent
from core.intents.goodbye import GoodbyeIntent
from core.intents.weather import WeatherIntent
from core.intents.news import NewsIntent


class IntentRouter:
    def __init__(self):
        self.http = RateLimitedHttpClient(min_delay=1.5, timeout=10)
        self.cache = TTLCache()

        self.weather_provider = WeatherProvider(self.http, self.cache)
        self.news_provider = NewsProvider(self.http, self.cache)

        self.intents = [
            GreetingIntent(),
            GoodbyeIntent(),
            WeatherIntent(self.weather_provider),
            NewsIntent(self.news_provider),
        ]

    def handle(self, text: str) -> str:
        q = (text or "").strip()

        if not q:
            return "I did not hear anything."

        for intent in self.intents:
            if intent.matches(q):
                return intent.handle(q)

        return "I'm not sure how to answer that yet."
