import io

from core import AppLaunchConfig, CoreService, IntentParser, LocalDeviceActionAdapter, Planner
from events import get_global_bus
from interfaces import text_repl
import memory.v1 as memory_v1
from skills import SkillContext, SkillManager, ToolSelector
from skills.builtin.device_action import DeviceActionSkill


EXPECTED_DEVICE_ACTIONS = [
    "echo",
    "system_status_mock",
    "list_actions",
    "list_apps",
    "lock_pc",
    "sleep_pc",
    "open_app",
]


def _enabled_app_config(app_id="calculator", display_name="Calculator"):
    return AppLaunchConfig(
        app_id=app_id,
        display_name=display_name,
        command_placeholder=f"C:\\Allowed\\{app_id}.exe",
        enabled=True,
        metadata={"source": "test_allowlist", "platform": "windows"},
    )


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
    assert (
        response.text
        == "Available device actions: echo, system_status_mock, list_actions, list_apps, lock_pc, sleep_pc, open_app."
    )
    assert [action["name"] for action in response.metadata["data"]["actions"]] == EXPECTED_DEVICE_ACTIONS


def test_device_action_skill_lists_apps():
    response = DeviceActionSkill().handle(
        "list apps",
        SkillContext(device_action_adapter=LocalDeviceActionAdapter()),
    )

    assert response.skill == "device_action"
    assert response.text == "Allowlisted apps: notepad (disabled), calculator, browser (disabled)."
    assert [app["app_id"] for app in response.metadata["data"]["apps"]] == [
        "notepad",
        "calculator",
        "browser",
    ]
    apps = {app["app_id"]: app for app in response.metadata["data"]["apps"]}
    assert apps["calculator"]["enabled"] is True
    assert apps["calculator"]["requires_confirmation"] is True
    assert apps["notepad"]["enabled"] is False
    assert apps["browser"]["enabled"] is False


def test_device_action_skill_system_status_mock_works():
    response = DeviceActionSkill().handle(
        "system status",
        SkillContext(device_action_adapter=LocalDeviceActionAdapter()),
    )

    assert response.skill == "device_action"
    assert response.text == "System status: ok."
    assert response.metadata["data"]["source"] == "pc_service"
    assert response.metadata["data"]["status"] == "ok"
    assert isinstance(response.metadata["data"]["operating_system"], str)
    assert isinstance(response.metadata["data"]["hostname"], str)
    assert isinstance(response.metadata["data"]["current_user"], str)
    assert isinstance(response.metadata["data"]["python_version"], str)
    assert response.metadata["data"]["available_actions"] == EXPECTED_DEVICE_ACTIONS
    assert response.metadata["data"]["checks"]["shell_execution"] == "disabled"
    assert response.metadata["data"]["checks"]["network_access"] == "not_used"


def test_device_action_skill_returns_confirmation_required_for_shutdown():
    response = DeviceActionSkill().handle(
        "shutdown",
        SkillContext(device_action_adapter=LocalDeviceActionAdapter()),
    )

    assert response.skill == "device_action"
    assert response.text == 'Confirmation required for device action "shutdown". Device action was not executed.'
    assert response.metadata["error"] == "confirmation_required"
    assert response.metadata["danger_classification"] == "confirmation_required"
    assert response.metadata["confirmation_required"] is True
    assert response.metadata["executed"] is False
    assert response.metadata["confirmation_request"]["token"] == "device-action-confirmation:shutdown"


def test_device_action_skill_returns_confirmation_required_for_restart():
    response = DeviceActionSkill().handle(
        "restart",
        SkillContext(device_action_adapter=LocalDeviceActionAdapter()),
    )

    assert response.skill == "device_action"
    assert response.text == 'Confirmation required for device action "restart". Device action was not executed.'
    assert response.metadata["error"] == "confirmation_required"
    assert response.metadata["danger_classification"] == "confirmation_required"
    assert response.metadata["confirmation_required"] is True
    assert response.metadata["executed"] is False


def test_device_action_skill_returns_confirmation_required_for_lock_pc_without_execution():
    calls = []
    adapter = LocalDeviceActionAdapter(
        lock_impl=lambda: calls.append("locked") or True,
        platform_system=lambda: "Windows",
    )
    response = DeviceActionSkill().handle(
        "lock pc",
        SkillContext(device_action_adapter=adapter),
    )

    assert response.skill == "device_action"
    assert response.text == 'Confirmation required for device action "lock_pc". Device action was not executed.'
    assert response.metadata["error"] == "confirmation_required"
    assert response.metadata["danger_classification"] == "confirmation_required"
    assert response.metadata["confirmation_required"] is True
    assert response.metadata["executed"] is False
    assert response.metadata["confirmation_request"]["token"] == "device-action-confirmation:lock_pc"
    assert calls == []


