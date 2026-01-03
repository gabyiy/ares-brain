from speech.emotional_voice import apply_emotion_to_voice, describe_voice, speak

print("=== Neutral voice test ===")
print(describe_voice())
speak("Hello Gabi. This is my neutral voice.")

input("\nPress Enter for HAPPY voice...")

apply_emotion_to_voice("happy", intensity=0.8)
print("\n=== Happy voice state ===")
print(describe_voice())
speak("I feel happy and curious today, Gabi. My voice should sound brighter.")

input("\nPress Enter for CALM voice...")

apply_emotion_to_voice("calm", intensity=0.8)
print("\n=== Calm voice state ===")
print(describe_voice())
speak("Now I am calm and relaxed, speaking softly and steadily.")

