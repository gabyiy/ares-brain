import json

import pytest

from core import (
    AppAllowlistConfigError,
    AppAllowlistLoader,
    AppLaunchConfig,
    DeviceAction,
    DeviceActionRegistry,
    DeviceActionResult,
    LocalDeviceActionAdapter,
)


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


def _write_apps_config(path, apps):
    path.write_text(json.dumps({"apps": apps}), encoding="utf-8")
    return path


def _app_config(
    app_id="calculator",
    display_name="Calculator",
    command="C:\\Allowed\\calculator.exe",
    enabled=True,
    requires_confirmation=True,
):
    return {
        "app_id": app_id,
        "display_name": display_name,
        "command": command,
        "enabled": enabled,
        "requires_confirmation": requires_confirmation,
        "metadata": {"source": "test_config", "platform": "windows"},
    }


def test_app_allowlist_loader_loads_valid_config(tmp_path):
    config_path = _write_apps_config(
        tmp_path / "apps.json",
        [_app_config(app_id="Calculator", display_name="Calculator")],
    )

    configs = AppAllowlistLoader(config_path).load()

    assert [config.to_dict() for config in configs] == [
        {
            "app_id": "calculator",
            "display_name": "Calculator",
            "command_placeholder": "C:\\Allowed\\calculator.exe",
            "enabled": True,
            "requires_confirmation": True,
            "metadata": {"source": "test_config", "platform": "windows"},
        }
    ]


def test_app_allowlist_loader_rejects_invalid_config(tmp_path):
    invalid_app = _app_config()
    del invalid_app["display_name"]
    config_path = _write_apps_config(tmp_path / "apps.json", [invalid_app])

    with pytest.raises(AppAllowlistConfigError, match="requires display_name"):
        AppAllowlistLoader(config_path).load()


def test_app_allowlist_loader_rejects_non_boolean_flags(tmp_path):
    invalid_app = _app_config(enabled="yes")
    config_path = _write_apps_config(tmp_path / "apps.json", [invalid_app])

    with pytest.raises(AppAllowlistConfigError, match="requires boolean enabled"):
        AppAllowlistLoader(config_path).load()


def test_app_allowlist_loader_rejects_duplicate_app_id(tmp_path):
    config_path = _write_apps_config(
        tmp_path / "apps.json",
        [
            _app_config(app_id="calculator"),
            _app_config(app_id="Calculator", display_name="Calculator Duplicate"),
        ],
    )

    with pytest.raises(AppAllowlistConfigError, match="Duplicate app_id"):
        AppAllowlistLoader(config_path).load()


def test_device_action_registry_registers_and_lists_actions():
    registry = DeviceActionRegistry()
    action = registry.register(
        DeviceAction(name="echo", description="Echo a message"),
        lambda parameters: DeviceActionResult(action_name="echo", success=True, text="ok"),
    )

    assert action.name == "echo"
    assert registry.get("echo") == action
    assert registry.list_actions() == [action]
    assert registry.list_actions()[0].to_dict() == {
        "name": "echo",
        "description": "Echo a message",
        "danger_classification": "safe",
        "requires_confirmation": False,
        "dangerous": False,
        "metadata": {},
    }


def test_unknown_device_action_fails_safely():
    result = LocalDeviceActionAdapter().execute("missing action")

    assert result.success is False
    assert result.error_message == "Device action is not available: missing_action"
    assert result.metadata == {"missing_action": "missing_action"}


def test_echo_device_action_works_without_shell_execution():
    result = LocalDeviceActionAdapter().execute("echo", {"message": "hello device"})

    assert result.success is True
    assert result.text == "hello device"
    assert result.data == {"message": "hello device"}
    assert result.metadata == {"safe": True}


def test_list_available_device_actions_works():
    result = LocalDeviceActionAdapter().execute("list available actions")

    assert result.success is True
    assert result.action_name == "list_actions"
    assert [action["name"] for action in result.data["actions"]] == EXPECTED_DEVICE_ACTIONS
    assert (
        "Available device actions: echo, system_status_mock, list_actions, list_apps, "
        "lock_pc, sleep_pc, open_app."
    ) == result.text


