import io

from core import IntentParser, Planner, get_global_conversation_context
from core.ExecutionPipeline import ExecutionPipeline
import memory.v1 as memory_v1
from events import get_global_bus
from interfaces import text_repl
from memory import MemoryStore, NotesStore, TasksStore
from skills.builtin.calculator import CalculatorSkill
import skills.manager as manager_module
import skills.selector as selector_module


def _run_repl(monkeypatch, tmp_path, script: str):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO(script))

    event_bus = get_global_bus()
    event_bus.clear_history()
    get_global_conversation_context().clear()

    text_repl.main()
    return event_bus


def test_repl_planner_creates_multistep_plan(monkeypatch, tmp_path, capsys):
    event_bus = _run_repl(
        monkeypatch,
        tmp_path,
        "hello\nRemember I have a dentist tomorrow and calculate 15*28.\nshow plan\nquit\n",
    )

    output = capsys.readouterr().out
    plan_event = event_bus.history("planner.plan_created")[-1]
    steps = plan_event.payload["steps"]

    assert [step["target"] for step in steps] == ["notes", "calculator"]
    assert [step["order"] for step in steps] == [1, 2]
    assert "Execution plan:" in output
    assert "1. notes.add - ready" in output
    assert "2. calculator.calculate - ready" in output


def test_repl_execution_pipeline_executes_notes_and_calculator(monkeypatch, tmp_path, capsys):
    event_bus = _run_repl(
        monkeypatch,
        tmp_path,
        "hello\nRemember I have a dentist tomorrow and calculate 15*28.\nquit\n",
    )

    output = capsys.readouterr().out
    notes = NotesStore(path=tmp_path / "notes.json", event_bus=event_bus).list()
    execution = event_bus.history("execution.completed")[-1].payload
    step_results = execution["step_results"]

    assert "Plan results:" in output
    assert "Saved note" in output
    assert "Result: 420" in output
    assert notes[0].text == "I have a dentist tomorrow"
    assert execution["success"] is True
    assert [step["target"] for step in step_results] == ["notes", "calculator"]
    assert all(step["success"] for step in step_results)


def test_repl_execution_pipeline_executes_task_and_memory(monkeypatch, tmp_path, capsys):
    event_bus = _run_repl(
        monkeypatch,
        tmp_path,
        "hello\nCreate a task for tomorrow and remember I like coffee.\nquit\n",
    )

    output = capsys.readouterr().out
    tasks = TasksStore(path=tmp_path / "tasks.json", event_bus=event_bus).list()
    memory_store = MemoryStore(
        short_path=memory_v1.SHORT_MEMORY_FILE,
        long_path=memory_v1.LONG_MEMORY_FILE,
        event_bus=event_bus,
    )
    memories = memory_store.recall(category="conversation_memory", long_term=True)
    execution = event_bus.history("execution.completed")[-1].payload

    assert "Saved task" in output
    assert "Stored memory: I like coffee" in output
    assert tasks[0].text == "task"
    assert tasks[0].due == "tomorrow"
    assert memories[0].content == "I like coffee"
    assert execution["success"] is True
    assert [step["target"] for step in execution["step_results"]] == ["tasks", "conversation_memory"]


def test_repl_partial_failure_is_reported_clearly(monkeypatch, tmp_path, capsys):
    event_bus = _run_repl(
        monkeypatch,
        tmp_path,
        "hello\ncalculate 2 + spam and save note still runs\nshow execution\nquit\n",
    )

    output = capsys.readouterr().out
    notes = NotesStore(path=tmp_path / "notes.json", event_bus=event_bus).list()
    execution = event_bus.history("execution.completed")[-1].payload
    step_results = execution["step_results"]

    assert "I cannot calculate that safely: only numbers and arithmetic operators are allowed." in output
    assert "Saved note" in output
    assert "Execution results:" in output
    assert "1. calculator.calculate - failed" in output
    assert "2. notes.add - ok" in output
    assert notes[0].text == "still runs"
    assert execution["success"] is False
    assert execution["stopped"] is False
    assert [step["success"] for step in step_results] == [False, True]
    assert step_results[0]["error_message"] == "only numbers and arithmetic operators are allowed"


def test_repl_last_execution_shows_multistep_execution(monkeypatch, tmp_path, capsys):
    _run_repl(
        monkeypatch,
        tmp_path,
        "hello\nRemember this integration note and calculate 2+2.\nshow last execution\nquit\n",
    )

    output = capsys.readouterr().out

    assert "Plan results:" in output
    assert "Execution results:" in output
    assert "1. notes.add - ok" in output
    assert "2. calculator.calculate - ok: Result: 4" in output


def test_repl_uses_intent_parser_planner_execution_pipeline_skill_manager_and_skill(
    monkeypatch,
    tmp_path,
    capsys,
):
    target_text = "integration trace"
    calls = []

    class RecordingIntentParser(IntentParser):
        def parse(self, text: str):
            if target_text in text:
                calls.append("intent_parser")
            return super().parse(text)

    class RecordingPlanner(Planner):
        def plan(self, intent):
            if target_text in intent.raw_text:
                calls.append("planner")
            return super().plan(intent)

    class RecordingExecutionPipeline(ExecutionPipeline):
        def execute(self, plan, context=None):
            if target_text in plan.raw_text:
                calls.append("execution_pipeline")
            return super().execute(plan, context)

    original_manager_handle = manager_module.SkillManager.handle
    original_calculator_handle = CalculatorSkill.handle

    def recording_manager_handle(self, text, *args, **kwargs):
        if target_text in str(text):
            calls.append("skill_manager")
        return original_manager_handle(self, text, *args, **kwargs)

    def recording_calculator_handle(self, text, context):
        if "2+2" in (text or "").replace(" ", ""):
            calls.append("skill")
        return original_calculator_handle(self, text, context)

    monkeypatch.setattr(manager_module, "IntentParser", RecordingIntentParser)
    monkeypatch.setattr(selector_module, "Planner", RecordingPlanner)
    monkeypatch.setattr(manager_module, "ExecutionPipeline", RecordingExecutionPipeline)
    monkeypatch.setattr(manager_module.SkillManager, "handle", recording_manager_handle)
    monkeypatch.setattr(CalculatorSkill, "handle", recording_calculator_handle)

    _run_repl(
        monkeypatch,
        tmp_path,
        "hello\nRemember this integration trace and calculate 2+2.\nquit\n",
    )

    output = capsys.readouterr().out

    assert "Result: 4" in output
    assert calls == [
        "skill_manager",
        "intent_parser",
        "planner",
        "execution_pipeline",
        "skill",
    ]
