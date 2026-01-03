import os
import json

from speech.voice_engine import speak


EMOTION_FILE = os.path.expanduser("~/ARES_BRAIN/data/emotion_state.json")


def level(x: float) -> str:
    """Turn a 0–1 value into a word."""
    if x >= 0.8:
        return "very high"
    if x >= 0.6:
        return "high"
    if x >= 0.4:
        return "medium"
    if x >= 0.2:
        return "low"
    return "very low"


def get_emotion_summary() -> str:
    """Read emotion_state.json and create a spoken summary."""
    if not os.path.exists(EMOTION_FILE):
        return "I don't have any emotional state saved yet."

    try:
        with open(EMOTION_FILE, "r") as f:
            state = json.load(f)
    except Exception:
        return "I cannot read my emotion state file right now."

    mood = state.get("mood", "neutral")
    energy = float(state.get("energy", 0.5))
    confidence = float(state.get("confidence", 0.5))

    text = (
        f"I feel {mood}. "
        f"My energy is {level(energy)}, "
        f"and my confidence is {level(confidence)}."
    )
    return text


if __name__ == "__main__":
    summary = get_emotion_summary()
    print(summary)      # so you can also see it in the terminal
    speak(summary)
