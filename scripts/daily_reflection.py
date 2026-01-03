#!/usr/bin/env python3
import os
import sys
import json
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)      # ~/ARES_BRAIN
sys.path.append(BASE_DIR)

from memory.memory_manager import add_memory
from emotion.emotion_manager import apply_event, describe_emotion
from memory.conversation_log import LOG_DIR as CONV_DIR


def load_conversations_for_day(day_str: str):
    path = os.path.join(CONV_DIR, f"{day_str}.jsonl")
    if not os.path.exists(path):
        return []

    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                print("[DailyReflection] Skipping bad line in", day_str)
    return entries


def analyze_day(entries):
    """
    Very simple sentiment + activity analysis.
    Returns (summary_text, importance, sentiment_score).
    sentiment_score: -1.0 (very negative) to +1.0 (very positive)
    """
    if not entries:
        return ("Today I had no conversations with Gabi.", 0.5, 0.0)

    user_name = "gabi"
    total_msgs = len(entries)
    total_user = sum(1 for e in entries if e.get("speaker") == user_name)
    total_ares = sum(1 for e in entries if e.get("speaker") == "ares")

    positive_words = ["happy", "excited", "good", "proud", "love", "fun"]
    negative_words = ["sad", "tired", "worried", "anxious", "angry", "frustrated"]

    pos = 0
    neg = 0

    for e in entries:
        text = e.get("text", "").lower()
        if any(w in text for w in positive_words):
            pos += 1
        if any(w in text for w in negative_words):
            neg += 1

    if pos + neg == 0:
        sentiment = 0.0
    else:
        sentiment = (pos - neg) / float(pos + neg)

    if sentiment > 0.4:
        feeling = "Overall the day felt positive and light."
    elif sentiment < -0.4:
        feeling = "Overall the day felt heavy and a bit difficult."
    else:
        feeling = "Overall the day felt mixed or mostly neutral."

    summary = (
        f"Today I exchanged {total_msgs} messages with Gabi "
        f"({total_user} from Gabi, {total_ares} from me). "
        f"{feeling}"
    )

    importance = 0.85 if abs(sentiment) > 0.4 else 0.7
    return summary, importance, sentiment


def update_mood_from_sentiment(sentiment: float):
    """
    Map sentiment to an emotion event.
    """
    if sentiment > 0.4:
        event = "good_day"
        intensity = 0.8
    elif sentiment < -0.4:
        event = "hard_day"
        intensity = 0.8
    else:
        event = "normal_day"
        intensity = 0.4

    print(f"[DailyReflection] Applying mood event: {event} (intensity={intensity})")
    apply_event(event, intensity=intensity)
    print("[DailyReflection] New emotional description:")
    print(describe_emotion())


def main():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    print(f"[DailyReflection] Running for {today}")

    entries = load_conversations_for_day(today)
    summary, importance, sentiment = analyze_day(entries)

    print("[DailyReflection] Summary:")
    print(" ", summary)
    print(f"[DailyReflection] Sentiment score: {sentiment:.2f}")

    # Save as long-term memory
    add_memory(
        category="daily_reflection",
        content=summary,
        importance=importance,
        tags=["reflection", "daily"]
    )
    print("[DailyReflection] Saved daily reflection to long-term memory.")

    # Update mood
    update_mood_from_sentiment(sentiment)


if __name__ == "__main__":
    main()