def test_list_allowlisted_apps_works():
    result = LocalDeviceActionAdapter().execute("list apps")

    assert result.success is True
    assert result.action_name == "list_apps"
    assert result.text == "Allowlisted apps: notepad (disabled), calculator, browser (disabled)."
    apps = {app["app_id"]: app for app in result.data["apps"]}
    assert apps["notepad"] == {
        "app_id": "notepad",
        "display_name": "Notepad",
        "command_placeholder": "C:\\Windows\\System32\\notepad.exe",
        "enabled": False,
        "requires_confirmation": True,
        "metadata": {"source": "config_allowlist", "platform": "windows"},
    }
    assert apps["calculator"]["enabled"] is True
    assert apps["calculator"]["requires_confirmation"] is True
    assert apps["browser"]["enabled"] is False
    assert result.metadata == {"safe": True, "allowlist_only": True}


def test_loaded_config_enables_only_calculator():
    apps = {app.app_id: app for app in AppAllowlistLoader().load()}

    assert apps["calculator"].enabled is True
    assert apps["calculator"].requires_confirmation is True
    assert apps["calculator"].command_placeholder == "C:\\Windows\\System32\\calc.exe"
    assert apps["notepad"].enabled is False
    assert apps["browser"].enabled is False


def test_unknown_app_is_rejected_without_launching():
    calls = []
    adapter = LocalDeviceActionAdapter(
        app_allowlist=[_enabled_app_config()],
        app_launcher=lambda app: calls.append(app.app_id) or True,
        platform_system=lambda: "Windows",
    )

    result = adapter.execute(
        "open_app",
        {"app_id": "unknown", "confirmation_approved": True},
    )

    assert result.success is False
    assert result.text == "App is not allowlisted: unknown"
    assert result.error_message == "unknown_app"
    assert result.metadata["executed"] is False
    assert result.metadata["allowlist_only"] is True
    assert calls == []


def test_disabled_notepad_is_rejected_without_launching():
    calls = []
    adapter = LocalDeviceActionAdapter(app_launcher=lambda app: calls.append(app.app_id) or True)

    result = adapter.execute(
        "open_app",
        {"app_id": "notepad", "confirmation_approved": True},
    )

    assert result.success is False
    assert result.text == "App is disabled: notepad"
    assert result.error_message == "disabled_app"
    assert result.data["app"]["app_id"] == "notepad"
    assert result.metadata["executed"] is False
    assert calls == []


def test_disabled_browser_is_rejected_without_launching():
    calls = []
    adapter = LocalDeviceActionAdapter(app_launcher=lambda app: calls.append(app.app_id) or True)

    result = adapter.execute(
        "open_app",
        {"app_id": "browser", "confirmation_approved": True},
    )

    assert result.success is False
    assert result.text == "App is disabled: browser"
    assert result.error_message == "disabled_app"
    assert result.data["app"]["app_id"] == "browser"
    assert result.metadata["executed"] is False
    assert calls == []


def test_open_app_requires_confirmation_before_mock_launch():
    calls = []
    adapter = LocalDeviceActionAdapter(app_launcher=lambda app: calls.append(app.app_id) or True)

    result = adapter.execute("open app", {"app_id": "calculator"})

    assert result.success is False
    assert result.text == 'Confirmation required for device action "open_app". Device action was not executed.'
    assert result.error_message == "confirmation_required"
    assert result.metadata["confirmation_required"] is True
    assert result.metadata["executed"] is False
    assert result.metadata["confirmation_request"] == {
        "token": "device-action-confirmation:open_app",
        "action_name": "open_app",
        "classification": "confirmation_required",
        "reason": "open_app requires explicit confirmation before opening an allowlisted Windows app",
        "prompt": (
            'Confirmation required for device action "open_app". '
            "This will open an enabled allowlisted Windows app if confirmed."
        ),
    }
    assert calls == []


def test_confirmed_calculator_from_loaded_config_calls_mocked_windows_launcher():
    calls = []

    def launch(app: AppLaunchConfig):
        calls.append(app.to_dict())
        return True

    adapter = LocalDeviceActionAdapter(
        app_launcher=launch,
        platform_system=lambda: "Windows",
    )

    result = adapter.execute(
        "open_app",
        {"app_id": "calculator", "confirmation_approved": True},
    )

    assert result.success is True
    assert result.text == "Windows app launch requested: Calculator."
    assert result.data["app"]["app_id"] == "calculator"
    assert result.data["app"]["command_placeholder"] == "C:\\Windows\\System32\\calc.exe"
    assert result.metadata["executed"] is True
    assert calls == [result.data["app"]]


