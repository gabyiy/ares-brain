from network.http_client import RateLimitedHttpClient
from network.cache import TTLCache
from network.providers.wikipedia import WikipediaProvider
from network.providers.weather import WeatherProvider
from network.providers.news import NewsProvider


class IntentRouter:
    def __init__(self):
        self.http = RateLimitedHttpClient(min_delay=1.5, timeout=10)
        self.cache = TTLCache()
        self.wiki = WikipediaProvider(self.http, self.cache)
        self.weather = WeatherProvider(self.http, self.cache)
        self.news = NewsProvider(self.http, self.cache)

    def _extract_weather_city(self, text: str) -> str:
        low = text.lower()

        cities = [
            "madrid", "barcelona", "valencia", "sevilla", "malaga",
            "zaragoza", "bilbao", "london", "paris", "berlin",
            "bucharest", "rome", "lisbon", "new york", "tokyo"
        ]

        for city in cities:
            if city in low:
                return city.title()

        filler = [
            "weather", "temperature", "rain", "wind", "forecast",
            "today", "tomorrow", "next", "week", "now", "right",
            "how", "is", "the", "in", "for", "will", "it", "be",
            "outside", "like", "ares", "please", "tell", "me"
        ]

        words = []
        for word in low.replace("?", " ").replace(",", " ").split():
            if word not in filler and len(word) > 2:
                words.append(word)

        if words:
            return " ".join(words).title()

        return "Madrid"

    def _extract_weather_mode(self, text: str) -> str:
        low = text.lower()

        if "tomorrow" in low:
            return "tomorrow"

        if "next week" in low or "week" in low or "7 day" in low or "seven day" in low:
            return "week"

        if "now" in low or "right now" in low:
            return "now"

        return "today"

    def _is_weather_intent(self, low: str) -> bool:
        triggers = [
            "weather", "temperature", "rain", "wind",
            "hot", "cold", "forecast", "outside"
        ]
        return any(t in low for t in triggers)

    def _extract_news_query(self, text: str) -> str:
        low = text.lower()

        known_topics = {
            "rheinmetal": "rheinmetall",
            "rheinmetall": "rheinmetall",
            "rhm": "rheinmetall",
            "nvidia": "nvidia",
            "nvda": "nvidia",
            "tesla": "tesla",
            "apple": "apple",
            "microsoft": "microsoft",
            "openai": "openai",
            "google": "google",
            "meta": "meta",
            "palantir": "palantir",
            "lockheed": "lockheed martin",
            "leonardo": "leonardo defense",
            "thales": "thales defense",
            "safran": "safran defense",
            "defense": "defense",
            "ai": "artificial intelligence",
            "bitcoin": "bitcoin",
            "btc": "bitcoin",
        }

        for word, topic in known_topics.items():
            if word in low:
                return topic

        filler = [
            "what", "are", "is", "the", "latest", "news", "on", "about",
            "i", "saw", "that", "not", "doing", "well", "bad", "good",
            "tell", "me", "get", "give", "show", "please", "my", "friend",
            "happening", "happened", "with", "today", "now", "ares"
        ]

        words = []
        for word in low.replace("?", " ").replace(",", " ").split():
            if word not in filler and len(word) > 2:
                words.append(word)

        return " ".join(words).strip()

    def _is_news_intent(self, low: str) -> bool:
        triggers = ["news", "latest", "happening", "happened", "doing well", "not doing well"]
        return any(t in low for t in triggers)

    def _format_news(self, query: str, results: list) -> str:
        lines = [f"Latest news about {query}:"]

        for i, r in enumerate(results, 1):
            title = r.get("title") or "No title"
            source = r.get("domain") or "unknown source"
            date = r.get("date") or ""

            item = f"{i}. {title}\n   Source: {source}"
            if date:
                item += f"\n   Date: {date}"

            lines.append(item)

        return "\n\n".join(lines)

    def handle(self, text: str) -> str:
        q = (text or "").strip()
        low = q.lower()

        if not q:
            return "I did not hear anything."

        if low in ("hello ares", "hi ares", "hey ares"):
            return "Hello Gabi, I am here."

        if low in ("goodbye", "goodbye ares", "goodby", "goodby ares", "exit", "quit"):
            return "Goodbye Gabi."

        if self._is_weather_intent(low):
            city = self._extract_weather_city(q)
            mode = self._extract_weather_mode(q)
            return self.weather.format_forecast(city, mode)

        if low.startswith("news ") or self._is_news_intent(low):
            if low.startswith("news "):
                query = q[len("news "):].strip()
            else:
                query = self._extract_news_query(q)

            if not query:
                return "Tell me what news topic to search."

            results = self.news.search(query, limit=5)
            if not results:
                return f"I found no recent news for {query}."

            return self._format_news(query, results)

        if low.startswith("wiki "):
            title = q[5:].strip()
            data = self.wiki.summary(title)
            return f"{data.get('title')}: {data.get('extract')}\n{data.get('url')}"

        if low.startswith("search wikipedia "):
            query = q[len("search wikipedia "):].strip()
            results = self.wiki.search(query, limit=3)

            if not results:
                return "I found no Wikipedia results."

            lines = ["Top Wikipedia results:"]
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r.get('title')} - {r.get('url')}")
            return "\n".join(lines)

        return "I'm not sure how to answer that yet."
