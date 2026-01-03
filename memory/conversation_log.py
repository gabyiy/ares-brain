import json
import os
from datetime import datetime

# Absolute paths to avoid errors
DATA_DIR = "/home/gabi/ARES_BRAIN/data"
LOG_DIR = "/home/gabi/ARES_BRAIN/data/conversations"

# Ensure folder exists
os.makedirs(LOG_DIR, exist_ok=True)

def log_turn(speaker: str, text: str):
    """
    Save one line of conversation.

    speaker : "gabi" or "ares"
    text    : what was said
    """
    day = datetime.utcnow().strftime("%Y-%m-%d")
    path = os.path.join(LOG_DIR, f"{day}.jsonl")

    entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "speaker": speaker,
        "text": text,
    }

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
