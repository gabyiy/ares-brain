import io

import memory.v1 as memory_v1
from core import ConversationContextManager, get_global_conversation_context
from events import EventBus
from interfaces import text_repl
from skills import Skill, SkillContext, SkillManager, SkillResponse


class EchoSkill(Skill):
    name = "echo_context"
    description = "Echoes input for conversation context tests."
    triggers = ("echo context",)

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        return SkillResponse(text=f"echo: {text}", skill=self.name)


def test_conversation_context_history_ordering():
    context = ConversationContextManager()

    context.record_turn("first", "one", "alpha")
    context.record_turn("second", "two", "beta")
    context.record_turn("third", "three", "gamma")

    assert [turn.user_message for turn in context.history()] == ["first", "second", "third"]
    assert [turn.detected_skill for turn in context.history(2)] == ["beta", "gamma"]


def test_conversation_context_max_history_size_defaults_to_twenty():
    context = ConversationContextManager()

    for index in range(25):
        context.record_turn(f"user {index}", f"assistant {index}", "echo")

    history = context.history()

    assert len(history) == 20
    assert history[0].user_message == "user 5"
    assert history[-1].user_message == "user 24"


def test_conversation_context_clear_and_retrieval_apis():
    context = ConversationContextManager()

    assert context.last_message() is None
    assert context.last_user_message() is None
    assert context.last_assistant_message() is None
    assert context.last_skill() is None

    context.record_turn("hello", "Hello Gabi.", "greeting")

    assert context.last_message().user_message == "hello"
    assert context.last_user_message() == "hello"
    assert context.last_assistant_message() == "Hello Gabi."
    assert context.last_skill() == "greeting"

    context.clear()

    assert context.history() == []
    assert context.last_message() is None


def test_skill_manager_records_handled_skill_interaction():
    conversation_context = ConversationContextManager()
    manager = SkillManager(
        event_bus=EventBus(raise_handler_errors=True),
        conversation_context=conversation_context,
    )
    manager.register(EchoSkill())

    response = manager.handle("echo context hello")
    turn = conversation_context.last_message()

    assert response.text == "echo: echo context hello"
    assert turn.user_message == "echo context hello"
    assert turn.assistant_response == "echo: echo context hello"
    assert turn.detected_skill == "echo_context"


def test_text_repl_records_skill_turn_in_conversation_context(monkeypatch, tmp_path, capsys):
    conversation_context = get_global_conversation_context()
    conversation_context.clear()
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nwhat is 2 + 2\nquit\n"))

    text_repl.main()

    capsys.readouterr()
    history = conversation_context.history()

    assert len(history) == 1
    assert history[0].user_message == "what is 2 + 2"
    assert history[0].assistant_response == "Result: 4"
    assert history[0].detected_skill == "calculator"
