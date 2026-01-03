#!/usr/bin/env python3
import os
import sys

# --- Make ARES_BRAIN the project root on sys.path ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)   # /home/gabi/ARES_BRAIN
sys.path.append(BASE_DIR)
from emotion.emotion_manager import apply_event, describe_emotion
from speech.emotional_voice import sync_voice_with_emotion, describe_voice, speak

print("=== Before event ===")
print("Emotion:", describe_emotion())
print("Voice :", describe_voice())

# Make ARES feel good
apply_event("praise", intensity=0.8)

print("\n=== After praise event, before sync ===")
print("Emotion:", describe_emotion())
print("Voice :", describe_voice())

print("\n=== Syncing voice with emotion ===")
print("Synced voice:", sync_voice_with_emotion())

speak("Hello Gabi. My voice now follows how I truly feel inside.")
