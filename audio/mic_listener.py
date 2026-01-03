import json
import queue
import sounddevice as sd
from vosk import Model, KaldiRecognizer

from speech.emotional_voice import speak
from memory.conversation_logger import log_message
from online.web_search import search_and_summarise


# ===== Audio config =====
SAMPLE_RATE = 44100          # works fine with your USB mic
BLOCK_SIZE  = 4096
DEVICE_INDEX = 0             # 0 = your USB mic (as before)

# ===== Grammar: limited vocabulary so it understands you better =====
GRAMMAR = json.dumps([
    "ares", "hello", "ares hello", "hello ares",
    "hi ares", "hey ares",
    "how are you",
    "thank you", "thanks",
    "weather", "weather tomorrow",
    "score", "result",
    "search",
    "goodbye", "bye",
])

# ===== Simple intent handler (text only) =====
def handle_intent(text: str, from_voice: bool = True) -> str:
    """
    Very small intent engine for non-web questions.
    Returns a reply string; caller decides whether to speak it.
    """
    lower = text.lower()

    if "how are you" in lower:
        return "I feel good and ready to help you. And how are you today, Gabi?"

    if "thank" in lower:
        return "You are welcome, Gabi."

    # Fallback
    return "I'm not sure how to answer that yet."


# ===== Microphone streaming =====
_audio_queue: "queue.Queue[bytes]" = queue.Queue()


def _audio_callback(indata, frames, time_info, status):
    if status:
        print(status, flush=True)
    _audio_queue.put(bytes(indata))


def main():
    print("[Mic] Loading Vosk model from: ./models/vosk_en_small")
    model = Model("models/vosk_en_small")
    rec = KaldiRecognizer(model, SAMPLE_RATE, GRAMMAR)

    active = False
    MIN_WORDS = 3

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=BLOCK_SIZE,
        device=DEVICE_INDEX,
        dtype="int16",
        channels=1,
        callback=_audio_callback,
    ):
        print("[Mic] Model loaded. Listening...")
        print("[Mic] Say 'hello ares' to wake me up.")

        while True:
            data = _audio_queue.get()

            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                text = result.get("text", "").strip()
                if not text:
                    continue

                print(f"[Mic final] {text}")
                lower = text.lower()

                # Sleeping: only listen for wake word
                if not active:
                    if lower in ("hello ares", "hi ares", "ares hello", "ares hi"):
                        active = True
                        reply = "Hello Gabi, I am here."
                        print(f"ARES: {reply}")
                        speak(reply)
                    else:
                        print(f"[Mic asleep] ignoring: {lower}")
                    continue

                # Active: check for goodbye
                if any(w in lower for w in ("goodbye", "bye")):
                    reply = "Goodbye Gabi."
                    print(f"ARES: {reply}")
                    speak(reply)
                    active = False
                    print("[Mic] ARES is sleeping. Say 'hello ares' to wake him.")
                    continue

                # Ignore very short noise / fragments
                if len(lower.split()) < MIN_WORDS:
                    print("[Mic awake] too short, ignored.")
                    continue

                # Decide if it's a web question
                if any(w in lower for w in ("score", "result", "search", "weather", "stock")):
                    print(f"[TEXT] Processing web question: '{lower}'")
                    answer = search_and_summarise(lower)

                    log_message("user", "voice", lower)
                    log_message("ares", "voice", answer)

                    print(f"ARES: {answer}")
                    speak(answer)
                else:
                    # Local small-talk intent
                    reply = handle_intent(lower, from_voice=True)

                    log_message("user", "voice", lower)
                    log_message("ares", "voice", reply)

                    print(f"ARES: {reply}")
                    speak(reply)

            else:
                partial = json.loads(rec.PartialResult()).get("partial", "").strip()
                if partial:
                    print(f"[Mic partial] {partial}")


if __name__ == "__main__":
    main()
