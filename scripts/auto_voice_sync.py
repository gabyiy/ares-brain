#!/usr/bin/env python3
import sys
import os

# Force Python to load modules from the ARES_BRAIN root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
#!/usr/bin/env python3
"""
auto_voice_sync.py

Periodically sync ARES's voice with his current emotional state.
Run with:  python3 scripts/auto_voice_sync.py
Stop with: Ctrl + C
"""
from emotion.emotion_manager import describe_emotion
from speech.emotional_voice import sync_voice_with_emotion
import time
from emotion.emotion_manager import describe_emotion
from speech.emotional_voice import sync_voice_with_emotion, describe_voice


def main():
    print("[AutoVoiceSync] Started. Press Ctrl+C to stop.")
    interval = 30  # seconds between syncs

    while True:
        emo_text = describe_emotion()
        voice_text = sync_voice_with_emotion()

        print("\n=== Auto sync tick ===")
        print("Emotion:", emo_text)
        print("Voice  :", voice_text)

        time.sleep(interval)


if __name__ == "__main__":
    main()
