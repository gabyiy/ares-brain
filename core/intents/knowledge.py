class KnowledgeIntent:
    def __init__(self, provider):
        self.provider = provider

    def matches(self, text: str) -> bool:
        low = text.lower()
        return any(trigger in low for trigger in [
            "what is",
            "who is",
            "who was",
            "how hot",
            "how big",
            "how many",
            "tell me about",
            "explain",
        ])

    def topic(self, text: str) -> str:
        low = text.lower()

        if "sun" in low and ("hot" in low or "temperature" in low or "degree" in low):
            return "how hot is the sun"

        known_topics = {
            "sun": "Sun",
            "moon": "Moon",
            "mars": "Mars",
            "earth": "Earth",
            "artificial intelligence": "Artificial intelligence",
            "ai": "Artificial intelligence",
            "nikola tesla": "Nikola Tesla",
            "rheinmetall": "Rheinmetall",
        }

        for key, value in known_topics.items():
            if key in low:
                return value

        filler = [
            "what", "is", "are", "who", "was", "how", "many",
            "hot", "big", "tell", "me", "about", "explain",
            "the", "a", "an", "does", "get",
        ]

        words = [
            word for word in low.replace("?", " ").replace(",", " ").split()
            if word not in filler and len(word) > 2
        ]

        return " ".join(words).title()

    def handle(self, text: str) -> str:
        return self.provider.answer(self.topic(text))
