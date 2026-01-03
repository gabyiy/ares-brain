import os
import json
import subprocess
from emotion.emotion_manager import get_emotion
# ===== Paths =====
BASE_DIR = os.path.dirname(os.path.dirname(__file__))   # ~/ARES_BRAIN
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

VOICE_STATE_PATH = os.path.join(DATA_DIR, "voice_state.json")


# ===== Helpers =====

def _default_state():
    return {
        "warmth": 0.5,
        "energy": 0.5,
        "confidence": 0.5,
        "stability": 0.5
    }


def _load_state():
    if not os.path.exists(VOICE_STATE_PATH):
        return _default_state()

    try:
        with open(VOICE_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return _default_state()


def _save_state(state):
    with open(VOICE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ===== PUBLIC API =====

def describe_voice():
    st = _load_state()
    return (
        f"My voice sounds warm. My energy is at {st['energy']:.2f}. "
        f"My confidence is {st['confidence']:.2f}. "
        f"My stability is {st['stability']:.2f}."
    )


def apply_emotion_to_voice(emotion: str, intensity: float = 0.5):
    intensity = max(0.0, min(1.0, intensity))
    st = _load_state()

    if emotion == "happy":
        st["warmth"] = min(1.0, st["warmth"] + 0.2 * intensity)
        st["energy"] = min(1.0, st["energy"] + 0.3 * intensity)
        st["confidence"] = min(1.0, st["confidence"] + 0.2 * intensity)
        st["stability"] = min(1.0, st["stability"] + 0.1 * intensity)

    elif emotion == "sad":
        st["warmth"] = max(0.0, st["warmth"] - 0.2 * intensity)
        st["energy"] = max(0.0, st["energy"] - 0.3 * intensity)
        st["confidence"] = max(0.0, st["confidence"] - 0.2 * intensity)
        st["stability"] = max(0.0, st["stability"] - 0.1 * intensity)

    elif emotion == "angry":
        st["warmth"] = max(0.0, st["warmth"] - 0.1 * intensity)
        st["energy"] = min(1.0, st["energy"] + 0.4 * intensity)
        st["confidence"] = min(1.0, st["confidence"] + 0.3 * intensity)
        st["stability"] = max(0.0, st["stability"] - 0.2 * intensity)

    elif emotion == "calm":
        st["warmth"] = min(1.0, st["warmth"] + 0.1 * intensity)
        st["energy"] = max(0.0, st["energy"] - 0.2 * intensity)
        st["stability"] = min(1.0, st["stability"] + 0.3 * intensity)

    _save_state(st)
    return describe_voice()


def speak(text: str):
    st = _load_state()

    # Base values
    base_speed = 160      # calm base speed
    base_pitch = 50       # neutral pitch

    # Convert emotion to voice parameters
    speed = int(base_speed + (st["energy"] - 0.5) * 80)
    pitch = int(base_pitch + (st["warmth"] - 0.5) * 30)

    speed = max(80, min(250, speed))
    pitch = max(0, min(99, pitch))

    cmd = [
        "espeak-ng",
        f"-s{speed}",
        f"-p{pitch}",
        text
    ]

    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        print("[EmotionalVoice] espeak-ng not found. Install with: sudo apt install espeak-ng")


def sync_voice_with_emotion():
    """
    Pull ARES's current emotional state from emotion_manager
    and convert it into voice parameters.
    """
    emo = get_emotion()
    st = _load_state()

    def clamp(v): return max(0.0, min(1.0, v))

    st["energy"] = clamp(float(emo.get("energy", st["energy"])))
    st["confidence"] = clamp(float(emo.get("confidence", st["confidence"])))
    st["stability"] = clamp(float(emo.get("stability", st["stability"])))
    st["warmth"] = clamp(float(emo.get("warmth", st["warmth"])))

    _save_state(st)
    return describe_voice()
