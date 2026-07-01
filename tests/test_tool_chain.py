import io

from core import (
    ExecutionPipeline,
    IntentParser,
    MAX_CHAIN_DEPTH,
    Plan,
    Planner,
    PlanStep,
    ToolChain,
    get_global_conversation_context,
)
import memory.v1 as memory_v1
from events import EventBus, get_global_bus
from interfaces import text_repl
from memory import MemoryStore, NotesStore, TasksStore
from skills import SkillContext, SkillRegistry
from skills.builtin.calculator import CalculatorSkill
from skills.builtin.notes import NotesSkill
from skills.builtin.tasks import TasksSkill


def _plan(text: str) -> Plan:
    return Planner().plan(IntentParser().parse(text))


def _registry(*skills):
    registry = SkillRegistry()
    for skill in skills:
        registry.register(skill)
    return registry


def _chain(registry, event_bus=None, memory_store=None, max_depth=MAX_CHAIN_DEPTH):
    pipeline = ExecutionPipeline(
        skill_resolver=registry.get,
        event_bus=event_bus,
        memory_store=memory_store,
    )
    return ToolChain(
        execution_pipeline=pipeline,
        event_bus=event_bus,
        max_depth=max_depth,
    )


def test_tool_chain_executes_memory_and_calculator(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    memory_store = MemoryStore(
        short_path=tmp_path / "short.json",
        long_path=tmp_path / "long.json",
        event_bus=event_bus,
    )
    chain = _chain(_registry(CalculatorSkill()), event_bus=event_bus, memory_store=memory_store)

    result = chain.execute(
        _plan("remember I like coffee and calculate 2+2"),
        SkillContext(event_bus=event_bus, memory_store=memory_store),
    )
    memories = memory_store.recall(category="conversation_memory", long_term=True)

    assert result.success is True
    assert [step.target for step in result.trace] == ["conversation_memory", "calculator"]
    assert [step.status for step in result.trace] == ["ok", "ok"]
    assert memories[0].content == "I like coffee"
    assert result.execution.step_results[-1].returned_data["text"] == "Result: 4"
    assert event_bus.history("tool_chain.completed")


def test_tool_chain_executes_note_and_memory(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    notes_store = NotesStore(path=tmp_path / "notes.json", event_bus=event_bus)
    memory_store = MemoryStore(
        short_path=tmp_path / "short.json",
        long_path=tmp_path / "long.json",
        event_bus=event_bus,
    )
    chain = _chain(_registry(NotesSkill()), event_bus=event_bus, memory_store=memory_store)

    result = chain.execute(
        _plan("save note calibrate rover sensors and remember I like rovers"),
        SkillContext(event_bus=event_bus, memory_store=memory_store, notes_store=notes_store),
    )

    assert result.success is True
    assert [step.target for step in result.trace] == ["notes", "conversation_memory"]
    assert notes_store.list()[0].text == "calibrate rover sensors"
    assert memory_store.recall(category="conversation_memory", long_term=True)[0].content == "I like rovers"


def test_tool_chain_executes_task_reminder_and_memory(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    tasks_store = TasksStore(path=tmp_path / "tasks.json", event_bus=event_bus)
    memory_store = MemoryStore(
        short_path=tmp_path / "short.json",
        long_path=tmp_path / "long.json",
        event_bus=event_bus,
    )
    chain = _chain(_registry(TasksSkill()), event_bus=event_bus, memory_store=memory_store)

    result = chain.execute(
        _plan("remind me to call mom tomorrow and remember I like coffee"),
        SkillContext(event_bus=event_bus, memory_store=memory_store, tasks_store=tasks_store),
    )
    task = tasks_store.list()[0]

    assert result.success is True
    assert [step.target for step in result.trace] == ["tasks", "conversation_memory"]
    assert task.text == "call mom"
    assert task.due == "tomorrow"
    assert memory_store.recall(category="conversation_memory", long_term=True)[0].content == "I like coffee"


def test_tool_chain_preserves_ordering(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    notes_store = NotesStore(path=tmp_path / "notes.json", event_bus=event_bus)
    memory_store = MemoryStore(
        short_path=tmp_path / "short.json",
        long_path=tmp_path / "long.json",
        event_bus=event_bus,
    )
    chain = _chain(
        _registry(NotesSkill(), CalculatorSkill()),
        event_bus=event_bus,
        memory_store=memory_store,
    )

    result = chain.execute(
        _plan("save note first step and remember I like order and calculate 1+1"),
        SkillContext(event_bus=event_bus, memory_store=memory_store, notes_store=notes_store),
    )

    assert result.success is True
    assert [step.order for step in result.trace] == [1, 2, 3]
    assert [step.target for step in result.trace] == ["notes", "conversation_memory", "calculator"]


def test_tool_chain_rejects_max_depth_without_execution():
    class FailingPipeline:
        def __init__(self):
            self.called = False

        def execute(self, plan, context=None):
            self.called = True
            raise AssertionError("ToolChain should reject before execution")

    pipeline = FailingPipeline()
    chain = ToolChain(execution_pipeline=pipeline, max_depth=MAX_CHAIN_DEPTH)
    plan = Plan(
        raw_text="too deep",
        intent_name="calculate",
        steps=[
            PlanStep(
                order=index,
                target="calculator",
                action="calculate",
                input_text=f"calculate {index}+1",
                intent_name="calculate",
                entities={"expression": f"{index}+1"},
            )
            for index in range(1, MAX_CHAIN_DEPTH + 2)
        ],
    )

    result = chain.execute(plan, SkillContext())

    assert result.success is False
    assert pipeline.called is False
    assert result.execution is None
    assert result.errors == [f"Tool chain exceeds max depth {MAX_CHAIN_DEPTH}: {MAX_CHAIN_DEPTH + 1} steps."]
    assert all(step.status == "rejected" for step in result.trace)


def test_tool_chain_prevents_repeated_step_loop_without_execution():
    class FailingPipeline:
        def __init__(self):
            self.called = False

        def execute(self, plan, context=None):
            self.called = True
            raise AssertionError("ToolChain should reject before execution")

    duplicate = PlanStep(
        order=1,
        target="calculator",
        action="calculate",
        input_text="calculate 2+2",
        intent_name="calculate",
        entities={"expression": "2+2"},
    )
    plan = Plan(
        raw_text="loop",
        intent_name="calculate",
        steps=[
            duplicate,
            PlanStep(
                order=2,
                target=duplicate.target,
                action=duplicate.action,
                input_text=duplicate.input_text,
                intent_name=duplicate.intent_name,
                entities=dict(duplicate.entities),
            ),
        ],
    )
    pipeline = FailingPipeline()
    chain = ToolChain(execution_pipeline=pipeline)

    result = chain.execute(plan, SkillContext())

    assert result.success is False
    assert pipeline.called is False
    assert result.execution is None
    assert result.errors == ["Loop detected: step 1 and step 2 repeat calculator.calculate."]
    assert all(step.status == "rejected" for step in result.trace)


def test_text_repl_shows_chain_and_chain_history(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("hello\nremember I like tea and calculate 2+2\nshow chain\nshow chain history\nquit\n"),
    )

    event_bus = get_global_bus()
    event_bus.clear_history()
    get_global_conversation_context().clear()

    text_repl.main()

    output = capsys.readouterr().out

    assert "Plan results:" in output
    assert "Tool chain:" in output
    assert "1. conversation_memory.remember - ok" in output
    assert "2. calculator.calculate - ok" in output
    assert "Tool chain history:" in output
    assert "ok - 2 steps - remember I like tea and calculate 2+2" in output
