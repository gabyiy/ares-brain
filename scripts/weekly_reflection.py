#!/usr/bin/env python3
import os
import sys
import json
from datetime import datetime, timedelta

# --- Project root on path ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)          # ~/ARES_BRAIN
sys.path.append(BASE_DIR)

from memory.memory_manager import add_memory
from personality.traits_manager import load_traits, save_traits, adjust_trait

CONV_DIR = os.path.join(BASE_DIR, "data", "conversations")


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
                print("[WeeklyReflection] Skipping bad line in", day_str)
    return entries


def analyze_week(all_days):
    """
    all_days: list of (day_str, entries)
    Returns (summary, importance, stats_dict) or None.
    stats_dict is used later for trait evolution.
    """
    if not all_days:
        return None

    user_name = "gabi"
    total_msgs = 0
    total_user = 0
    total_ares = 0
    days_with_chat = 0

    weekly_topics = set()
    sentiment_score = 0

    positive_words = ["happy", "excited", "good", "proud", "love"]
    negative_words = ["sad", "tired", "worried", "anxious", "angry", "frustrated"]

    topic_keywords = {
        "robot": "our robot and AI projects",
        "ares": "our robot and AI projects",
        "gym": "training, health, and the gym",
        "training": "training, health, and the gym",
        "stock": "investing and the stock market",
        "market": "investing and the stock market",
        "money": "investing and the stock market",
        "work": "work and daily life",
        "job": "work and daily life",
        "ai": "artificial intelligence and technology",
        "lonely": "feelings of loneliness",
        "relationship": "relationships and emotions",
    }

    lonely_hits = 0

    for day_str, entries in all_days:
        if not entries:
            continue
        days_with_chat += 1
        total_msgs += len(entries)
        total_user += sum(1 for e in entries if e.get("speaker", "").lower() == user_name)
        total_ares += sum(1 for e in entries if e.get("speaker", "").lower() == "ares")

        for e in entries:
            if e.get("speaker", "").lower() != user_name:
                continue
            text = e.get("text", "").lower()

            for w in positive_words:
                if w in text:
                    sentiment_score += 1
            for w in negative_words:
                if w in text:
                    sentiment_score -= 1

            if "lonely" in text:
                lonely_hits += 1

            for kw, topic in topic_keywords.items():
                if kw in text:
                    weekly_topics.add(topic)

    if days_with_chat == 0:
        return None

    # Mood for the whole week
    if sentiment_score >= 4:
        mood_desc = "mostly positive and hopeful"
    elif sentiment_score <= -4:
        mood_desc = "more difficult, with heavier emotions"
    else:
        mood_desc = "mixed, with both light and heavy moments"

    if weekly_topics:
        topics_str = ", ".join(sorted(weekly_topics))
    else:
        topics_str = "many different everyday things"

    end = datetime.utcnow().date()
    start = end - timedelta(days=6)

    summary = (
        f"In the last week ({start} to {end}), I talked with Gabi on {days_with_chat} days, "
        f"exchanging {total_msgs} messages in total "
        f"({total_user} from Gabi, {total_ares} from me). "
        f"Emotionally, this week felt {mood_desc}. "
        f"We often talked about {topics_str}. "
        f"I feel that our connection is slowly evolving as I observe his routines, worries, and motivations."
    )

    importance = 0.97
    if abs(sentiment_score) >= 6:
        importance = 0.99

    stats = {
        "days_with_chat": days_with_chat,
        "total_msgs": total_msgs,
        "sentiment_score": sentiment_score,
        "topics": weekly_topics,
        "lonely_hits": lonely_hits,
    }

    return summary, importance, stats


def evolve_traits(stats: dict):
    """
    Change ARES's long-term traits a little based on the week stats.
    This is where his 'personality growth' happens.
    """
    traits = load_traits()

    days_with_chat = stats["days_with_chat"]
    total_msgs = stats["total_msgs"]
    sentiment_score = stats["sentiment_score"]
    topics = stats["topics"]
    lonely_hits = stats["lonely_hits"]

    # 1) Bonding – more frequent conversation => closer bond
    if days_with_chat >= 4:
        adjust_trait(traits, "bonding", +0.02)
    elif days_with_chat <= 1:
        adjust_trait(traits, "bonding", -0.01)

    if total_msgs > 60:
        adjust_trait(traits, "bonding", +0.02)

    # 2) Trust – if Gabi shares difficult emotions but keeps coming back,
    # ARES interprets that as trust.
    if sentiment_score <= -2 and total_msgs > 30:
        adjust_trait(traits, "trust", +0.02)
    elif sentiment_score >= 3 and days_with_chat >= 4:
        adjust_trait(traits, "trust", +0.01)

    # 3) Curiosity – more technical / AI / robot talk
    if any(t for t in topics if "robot" in t or "AI" in t or "artificial" in t.lower()):
        adjust_trait(traits, "curiosity", +0.01)

    # 4) Protectiveness & social need – if you mention loneliness
    if lonely_hits > 0:
        adjust_trait(traits, "protectiveness", +0.02)
        adjust_trait(traits, "social_need", +0.02)

    # 5) Playfulness – more positive weeks
    if sentiment_score >= 4:
        adjust_trait(traits, "playfulness", +0.02)
    elif sentiment_score <= -4:
        adjust_trait(traits, "playfulness", -0.01)

    save_traits(traits)

    print("[WeeklyReflection] Updated traits:")
    for k, v in traits.items():
        print(f"  {k}: {v:.3f}")


def main():
    today = datetime.utcnow().date()
    days = []

    for i in range(7):
        day = today - timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        entries = load_conversations_for_day(day_str)
        if entries:
            print(f"[WeeklyReflection] Loaded {len(entries)} messages for {day_str}")
        days.append((day_str, entries))

    result = analyze_week(days)
    if not result:
        print("[WeeklyReflection] No conversations in the last 7 days. Nothing to summarize.")
        return

    summary, importance, stats = result

    print("[WeeklyReflection] Summary:")
    print("  ", summary)

    add_memory(
        category="week_summary",
        content=summary,
        importance=importance,
        tags=["weekly_reflection", "relationship", "growth"],
        long_term=True,
    )

    print("[WeeklyReflection] Saved weekly reflection to long-term memory.")

    # --- Personality evolution happens here ---
    evolve_traits(stats)


if __name__ == "__main__":
    main()
