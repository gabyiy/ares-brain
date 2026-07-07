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
    ]
    assert "Available device actions: echo, system_status_mock, list_actions." == result.text


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


def test_dangerous_device_action_placeholder_is_rejected():
    registry = DeviceActionRegistry()

    with pytest.raises(ValueError, match="Dangerous device actions are not implemented yet"):
        registry.register(
            DeviceAction(
                name="shutdown",
                description="Dangerous placeholder",
                requires_confirmation=True,
                dangerous=True,
            ),
            lambda parameters: DeviceActionResult(action_name="shutdown", success=True),
        )


def test_reserved_dangerous_action_name_is_rejected_even_without_flag():
    registry = DeviceActionRegistry()

    with pytest.raises(ValueError, match="Dangerous device actions are not implemented yet"):
        registry.register(
            DeviceAction(name="restart", description="Reserved dangerous placeholder"),
            lambda parameters: DeviceActionResult(action_name="restart", success=True),
        )
