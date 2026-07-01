import io

from core import IntentParser
import memory.v1 as memory_v1
from events import get_global_bus
from interfaces import text_repl
from memory import MemoryStore, NotesStore, TasksStore, UserProfileStore


def test_text_repl_records_turns_and_recalls_profile(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            "hello\n"
            "My name is Gabi\n"
            "I live in Madrid\n"
            "My favorite tank is Leopard 2\n"
            "What is my name?\n"
            "Where do I live?\n"
            "What is my favorite tank?\n"
            "quit\n"
        ),
    )

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    store = MemoryStore(
        short_path=memory_v1.SHORT_MEMORY_FILE,
        long_path=memory_v1.LONG_MEMORY_FILE,
        event_bus=event_bus,
    )
    profile = UserProfileStore(path=tmp_path / "profile.json", event_bus=event_bus)
    turns = store.recall(category="conversation_turn")
    events = [event.name for event in event_bus.history()]

    assert "Your name is Gabi." in output
    assert "You live in Madrid." in output
    assert "Your favorite tank is Leopard 2." in output
    assert profile.get_value("name") == "Gabi"
    assert profile.get_value("location") == "Madrid"
    assert profile.get_favorite("tank") == "Leopard 2"
    assert len(turns) == 8
    assert "user_message_received" in events
    assert "response_generated" in events
    assert "profile.fact_saved" in events


def test_text_repl_routes_calculator_skill_before_generic_knowledge(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nwhat is 2 + 3 * 4\nquit\n"))

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    events = [event.name for event in event_bus.history()]
    detected = [
        event.payload
        for event in event_bus.history("skill.detected")
        if event.payload.get("skill") == "calculator"
    ]

    assert "Result: 14" in output
    assert detected
    assert "response_generated" in events


def test_text_repl_uses_intent_parser_for_live_skill_flow(monkeypatch, tmp_path, capsys):
    parsed_intents = []

    class RecordingIntentParser(IntentParser):
        def parse(self, text: str):
            intent = super().parse(text)
            parsed_intents.append(intent)
            return intent

    monkeypatch.setattr("skills.manager.IntentParser", RecordingIntentParser)
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nwhat is 2 + 3 * 4\nquit\n"))

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    detected = [
        event.payload
        for event in event_bus.history("skill.detected")
        if event.payload.get("skill") == "calculator"
    ]

    assert "Result: 14" in output
    assert any(
        intent.raw_text == "what is 2 + 3 * 4" and intent.intent_name == "calculate"
        for intent in parsed_intents
    )
    assert detected
    assert detected[-1]["intent"] == "calculate"
    assert detected[-1]["reason"] == "structured intent match: calculate"


def test_text_repl_routes_notes_skill_and_persists_note(monkeypatch, tmp_path, capsys):
    notes_path = tmp_path / "notes.json"
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(notes_path))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("hello\nsave note calibrate rover sensors\nlist my notes\nquit\n"),
    )

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    notes = NotesStore(path=notes_path, event_bus=event_bus).list()
    detected = [
        event.payload
        for event in event_bus.history("skill.detected")
        if event.payload.get("skill") == "notes"
    ]

    assert len(notes) == 1
    assert notes[0].text == "calibrate rover sensors"
    assert "Saved note" in output
    assert "Your notes:" in output
    assert "calibrate rover sensors" in output
    assert detected


def test_text_repl_routes_tasks_skill_and_persists_task(monkeypatch, tmp_path, capsys):
    tasks_path = tmp_path / "tasks.json"
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tasks_path))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("hello\nremember buy milk tomorrow\nlist tasks\nquit\n"),
    )

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    tasks = TasksStore(path=tasks_path, event_bus=event_bus).list()
    detected = [
        event.payload
        for event in event_bus.history("skill.detected")
        if event.payload.get("skill") == "tasks"
    ]

    assert len(tasks) == 1
    assert tasks[0].text == "buy milk"
    assert tasks[0].due == "tomorrow"
    assert tasks[0].completed is False
    assert "Saved task" in output
    assert "Your tasks:" in output
    assert "buy milk" in output
    assert "due tomorrow" in output
    assert detected


def test_text_repl_shows_last_plan(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("hello\ncalculate 2 + 2\nshow plan\nquit\n"),
    )

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out

    assert "Result: 4" in output
    assert "Execution plan:" in output
    assert "1. calculator.calculate - ready" in output


def test_text_repl_unknown_intent_stays_safe(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("hello\nthe rover is parked beside the desk\nquit\n"),
    )

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    unmatched = event_bus.history("intent.unmatched")

    assert "I'm not sure how to answer that yet." in output
    assert unmatched
    assert unmatched[-1].payload["text"] == "the rover is parked beside the desk"
