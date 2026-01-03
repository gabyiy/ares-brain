import os
import json
import datetime
from pathlib import Path

# Base folder = ARES_BRAIN
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs" / "conversations"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _get_log_path() -> Path:
    """Return today's conversation log file path."""
    today = datetime.date.today().isoformat()
    return LOG_DIR / f"{today}.jsonl"


def log_message(role: str, text: str, modality: str = "voice"):
    """
    Append one message to today's log.

    role: "user" or "ares"
    text: message content
    modality: "voice", "text", "system"
    """
    text = text.strip()
    if not text:
        return

    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "role": role,
        "modality": modality,
        "text": text,
    }

    path = _get_log_path()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