def test_device_action_skill_returns_confirmation_required_for_sleep_pc_without_execution():
    calls = []
    adapter = LocalDeviceActionAdapter(
        sleep_impl=lambda: calls.append("slept") or True,
        platform_system=lambda: "Windows",
    )
    response = DeviceActionSkill().handle(
        "sleep pc",
        SkillContext(device_action_adapter=adapter),
    )

    assert response.skill == "device_action"
    assert response.text == 'Confirmation required for device action "sleep_pc". Device action was not executed.'
    assert response.metadata["error"] == "confirmation_required"
    assert response.metadata["danger_classification"] == "confirmation_required"
    assert response.metadata["confirmation_required"] is True
    assert response.metadata["executed"] is False
    assert response.metadata["confirmation_request"]["token"] == "device-action-confirmation:sleep_pc"
    assert calls == []


def test_device_action_skill_returns_confirmation_required_for_open_app_without_execution():
    calls = []
    adapter = LocalDeviceActionAdapter(app_launcher=lambda app: calls.append(app.app_id) or True)

    response = DeviceActionSkill().handle(
        "open app calculator",
        SkillContext(device_action_adapter=adapter),
    )

    assert response.skill == "device_action"
    assert response.text == 'Confirmation required for device action "open_app". Device action was not executed.'
    assert response.metadata["error"] == "confirmation_required"
    assert response.metadata["danger_classification"] == "confirmation_required"
    assert response.metadata["confirmation_required"] is True
    assert response.metadata["executed"] is False
    assert response.metadata["confirmation_request"]["token"] == "device-action-confirmation:open_app"
    assert calls == []


def test_device_action_skill_forbids_run_command_without_execution():
    response = DeviceActionSkill().handle(
        "run command del important-file.txt",
        SkillContext(device_action_adapter=LocalDeviceActionAdapter()),
    )

    assert response.skill == "device_action"
    assert response.text == 'Device action "run_command" is forbidden and was not executed.'
    assert response.metadata["error"] == "forbidden"
    assert response.metadata["danger_classification"] == "forbidden"
    assert response.metadata["executed"] is False


def test_device_action_skill_forbids_delete_without_execution():
    response = DeviceActionSkill().handle(
        "delete all files",
        SkillContext(device_action_adapter=LocalDeviceActionAdapter()),
    )

    assert response.skill == "device_action"
    assert response.text == 'Device action "delete" is forbidden and was not executed.'
    assert response.metadata["error"] == "forbidden"
    assert response.metadata["danger_classification"] == "forbidden"
    assert response.metadata["executed"] is False


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


def test_planner_preserves_confirmation_required_device_action():
    intent = IntentParser().parse("lock pc")
    plan = Planner().plan(intent)

    assert intent.intent_name == "device_action"
    assert plan.errors == []
    assert len(plan.steps) == 1
    assert plan.steps[0].target == "device_action"
    assert plan.steps[0].action == "lock_pc"
    assert plan.steps[0].entities["danger_classification"] == "confirmation_required"
    assert plan.steps[0].entities["confirmation_required"] is True


def test_planner_preserves_open_app_confirmation_and_app_id():
    intent = IntentParser().parse("open app calculator")
    plan = Planner().plan(intent)

    assert intent.intent_name == "device_action"
    assert intent.extracted_entities["app_id"] == "calculator"
    assert plan.errors == []
    assert len(plan.steps) == 1
    assert plan.steps[0].target == "device_action"
    assert plan.steps[0].action == "open_app"
    assert plan.steps[0].input_text == "open app calculator"
    assert plan.steps[0].entities["parameters"] == {"app_id": "calculator"}
    assert plan.steps[0].entities["confirmation_required"] is True


def test_skill_manager_executes_device_action_through_pipeline():
    manager = SkillManager(event_bus=get_global_bus())
    manager.register(DeviceActionSkill())

    response = manager.handle("system status", run_before_intents=True)
    step_result = manager.last_execution.step_results[0]

    assert response.skill == "device_action"
    assert response.text == "System status: ok."
    assert manager.last_plan.steps[0].target == "device_action"
    assert step_result.target == "device_action"
    assert step_result.success is True
    assert step_result.returned_data["metadata"]["data"]["source"] == "pc_service"
    assert step_result.returned_data["metadata"]["data"]["checks"]["network_access"] == "not_used"


def test_skill_manager_exposes_core_service_for_device_actions():
    core_service = CoreService(register_default_pc=False)
    manager = SkillManager(event_bus=get_global_bus(), core_service=core_service)
    manager.register(DeviceActionSkill())

    response = manager.handle("list device actions", run_before_intents=True)

    assert response.skill == "device_action"
    assert manager.core_service is core_service
    assert manager.create_context().core_service is core_service
    assert manager.core_service.get_service("pc") is not None
    assert response.text.startswith("Available device actions:")


