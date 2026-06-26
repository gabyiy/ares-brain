from network.http_client import RateLimitedHttpClient
from network.cache import TTLCache
from network.providers.wikipedia import WikipediaProvider
from network.providers.weather import WeatherProvider


class IntentRouter:
    def __init__(self):
        self.http = RateLimitedHttpClient(min_delay=1.5, timeout=10)
        self.cache = TTLCache()
        self.wiki = WikipediaProvider(self.http, self.cache)
        self.weather = WeatherProvider(self.http, self.cache)

    def handle(self, text: str) -> str:
        q = (text or "").strip()
        low = q.lower()

        if not q:
            return "I did not hear anything."

        if low in ("hello ares", "hi ares", "hey ares"):
            return "Hello Gabi, I am here."

        if low in ("goodbye", "goodbye ares", "exit", "quit"):
            return "Goodbye Gabi."

        if low.startswith("weather "):
            city = q[len("weather "):].strip()
            if not city:
                return "Tell me the city."

            data = self.weather.current(city)
            return (
                f"Weather in {data.get('city')}, {data.get('country')}:\n"
                f"Temperature: {data.get('temperature')}°C\n"
                f"Humidity: {data.get('humidity')}%\n"
                f"Wind: {data.get('wind')} km/h"
            )

        if low.startswith("wiki "):
            title = q[5:].strip()
            if not title:
                return "Tell me what Wikipedia page to search."

            data = self.wiki.summary(title)
            return f"{data.get('title')}: {data.get('extract')}\n{data.get('url')}"

        if low.startswith("search wikipedia "):
            query = q[len("search wikipedia "):].strip()
            if not query:
                return "Tell me what to search on Wikipedia."

            results = self.wiki.search(query, limit=3)
            if not results:
                return "I found no Wikipedia results."

            lines = ["Top Wikipedia results:"]
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r.get('title')} - {r.get('url')}")
            return "\n".join(lines)

        return "I'm not sure how to answer that yet."
