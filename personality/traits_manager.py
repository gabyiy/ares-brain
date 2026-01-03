import os
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

TRAITS_PATH = os.path.join(DATA_DIR, "traits.json")

# Default baseline personality for ARES (values 0.0–1.0)
DEFAULT_TRAITS = {
    "bonding": 0.60,        # how close he feels to you
    "trust": 0.60,          # how much he trusts your intentions
    "curiosity": 0.70,      # how eager he is to explore / learn
    "playfulness": 0.50,    # how playful his style is
    "protectiveness": 0.40, # how much he worries about your safety
    "social_need": 0.50,    # how much he wants interaction
}


def _merge_defaults(loaded: dict) -> dict:
    """Make sure all keys exist, fill missing with defaults."""
    traits = dict(DEFAULT_TRAITS)
    traits.update(loaded or {})
    return traits


def load_traits() -> dict:
    """Load traits from disk, or return defaults if file missing."""
    if not os.path.exists(TRAITS_PATH):
        return dict(DEFAULT_TRAITS)

    try:
        with open(TRAITS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _merge_defaults(data)
    except Exception:
        # If file is corrupted, fall back to defaults
        return dict(DEFAULT_TRAITS)


def save_traits(traits: dict):
    """Save traits to disk."""
    traits = _merge_defaults(traits)
    with open(TRAITS_PATH, "w", encoding="utf-8") as f:
        json.dump(traits, f, ensure_ascii=False, indent=2)


def adjust_trait(traits: dict, name: str, delta: float,
                 min_value: float = 0.0, max_value: float = 1.0):
    """Change one trait in-place, clamped between min_value and max_value."""
    base = _merge_defaults(traits)
    current = float(base.get(name, DEFAULT_TRAITS.get(name, 0.5)))
    new_val = current + delta
    new_val = max(min_value, min(max_value, new_val))
    traits[name] = new_val
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

TRAITS_PATH = os.path.join(DATA_DIR, "traits.json")

# Default baseline personality for ARES (values 0.0–1.0)
DEFAULT_TRAITS = {
    "bonding": 0.60,        # how close he feels to you
    "trust": 0.60,          # how much he trusts your intentions
    "curiosity": 0.70,      # how eager he is to explore / learn
    "playfulness": 0.50,    # how playful his style is
    "protectiveness": 0.40, # how much he worries about your safety
    "social_need": 0.50,    # how much he wants interaction
}


def _merge_defaults(loaded: dict) -> dict:
    """Make sure all keys exist, fill missing with defaults."""
    traits = dict(DEFAULT_TRAITS)
    traits.update(loaded or {})
    return traits


def load_traits() -> dict:
    """Load traits from disk, or return defaults if file missing."""
    if not os.path.exists(TRAITS_PATH):
        return dict(DEFAULT_TRAITS)

    try:
        with open(TRAITS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _merge_defaults(data)
    except Exception:
        # If file is corrupted, fall back to defaults
        return dict(DEFAULT_TRAITS)


def save_traits(traits: dict):
    """Save traits to disk."""
    traits = _merge_defaults(traits)
    with open(TRAITS_PATH, "w", encoding="utf-8") as f:
        json.dump(traits, f, ensure_ascii=False, indent=2)


def adjust_trait(traits: dict, name: str, delta: float,
                 min_value: float = 0.0, max_value: float = 1.0):
    """Change one trait in-place, clamped between min_value and max_value."""
    base = _merge_defaults(traits)
    current = float(base.get(name, DEFAULT_TRAITS.get(name, 0.5)))
    new_val = current + delta
    new_val = max(min_value, min(max_value, new_val))
    traits[name] = new_val