def test_confirmed_enabled_open_app_calls_mocked_windows_launcher():
    calls = []

    def launch(app: AppLaunchConfig):
        calls.append(app.to_dict())
        return True

    adapter = LocalDeviceActionAdapter(
        app_allowlist=[_enabled_app_config()],
        app_launcher=launch,
        platform_system=lambda: "Windows",
    )

    result = adapter.execute(
        "open_app",
        {"app_id": "calculator", "confirmation_approved": True},
    )

    assert result.success is True
    assert result.text == "Windows app launch requested: Calculator."
    assert result.data["app"]["app_id"] == "calculator"
    assert result.data["app"]["command_placeholder"] == "C:\\Allowed\\calculator.exe"
    assert result.metadata["executed"] is True
    assert result.metadata["platform"] == "Windows"
    assert calls == [result.data["app"]]


def test_confirmed_enabled_open_app_uses_loaded_config_with_mocked_launcher(tmp_path):
    config_path = _write_apps_config(
        tmp_path / "apps.json",
        [_app_config(command="C:\\Allowed\\from-config.exe")],
    )
    calls = []

    def launch(app: AppLaunchConfig):
        calls.append(app.to_dict())
        return True

    adapter = LocalDeviceActionAdapter(
        app_allowlist_path=config_path,
        app_launcher=launch,
        platform_system=lambda: "Windows",
    )

    result = adapter.execute(
        "open_app",
        {"app_id": "calculator", "confirmation_approved": True},
    )

    assert result.success is True
    assert result.data["app"]["command_placeholder"] == "C:\\Allowed\\from-config.exe"
    assert calls == [result.data["app"]]


def test_open_app_uses_configured_command_not_user_supplied_path(tmp_path):
    config_path = _write_apps_config(
        tmp_path / "apps.json",
        [
            _app_config(
                app_id="safe_app",
                display_name="Safe App",
                command="C:\\Allowed\\safe.exe",
            )
        ],
    )
    calls = []

    def launch(app: AppLaunchConfig):
        calls.append(app.to_dict())
        return True

    adapter = LocalDeviceActionAdapter(
        app_allowlist_path=config_path,
        app_launcher=launch,
        platform_system=lambda: "Windows",
    )

    result = adapter.execute(
        "open_app",
        {
            "app_id": "safe_app",
            "command": "C:\\Windows\\System32\\cmd.exe",
            "path": "C:\\Windows\\System32\\cmd.exe",
            "confirmation_approved": True,
        },
    )

    assert result.success is True
    assert result.data["app"]["command_placeholder"] == "C:\\Allowed\\safe.exe"
    assert calls == [result.data["app"]]


def test_arbitrary_app_path_is_rejected_without_launching():
    calls = []
    adapter = LocalDeviceActionAdapter(
        app_allowlist=[_enabled_app_config()],
        app_launcher=lambda app: calls.append(app.app_id) or True,
        platform_system=lambda: "Windows",
    )

    result = adapter.execute(
        "open_app",
        {"app_id": "C:\\Windows\\System32\\notepad.exe", "confirmation_approved": True},
    )

    assert result.success is False
    assert result.error_message == "invalid_app_id"
    assert result.metadata["executed"] is False
    assert calls == []


def test_shell_like_app_input_is_rejected_without_launching():
    calls = []
    adapter = LocalDeviceActionAdapter(
        app_allowlist=[_enabled_app_config()],
        app_launcher=lambda app: calls.append(app.app_id) or True,
        platform_system=lambda: "Windows",
    )

    result = adapter.execute(
        "open_app",
        {"app_id": "calculator && del C:\\important", "confirmation_approved": True},
    )
    forbidden = adapter.execute("run command open app calculator", {"confirmation_approved": True})

    assert result.success is False
    assert result.error_message == "invalid_app_id"
    assert result.metadata["executed"] is False
    assert forbidden.success is False
    assert forbidden.error_message == "forbidden"
    assert calls == []


