import time

from memory.memory_manager import add_memory, get_memories, get_long_term_memories
from memory.memory_manager import add_memory, get_memories
from utils.logger import log
from audio.audio_manager import play_beep
from speech.voice_engine import speak
from emotion.emotion_manager import get_emotion, set_mood, apply_event, describe_emotion

def recall_long_term():
    """ARES speaks one long-term memory and prints it."""
    memories = get_long_term_memories()
    if not memories:
        msg = "I do not have long-term memories yet."
        log(msg)
        print(msg)
        speak(msg)
        return

    last = memories[-1]
    content = last.get("content", "I remember something important.")
    msg = f"I remember: {content}"
    log(f"Recalling long-term memory: {content}")
    print(msg)          # so you see it in the terminal
  #  speak(msg)          # so you hear it at home

def main():
    # Startup
    log("ARES Brain Started.")
    play_beep()

    # On boot, set a default emotion
    set_mood("curious", energy=0.8, confidence=0.7)
    log("Initial emotion set to curious.")

    while True:
        # 1) Log idle status
        log("ARES is idle... waiting for commands.")

        # 2) Log & remember emotion state every loop
        state = get_emotion()
        log(
            f"Emotion: mood={state['mood']}, "
            f"energy={state['energy']:.2f}, "
            f"confidence={state['confidence']:.2f}"
        )

        # 3) Every 60 seconds, store an emotion memory
        if int(time.time()) % 60 == 0:
            text = describe_emotion()
            add_memory("emotion", text)

        # 4) Every 5 seconds we just pause (placeholder for future logic)
        time.sleep(5)


if __name__ == "__main__":
    main()
