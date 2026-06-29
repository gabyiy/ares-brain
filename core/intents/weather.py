class WeatherIntent:
    def __init__(self, provider):
        self.provider = provider

    def matches(self, text: str) -> bool:
        low = text.lower()
        return any(word in low for word in [
            "weather",
            "temperature",
            "rain",
            "wind",
            "forecast",
            "hot outside",
            "cold outside",
        ])

    def city(self, text: str) -> str:
        low = text.lower()

        cities = [
            "madrid",
            "barcelona",
            "valencia",
            "london",
            "paris",
            "berlin",
            "bucharest",
            "rome",
            "lisbon",
        ]

        for city in cities:
            if city in low:
                return city.title()

        return "Madrid"

    def mode(self, text: str) -> str:
        low = text.lower()

        if "tomorrow" in low or "tomorow" in low or "tommorow" in low:
            return "tomorrow"

        if "next week" in low or "week" in low:
            return "week"

        if "now" in low or "right now" in low:
            return "now"

        return "today"

    def handle(self, text: str) -> str:
        return self.provider.format_forecast(
            self.city(text),
            self.mode(text),
        )
