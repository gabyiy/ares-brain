from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.intent_router import IntentRouter
from events import get_global_bus
from memory import MemoryStore
from skills import SkillManager
from skills.builtin import create_builtin_plugin


def record_conversation_turn(memory_store, user_message: str, response: str):
    content = f"User: {user_message}\nARES: {response}"
    return memory_store.remember(
        content=content,
        category="conversation_turn",
        importance=0.35,
        tags=["conversation", "text"],
        metadata={
            "user_message": user_message,
            "response": response,
            "interface": "text_repl",
        },
        source="interfaces.text_repl",
    )


def print_and_record(memory_store, user_message: str, response: str):
    print("ARES:", response)
    record_conversation_turn(memory_store, user_message, response)


def main():
    event_bus = get_global_bus()
    memory_store = MemoryStore(event_bus=event_bus)
    skill_manager = SkillManager(event_bus=event_bus, memory_store=memory_store)
    skill_manager.register_plugin(create_builtin_plugin())
    router = IntentRouter(event_bus=event_bus, skill_manager=skill_manager)
    awake = False

    print("ARES text mode ready.")
    print("Say 'hello ares' to wake me.")
    print("Say 'goodbye ares' to exit.")

    while True:
        try:
            user = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nARES: Goodbye Gabi.")
            break

        low = user.lower()

        if low in ("hello", "hello ares", "hi ares", "hey ares"):
            awake = True
            print_and_record(memory_store, user, router.handle(user))
            continue

        if low in ("goodbye", "goodbye ares", "exit", "quit"):
            print_and_record(memory_store, user, router.handle(user))
            break

        if not awake:
            response = "I am sleeping. Say 'hello ares' to wake me."
            event_bus.publish(
                "user_message_received",
                {"text": user, "awake": False},
                source="text_repl",
            )
            event_bus.publish(
                "response_generated",
                {"intent": None, "response": response, "text": user},
                source="text_repl",
            )
            print_and_record(memory_store, user, response)
            continue

        try:
            print_and_record(memory_store, user, router.handle(user))
        except Exception as e:
            response = f"Error: {e}"
            event_bus.publish(
                "response_generated",
                {"intent": None, "response": response, "text": user, "error": True},
                source="text_repl",
            )
            print_and_record(memory_store, user, response)


if __name__ == "__main__":
    main()
