from audio.mic_listener import handle_intent
from speech.emotional_voice import speak
from online.web_search import search_and_summarise


def _looks_like_web_question(lower: str) -> bool:
    """
    Decide if this text should go to web search.
    We allow some common typos (wheather, wheater) and generic words.
    """
    web_keywords = [
        "score",
        "result",
        "search",
        "stock",
        "price",
        "forecast",
        "temperature",
        "rain",
        "sunny",
    ]

    if any(w in lower for w in web_keywords):
        return True

    # Handle common 'weather' typos
    if "weather" in lower or "wheather" in lower or "wheater" in lower:
        return True

    return False


def main():
    print("ARES text chat. Type 'hello ares' to start talking.")
    print("Type 'goodbye' or 'exit' to finish.\n")

    active = False

    while True:
        user = input("You: ").strip()
        lower = user.lower()

        # Exit command
        if lower in ["exit", "quit", "goodbye"]:
            print("ARES: Goodbye Gabi.")
            speak("Goodbye Gabi.")
            break

        # Wake-up phrase
        if not active and lower in ["hello ares", "hi ares", "ares hello", "ares hi"]:
            active = True
            reply = "Hello Gabi, I am here."
            print(f"ARES: {reply}")
            speak(reply)
            continue

        if not active:
            print("ARES is sleeping. Say 'hello ares' to wake him.")
            continue

        # Web vs local intent
        if _looks_like_web_question(lower):
            print(f"[TEXT] Processing web question: '{lower}'")
            answer = search_and_summarise(lower)
            print(f"ARES: {answer}")
            speak(answer)
        else:
            print(f"[TEXT] Processing local intent: '{lower}'")
            reply = handle_intent(lower, from_voice=False)
            print(f"ARES: {reply}")
            speak(reply)


if __name__ == "__main__":
    main()
