import os
import json
from datetime import datetime

# --- Paths ---------------------------------------------------------

DATA_DIR = os.path.expanduser("~/ARES_BRAIN/data")
os.makedirs(DATA_DIR, exist_ok=True)

SHORT_FILE = os.path.join(DATA_DIR, "memories_short.json")
LONG_FILE = os.path.join(DATA_DIR, "memories_long.json")


# --- Helpers -------------------------------------------------------

def _load(path):
    """Load a list of memories from JSON file."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save(path, data):
    """Save a list of memories to JSON file."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# --- Public API ----------------------------------------------------

def add_memory(category, content, importance=0.5, tags=None, long_term=False):
    """
    Store a new memory.

    - category   : what type of memory ("status", "owner", "mission", etc.)
    - content    : text of the memory
    - importance : 0.0–1.0 (higher = more likely long-term)
    - tags       : optional list of strings
    - long_term  : if True, go straight to long-term
    """
    if tags is None:
        tags = []

    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "category": category,
        "content": content,
        "importance": float(importance),
        "tags": tags,
    }

    if long_term or importance >= 0.85:
        # very important → directly long-term
        data = _load(LONG_FILE)
        data.append(entry)
        _save(LONG_FILE, data)
    else:
        # normal memory → short-term
        data = _load(SHORT_FILE)
        data.append(entry)
        _save(SHORT_FILE, data)

    return entry


def get_memories(category=None, long_term=None, limit=None):
    """
    Read memories.

    - category  : filter by category (or None for all)
    - long_term : True = only long, False = only short, None = both
    - limit     : if set, return only latest N memories
    """
    result = []

    if long_term in (False, None):
        result.extend(_load(SHORT_FILE))
    if long_term in (True, None):
        result.extend(_load(LONG_FILE))

    if category:
        result = [m for m in result if m.get("category") == category]

    # sort by time
    result.sort(key=lambda m: m.get("timestamp", ""))

    if limit is not None:
        result = result[-limit:]

    return result


def auto_promote_old_memories(max_short=50, min_importance=0.6):
    """
    Move important older memories from short-term to long-term.

    - max_short      : how many short-term memories to keep
    - min_importance : only promote if importance >= this
    """
    short = _load(SHORT_FILE)
    long = _load(LONG_FILE)

    if len(short) <= max_short:
        return 0

    # promote important ones
    to_promote = [m for m in short if m.get("importance", 0.0) >= min_importance]

    remaining = [m for m in short if m not in to_promote]

    # If still too many, drop oldest extra ones
    if len(remaining) > max_short:
        extra = len(remaining) - max_short
        remaining = remaining[extra:]  # keep newest

    promoted_count = len(to_promote)
    if promoted_count:
        long.extend(to_promote)

    _save(SHORT_FILE, remaining)
    _save(LONG_FILE, long)

    return promoted_count


def clear_memories(long_term=None):
    """
    Clear memories.

    - long_term True  : clear only long-term
    - long_term False : clear only short-term
    - long_term None  : clear both
    """
    if long_term in (False, None):
        _save(SHORT_FILE, [])
    if long_term in (True, None):
        _save(LONG_FILE, [])
def get_long_term_memories():
    """Return all long-term memories."""
    return _load(LONG_FILE)
from datetime import datetime, timedelta

def compress_old_memories(days: int = 30, decay: float = 0.8, min_importance: float = 0.2):
    """
    Older memories lose importance over time.
    - memories older than `days` get importance *= decay
    - if importance < min_importance, they are deleted
    """
    memories = load_memories()

    if not memories:
        return

    cutoff = datetime.utcnow() - timedelta(days=days)
    new_list = []

    for m in memories:
        ts = m.get("timestamp")
        try:
            t = datetime.fromisoformat(ts.replace("Z", ""))
        except Exception:
            new_list.append(m)
            continue

        if t < cutoff:
            imp = m.get("importance", 0.5) * decay
            if imp >= min_importance:
                m["importance"] = imp
                new_list.append(m)
        else:
            new_list.append(m)

    save_memories(new_list)
