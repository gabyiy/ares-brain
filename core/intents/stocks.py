class StockIntent:

    SYMBOLS = {
        "nvidia": "NVDA",
        "apple": "AAPL",
        "microsoft": "MSFT",
        "tesla": "TSLA",
        "amazon": "AMZN",
        "google": "GOOG",
        "alphabet": "GOOG",
        "meta": "META",
        "amd": "AMD",
        "intel": "INTC",
        "rheinmetall": "RHM.DE",
        "safran": "SAF.PA",
        "leonardo": "LDO.MI",
        "thales": "HO.PA",
        "lockheed": "LMT",
        "boeing": "BA",
    }

    def __init__(self, provider):
        self.provider = provider

    def matches(self, text):
        low = text.lower()

        return any(
            word in low
            for word in (
                "stock",
                "share",
                "price",
                "trading",
                "market",
                "worth",
            )
        ) or any(company in low for company in self.SYMBOLS)

    def handle(self, text):
        low = text.lower()

        for company, symbol in self.SYMBOLS.items():
            if company in low:
                return self.provider.format_quote(symbol)

        return "Which company do you want to check?"
