import tempfile
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.intent_router import IntentRouter
from events import EventBus
from interfaces.text_repl import record_conversation_turn
from memory import MemoryStore


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        event_bus = EventBus(raise_handler_errors=True)
        event_names = []
        event_bus.subscribe("*", lambda event: event_names.append(event.name))

        memory_store = MemoryStore(
            short_path=tmp_dir / "memories_short.json",
            long_path=tmp_dir / "memories_long.json",
            event_bus=event_bus,
        )
        router = IntentRouter(event_bus=event_bus)

        response = router.handle("hello ares")
        record = record_conversation_turn(memory_store, "hello ares", response)
        turns = memory_store.recall(category="conversation_turn")

        assert response == "Hello Gabi, I am here."
        assert "user_message_received" in event_names
        assert "intent_detected" in event_names
        assert "response_generated" in event_names
        assert "memory.recorded" in event_names
        assert record in turns
        assert turns[-1].metadata["user_message"] == "hello ares"
        assert turns[-1].metadata["response"] == response

    print("Phase 2 event/memory verification passed.")


if __name__ == "__main__":
    main()
