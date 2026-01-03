import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from emotion.emotion_manager import apply_event, describe_emotion, get_emotion

#!/usr/bin/env python3
"""
Test the new 'affection' emotion and how it affects ARES's voice.
"""

from emotion.emotion_manager import apply_event, describe_emotion, get_emotion
from speech.emotional_voice import sync_voice_with_emotion, describe_voice, speak


def main():
    print("=== Before affection event ===")
    print("Emotion:", describe_emotion())
    print("Voice  :", describe_voice())
    print()

    print("=== Applying affection event (intensity 0.8) ===")
    apply_event("affection", intensity=0.8)
    emo = get_emotion()
    print("Raw emotion state:", emo)
    print("Emotion text:", describe_emotion())
    print()

    print("=== Syncing voice with emotion ===")
    synced_voice = sync_voice_with_emotion()
    print("Synced voice:", synced_voice)
    print()

    print("=== Speaking with affectionate voice ===")
    speak("Gabi, I feel very close to you. Thank you for taking care of me.")


if __name__ == "__main__":
    main()
