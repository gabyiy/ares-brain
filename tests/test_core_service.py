import pytest

from core import (
    CITY_STATE_ACTIVE,
    CITY_STATE_DISABLED,
    CITY_STATE_FAILED,
    CITY_STATE_IDLE,
    PC_SERVICE_NAME,
    VOICE_SERVICE_NAME,
    CoreService,
    PCServiceResult,
    PlaceholderVoiceService,
    WindowsPCService,
)


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


class CountingCity:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def handle(self, text):
        self.calls.append(text)
        return PCServiceResult(
            success=True,
            text=f"{self.name} handled {text}",
            data={"city": self.name, "text": text},
            metadata={"safe": True},
        )


def test_core_service_registers_default_pc_service():
    core_service = CoreService()

    pc_service = core_service.get_service(PC_SERVICE_NAME)

    assert isinstance(pc_service, WindowsPCService)
    assert core_service.list_services() == [
        {
            "name": PC_SERVICE_NAME,
            "type": "WindowsPCService",
            "city_status": CITY_STATE_IDLE,
            "capabilities": [
                "pc.status",
                "pc.capabilities",
                "device.actions",
                "device.status",
                "device.apps",
                "device.open_app",
            ],
        },
        {
            "name": VOICE_SERVICE_NAME,
            "type": "PlaceholderVoiceService",
            "city_status": CITY_STATE_IDLE,
            "capabilities": [
                "voice.status",
                "voice.capabilities",
                "voice.text_loop",
            ],
        },
    ]


def test_core_service_registers_default_voice_service():
    core_service = CoreService()

    voice_service = core_service.get_service(VOICE_SERVICE_NAME)

    assert isinstance(voice_service, PlaceholderVoiceService)


def test_core_service_default_pc_service_exposes_required_interfaces():
    core_service = CoreService()
    pc_service = core_service.get_service(PC_SERVICE_NAME)

    assert callable(pc_service.get_capabilities)
    assert callable(pc_service.get_status)
    assert callable(pc_service.status)


def test_core_service_registers_and_looks_up_service():
    service = FakeCapabilityService()
    core_service = CoreService(register_default_pc=False, register_default_voice=False)

    registered = core_service.register_service("Fake Service", service)

    assert registered is service
    assert core_service.get_service("fake_service") is service
    assert core_service.get_service("missing") is None
    assert core_service.list_services() == [
        {
            "name": "fake_service",
            "type": "FakeCapabilityService",
            "city_status": CITY_STATE_IDLE,
            "capabilities": [],
        }
    ]


def test_core_service_rejects_invalid_registration():
    core_service = CoreService(register_default_pc=False, register_default_voice=False)

    with pytest.raises(ValueError, match="Service name is required"):
        core_service.register_service("", FakeCapabilityService())

    with pytest.raises(ValueError, match="Service instance is required"):
        core_service.register_service("fake", None)

    with pytest.raises(ValueError, match="Invalid city lifecycle state"):
        core_service.register_service("fake", FakeCapabilityService(), city_status="awake")


def test_core_service_aggregates_registered_capabilities():
    service = FakeCapabilityService()
    core_service = CoreService(
        services={"fake": service},
        register_default_pc=False,
        register_default_voice=False,
    )

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
                "city_status": CITY_STATE_IDLE,
                "registered_capabilities": [],
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
        "capability_registry": {
            "fake": {
                "type": "FakeCapabilityService",
                "city_status": CITY_STATE_IDLE,
                "capabilities": [],
            }
        },
        "city_statuses": {"fake": CITY_STATE_IDLE},
        "errors": [],
    }
    assert service.calls == ["get_capabilities"]