def test_confirmed_open_app_returns_unsupported_on_non_windows_without_launching():
    calls = []
    adapter = LocalDeviceActionAdapter(
        app_allowlist=[_enabled_app_config()],
        app_launcher=lambda app: calls.append(app.app_id) or True,
        platform_system=lambda: "Linux",
    )

    result = adapter.execute(
        "open_app",
        {"app_id": "calculator", "confirmation_approved": True},
    )

    assert result.success is False
    assert result.text == "Windows app launch is unsupported on this platform."
    assert result.error_message == "unsupported_platform"
    assert result.metadata["platform"] == "Linux"
    assert result.metadata["supported"] is False
    assert result.metadata["executed"] is False
    assert calls == []


def test_system_status_mock_action_is_deterministic():
    result = LocalDeviceActionAdapter().execute("system status")

    assert result.success is True
    assert result.text == "System status mock: ok."
    assert result.data == {
        "status": "ok",
        "source": "mock",
        "checks": {
            "device_actions": "safe",
            "shell_execution": "disabled",
            "remote_control": "disabled",
        },
    }


def test_device_action_execution_result_format_is_stable():
    result = LocalDeviceActionAdapter().execute("echo", {"text": "stable"})

    assert result.to_dict() == {
        "action_name": "echo",
        "success": True,
        "text": "stable",
        "data": {"message": "stable"},
        "error_message": "",
        "metadata": {"safe": True},
    }


def test_dangerous_device_action_placeholder_is_rejected_by_registry():
    registry = DeviceActionRegistry()

    with pytest.raises(ValueError, match="Non-safe device actions are not implemented yet"):
        registry.register(
            DeviceAction(
                name="shutdown",
                description="Dangerous placeholder",
                danger_classification="confirmation_required",
                requires_confirmation=True,
                dangerous=True,
            ),
            lambda parameters: DeviceActionResult(action_name="shutdown", success=True),
        )


def test_reserved_dangerous_action_name_is_rejected_even_without_flag():
    registry = DeviceActionRegistry()

    with pytest.raises(ValueError, match="Non-safe device actions are not implemented yet"):
        registry.register(
            DeviceAction(name="restart", description="Reserved dangerous placeholder"),
            lambda parameters: DeviceActionResult(action_name="restart", success=True),
        )


def test_confirmation_required_device_action_result_is_stable_and_not_executed():
    result = LocalDeviceActionAdapter().execute("shutdown")

    assert result.success is False
    assert result.text == 'Confirmation required for device action "shutdown". Device action was not executed.'
    assert result.error_message == "confirmation_required"
    assert result.metadata["danger_classification"] == "confirmation_required"
    assert result.metadata["confirmation_required"] is True
    assert result.metadata["executed"] is False
    assert result.metadata["confirmation_request"] == {
        "token": "device-action-confirmation:shutdown",
        "action_name": "shutdown",
        "classification": "confirmation_required",
        "reason": "shutdown requires explicit confirmation and is not implemented",
        "prompt": (
            'Confirmation required for device action "shutdown". '
            "This placeholder cannot execute real OS commands yet."
        ),
    }


def test_lock_pc_is_confirmation_required_by_default():
    result = LocalDeviceActionAdapter().execute("lock pc")

    assert result.success is False
    assert result.text == 'Confirmation required for device action "lock_pc". Device action was not executed.'
    assert result.error_message == "confirmation_required"
    assert result.metadata["danger_classification"] == "confirmation_required"
    assert result.metadata["confirmation_required"] is True
    assert result.metadata["executed"] is False
    assert result.metadata["confirmation_request"] == {
        "token": "device-action-confirmation:lock_pc",
        "action_name": "lock_pc",
        "classification": "confirmation_required",
        "reason": "lock_pc requires explicit confirmation before locking the Windows session",
        "prompt": (
            'Confirmation required for device action "lock_pc". '
            "This will lock the current Windows session if confirmed."
        ),
    }


def test_lock_pc_does_not_execute_without_confirmation():
    calls = []
    adapter = LocalDeviceActionAdapter(
        lock_impl=lambda: calls.append("locked") or True,
        platform_system=lambda: "Windows",
    )

    result = adapter.execute("lock_pc")

    assert result.success is False
    assert result.error_message == "confirmation_required"
    assert calls == []


