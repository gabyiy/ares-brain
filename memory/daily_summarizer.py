import json
from datetime import datetime, date
from pathlib import Path

# ===== Paths =====
BASE_DIR = Path(__file__).resolve().parent.parent   # /home/gabi/ARES_BRAIN
LOG_DIR = BASE_DIR / "logs" / "conversations"
SUMMARY_DIR = BASE_DIR / "logs" / "summaries"
LT_DIR = BASE_DIR / "memory"

# Make sure directories exist
SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
LT_DIR.mkdir(parents=True, exist_ok=True)


def _today_str() -> str:
    return date.today().isoformat()


def _today_log_path() -> Path:
    """logs/conversations/YYYY-MM-DD.jsonl"""
    return LOG_DIR / f"{_today_str()}.jsonl"


def load_today_logs():
    """Load today's conversation log as a list of dicts."""
    path = _today_log_path()
    if not path.exists():
        return []

    entries = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # skip broken lines instead of crashing
                continue
    return entries


def summarize(logs):
    """Create a small structured summary from the raw logs."""
    summary = {
        "date": _today_str(),
        "total_messages": len(logs),
        "greetings": 0,
        "goodbyes": 0,
        "topics": [],
        "mood_references": [],
        "important_messages": [],
    }

    topics = ["weather", "stock", "training", "health", "rheinmetall", "work"]
    moods = ["tired", "good", "bad", "angry", "happy", "sad", "stressed", "worried"]

    for entry in logs:
        text = entry.get("text", "")
        if not text:
            continue

        lower = text.lower()

        # Count greetings / goodbyes
        if "hello" in lower or "hi ares" in lower:
            summary["greetings"] += 1
        if "goodbye" in lower or "bye" in lower:
            summary["goodbyes"] += 1

        # Detect topics (only keep each once)
        for t in topics:
            if t in lower and t not in summary["topics"]:
                summary["topics"].append(t)

        # Mood references
        for mood in moods:
            if mood in lower:
                summary["mood_references"].append(text)
                break

        # Important user messages for long-term memory
        if entry.get("role") == "user":
            if (
                "i felt" in lower
                or "i feel" in lower
                or "i think" in lower
                or "important" in lower
            ):
                summary["important_messages"].append(text)

    return summary


def write_summary(summary):
    """Save daily summary + append important messages to long-term memory."""
    # Save daily summary file
    fname = SUMMARY_DIR / f"{summary['date']}.summary"
    with fname.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    # Append important messages to long_term_memory.jsonl
    if summary["important_messages"]:
        lt_file = LT_DIR / "long_term_memory.jsonl"
        with lt_file.open("a", encoding="utf-8") as f:
            for msg in summary["important_messages"]:
                rec = {
                    "ts": datetime.now().isoformat(),
                    "text": msg,
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main():
    logs = load_today_logs()
    if not logs:
        print("No logs for today.")
        return

    summary = summarize(logs)
    write_summary(summary)
    print("Daily summary created.")


if __name__ == "__main__":
    main()
