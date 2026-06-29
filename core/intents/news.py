class NewsIntent:
    def __init__(self, provider):
        self.provider = provider

    def matches(self, text: str) -> bool:
        low = text.lower()
        return low.startswith("news ") or any(word in low for word in [
            "latest news",
            "what happened",
            "what is happening",
            "doing well",
            "not doing well",
            "market news",
        ])

    def query(self, text: str) -> str:
        low = text.lower()

        if low.startswith("news "):
            return text[5:].strip()

        topics = {
            "rheinmetal": "rheinmetall",
            "rheinmetall": "rheinmetall",
            "rhm": "rheinmetall",
            "nvidia": "nvidia",
            "nvda": "nvidia",
            "tesla": "tesla",
            "bitcoin": "bitcoin",
            "btc": "bitcoin",
            "defense": "defense",
            "ai": "artificial intelligence",
        }

        for key, value in topics.items():
            if key in low:
                return value

        filler = [
            "what", "are", "is", "the", "latest", "news", "on", "about",
            "tell", "me", "my", "friend", "happening", "happened",
            "with", "i", "heard", "saw", "that", "doing", "well", "not"
        ]

        words = [
            word for word in low.replace("?", " ").replace(",", " ").split()
            if word not in filler and len(word) > 2
        ]

        return " ".join(words).strip()

    def handle(self, text: str) -> str:
        query = self.query(text)

        if not query:
            return "Tell me what news topic to search."

        results = self.provider.search(query, limit=5)

        if not results:
            return f"I found no recent news for {query}."

        lines = [f"Latest news about {query}:"]

        for index, result in enumerate(results, 1):
            title = result.get("title") or "No title"
            source = result.get("domain") or "unknown source"
            date = result.get("date") or ""

            item = f"{index}. {title}\n   Source: {source}"

            if date:
                item += f"\n   Date: {date}"

            lines.append(item)

        return "\n\n".join(lines)
