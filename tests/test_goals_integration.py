import io

from core import ExecutionPipeline, IntentParser, Planner, ToolChain
from events import EventBus, get_global_bus
from interfaces import text_repl
from memory import GoalsStore, MemoryStore
import memory.v1 as memory_v1
from skills import SkillContext, SkillManager, SkillRegistry, ToolSelector
from skills.builtin.calculator import CalculatorSkill
from skills.builtin.goals import GoalsSkill


def test_intent_parser_routes_goal_commands():
    parser = IntentParser()

    add = parser.parse("add goal Build ARES priority high")
    listed = parser.parse("list goals")
    shown = parser.parse("show goal goal-123")
    milestone = parser.parse("add milestone to goal goal-123 Write architecture")

    assert add.intent_name == "goal"
    assert add.extracted_entities == {
        "action": "add",
        "title": "Build ARES",
        "description": "",
        "priority": "high",
    }
    assert listed.intent_name == "goal"
    assert listed.extracted_entities["action"] == "list"
    assert shown.extracted_entities == {"action": "show", "goal_id": "goal-123"}
    assert milestone.extracted_entities == {
        "action": "add_milestone",
        "goal_id": "goal-123",
        "milestone": "Write architecture",
    }


def test_tool_selector_routes_goal_skill():
    selector = ToolSelector()
    selection = selector.select(
        "add goal Build ARES",
        [GoalsSkill()],
        run_before_intents=True,
    )

    assert selection.skill.name == "goals"
    assert selection.reason == "structured intent match: goal"
    assert selection.plan.steps[0].target == "goals"


def test_planner_builds_goal_steps():
    parser = IntentParser()
    planner = Planner()

    add_plan = planner.plan(parser.parse("add goal Build ARES priority high"))
    milestone_plan = planner.plan(parser.parse("add milestone to goal goal-123 Write architecture"))

    assert add_plan.errors == []
    assert add_plan.steps[0].target == "goals"
    assert add_plan.steps[0].action == "add"
    assert add_plan.steps[0].entities["title"] == "Build ARES"
    assert add_plan.steps[0].entities["priority"] == "high"
    assert milestone_plan.steps[0].action == "add_milestone"
    assert milestone_plan.steps[0].entities["goal_id"] == "goal-123"


def test_execution_pipeline_runs_goal_step(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    goals_store = GoalsStore(path=tmp_path / "goals.json", event_bus=event_bus)
    registry = SkillRegistry()
    registry.register(GoalsSkill())
    pipeline = ExecutionPipeline(registry.get, event_bus=event_bus)
    plan = Planner().plan(IntentParser().parse("add goal Build ARES"))

    result = pipeline.execute(
        plan,
        SkillContext(event_bus=event_bus, goals_store=goals_store),
    )

    assert result.success is True
    assert result.step_results[0].target == "goals"
    assert result.step_results[0].returned_data["skill"] == "goals"
    assert goals_store.list()[0].title == "Build ARES"


def test_skill_manager_executes_goal_through_planner_pipeline(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    goals_store = GoalsStore(path=tmp_path / "goals.json", event_bus=event_bus)
    manager = SkillManager(event_bus=event_bus, goals_store=goals_store)
    manager.register(GoalsSkill())

    response = manager.handle("add goal Build ARES", run_before_intents=True)

    assert response.skill == "goals"
    assert "Saved goal" in response.text
    assert goals_store.list()[0].title == "Build ARES"
    assert manager.last_plan.steps[0].target == "goals"
    assert manager.last_execution.success is True
    assert event_bus.history("tool_chain.completed")
    assert event_bus.history("execution.completed")


def test_text_repl_routes_goals_skill_and_persists_goal(monkeypatch, tmp_path, capsys):
    goals_path = tmp_path / "goals.json"
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(goals_path))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("hello\nadd goal Build ARES\nlist goals\nquit\n"),
    )

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    goals = GoalsStore(path=goals_path, event_bus=event_bus).list()
    detected = [
        event.payload
        for event in event_bus.history("skill.detected")
        if event.payload.get("skill") == "goals"
    ]

    assert len(goals) == 1
    assert goals[0].title == "Build ARES"
    assert "Saved goal" in output
    assert "Your goals:" in output
    assert "Build ARES" in output
    assert detected


