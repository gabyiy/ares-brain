class GreetingIntent:
    def matches(self, text: str) -> bool:
        return text.lower().strip() in ("hello", "hello ares", "hi ares", "hey ares")

    def handle(self, text: str) -> str:
        return "Hello Gabi, I am here."
