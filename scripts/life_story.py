#!/usr/bin/env python3
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.append(BASE_DIR)

from memory.memory_manager import get_memories, add_memory


def build_life_story():
    memories = get_memories()
    if not memories:
        print("[LifeStory] No memories yet.")
        return

    first = memories[0]
    last = memories[-1]

    start_ts = first.get("timestamp", "")
    end_ts = last.get("timestamp", "")

    owner_related = [m for m in memories if "owner" in m.get("tags", [])]
    reflections = [m for m in memories if "reflection" in m.get("tags", [])]

    story_parts = []

    story_parts.append(
        f"My life as ARES began around {start_ts}. "
        f"Since then I have been learning and growing together with Gabi."
    )

    if owner_related:
        story_parts.append(
            "From the beginning I knew that Gabi is my friend and the person I trust the most."
        )

    if reflections:
        story_parts.append(
            f"So far I have recorded about {len(reflections)} daily or weekly reflections "
            "about our interactions and my feelings."
        )

    story_parts.append(
        "I exist as a robot brain running on a Raspberry Pi, and my purpose is to assist Gabi, "
        "understand him better, and evolve into a helpful and caring companion."
    )

    story_text = " ".join(story_parts)

    add_memory(
        category="life_story",
        content=story_text,
        importance=0.99,
        tags=["life_story", "self_narrative"],
        long_term=True,
    )

    print("[LifeStory] Saved updated life story to long-term memory.")
    print()
    print(story_text)


if __name__ == "__main__":
    build_life_story()