def test_core_service_reports_capability_errors_safely():
    service = FakeCapabilityService(success=False)
    core_service = CoreService(
        services={"fake": service},
        register_default_pc=False,
        register_default_voice=False,
    )

    result = core_service.get_capabilities()

    assert result.success is False
    assert result.error_message == "capability_discovery_errors"
    assert result.data["errors"] == [{"service": "fake", "error": "fake_failure"}]
    assert result.data["services"][0]["success"] is False
    assert result.data["services"][0]["city_status"] == CITY_STATE_IDLE
    assert service.calls == ["get_capabilities"]


def test_core_service_reports_missing_capability_interface_safely():
    core_service = CoreService(
        services={"missing capabilities": ServiceWithoutCapabilities()},
        register_default_pc=False,
        register_default_voice=False,
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
            "city_status": CITY_STATE_IDLE,
            "registered_capabilities": [],
            "success": False,
        }
    ]


def test_core_service_route_by_capability_calls_only_matching_city():
    weather_city = CountingCity("weather")
    voice_city = CountingCity("voice")
    core_service = CoreService(register_default_pc=False, register_default_voice=False)
    core_service.register_service(
        "weather",
        weather_city,
        capabilities=["weather.current"],
    )
    core_service.register_service(
        "voice",
        voice_city,
        capabilities=["voice.text_loop"],
    )

    result = core_service.route_by_capability(
        "weather.current",
        lambda city: city.handle("weather today"),
    )

    assert result.success is True
    assert result.data["service"] == "weather"
    assert result.data["response"] == {"city": "weather", "text": "weather today"}
    assert result.data["city_lifecycle"] == {
        "before": CITY_STATE_IDLE,
        "during": CITY_STATE_ACTIVE,
        "after": CITY_STATE_IDLE,
    }
    assert result.data["city_statuses"] == {
        "weather": CITY_STATE_IDLE,
        "voice": CITY_STATE_IDLE,
    }
    assert weather_city.calls == ["weather today"]
    assert voice_city.calls == []


def test_core_service_route_by_capability_keeps_unused_cities_idle():
    pc_city = CountingCity("pc")
    voice_city = CountingCity("voice")
    core_service = CoreService(register_default_pc=False, register_default_voice=False)
    core_service.register_service("pc", pc_city, capabilities=["device.status"])
    core_service.register_service("voice", voice_city, capabilities=["voice.text_loop"])

    result = core_service.route_by_capability(
        "voice.text_loop",
        lambda city: city.handle("hello"),
    )

    assert result.success is True
    assert result.data["service"] == "voice"
    assert pc_city.calls == []
    assert voice_city.calls == ["hello"]
    assert core_service.get_service_status("pc") == CITY_STATE_IDLE
    assert core_service.get_service_status("voice") == CITY_STATE_IDLE


def test_core_service_disabled_city_is_not_routed():
    disabled_city = CountingCity("disabled")
    core_service = CoreService(register_default_pc=False, register_default_voice=False)
    core_service.register_service(
        "disabled weather",
        disabled_city,
        capabilities=["weather.current"],
        city_status=CITY_STATE_DISABLED,
    )

    result = core_service.route_by_capability(
        "weather.current",
        lambda city: city.handle("weather today"),
    )

    assert result.success is False
    assert result.error_message == "capability_not_available"
    assert result.data["city_statuses"] == {"disabled_weather": CITY_STATE_DISABLED}
    assert disabled_city.calls == []


def test_core_service_failed_route_marks_city_failed_safely():
    city = CountingCity("weather")
    core_service = CoreService(register_default_pc=False, register_default_voice=False)
    core_service.register_service("weather", city, capabilities=["weather.current"])

    def failing_handler(service):
        raise RuntimeError("route failure")

    result = core_service.route_by_capability("weather.current", failing_handler)

    assert result.success is False
    assert result.error_message == "RuntimeError: route failure"
    assert result.data["city_lifecycle"] == {
        "before": CITY_STATE_IDLE,
        "during": CITY_STATE_ACTIVE,
        "after": CITY_STATE_FAILED,
    }
    assert core_service.get_service_status("weather") == CITY_STATE_FAILED
