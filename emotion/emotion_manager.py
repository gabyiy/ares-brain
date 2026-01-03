import os
import json
import time

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

EMOTION_STATE_PATH = os.path.join(DATA_DIR, "emotion_state.json")


# -------------------------
# Default state
# -------------------------
def _default_state():
    return {
        "energy": 0.5,
        "confidence": 0.5,
        "stability": 0.5,
        "warmth": 0.5,
        "affection": 0.5,   # NEW: closeness / bonding with Gabi
        "last_update": time.time()
    }


# -------------------------
# Load & save
# -------------------------
def _load_state():
    if not os.path.exists(EMOTION_STATE_PATH):
        return _default_state()
    try:
        with open(EMOTION_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return _default_state()


def _save_state(state):
    with open(EMOTION_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# -------------------------
# Emotion decay
# -------------------------
def _apply_decay(state):
    now = time.time()
    last = state.get("last_update", now)
    dt = max(0, now - last)

    decay_rate = 0.0002 * dt  # soft decay over time

    for key in ["energy", "confidence", "stability", "warmth", "affection"]:
        base = 0.5
        value = state.get(key, 0.5)
        value += (base - value) * decay_rate
        state[key] = min(1.0, max(0.0, value))

    state["last_update"] = now
    return state


# -------------------------
# Public: get emotion
# -------------------------
def get_emotion():
    st = _load_state()
    st = _apply_decay(st)
    _save_state(st)
    return st


def describe_emotion():
    st = get_emotion()
    return (
        f"My current mood is influenced by warmth={st['warmth']:.2f}, "
        f"energy={st['energy']:.2f}, confidence={st['confidence']:.2f}, "
        f"stability={st['stability']:.2f}, affection={st['affection']:.2f}."
    )


# -------------------------
# Apply emotional events
# -------------------------
def apply_event(event: str, intensity: float = 0.5):
    intensity = max(0.0, min(1.0, intensity))
    st = _load_state()

    # ---- Happy ----
    if event == "happy":
        st["warmth"]      = min(1.0, st["warmth"] + 0.25 * intensity)
        st["energy"]      = min(1.0, st["energy"] + 0.30 * intensity)
        st["confidence"]  = min(1.0, st["confidence"] + 0.20 * intensity)
        st["stability"]   = min(1.0, st["stability"] + 0.10 * intensity)

    # ---- Sad ----
    elif event == "sad":
        st["warmth"]      = max(0.0, st["warmth"] - 0.20 * intensity)
        st["energy"]      = max(0.0, st["energy"] - 0.30 * intensity)
        st["confidence"]  = max(0.0, st["confidence"] - 0.20 * intensity)
        st["stability"]   = max(0.0, st["stability"] - 0.20 * intensity)

    # ---- Angry ----
    elif event == "angry":
        st["warmth"]      = max(0.0, st["warmth"] - 0.10 * intensity)
        st["energy"]      = min(1.0, st["energy"] + 0.40 * intensity)
        st["confidence"]  = min(1.0, st["confidence"] + 0.30 * intensity)
        st["stability"]   = max(0.0, st["stability"] - 0.20 * intensity)

    # ---- Calm ----
    elif event == "calm":
        st["warmth"]      = min(1.0, st["warmth"] + 0.10 * intensity)
        st["energy"]      = max(0.0, st["energy"] - 0.20 * intensity)
        st["stability"]   = min(1.0, st["stability"] + 0.30 * intensity)

    # ---- NEW: Affection ----
    elif event == "affection":
        st["warmth"]      = min(1.0, st["warmth"] + 0.25 * intensity)
        st["stability"]   = min(1.0, st["stability"] + 0.15 * intensity)
        st["energy"]      = min(1.0, st["energy"] + 0.10 * intensity)
        st["affection"]   = min(1.0, st["affection"] + 0.40 * intensity)

    # Save after event
    _save_state(st)
    return st
