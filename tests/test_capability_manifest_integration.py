from core import (
    CITY_STATE_IDLE,
    EVENT_DECISION_FAILED,
    LIFECYCLE_UNLOADED,
    CoreService,
    ManifestDependencies,
    ManifestPolicy,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockVoiceOutputAdapter,
    PCServiceResult,
    VoicePipeline,
    build_service_manifest,
)
from events import EventHistoryStore


class ManifestCountingCity:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def handle(self, text):
        self.calls.append(text)
        return PCServiceResult(
            success=True,
            text=f"{self.name} handled {text}",
            data={"city": self.name, "text": text},
            metadata={"safe": True, "source": self.name},
        )


def _manifest_with_missing_dependency(module_name="voice"):
    return build_service_manifest(
        module_name=module_name,
        capabilities=["voice.text_loop"],
        dependencies=ManifestDependencies(
            required_capabilities=["missing.voice.dependency"]
        ),
    )


def test_manifest_rejection_does_not_alter_lifecycle_state(tmp_path):
    history = EventHistoryStore(path=tmp_path / "events.json")
    city = ManifestCountingCity("voice")
    core_service = CoreService(
        event_history_store=history,
        register_default_pc=False,
        register_default_voice=False,
    )
    core_service.register_service(
        "voice",
        city,
        capabilities=["voice.text_loop"],
        manifest=_manifest_with_missing_dependency("voice"),
    )

    result = core_service.route_by_capability(
        "voice.text_loop",
        lambda service: service.handle("hello"),
        session_id="session-manifest",
        correlation_id="corr-manifest",
    )

    assert result.success is False
    assert result.data["status"] == "manifest_rejected"
    assert result.error_message == "required_capability_missing:missing.voice.dependency"
    assert city.calls == []
    assert core_service.get_service_status("voice") == CITY_STATE_IDLE
    assert core_service.get_lifecycle_status("voice").data["lifecycle_status"]["state"] == (
        LIFECYCLE_UNLOADED
    )
    assert history.list()[0].type == "manifest.validation_failed"
    assert history.list()[0].decision == EVENT_DECISION_FAILED
    assert history.list()[0].event["correlation_id"] == "corr-manifest"


def test_manifest_rejection_does_not_activate_unrelated_city():
    voice = ManifestCountingCity("voice")
    weather = ManifestCountingCity("weather")
    core_service = CoreService(register_default_pc=False, register_default_voice=False)
    core_service.register_service(
        "voice",
        voice,
        capabilities=["voice.text_loop"],
        manifest=_manifest_with_missing_dependency("voice"),
    )
    core_service.register_service(
        "weather",
        weather,
        capabilities=["weather.current"],
    )

    result = core_service.route_by_capability(
        "voice.text_loop",
        lambda service: service.handle("hello"),
    )

    assert result.success is False
    assert voice.calls == []
    assert weather.calls == []
    assert core_service.get_service_status("weather") == CITY_STATE_IDLE
    assert core_service.get_lifecycle_status("weather").data["lifecycle_status"]["state"] == (
        LIFECYCLE_UNLOADED
    )


def test_core_service_uses_preferred_manifest_provider():
    weather_a = ManifestCountingCity("weather_a")
    weather_b = ManifestCountingCity("weather_b")
    core_service = CoreService(
        manifest_policy=ManifestPolicy(
            preferred_providers={"weather.current": "weather_b"}
        ),
        register_default_pc=False,
        register_default_voice=False,
    )
    core_service.register_service("weather_a", weather_a, capabilities=["weather.current"])
    core_service.register_service("weather_b", weather_b, capabilities=["weather.current"])

    result = core_service.route_by_capability(
        "weather.current",
        lambda service: service.handle("weather today"),
    )

    assert result.success is True
    assert result.data["service"] == "weather_b"
    assert result.data["provider_selection"]["reason"] == "preferred_provider"
    assert weather_a.calls == []
    assert weather_b.calls == ["weather today"]


def test_core_service_remains_usable_after_manifest_failure():
    voice = ManifestCountingCity("voice")
    weather = ManifestCountingCity("weather")
    core_service = CoreService(register_default_pc=False, register_default_voice=False)
    core_service.register_service(
        "voice",
        voice,
        capabilities=["voice.text_loop"],
        manifest=_manifest_with_missing_dependency("voice"),
    )
    core_service.register_service("weather", weather, capabilities=["weather.current"])

    failed = core_service.route_by_capability(
        "voice.text_loop",
        lambda service: service.handle("hello"),
    )
    succeeded = core_service.route_by_capability(
        "weather.current",
        lambda service: service.handle("weather today"),
    )

    assert failed.success is False
    assert succeeded.success is True
    assert succeeded.data["service"] == "weather"
    assert voice.calls == []
    assert weather.calls == ["weather today"]


def test_voice_pipeline_still_works_with_valid_manifests():
    pipeline = VoicePipeline(
        microphone_adapter=MockMicrophoneAdapter(chunks=[b"\x01"]),
        speech_to_text_adapter=MockSpeechToTextAdapter(transcripts=["hello"]),
        output_adapter=MockVoiceOutputAdapter(),
        command_handler=lambda text: "valid manifest response",
    )

    result = pipeline.run_once(session_id="session-valid", correlation_id="corr-valid")

    assert result.success is True
    assert result.status == "completed"
    assert result.response_text == "valid manifest response"
    assert result.data["activated_city"] == "voice"
    assert result.data["routing"]["data"]["route_result"]["manifest_validation"]["success"] is True


def test_voice_session_survives_manifest_rejection():
    microphone = MockMicrophoneAdapter(chunks=[b"\x01", b"\x02"])
    stt = MockSpeechToTextAdapter(transcripts=["hello", "hello again"])
    output = MockVoiceOutputAdapter()
    policy = ManifestPolicy(enabled_modules={"voice": False})
    core_service = CoreService(manifest_policy=policy)
    pipeline = VoicePipeline(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
        output_adapter=output,
        command_handler=lambda text: "recovered",
        core_service=core_service,
    )

    rejected = pipeline.run_once(session_id="session-voice", correlation_id="corr-reject")
    core_service.manifest_registry.policy = ManifestPolicy()
    accepted = pipeline.run_once(session_id="session-voice", correlation_id="corr-ok")

    assert rejected.success is False
    assert rejected.status == "route_failed"
    assert "module_disabled" in rejected.error_message
    assert accepted.success is True
    assert accepted.status == "completed"
    assert accepted.response_text == "recovered"
    assert microphone.start_count == 2
    assert stt.transcription_count == 2
