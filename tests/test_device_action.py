import pytest

from core import DeviceAction, DeviceActionRegistry, DeviceActionResult, LocalDeviceActionAdapter


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
    assert [action["name"] for action in result.data["actions"]] == [
        "echo",
        "system_status_mock",
        "list_actions",
        "lock_pc",
    ]
    assert "Available device actions: echo, system_status_mock, list_actions, lock_pc." == result.text


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
