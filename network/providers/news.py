import xml.etree.ElementTree as ET
import urllib.parse

from network.http_client import RateLimitedHttpClient
from network.cache import TTLCache


class NewsProvider:
    BASE_URL = "https://news.google.com/rss/search"

    def __init__(self, http: RateLimitedHttpClient, cache: TTLCache):
        self.http = http
        self.cache = cache

    def search(self, query: str, limit: int = 5):
        query = query.strip()
        if not query:
            raise ValueError("News query is required")

        limit = max(1, min(int(limit), 10))
        key = f"news:google:rss:{query.lower()}:{limit}"

        cached = self.cache.get(key)
        if cached:
            return cached

        params = {
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }

        url = self.BASE_URL + "?" + urllib.parse.urlencode(params)

        response = self.http.get(
            url,
            service_delay=2.0,
        )

        root = ET.fromstring(response.text)
        items = root.findall("./channel/item")

        results = []

        for item in items[:limit]:
            title = item.findtext("title") or "No title"
            link = item.findtext("link") or ""
            date = item.findtext("pubDate") or ""

            domain = "Google News"
            if " - " in title:
                title_part, source_part = title.rsplit(" - ", 1)
                title = title_part.strip()
                domain = source_part.strip()

            results.append({
                "title": title,
                "url": link,
                "domain": domain,
                "date": date,
            })

        self.cache.set(key, results, ttl_seconds=60 * 15)
        return results
