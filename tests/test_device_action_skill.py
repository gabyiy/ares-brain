import io

from core import IntentParser, LocalDeviceActionAdapter, Planner
from events import get_global_bus
from interfaces import text_repl
import memory.v1 as memory_v1
from skills import SkillContext, SkillManager, ToolSelector
from skills.builtin.device_action import DeviceActionSkill


def test_device_action_skill_echo_works():
    response = DeviceActionSkill().handle(
        "echo hello device",
        SkillContext(device_action_adapter=LocalDeviceActionAdapter()),
    )

    assert response.skill == "device_action"
    assert response.text == "hello device"
    assert response.metadata["action_name"] == "echo"
    assert response.metadata["data"] == {"message": "hello device"}


def test_device_action_skill_lists_actions():
    response = DeviceActionSkill().handle(
        "list device actions",
        SkillContext(device_action_adapter=LocalDeviceActionAdapter()),
    )

    assert response.skill == "device_action"
    assert response.text == "Available device actions: echo, system_status_mock, list_actions."
    assert [action["name"] for action in response.metadata["data"]["actions"]] == [
        "echo",
        "system_status_mock",
        "list_actions",
    ]


def test_device_action_skill_system_status_mock_works():
    response = DeviceActionSkill().handle(
        "system status",
        SkillContext(device_action_adapter=LocalDeviceActionAdapter()),
    )

    assert response.skill == "device_action"
    assert response.text == "System status mock: ok."
    assert response.metadata["data"]["source"] == "mock"
    assert response.metadata["data"]["checks"]["shell_execution"] == "disabled"


def test_device_action_skill_rejects_dangerous_actions():
    response = DeviceActionSkill().handle(
        "run command del important-file.txt",
        SkillContext(device_action_adapter=LocalDeviceActionAdapter()),
    )

    assert response.skill == "device_action"
    assert response.text == "I cannot run that device action safely: run command is not available."
    assert response.metadata["error"] == "run command is not available"
    assert response.metadata["dangerous"] is True


def test_unknown_device_action_fails_safely_through_skill():
    response = DeviceActionSkill().handle(
        "device action calibrate",
        SkillContext(device_action_adapter=LocalDeviceActionAdapter()),
    )

    assert response.skill == "device_action"
    assert response.text == "Device action is not available: calibrate"
    assert response.metadata["error"] == "Device action is not available: calibrate"


def test_tool_selector_routes_device_action_skill():
    selector = ToolSelector()
    selection = selector.select("system status", [DeviceActionSkill()], run_before_intents=True)

    assert selection.skill.name == "device_action"
    assert selection.reason == "structured intent match: device_action"
    assert selection.plan.steps[0].target == "device_action"


def test_planner_routes_safe_device_action():
    intent = IntentParser().parse("echo hello planner")
    plan = Planner().plan(intent)

    assert intent.intent_name == "device_action"
    assert plan.errors == []
    assert len(plan.steps) == 1
    assert plan.steps[0].target == "device_action"
    assert plan.steps[0].action == "echo"
    assert plan.steps[0].entities["parameters"] == {"message": "hello planner"}


def test_skill_manager_executes_device_action_through_pipeline():
    manager = SkillManager(event_bus=get_global_bus())
    manager.register(DeviceActionSkill())

    response = manager.handle("system status", run_before_intents=True)

    assert response.skill == "device_action"
    assert response.text == "System status mock: ok."
    assert manager.last_plan.steps[0].target == "device_action"
    assert manager.last_execution.step_results[0].target == "device_action"
    assert manager.last_execution.step_results[0].success is True


def test_text_repl_executes_safe_device_action(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nlist device actions\nquit\n"))

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    detected = [
        event.payload
        for event in event_bus.history("skill.detected")
        if event.payload.get("skill") == "device_action"
    ]

    assert "Available device actions: echo, system_status_mock, list_actions." in output
    assert detected
