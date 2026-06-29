class KnowledgeProvider:
    def __init__(self, wiki_provider):
        self.wiki = wiki_provider

    def answer(self, topic: str) -> str:
        topic = topic.strip()

        if not topic:
            return "Tell me what you want to know."

        low = topic.lower()

        if "sun" in low and ("hot" in low or "temperature" in low or "degree" in low):
            return "The Sun is about 5,500°C at the surface and around 15 million°C at the core."

        data = self.wiki.summary(topic)
        return f"{data.get('title')}: {data.get('extract')}\n{data.get('url')}"
