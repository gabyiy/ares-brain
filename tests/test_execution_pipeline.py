import logging

from core import ExecutionPipeline, IntentParser, Plan, Planner, PlanStep, RollbackHook
from events import EventBus
from memory import MemoryStore, NotesStore, TasksStore
from skills import SkillContext, SkillManager, SkillRegistry
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


def test_execution_pipeline_runs_single_step():
    event_bus = EventBus(raise_handler_errors=True)
    registry = _registry(CalculatorSkill())
    pipeline = ExecutionPipeline(registry.get, event_bus=event_bus)

    result = pipeline.execute(
        _plan("calculate 2 + 2"),
        SkillContext(event_bus=event_bus),
    )
    step = result.step_results[0]

    assert result.success is True
    assert result.stopped is False
    assert step.success is True
    assert step.start_time.endswith("Z")
    assert step.end_time.endswith("Z")
    assert step.duration >= 0
    assert step.returned_data["text"] == "Result: 4"
    assert step.error_message == ""
    assert event_bus.history("execution.step_completed")
    assert event_bus.history("execution.completed")[-1].payload["success"] is True


def test_execution_pipeline_runs_multi_step_notes_and_calculator(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    notes_store = NotesStore(path=tmp_path / "notes.json", event_bus=event_bus)
    registry = _registry(NotesSkill(), CalculatorSkill())
    pipeline = ExecutionPipeline(registry.get, event_bus=event_bus)

    result = pipeline.execute(
        _plan("Remember I have a dentist tomorrow and calculate 15*28."),
        SkillContext(event_bus=event_bus, notes_store=notes_store),
    )

    assert result.success is True
    assert [step.order for step in result.step_results] == [1, 2]
    assert [step.target for step in result.step_results] == ["notes", "calculator"]
    assert notes_store.list()[0].text == "I have a dentist tomorrow"
    assert "Saved note" in result.format_response_text()
    assert "Result: 420" in result.format_response_text()


def test_execution_pipeline_runs_task_and_memory_steps(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    tasks_store = TasksStore(path=tmp_path / "tasks.json", event_bus=event_bus)
    memory_store = MemoryStore(
        short_path=tmp_path / "short.json",
        long_path=tmp_path / "long.json",
        event_bus=event_bus,
    )
    registry = _registry(TasksSkill())
    pipeline = ExecutionPipeline(registry.get, event_bus=event_bus, memory_store=memory_store)

    result = pipeline.execute(
        _plan("Create a task for tomorrow and remember I like coffee."),
        SkillContext(event_bus=event_bus, memory_store=memory_store, tasks_store=tasks_store),
    )

    tasks = tasks_store.list()
    memories = memory_store.recall(category="conversation_memory", long_term=True)

    assert result.success is True
    assert tasks[0].text == "task"
    assert tasks[0].due == "tomorrow"
    assert memories[0].content == "I like coffee"


def test_execution_pipeline_stops_on_unrecoverable_failure_and_calls_rollback():
    class RecordingRollback(RollbackHook):
        def __init__(self):
            self.calls = []

        def rollback(self, completed_steps, failed_step, context):
            self.calls.append((completed_steps, failed_step, context))

    event_bus = EventBus(raise_handler_errors=True)
    registry = _registry(CalculatorSkill())
    rollback = RecordingRollback()
    pipeline = ExecutionPipeline(registry.get, event_bus=event_bus, rollback_hook=rollback)
    plan = Plan(
        raw_text="missing skill then calculate",
        intent_name="unknown",
        steps=[
            PlanStep(
                order=1,
                target="missing_skill",
                action="run",
                input_text="missing",
                intent_name="unknown",
            ),
            PlanStep(
                order=2,
                target="calculator",
                action="calculate",
                input_text="calculate 2 + 2",
                intent_name="calculate",
                entities={"expression": "2 + 2"},
            ),
        ],
    )

    result = pipeline.execute(plan, SkillContext(event_bus=event_bus))

    assert result.success is False
    assert result.stopped is True
    assert result.rollback_attempted is True
    assert len(result.step_results) == 1
    assert result.step_results[0].recoverable is False
    assert result.step_results[0].error_message == "Skill is not available: missing_skill"
    assert rollback.calls
    assert rollback.calls[0][1].target == "missing_skill"
    assert event_bus.history("execution.rollback_requested")


def test_execution_pipeline_continues_after_recoverable_failure(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    notes_store = NotesStore(path=tmp_path / "notes.json", event_bus=event_bus)
    registry = _registry(CalculatorSkill(), NotesSkill())
    pipeline = ExecutionPipeline(registry.get, event_bus=event_bus)
    plan = Plan(
        raw_text="bad math then note",
        intent_name="calculate",
        steps=[
            PlanStep(
                order=1,
                target="calculator",
                action="calculate",
                input_text="calculate __import__('os')",
                intent_name="calculate",
                entities={"expression": "__import__('os')"},
            ),
            PlanStep(
                order=2,
                target="notes",
                action="add",
                input_text="save note still runs",
                intent_name="note",
                entities={"action": "add", "text": "still runs"},
            ),
        ],
    )

    result = pipeline.execute(plan, SkillContext(event_bus=event_bus, notes_store=notes_store))

    assert result.success is False
    assert result.stopped is False
    assert [step.success for step in result.step_results] == [False, True]
    assert result.step_results[0].recoverable is True
    assert result.step_results[0].error_message == "only numbers and arithmetic operators are allowed"
    assert notes_store.list()[0].text == "still runs"


def test_execution_pipeline_preserves_execution_order():
    registry = _registry(CalculatorSkill())
    pipeline = ExecutionPipeline(registry.get)
    plan = Plan(
        raw_text="out of order",
        intent_name="calculate",
        steps=[
            PlanStep(
                order=2,
                target="calculator",
                action="calculate",
                input_text="calculate 3 + 3",
                intent_name="calculate",
                entities={"expression": "3 + 3"},
            ),
            PlanStep(
                order=1,
                target="calculator",
                action="calculate",
                input_text="calculate 1 + 1",
                intent_name="calculate",
                entities={"expression": "1 + 1"},
            ),
        ],
    )

    result = pipeline.execute(plan, SkillContext())

    assert [step.order for step in result.step_results] == [1, 2]
    assert [step.returned_data["text"] for step in result.step_results] == ["Result: 2", "Result: 6"]


def test_execution_pipeline_logs_execution(caplog):
    registry = _registry(CalculatorSkill())
    pipeline = ExecutionPipeline(registry.get)
    caplog.set_level(logging.INFO, logger="ares.execution")

    pipeline.execute(_plan("calculate 2 + 2"), SkillContext())

    messages = [record.getMessage() for record in caplog.records if record.name == "ares.execution"]
    assert any("Execution started" in message for message in messages)
    assert any("Execution step started" in message for message in messages)
    assert any("Execution step completed" in message for message in messages)
    assert any("Execution completed" in message for message in messages)


def test_skill_manager_uses_execution_pipeline_for_plan_steps(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    notes_store = NotesStore(path=tmp_path / "notes.json", event_bus=event_bus)
    manager = SkillManager(event_bus=event_bus, notes_store=notes_store)
    manager.register(NotesSkill())
    manager.register(CalculatorSkill())

    response = manager.handle(
        "Remember I have a dentist tomorrow and calculate 15*28.",
        run_before_intents=True,
    )

    assert response.skill == "planner"
    assert response.metadata["execution"]["success"] is True
    assert manager.last_execution.success is True
    assert "Saved note" in response.text
    assert "Result: 420" in response.text
    assert event_bus.history("execution.completed")
