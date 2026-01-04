from network.http_client import RateLimitedHttpClient
from network.cache import TTLCache
from network.providers.wikipedia import WikipediaProvider


def main():
    http = RateLimitedHttpClient(min_delay=2.0, timeout=8)
    cache = TTLCache()

    wiki = WikipediaProvider(http, cache)

    print("=== SUMMARY: Raspberry Pi ===")
    print(wiki.summary("Raspberry Pi"))

    print("\n=== SEARCH: Apollo program (top 3) ===")
    print(wiki.search("Apollo program", limit=3))


if __name__ == "__main__":
    main()
