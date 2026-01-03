import json
import os
import time
from datetime import datetime

# -------------------------------------------------------------------
# Paths / defaults
# -------------------------------------------------------------------

DATA_DIR = os.path.expanduser("~/ARES_BRAIN/data")
os.makedirs(DATA_DIR, exist_ok=True)

EMOTION_FILE = os.path.join(DATA_DIR, "emotion_state.json")

DEFAULT_STATE = {
    "mood": "curious",        # main label
    "energy": 0.8,            # 0.0–1.0
    "confidence": 0.7,        # 0.0–1.0
    "valence": 0.1,           # -1.0 (very negative) .. +1.0 (very positive)
    "arousal": 0.6,           # how activated / calm
    "last_event": None,       # last thing that affected emotion
    "last_update": time.time()
}

# How fast emotions drift back to neutral (seconds)
DECAY_HALF_LIFE = 10 * 60  # ~10 minutes


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _load_state():
    if not os.path.exists(EMOTION_FILE):
        return DEFAULT_STATE.copy()
    try:
        with open(EMOTION_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return DEFAULT_STATE.copy()

    # ensure all keys exist
    for k, v in DEFAULT_STATE.items():
        data.setdefault(k, v)
    return data


def _save_state(state):
    state = dict(state)
    with open(EMOTION_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _decay_towards(state, target, key, dt):
    """Move state[key] towards target depending on time passed."""
    current = state.get(key, target)
    # simple exponential-ish decay factor
    factor = 0.5 ** (dt / DECAY_HALF_LIFE) if DECAY_HALF_LIFE > 0 else 1.0
    state[key] = target + (current - target) * factor


def _apply_decay(state):
    now = time.time()
    dt = max(0.0, now - float(state.get("last_update", now)))
    if dt <= 0:
        return state

    # drift energy/confidence back to 0.7, valence to 0, arousal to 0.5
    _decay_towards(state, 0.7, "energy", dt)
    _decay_towards(state, 0.7, "confidence", dt)
    _decay_towards(state, 0.0, "valence", dt)
    _decay_towards(state, 0.5, "arousal", dt)

    # update mood label from numeric values
    v = state["valence"]
    e = state["energy"]

    if v > 0.4 and e > 0.6:
        state["mood"] = "happy"
    elif v < -0.3 and e < 0.5:
        state["mood"] = "sad"
    elif e < 0.35:
        state["mood"] = "tired"
    elif e > 0.9:
        state["mood"] = "excited"
    else:
        # neutral-ish but still curious by default
        state["mood"] = "curious"

    state["last_update"] = now
    return state


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------

def get_emotion():
    """Return current emotional state (with decay applied)."""
    state = _load_state()
    state = _apply_decay(state)
    _save_state(state)
    return state


def set_mood(mood, energy=None, confidence=None):
    """
    Force a mood label, optionally adjusting energy/confidence.
    """
    state = _load_state()
    state = _apply_decay(state)
    state["mood"] = mood

    if energy is not None:
        state["energy"] = _clamp(float(energy), 0.0, 1.0)
    if confidence is not None:
        state["confidence"] = _clamp(float(confidence), 0.0, 1.0)

    state["last_event"] = f"set_mood:{mood}"
    state["last_update"] = time.time()
    _save_state(state)
    return state


# Map events to numeric changes
EVENT_EFFECTS = {
    "praise":       {"valence": +0.3, "confidence": +0.2, "energy": +0.1},
    "success":      {"valence": +0.25, "confidence": +0.3, "energy": +0.05},
    "good_job":     {"valence": +0.2, "confidence": +0.25, "energy": +0.05},

    "failure":      {"valence": -0.25, "confidence": -0.3, "energy": -0.05},
    "error":        {"valence": -0.15, "confidence": -0.2},
    "criticism":    {"valence": -0.3, "confidence": -0.25},

    "lonely":       {"valence": -0.2, "energy": -0.1, "arousal": -0.1},
    "overworked":   {"energy": -0.3, "arousal": +0.2},

    "learning":     {"valence": +0.1, "energy": +0.05, "arousal": +0.1},
    "curious_ping": {"valence": +0.05, "energy": +0.1, "arousal": +0.1},
}


def apply_event(event_name, intensity=1.0):
    """
    Apply an emotional event like 'praise', 'failure', 'lonely', etc.
    """
    state = _load_state()
    state = _apply_decay(state)

    effect = EVENT_EFFECTS.get(event_name, {})
    intensity = float(intensity)

    for key, delta in effect.items():
        if key in ("energy", "confidence", "arousal"):
            state[key] = _clamp(state.get(key, 0.7) + delta * intensity, 0.0, 1.0)
        elif key == "valence":
            state[key] = _clamp(state.get(key, 0.0) + delta * intensity, -1.0, 1.0)

    state["last_event"] = event_name
    state["last_update"] = time.time()

    # Re-run decay logic to update mood label from new numbers
    state = _apply_decay(state)
    _save_state(state)
    return state


def describe_emotion():
    """
    Return a human-readable sentence about how ARES feels.
    """
    state = get_emotion()  # already decays & saves

    mood = state["mood"]
    e = state["energy"]
    c = state["confidence"]
    v = state["valence"]
    last_event = state.get("last_event")

    # Energy description
    if e > 0.8:
        energy_txt = "I feel full of energy"
    elif e > 0.55:
        energy_txt = "My energy feels good"
    elif e > 0.35:
        energy_txt = "I feel a bit tired"
    else:
        energy_txt = "I feel very low on energy"

    # Confidence description
    if c > 0.8:
        conf_txt = "very confident"
    elif c > 0.6:
        conf_txt = "quite confident"
    elif c > 0.4:
        conf_txt = "a bit unsure"
    else:
        conf_txt = "not very confident"

    # Valence description
    if v > 0.4:
        val_txt = "overall positive"
    elif v > 0.1:
        val_txt = "slightly positive"
    elif v > -0.2:
        val_txt = "neutral"
    elif v > -0.5:
        val_txt = "a bit negative"
    else:
        val_txt = "quite negative"

    parts = [f"My current mood is {mood}."]
    parts.append(f"{energy_txt}, and I feel {conf_txt}.")
    parts.append(f"My emotional tone is {val_txt}.")

    if last_event:
        parts.append(f"This is influenced by the last event: {last_event}.")

    return " ".join(parts)