def test_confirmed_lock_pc_calls_mocked_windows_lock_implementation():
    calls = []
    adapter = LocalDeviceActionAdapter(
        lock_impl=lambda: calls.append("locked") or True,
        platform_system=lambda: "Windows",
    )

    result = adapter.execute("lock_pc", {"confirmation_approved": True})

    assert result.success is True
    assert result.text == "Windows session lock requested."
    assert result.data == {"action": "lock_pc"}
    assert result.metadata["executed"] is True
    assert result.metadata["platform"] == "Windows"
    assert calls == ["locked"]


def test_confirmed_lock_pc_fails_safely_on_non_windows_platform():
    calls = []
    adapter = LocalDeviceActionAdapter(
        lock_impl=lambda: calls.append("locked") or True,
        platform_system=lambda: "Linux",
    )

    result = adapter.execute("lock_pc", {"confirmation_approved": True})

    assert result.success is False
    assert result.text == "Windows session lock is unsupported on this platform."
    assert result.error_message == "unsupported_platform"
    assert result.metadata["executed"] is False
    assert result.metadata["supported"] is False
    assert result.metadata["platform"] == "Linux"
    assert calls == []


def test_sleep_pc_is_confirmation_required_by_default():
    result = LocalDeviceActionAdapter().execute("sleep pc")

    assert result.success is False
    assert result.text == 'Confirmation required for device action "sleep_pc". Device action was not executed.'
    assert result.error_message == "confirmation_required"
    assert result.metadata["danger_classification"] == "confirmation_required"
    assert result.metadata["confirmation_required"] is True
    assert result.metadata["executed"] is False
    assert result.metadata["confirmation_request"] == {
        "token": "device-action-confirmation:sleep_pc",
        "action_name": "sleep_pc",
        "classification": "confirmation_required",
        "reason": "sleep_pc requires explicit confirmation before putting Windows to sleep",
        "prompt": (
            'Confirmation required for device action "sleep_pc". '
            "This will put the Windows PC to sleep if confirmed."
        ),
    }


def test_sleep_pc_does_not_execute_without_confirmation():
    calls = []
    adapter = LocalDeviceActionAdapter(
        sleep_impl=lambda: calls.append("slept") or True,
        platform_system=lambda: "Windows",
    )

    result = adapter.execute("sleep_pc")

    assert result.success is False
    assert result.error_message == "confirmation_required"
    assert calls == []


def test_confirmed_sleep_pc_calls_mocked_windows_sleep_implementation():
    calls = []
    adapter = LocalDeviceActionAdapter(
        sleep_impl=lambda: calls.append("slept") or True,
        platform_system=lambda: "Windows",
    )

    result = adapter.execute("sleep_pc", {"confirmation_approved": True})

    assert result.success is True
    assert result.text == "Windows sleep requested."
    assert result.data == {"action": "sleep_pc"}
    assert result.metadata["executed"] is True
    assert result.metadata["platform"] == "Windows"
    assert calls == ["slept"]


def test_confirmed_sleep_pc_fails_safely_on_non_windows_platform():
    calls = []
    adapter = LocalDeviceActionAdapter(
        sleep_impl=lambda: calls.append("slept") or True,
        platform_system=lambda: "Linux",
    )

    result = adapter.execute("sleep_pc", {"confirmation_approved": True})

    assert result.success is False
    assert result.text == "Windows sleep is unsupported on this platform."
    assert result.error_message == "unsupported_platform"
    assert result.metadata["executed"] is False
    assert result.metadata["supported"] is False
    assert result.metadata["platform"] == "Linux"
    assert calls == []


def test_shutdown_and_restart_remain_not_executable():
    adapter = LocalDeviceActionAdapter()
    action_names = [action.name for action in adapter.list_actions()]

    assert "shutdown" not in action_names
    assert "restart" not in action_names
    assert adapter.execute("shutdown").metadata["confirmation_request"]["prompt"].endswith(
        "This placeholder cannot execute real OS commands yet."
    )
    assert adapter.execute("restart").metadata["confirmation_request"]["prompt"].endswith(
        "This placeholder cannot execute real OS commands yet."
    )


def test_forbidden_device_action_result_is_stable_and_not_executed():
    result = LocalDeviceActionAdapter().execute("run command")

    assert result.success is False
    assert result.text == 'Device action "run_command" is forbidden and was not executed.'
    assert result.error_message == "forbidden"
    assert result.metadata == {
        "danger_classification": "forbidden",
        "forbidden": True,
        "executed": False,
        "reason": "run_command is forbidden and is not implemented",
    }
