import urllib.parse
from typing import Any, Dict, List, Optional

from network.http_client import RateLimitedHttpClient
from network.cache import TTLCache


class WikipediaProvider:
    """
    Wikipedia integration:
      - Summary uses Wikipedia REST API (rest_v1)
      - Search uses MediaWiki API (w/api.php) because REST v1 has no /search/title
    """

    REST_BASE = "https://en.wikipedia.org/api/rest_v1"
    MW_BASE = "https://en.wikipedia.org/w/api.php"

    def __init__(self, http: RateLimitedHttpClient, cache: TTLCache):
        self.http = http
        self.cache = cache

        # Be polite. Wikipedia will throttle/ban abusive clients.
        self.service_delay = 1.5

    def summary(self, title: str) -> Dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("Empty title")

        key = f"wiki:summary:{title.lower()}"
        cached = self.cache.get(key)
        if cached:
            return cached

        safe_title = urllib.parse.quote(title.replace(" ", "_"))
        url = f"{self.REST_BASE}/page/summary/{safe_title}"

        data = self.http.get_json(url, service_delay=self.service_delay)

        result = {
            "title": data.get("title"),
            "extract": data.get("extract"),
            "url": data.get("content_urls", {})
                     .get("desktop", {})
                     .get("page"),
        }

        self.cache.set(key, result, ttl_seconds=60 * 30)  # 30 min
        return result

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            raise ValueError("Empty query")

        limit = int(limit)
        if limit < 1:
            limit = 1
        if limit > 10:
            limit = 10  # keep it reasonable

        key = f"wiki:search:{query.lower()}:{limit}"
        cached = self.cache.get(key)
        if cached:
            return cached

        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
            "format": "json",
            "utf8": 1,
        }

        data = self.http.get_json(self.MW_BASE, params=params, service_delay=self.service_delay)

        items: List[Dict[str, Any]] = []
        for item in data.get("query", {}).get("search", []):
            title = item.get("title")
            url = None
            if title:
                url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"

            items.append({
                "title": title,
                "snippet": item.get("snippet"),
                "url": url,
            })

        self.cache.set(key, items, ttl_seconds=60 * 30)  # 30 min
        return items
