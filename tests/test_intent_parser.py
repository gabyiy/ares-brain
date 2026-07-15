import pytest

from core import IntentParser
from events import EventBus
from memory import TasksStore
from skills import ToolSelector
from skills import SkillManager
from skills.builtin.calculator import CalculatorSkill
from skills.builtin.memory_recall import MemoryRecallSkill
from skills.builtin.notes import NotesSkill
from skills.builtin.tasks import TasksSkill
from skills.builtin.time_date import TimeDateSkill


@pytest.mark.parametrize(
    ("text", "intent_name"),
    [
        ("remember to buy milk", "task"),
        ("remember this idea: build ARES memory", "note"),
        ("what is 12 * 8", "calculate"),
        ("calculate 2 + 2", "calculate"),
        ("what time is it", "time_date"),
        ("what is my birthday", "owner_memory"),
        ("show my notes", "note"),
        ("list tasks", "task"),
        ("delete note 2", "note"),
        ("mark task 3 done", "task"),
        ("the rover is parked beside the desk", "unknown"),
    ],
)
def test_intent_parser_handles_ambiguous_and_real_user_phrases(text, intent_name):
    intent = IntentParser().parse(text)

    assert intent.intent_name == intent_name


def test_intent_parser_detects_supported_intents():
    parser = IntentParser()

    assert parser.parse("calculate 15*8").intent_name == "calculate"
    assert parser.parse("show my notes").intent_name == "note"
    assert parser.parse("remember buy milk tomorrow").intent_name == "task"
    assert parser.parse("what did I tell you about my job").intent_name == "owner_memory"
    assert parser.parse("what time is it").intent_name == "time_date"


def test_intent_parser_confidence_values():
    parser = IntentParser()

    assert parser.parse("calculate 15*8").confidence >= 0.9
    assert parser.parse("show my notes").confidence >= 0.9
    assert parser.parse("what did I tell you about my job").confidence >= 0.8
    assert parser.parse("the sky is blue").confidence == 0.0


def test_intent_parser_extracts_task_entities():
    intent = IntentParser().parse("remember buy milk tomorrow")

    assert intent.intent_name == "task"
    assert intent.extracted_entities == {
        "action": "add",
        "text": "buy milk",
        "due": "tomorrow",
    }
    assert intent.raw_text == "remember buy milk tomorrow"


def test_intent_parser_cleans_remember_to_task_text():
    intent = IntentParser().parse("remember to buy milk")

    assert intent.intent_name == "task"
    assert intent.extracted_entities == {
        "action": "add",
        "text": "buy milk",
    }


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("remember that my favorite color is blue", "save"),
        ("remember my favorite color is blue", "save"),
        ("my favorite color is blue", "save"),
        ("what is my favorite color", "recall"),
        ("do you remember my favorite color", "recall"),
        ("forget my favorite color", "forget_keyed_fact"),
        ("delete my favorite color", "forget_keyed_fact"),
        ("update my favorite color to red", "update"),
        ("remember that modified white color is blue", "save"),
    ],
)
def test_owner_memory_phrases_outrank_task_and_device_rules(text, action):
    intent = IntentParser().parse(text)

    assert intent.intent_name == "owner_memory"
    assert intent.extracted_entities["action"] == action
    assert intent.extracted_entities["normalized_key"] == "favorite_color"


def test_intent_parser_keeps_remember_this_note_text():
    intent = IntentParser().parse("remember this idea: build ARES memory")

    assert intent.intent_name == "note"
    assert intent.extracted_entities == {
        "action": "add",
        "text": "idea: build ARES memory",
    }


def test_intent_parser_extracts_useful_entities_for_other_intents():
    parser = IntentParser()

    assert parser.parse("calculate 15*8").extracted_entities["expression"] == "15*8"
    assert parser.parse("show my notes").extracted_entities["action"] == "list"
    assert parser.parse("what time is it").extracted_entities["query_type"] == "time"
    assert parser.parse("what did I tell you about my job").extracted_entities["normalized_key"] == "job"


def test_intent_parser_unknown_intent():
    intent = IntentParser().parse("tell the rover to dance")

    assert intent.intent_name == "unknown"
    assert intent.confidence == 0.0
    assert intent.extracted_entities == {}
    assert intent.raw_text == "tell the rover to dance"


def test_tool_selector_uses_structured_intents():
    parser = IntentParser()
    selector = ToolSelector()
    skills = [
        TimeDateSkill(),
        MemoryRecallSkill(),
        CalculatorSkill(),
        NotesSkill(),
        TasksSkill(),
    ]

    task_selection = selector.select(parser.parse("remember buy milk tomorrow"), skills, run_before_intents=True)
    note_selection = selector.select(parser.parse("show my notes"), skills, run_before_intents=True)
    calculator_selection = selector.select(parser.parse("calculate 15*8"), skills, run_before_intents=True)

    assert task_selection.skill.name == "tasks"
    assert task_selection.reason == "structured intent match: task"
    assert note_selection.skill.name == "notes"
    assert calculator_selection.skill.name == "calculator"


def test_skill_manager_routes_structured_task_intent(tmp_path):
    tasks_store = TasksStore(path=tmp_path / "tasks.json", event_bus=EventBus())
    event_bus = EventBus(raise_handler_errors=True)
    manager = SkillManager(
        event_bus=event_bus,
        tasks_store=tasks_store,
    )
    manager.register(TasksSkill())

    response = manager.handle("remember buy milk tomorrow", run_before_intents=True)
    task = tasks_store.list()[0]
    detected = event_bus.history("skill.detected")[-1]

    assert response.skill == "tasks"
    assert response.text == f"Saved task {task.id}: buy milk (due tomorrow)"
    assert task.text == "buy milk"
    assert task.due == "tomorrow"
    assert detected.payload["intent"] == "task"
    assert detected.payload["entities"] == {
        "action": "add",
        "text": "buy milk",
        "due": "tomorrow",
    }
    assert detected.payload["reason"] == "structured intent match: task"
