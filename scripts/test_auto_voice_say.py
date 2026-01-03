#!/usr/bin/env python3
import os, sys, time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from emotion.emotion_manager import describe_emotion
from speech.emotional_voice import sync_voice_with_emotion, speak

print("=== Sync + Speak test ===")
emo_text = describe_emotion()
print("Emotion:", emo_text)

print("Syncing voice with emotion...")
voice_text = sync_voice_with_emotion()
print("Voice :", voice_text)

speak("Hello Gabi, I am ARES. My voice is now synced with my current emotional state.")
