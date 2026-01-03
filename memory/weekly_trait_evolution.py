import json
from datetime import datetime, date, timedelta
from pathlib import Path

# ===== Paths =====
BASE_DIR = Path(__file__).resolve().parent.parent   # /home/gabi/ARES_BRAIN
SUMMARY_DIR = BASE_DIR / "logs" / "summaries"
TRAIT_LOG = BASE_DIR / "memory" / "weekly_traits.jsonl"

# Make sure directories exist
SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
TRAIT_LOG.parent.mkdir(parents=True, exist_ok=True)

# ===== Helpers =====
def _date_str(d: date) -> str:
    return d.isoformat()


def _daily_summary_path(d: date) -> Path:
    """logs/summaries/YYYY-MM-DD.summary"""
    return SUMMARY_DIR / f"{_date_str(d)}.summary"


def load_daily_summary(d: date):
    """Load one day's summary JSON (or None if missing/broken)."""
    path = _daily_summary_path(d)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None


def collect_week_summaries(end_date: date):
    """
    Collect up to 7 daily summaries ending at end_date (inclusive).
    Returns list in chronological order.
    """
    summaries = []
    for i in range(6, -1, -1):  # 6..0
        day = end_date - timedelta(days=i)
        s = load_daily_summary(day)
        if s:
            summaries.append(s)
    return summaries


def aggregate_week(summaries):
    """Merge daily summaries into one weekly aggregate."""
    if not summaries:
        return None

    agg = {
        "from": summaries[0].get("date"),
        "to": summaries[-1].get("date"),
        "days_count": len(summaries),
        "greetings": 0,
        "goodbyes": 0,
        "topics": {},
        "moods": {},
        "important_messages": [],
    }

    mood_tags = ["tired", "good", "bad", "angry", "happy", "sad"]

    for s in summaries:
        agg["greetings"] += int(s.get("greetings", 0))
        agg["goodbyes"] += int(s.get("goodbyes", 0))

        # topics
        for t in s.get("topics", []):
            agg["topics"][t] = agg["topics"].get(t, 0) + 1

        # mood lines -> count mood words
        for line in s.get("mood_references", []):
            text = str(line).lower()
            for m in mood_tags:
                if m in text:
                    agg["moods"][m] = agg["moods"].get(m, 0) + 1

        # keep all important messages (can trim later)
        agg["important_messages"].extend(s.get("important_messages", []))

    return agg


# ===== Trait evolution =====

DEFAULT_TRAITS = {
    "supportive": 0.5,       # how comforting / caring ARES is
    "coach": 0.5,            # training / discipline support
    "market_analyst": 0.5,   # focus on stocks / markets
    "curious": 0.5,          # how much he asks and explores
}


def load_current_traits():
    """
    Read the last traits snapshot from weekly_traits.jsonl
    or start from DEFAULT_TRAITS.
    """
    if not TRAIT_LOG.exists():
        return DEFAULT_TRAITS.copy()

    last = None
    with open(TRAIT_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                last = obj
            except json.JSONDecodeError:
                continue

    if last and isinstance(last, dict) and "traits" in last:
        return dict(last["traits"])

    return DEFAULT_TRAITS.copy()


def _bump(traits: dict, name: str, delta: float):
    """Change one trait and clamp between 0.0 and 1.0."""
    base = traits.get(name, DEFAULT_TRAITS.get(name, 0.5))
    base += delta
    base = max(0.0, min(1.0, base))
    traits[name] = round(base, 3)


def evolve_traits(traits: dict, agg: dict) -> dict:
    """
    Simple rules based on topics + moods:
      - More 'bad/tired/sad' mood -> more supportive.
      - More training/health topics -> stronger coach.
      - More stock/rheinmetall/market topics -> market_analyst.
      - More variety of topics -> curious.
    """
    topics = agg.get("topics", {})
    moods = agg.get("moods", {})

    # topic counts
    stock_hits = sum(
        count for key, count in topics.items()
        if key in ("stock", "stocks", "rheimetall", "market", "markets", "finance")
    )
    train_hits = topics.get("training", 0)
    health_hits = topics.get("health", 0)
    weather_hits = topics.get("weather", 0)

    # mood counts
    bad_mood = (
        moods.get("tired", 0)
        + moods.get("bad", 0)
        + moods.get("sad", 0)
        + moods.get("angry", 0)
    )
    good_mood = moods.get("good", 0) + moods.get("happy", 0)

    # --- supportive ---
    if bad_mood > 0:
        _bump(traits, "supportive", 0.03)
    if good_mood > bad_mood and good_mood > 0:
        _bump(traits, "supportive", -0.01)

    # --- coach (training + health) ---
    training_health = train_hits + health_hits
    if training_health >= 3:
        _bump(traits, "coach", 0.03)
    elif training_health == 0:
        _bump(traits, "coach", -0.01)

    # --- market_analyst (stock / Rheinmetall / markets) ---
    if stock_hits >= 3:
        _bump(traits, "market_analyst", 0.03)
    elif stock_hits == 0:
        _bump(traits, "market_analyst", -0.01)

    # --- curious (variety of topics) ---
    topic_variety = len(topics)
    if topic_variety >= 4:
        _bump(traits, "curious", 0.03)
    elif topic_variety <= 1:
        _bump(traits, "curious", -0.01)

    return traits


def write_week_record(end_date: date, agg: dict, traits: dict):
    """Write pretty weekly summary + append traits history line."""
    week_str = _date_str(end_date)

    record = {
        "week_ending": week_str,
        "range": {
            "from": agg.get("from"),
            "to": agg.get("to"),
        },
        "stats": {
            "days_count": agg.get("days_count", 0),
            "greetings": agg.get("greetings", 0),
            "goodbyes": agg.get("goodbyes", 0),
            "topics": agg.get("topics", {}),
            "moods": agg.get("moods", {}),
        },
        "traits": traits,
        "sample_important_messages": agg.get("important_messages", [])[:5],
    }

    weekly_file = SUMMARY_DIR / f"weekly_{week_str}.summary"
    with open(weekly_file, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)

    # Append to trait history log
    history_line = {
        "ts": datetime.now().isoformat(),
        "week_ending": week_str,
        "traits": traits,
    }
    with open(TRAIT_LOG, "a", encoding="utf-8") as f:
        json.dump(history_line, f, ensure_ascii=False)
        f.write("\n")


def main():
    end_date = date.today()
    summaries = collect_week_summaries(end_date)

    if not summaries:
        print("No daily summaries found for this week.")
        return

    agg = aggregate_week(summaries)
    traits = load_current_traits()
    traits = evolve_traits(traits, agg)

    write_week_record(end_date, agg, traits)

    print(f"Weekly trait evolution updated for week ending {end_date.isoformat()}.")


if __name__ == "__main__":
    main()
