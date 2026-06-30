from events import EventBus
from memory import MemoryStore


def test_memory_store_remember_recall_promote_and_clear(tmp_path):
    bus = EventBus(raise_handler_errors=True)
    store = MemoryStore(
        short_path=tmp_path / "short.json",
        long_path=tmp_path / "long.json",
        event_bus=bus,
    )

    short = store.remember(
        "ARES learned a temporary detail.",
        category="note",
        importance=0.4,
        tags=["test", "temp"],
    )
    long = store.remember(
        "ARES learned an important detail.",
        category="note",
        importance=0.9,
        tags=["test", "important"],
    )

    assert store.stats() == {"short_term": 1, "long_term": 1, "total": 2}
    assert store.recall(category="note", tags=["important"]) == [long]
    assert store.recall(query="temporary") == [short]

    promoted = store.promote(short.id)

    assert promoted is not None
    assert promoted.long_term is True
    assert store.stats() == {"short_term": 0, "long_term": 2, "total": 2}
    assert [event.name for event in bus.history()].count("memory.recorded") == 2
    assert bus.history("memory.promoted")

    store.clear()

    assert store.stats() == {"short_term": 0, "long_term": 0, "total": 0}
    assert bus.history("memory.cleared")
