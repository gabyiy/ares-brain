import os


class StockProvider:
    URL = "https://www.alphavantage.co/query"

    def __init__(self, http, cache):
        self.http = http
        self.cache = cache
        self.api_key = self._load_api_key()

    def _load_api_key(self):
        env_path = ".env"

        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("ALPHA_VANTAGE_API_KEY="):
                        return line.strip().split("=", 1)[1]

        return ""

    def quote(self, symbol):
        symbol = symbol.strip().upper()

        if not self.api_key:
            return {"error": "Missing Alpha Vantage API key."}

        key = f"stock:alpha:{symbol}"
        cached = self.cache.get(key)
        if cached:
            return cached

        data = self.http.get_json(
            self.URL,
            params={
                "function": "GLOBAL_QUOTE",
                "symbol": symbol,
                "apikey": self.api_key,
            },
            service_delay=1.0,
        )

        quote = data.get("Global Quote", {})

        if not quote:
            return {"error": f"No stock data found for {symbol}."}

        self.cache.set(key, quote, ttl_seconds=300)
        return quote

    def format_quote(self, symbol):
        data = self.quote(symbol)

        if data.get("error"):
            return data["error"]

        price = data.get("05. price")
        change = data.get("09. change")
        percent = data.get("10. change percent")

        return (
            f"Stock: {symbol}\n"
            f"Price: {price}\n"
            f"Change: {change}\n"
            f"Percent: {percent}"
        )
