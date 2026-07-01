from core import IntentParser, Planner
from events import EventBus
from memory import MemoryStore, NotesStore, TasksStore
from skills import SkillManager, ToolSelector
from skills.builtin.calculator import CalculatorSkill
from skills.builtin.notes import NotesSkill
from skills.builtin.tasks import TasksSkill


def _plan(text: str):
    intent = IntentParser().parse(text)
    return Planner().plan(intent)


def test_planner_builds_single_step_plan():
    plan = _plan("calculate 2 + 2")

    assert plan.errors == []
    assert len(plan.steps) == 1
    assert plan.steps[0].order == 1
    assert plan.steps[0].target == "calculator"
    assert plan.steps[0].action == "calculate"
    assert plan.steps[0].entities["expression"] == "2 + 2"


def test_planner_builds_two_step_plan():
    plan = _plan("Remember I have a dentist tomorrow and calculate 15*28.")

    assert plan.errors == []
    assert [step.order for step in plan.steps] == [1, 2]
    assert [step.target for step in plan.steps] == ["notes", "calculator"]


def test_planner_supports_mixed_notes_and_calculator():
    plan = _plan("Remember I have a dentist tomorrow and calculate 15*28.")

    note_step, calculator_step = plan.steps

    assert note_step.input_text == "save note I have a dentist tomorrow"
    assert note_step.entities["text"] == "I have a dentist tomorrow"
    assert calculator_step.input_text == "calculate 15*28"
    assert calculator_step.entities["expression"] == "15*28"


def test_planner_supports_mixed_task_and_memory():
    plan = _plan("Create a task for tomorrow and remember I like coffee.")

    task_step, memory_step = plan.steps

    assert plan.errors == []
    assert task_step.target == "tasks"
    assert task_step.entities == {"action": "add", "text": "task", "due": "tomorrow"}
    assert task_step.input_text == "add task task due tomorrow"
    assert memory_step.target == "conversation_memory"
    assert memory_step.entities == {"content": "I like coffee"}


def test_planner_returns_invalid_plan_errors_cleanly():
    plan = _plan("the rover is parked beside the desk")

    assert plan.steps == []
    assert plan.errors == ["No planner support for intent: unknown"]


def test_planner_skips_impossible_steps_and_preserves_order():
    plan = _plan("save note and calculate 2+2")

    assert [step.order for step in plan.steps] == [1]
    assert plan.steps[0].target == "calculator"
    assert plan.errors == ["Skipped notes.add: Missing note text."]


def test_planner_serialization():
    plan = _plan("Remember I have a dentist tomorrow and calculate 15*28.")
    payload = plan.to_dict()

    assert payload["raw_text"] == "Remember I have a dentist tomorrow and calculate 15*28."
    assert payload["intent_name"] == "task"
    assert payload["steps"][0]["target"] == "notes"
    assert payload["steps"][1]["target"] == "calculator"
    assert payload["errors"] == []


def test_tool_selector_attaches_plan_to_selection():
    selector = ToolSelector()
    selection = selector.select(
        "calculate 2 + 2",
        [CalculatorSkill()],
        run_before_intents=True,
    )

    assert selection.skill.name == "calculator"
    assert selection.plan.steps[0].target == "calculator"
    assert selector.last_plan == selection.plan


def test_skill_manager_executes_mixed_note_calculator_plan(tmp_path):
    notes_store = NotesStore(path=tmp_path / "notes.json", event_bus=EventBus())
    manager = SkillManager(
        event_bus=EventBus(raise_handler_errors=True),
        notes_store=notes_store,
    )
    manager.register(NotesSkill())
    manager.register(CalculatorSkill())

    response = manager.handle(
        "Remember I have a dentist tomorrow and calculate 15*28.",
        run_before_intents=True,
    )

    assert response.skill == "planner"
    assert "Saved note" in response.text
    assert "Result: 420" in response.text
    assert notes_store.list()[0].text == "I have a dentist tomorrow"
    assert manager.last_plan.steps[0].target == "notes"


def test_skill_manager_executes_mixed_task_memory_plan(tmp_path):
    tasks_store = TasksStore(path=tmp_path / "tasks.json", event_bus=EventBus())
    memory_store = MemoryStore(
        short_path=tmp_path / "short.json",
        long_path=tmp_path / "long.json",
        event_bus=EventBus(),
    )
    manager = SkillManager(
        event_bus=EventBus(raise_handler_errors=True),
        memory_store=memory_store,
        tasks_store=tasks_store,
    )
    manager.register(TasksSkill())

    response = manager.handle(
        "Create a task for tomorrow and remember I like coffee.",
        run_before_intents=True,
    )

    tasks = tasks_store.list()
    memories = memory_store.recall(category="conversation_memory", long_term=True)

    assert response.skill == "planner"
    assert tasks[0].text == "task"
    assert tasks[0].due == "tomorrow"
    assert memories[0].content == "I like coffee"
