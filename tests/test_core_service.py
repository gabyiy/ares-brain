import pytest

from core import PC_SERVICE_NAME, CoreService, PCServiceResult, WindowsPCService


class FakeCapabilityService:
    def __init__(self, success=True):
        self.calls = []
        self.success = success

    def get_capabilities(self):
        self.calls.append("get_capabilities")
        return PCServiceResult(
            success=self.success,
            text="Fake capabilities discovered." if self.success else "Fake capabilities failed.",
            data={
                "source": "fake_service",
                "supported_device_actions": [{"name": "echo"}],
                "supported_applications": [],
                "available_status_providers": ["fake_status"],
                "available_services": ["fake_service"],
            },
            error_message="" if self.success else "fake_failure",
            metadata={"safe": True, "source": "fake_service"},
        )


class ServiceWithoutCapabilities:
    pass


def test_core_service_registers_default_pc_service():
    core_service = CoreService()

    pc_service = core_service.get_service(PC_SERVICE_NAME)

    assert isinstance(pc_service, WindowsPCService)
    assert core_service.list_services() == [{"name": PC_SERVICE_NAME, "type": "WindowsPCService"}]


def test_core_service_default_pc_service_exposes_required_interfaces():
    core_service = CoreService()
    pc_service = core_service.get_service(PC_SERVICE_NAME)

    assert callable(pc_service.get_capabilities)
    assert callable(pc_service.get_status)
    assert callable(pc_service.status)


def test_core_service_registers_and_looks_up_service():
    service = FakeCapabilityService()
    core_service = CoreService(register_default_pc=False)

    registered = core_service.register_service("Fake Service", service)

    assert registered is service
    assert core_service.get_service("fake_service") is service
    assert core_service.get_service("missing") is None
    assert core_service.list_services() == [{"name": "fake_service", "type": "FakeCapabilityService"}]


def test_core_service_rejects_invalid_registration():
    core_service = CoreService(register_default_pc=False)

    with pytest.raises(ValueError, match="Service name is required"):
        core_service.register_service("", FakeCapabilityService())

    with pytest.raises(ValueError, match="Service instance is required"):
        core_service.register_service("fake", None)


def test_core_service_aggregates_registered_capabilities():
    service = FakeCapabilityService()
    core_service = CoreService(services={"fake": service}, register_default_pc=False)

    result = core_service.get_capabilities()

    assert result.success is True
    assert result.text == "Core service capabilities discovered."
    assert result.error_message == ""
    assert result.metadata == {"safe": True, "source": "core_service"}
    assert result.data == {
        "source": "core_service",
        "services": [
            {
                "name": "fake",
                "type": "FakeCapabilityService",
                "success": True,
                "capabilities": {
                    "source": "fake_service",
                    "supported_device_actions": [{"name": "echo"}],
                    "supported_applications": [],
                    "available_status_providers": ["fake_status"],
                    "available_services": ["fake_service"],
                },
            }
        ],
        "available_services": ["fake"],
        "capabilities_by_service": {
            "fake": {
                "source": "fake_service",
                "supported_device_actions": [{"name": "echo"}],
                "supported_applications": [],
                "available_status_providers": ["fake_status"],
                "available_services": ["fake_service"],
            }
        },
        "errors": [],
    }
    assert service.calls == ["get_capabilities"]


def test_core_service_reports_capability_errors_safely():
    service = FakeCapabilityService(success=False)
    core_service = CoreService(services={"fake": service}, register_default_pc=False)

    result = core_service.get_capabilities()

    assert result.success is False
    assert result.error_message == "capability_discovery_errors"
    assert result.data["errors"] == [{"service": "fake", "error": "fake_failure"}]
    assert result.data["services"][0]["success"] is False
    assert service.calls == ["get_capabilities"]


def test_core_service_reports_missing_capability_interface_safely():
    core_service = CoreService(
        services={"missing capabilities": ServiceWithoutCapabilities()},
        register_default_pc=False,
    )

    result = core_service.get_capabilities()

    assert result.success is False
    assert result.error_message == "capability_discovery_errors"
    assert result.data["errors"] == [
        {"service": "missing_capabilities", "error": "missing_get_capabilities"}
    ]
    assert result.data["services"] == [
        {
            "name": "missing_capabilities",
            "type": "ServiceWithoutCapabilities",
            "success": False,
        }
    ]
