class GoodbyeIntent:
    def matches(self, text: str) -> bool:
        return text.lower().strip() in (
            "goodbye",
            "goodbye ares",
            "goodby",
            "goodby ares",
            "exit",
            "quit",
        )

    def handle(self, text: str) -> str:
        return "Goodbye Gabi."
