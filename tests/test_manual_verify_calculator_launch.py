from core import DeviceActionResult
from scripts import manual_verify_calculator_launch as manual_script


class FakeDeviceActionAdapter:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or DeviceActionResult(
            action_name="open_app",
            success=True,
            text="Windows app launch requested: Calculator.",
            data={"app": {"app_id": "calculator"}},
            metadata={"executed": True},
        )

    def execute(self, action_name, parameters):
        self.calls.append((action_name, dict(parameters)))
        return self.result


def test_manual_calculator_script_refuses_without_exact_confirmation():
    outputs = []
    factory_calls = []

    def adapter_factory():
        factory_calls.append("called")
        return FakeDeviceActionAdapter()

    exit_code = manual_script.run_manual_verification(
        input_func=lambda prompt: "yes",
        output_func=outputs.append,
        adapter_factory=adapter_factory,
    )

    assert exit_code == 1
    assert factory_calls == []
    assert outputs == [
        "WARNING: This manual verification can open Windows Calculator.",
        "App id: calculator",
        "Type YES_OPEN_CALCULATOR to continue.",
        "Confirmation did not match. Calculator was not opened.",
    ]


def test_manual_calculator_script_calls_same_open_app_device_action_path():
    outputs = []
    adapter = FakeDeviceActionAdapter()

    exit_code = manual_script.run_manual_verification(
        input_func=lambda prompt: manual_script.CONFIRMATION_PHRASE,
        output_func=outputs.append,
        adapter=adapter,
    )

    assert exit_code == 0
    assert adapter.calls == [
        (
            "open_app",
            {"app_id": "calculator", "confirmation_approved": True},
        )
    ]
    assert outputs[-1] == "Windows app launch requested: Calculator."


def test_manual_calculator_script_reports_adapter_failure_without_extra_actions():
    outputs = []
    adapter = FakeDeviceActionAdapter(
        DeviceActionResult(
            action_name="open_app",
            success=False,
            text="Windows app launch is unsupported on this platform.",
            error_message="unsupported_platform",
            metadata={"executed": False},
        )
    )

    exit_code = manual_script.run_manual_verification(
        input_func=lambda prompt: manual_script.CONFIRMATION_PHRASE,
        output_func=outputs.append,
        adapter=adapter,
    )

    assert exit_code == 2
    assert adapter.calls == [
        (
            "open_app",
            {"app_id": "calculator", "confirmation_approved": True},
        )
    ]
    assert outputs[-1] == "Windows app launch is unsupported on this platform."