def test_skill_manager_reports_confirmation_required_without_execution():
    manager = SkillManager(event_bus=get_global_bus())
    manager.register(DeviceActionSkill())

    response = manager.handle("shutdown", run_before_intents=True)
    step_result = manager.last_execution.step_results[0]

    assert response.skill == "device_action"
    assert response.text == 'Confirmation required for device action "shutdown". Device action was not executed.'
    assert step_result.success is False
    assert step_result.error_message == "confirmation_required"
    assert step_result.returned_data["metadata"]["danger_classification"] == "confirmation_required"
    assert step_result.returned_data["metadata"]["executed"] is False


def test_skill_manager_confirms_lock_pc_before_execution():
    calls = []
    adapter = LocalDeviceActionAdapter(
        lock_impl=lambda: calls.append("locked") or True,
        platform_system=lambda: "Windows",
    )
    manager = SkillManager(event_bus=get_global_bus(), device_action_adapter=adapter)
    manager.register(DeviceActionSkill())

    response = manager.handle("lock pc", run_before_intents=True)

    assert response.skill == "confirmation"
    assert "Confirmation required to lock Windows session" in response.text
    assert manager.confirmation_manager.pending() is not None
    assert calls == []

    confirmed = manager.handle("yes")

    assert confirmed.skill == "device_action"
    assert confirmed.text == "Windows session lock requested."
    assert calls == ["locked"]
    assert manager.confirmation_manager.pending() is None
    assert manager.last_execution.step_results[0].success is True
    assert manager.last_execution.step_results[0].returned_data["metadata"]["executed"] is True


def test_skill_manager_confirms_sleep_pc_before_execution():
    calls = []
    adapter = LocalDeviceActionAdapter(
        sleep_impl=lambda: calls.append("slept") or True,
        platform_system=lambda: "Windows",
    )
    manager = SkillManager(event_bus=get_global_bus(), device_action_adapter=adapter)
    manager.register(DeviceActionSkill())

    response = manager.handle("sleep pc", run_before_intents=True)

    assert response.skill == "confirmation"
    assert "Confirmation required to put Windows PC to sleep" in response.text
    assert manager.confirmation_manager.pending() is not None
    assert calls == []

    confirmed = manager.handle("confirm")

    assert confirmed.skill == "device_action"
    assert confirmed.text == "Windows sleep requested."
    assert calls == ["slept"]
    assert manager.confirmation_manager.pending() is None
    assert manager.last_execution.step_results[0].success is True
    assert manager.last_execution.step_results[0].returned_data["metadata"]["executed"] is True


def test_skill_manager_confirms_open_app_before_mocked_windows_launch():
    calls = []
    adapter = LocalDeviceActionAdapter(
        app_allowlist=[_enabled_app_config()],
        app_launcher=lambda app: calls.append(app.app_id) or True,
        platform_system=lambda: "Windows",
    )
    manager = SkillManager(event_bus=get_global_bus(), device_action_adapter=adapter)
    manager.register(DeviceActionSkill())

    response = manager.handle("open app calculator", run_before_intents=True)

    assert response.skill == "confirmation"
    assert "Confirmation required to open app calculator" in response.text
    assert manager.confirmation_manager.pending() is not None
    assert calls == []

    confirmed = manager.handle("yes")

    assert confirmed.skill == "device_action"
    assert confirmed.text == "Windows app launch requested: Calculator."
    assert calls == ["calculator"]
    assert manager.confirmation_manager.pending() is None
    assert manager.last_execution.step_results[0].success is True
    assert manager.last_execution.step_results[0].returned_data["metadata"]["executed"] is True


def test_text_repl_executes_safe_device_action(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nlist device actions\nlist apps\nquit\n"))

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out
    detected = [
        event.payload
        for event in event_bus.history("skill.detected")
        if event.payload.get("skill") == "device_action"
    ]

    assert (
        "Available device actions: echo, system_status_mock, list_actions, list_apps, "
        "lock_pc, sleep_pc, open_app."
    ) in output
    assert "Allowlisted apps: notepad (disabled), calculator, browser (disabled)." in output
    assert detected


def test_text_repl_shows_confirmation_required_device_action(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nshutdown\nquit\n"))

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out

    assert 'Confirmation required for device action "shutdown". Device action was not executed.' in output


def test_text_repl_shows_lock_pc_confirmation_prompt(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nlock pc\ncancel\nquit\n"))

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out

    assert "Confirmation required to lock Windows session" in output
    assert "Cancelled: lock Windows session." in output


def test_text_repl_shows_sleep_pc_confirmation_prompt(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\nsleep pc\ncancel\nquit\n"))

    event_bus = get_global_bus()
    event_bus.clear_history()

    text_repl.main()

    output = capsys.readouterr().out

    assert "Confirmation required to put Windows PC to sleep" in output
    assert "Cancelled: put Windows PC to sleep." in output