def test_text_repl_runs_goal_lifecycle_commands(monkeypatch, tmp_path, capsys):
    goals_path = tmp_path / "goals.json"
    event_bus = get_global_bus()
    event_bus.clear_history()
    seeded = GoalsStore(path=goals_path, event_bus=event_bus).add("Build ARES")

    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(goals_path))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            "hello\n"
            "list goals\n"
            f"add milestone to goal {seeded.id} Write tests\n"
            f"pause goal {seeded.id}\n"
            "confirm\n"
            f"complete goal {seeded.id}\n"
            "confirm\n"
            f"show goal {seeded.id}\n"
            "quit\n"
        ),
    )

    text_repl.main()

    output = capsys.readouterr().out
    reloaded = GoalsStore(path=goals_path, event_bus=event_bus)
    goal = reloaded.get(seeded.id)
    detected = [
        event.payload
        for event in event_bus.history("skill.detected")
        if event.payload.get("skill") == "goals"
    ]
    decisions = event_bus.history("confirmation.decision")

    assert goal.status == "completed"
    assert goal.milestones == ["Write tests"]
    assert "Your goals:" in output
    assert f"Added milestone to goal {seeded.id}: Write tests" in output
    assert f"Confirmation required to pause goal {seeded.id}" in output
    assert f"Paused goal {seeded.id}." in output
    assert f"Confirmation required to complete goal {seeded.id}" in output
    assert f"Completed goal {seeded.id}." in output
    assert "Milestones:" in output
    assert "Write tests" in output
    assert len(detected) >= 4
    assert len(decisions) == 2


def test_text_repl_goal_persistence_after_reload(monkeypatch, tmp_path, capsys):
    goals_path = tmp_path / "goals.json"
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(goals_path))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nadd goal Build persistent goals\nquit\n"))

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    capsys.readouterr()
    reloaded = GoalsStore(path=goals_path, event_bus=event_bus)
    goals = reloaded.list()

    assert len(goals) == 1
    assert goals[0].title == "Build persistent goals"
    assert goals[0].status == "active"


def test_tool_chain_executes_goal_and_calculator_steps(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    goals_store = GoalsStore(path=tmp_path / "goals.json", event_bus=event_bus)
    registry = SkillRegistry()
    registry.register(GoalsSkill())
    registry.register(CalculatorSkill())
    chain = ToolChain(
        execution_pipeline=ExecutionPipeline(registry.get, event_bus=event_bus),
        event_bus=event_bus,
    )
    plan = Planner().plan(IntentParser().parse("add goal Build ARES and calculate 2+2"))

    result = chain.execute(
        plan,
        SkillContext(event_bus=event_bus, goals_store=goals_store),
    )

    assert result.success is True
    assert [step.target for step in result.trace] == ["goals", "calculator"]
    assert [step.status for step in result.trace] == ["ok", "ok"]
    assert goals_store.list()[0].title == "Build ARES"
    assert result.execution.step_results[-1].returned_data["text"] == "Result: 4"
    assert event_bus.history("tool_chain.completed")


def test_tool_chain_executes_goal_and_memory_steps(tmp_path):
    event_bus = EventBus(raise_handler_errors=True)
    goals_store = GoalsStore(path=tmp_path / "goals.json", event_bus=event_bus)
    memory_store = MemoryStore(
        short_path=tmp_path / "short.json",
        long_path=tmp_path / "long.json",
        event_bus=event_bus,
    )
    registry = SkillRegistry()
    registry.register(GoalsSkill())
    chain = ToolChain(
        execution_pipeline=ExecutionPipeline(registry.get, event_bus=event_bus, memory_store=memory_store),
        event_bus=event_bus,
    )
    plan = Planner().plan(IntentParser().parse("add goal Build ARES and remember I like goals"))

    result = chain.execute(
        plan,
        SkillContext(event_bus=event_bus, goals_store=goals_store, memory_store=memory_store),
    )
    memories = memory_store.recall(category="conversation_memory", long_term=True)

    assert result.success is True
    assert [step.target for step in result.trace] == ["goals", "conversation_memory"]
    assert goals_store.list()[0].title == "Build ARES"
    assert memories[0].content == "I like goals"
