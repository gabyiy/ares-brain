import pytest

from core import (
    CONTRACT_CORE_EXECUTION_REQUEST,
    CONTRACT_CORE_EXECUTION_RESULT,
    CONTRACT_VERSION_V1,
    LIFECYCLE_OPERATION_HEALTH_CHECK,
    MODULE_TYPE_ADAPTER,
    MODULE_TYPE_CITY,
    MODULE_TYPE_SERVICE,
    MODULE_TYPE_SKILL,
    PERMISSION_NETWORK_OUTBOUND,
    CapabilityManifest,
    CapabilityManifestRegistry,
    ManifestDependencies,
    ManifestPolicy,
    PlatformCompatibility,
    build_service_manifest,
    build_skill_manifest,
    default_voice_related_manifests,
    load_manifest_policy,
)
from skills import SkillRegistry
from skills.base import Skill, SkillContext, SkillResponse


def _manifest(
    module_name="weather_mock",
    capabilities=None,
    dependencies=None,
    permissions=None,
    platform=None,
    lifecycle_support=None,
    module_type=MODULE_TYPE_SERVICE,
    provider="ares",
    metadata=None,
):
    return CapabilityManifest(
        module_name=module_name,
        module_type=module_type,
        module_version=CONTRACT_VERSION_V1,
        manifest_version=CONTRACT_VERSION_V1,
        description=f"Test manifest for {module_name}.",
        provider=provider,
        enabled_by_default=True,
        capabilities=capabilities or ["weather.current"],
        consumed_contracts={CONTRACT_CORE_EXECUTION_REQUEST: [CONTRACT_VERSION_V1]},
        produced_contracts={CONTRACT_CORE_EXECUTION_RESULT: [CONTRACT_VERSION_V1]},
        dependencies=dependencies or ManifestDependencies(),
        platform=platform or PlatformCompatibility(),
        permissions=permissions or [],
        lifecycle_support=lifecycle_support or [],
        metadata=metadata or {"purpose": "test"},
    )


def test_valid_manifest_is_accepted():
    registry = CapabilityManifestRegistry()
    manifest = _manifest()

    registered = registry.register_manifest(manifest)
    validation = registry.validate_manifest_requirements("weather_mock", "weather.current")

    assert registered == manifest
    assert validation.success is True
    assert validation.status == "valid"


def test_missing_module_name_is_rejected():
    with pytest.raises(ValueError, match="module_name is required"):
        _manifest(module_name="")


def test_unknown_module_type_is_rejected():
    with pytest.raises(ValueError, match="Unknown module_type"):
        _manifest(module_type="building")


def test_malformed_manifest_version_is_rejected():
    with pytest.raises(ValueError, match="Malformed manifest_version"):
        CapabilityManifest(
            module_name="bad_version",
            module_type=MODULE_TYPE_SERVICE,
            module_version="v1",
            manifest_version="1",
            description="Bad manifest version.",
            provider="ares",
            enabled_by_default=True,
            capabilities=["test.capability"],
            consumed_contracts={},
            produced_contracts={},
            permissions=[],
            lifecycle_support=[],
        )


def test_duplicate_module_registration_is_rejected():
    registry = CapabilityManifestRegistry()
    registry.register_manifest(_manifest(module_name="duplicate"))

    with pytest.raises(ValueError, match="Duplicate module manifest"):
        registry.register_manifest(_manifest(module_name="duplicate"))


def test_capability_lookup_works():
    registry = CapabilityManifestRegistry()
    registry.register_manifest(_manifest(module_name="weather_a"))

    matches = registry.list_modules_by_capability("weather.current")

    assert [manifest.module_name for manifest in matches] == ["weather_a"]


def test_provider_lookup_works():
    registry = CapabilityManifestRegistry()
    registry.register_manifest(_manifest(module_name="weather_a"))

    providers = registry.find_providers_for_capability("weather.current")

    assert [provider.module_name for provider in providers] == ["weather_a"]


def test_disabled_module_is_rejected_before_start():
    registry = CapabilityManifestRegistry(
        policy=ManifestPolicy(enabled_modules={"weather_a": False})
    )
    registry.register_manifest(_manifest(module_name="weather_a"))

    result = registry.validate_manifest_requirements("weather_a", "weather.current")

    assert result.success is False
    assert result.status == "module_disabled"


def test_unsupported_contract_version_is_rejected():
    registry = CapabilityManifestRegistry()
    manifest = CapabilityManifest(
        module_name="bad_contract",
        module_type=MODULE_TYPE_SERVICE,
        module_version="v1",
        manifest_version="v1",
        description="Unsupported contract version.",
        provider="ares",
        enabled_by_default=True,
        capabilities=["test.capability"],
        consumed_contracts={CONTRACT_CORE_EXECUTION_REQUEST: ["v2"]},
        produced_contracts={CONTRACT_CORE_EXECUTION_RESULT: ["v1"]},
        permissions=[],
        lifecycle_support=[],
    )

    with pytest.raises(ValueError, match="unsupported_contract_version"):
        registry.register_manifest(manifest)


def test_unknown_required_capability_is_rejected():
    registry = CapabilityManifestRegistry()
    registry.register_manifest(
        _manifest(
            module_name="needs_missing",
            dependencies=ManifestDependencies(required_capabilities=["missing.capability"]),
        )
    )

    result = registry.validate_manifest_requirements("needs_missing", "weather.current")

    assert result.success is False
    assert result.status == "required_capability_missing"


def test_missing_required_module_is_rejected():
    registry = CapabilityManifestRegistry()
    registry.register_manifest(
        _manifest(
            module_name="needs_module",
            dependencies=ManifestDependencies(required_modules=["missing_module"]),
        )
    )

    result = registry.validate_manifest_requirements("needs_module", "weather.current")

    assert result.success is False
    assert result.status == "required_module_missing"


def test_optional_dependency_does_not_block_startup():
    registry = CapabilityManifestRegistry()
    registry.register_manifest(
        _manifest(
            module_name="optional_missing",
            dependencies=ManifestDependencies(
                optional_capabilities=["optional.capability"],
                optional_modules=["optional_module"],
            ),
        )
    )

    result = registry.validate_manifest_requirements("optional_missing", "weather.current")

    assert result.success is True


def test_incompatible_capability_is_rejected():
    registry = CapabilityManifestRegistry()
    registry.register_manifest(_manifest(module_name="gpu_provider", capabilities=["gpu.present"]))
    registry.register_manifest(
        _manifest(
            module_name="incompatible",
            dependencies=ManifestDependencies(incompatible_capabilities=["gpu.present"]),
        )
    )

    result = registry.validate_manifest_requirements("incompatible", "weather.current")

    assert result.success is False
    assert result.status == "incompatible_capability_present"


def test_platform_mismatch_is_rejected():
    registry = CapabilityManifestRegistry()
    registry.register_manifest(
        _manifest(
            module_name="wrong_platform",
            platform=PlatformCompatibility(
                supported_operating_systems=["definitely-not-this-os"]
            ),
        )
    )

    result = registry.validate_manifest_requirements("wrong_platform", "weather.current")

    assert result.success is False
    assert result.status == "platform_mismatch"


def test_undeclared_permission_is_denied_by_policy():
    registry = CapabilityManifestRegistry(
        policy=ManifestPolicy(allowed_permissions=[])
    )
    registry.register_manifest(
        _manifest(
            module_name="network_module",
            permissions=[PERMISSION_NETWORK_OUTBOUND],
        )
    )

    result = registry.validate_manifest_requirements("network_module", "weather.current")

    assert result.success is False
    assert result.status == "permission_denied"


def test_allowed_permission_is_accepted():
    registry = CapabilityManifestRegistry(
        policy=ManifestPolicy(allowed_permissions=[PERMISSION_NETWORK_OUTBOUND])
    )
    registry.register_manifest(
        _manifest(
            module_name="network_module",
            permissions=[PERMISSION_NETWORK_OUTBOUND],
        )
    )

    result = registry.validate_manifest_requirements("network_module", "weather.current")

    assert result.success is True


def test_lifecycle_declaration_mismatch_is_rejected():
    class NoStatus:
        pass

    registry = CapabilityManifestRegistry()
    registry.register_manifest(
        _manifest(
            module_name="needs_health",
            lifecycle_support=[LIFECYCLE_OPERATION_HEALTH_CHECK],
        )
    )

    result = registry.validate_manifest_requirements(
        "needs_health",
        "weather.current",
        implementation=NoStatus(),
    )

    assert result.success is False
    assert result.status == "lifecycle_declaration_mismatch"


def test_manifest_serialization_is_deterministic():
    manifest = _manifest(module_name="stable")

    first = manifest.to_dict()
    second = manifest.to_dict()

    assert first == second
    assert list(first)[:7] == [
        "module_name",
        "module_type",
        "module_version",
        "manifest_version",
        "description",
        "provider",
        "enabled_by_default",
    ]


def test_manifest_metadata_round_trip():
    manifest = _manifest(metadata={"future": {"kept": True}})

    round_tripped = CapabilityManifest.from_dict(manifest.to_dict())

    assert round_tripped.metadata == {"future": {"kept": True}}


def test_explicit_provider_preference_is_respected():
    registry = CapabilityManifestRegistry(
        policy=ManifestPolicy(preferred_providers={"weather.current": "weather_b"})
    )
    registry.register_manifest(_manifest(module_name="weather_a", provider="ares"))
    registry.register_manifest(_manifest(module_name="weather_b", provider="ares"))

    result = registry.select_provider("weather.current")

    assert result.success is True
    assert result.selected_provider == "weather_b"
    assert result.reason == "preferred_provider"


def test_provider_ordering_is_deterministic():
    registry = CapabilityManifestRegistry()
    registry.register_manifest(_manifest(module_name="weather_b", provider="ares"))
    registry.register_manifest(_manifest(module_name="weather_a", provider="ares"))

    result = registry.select_provider("weather.current")

    assert result.success is True
    assert result.selected_provider == "weather_a"
    assert result.reason == "deterministic_order"
    assert [candidate["module_name"] for candidate in result.candidates] == [
        "weather_a",
        "weather_b",
    ]


def test_default_voice_related_manifests_cover_voice_modules():
    manifests = default_voice_related_manifests()

    assert {manifest.module_name for manifest in manifests} >= {
        "mock_microphone_adapter",
        "mock_speech_to_text_adapter",
        "voice_command_router",
        "voice_session_skill",
    }
    assert any("voice.capture" in manifest.capabilities for manifest in manifests)
    assert any(manifest.module_type == MODULE_TYPE_ADAPTER for manifest in manifests)
    assert any(manifest.module_type == MODULE_TYPE_CITY for manifest in [build_service_manifest("city", module_type=MODULE_TYPE_CITY)])


def test_manifest_policy_loads_from_local_json(tmp_path):
    path = tmp_path / "modules.json"
    path.write_text(
        """
{
  "enabled_modules": {"voice": true},
  "preferred_providers": {"voice.capture": "mock_microphone_adapter"},
  "allowed_permissions": ["network.outbound"]
}
""",
        encoding="utf-8",
    )

    policy = load_manifest_policy(path)

    assert policy.enabled_modules == {"voice": True}
    assert policy.preferred_provider("voice.capture") == "mock_microphone_adapter"
    assert policy.allowed_permissions == [PERMISSION_NETWORK_OUTBOUND]


class _ManifestSkill(Skill):
    name = "manifest_test_skill"
    description = "Test skill with explicit capabilities."
    capabilities = ("test.skill",)
    intent_names = ("test_intent",)

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        return SkillResponse(text="ok", skill=self.name)


def test_skill_manifest_builder_declares_skill_module():
    manifest = build_skill_manifest("example_skill", capabilities=["example.run"])

    assert manifest.module_type == MODULE_TYPE_SKILL
    assert manifest.capabilities == ["example.run"]
    assert manifest.lifecycle_support == ["execute"]


def test_skill_registry_registers_skill_manifest():
    registry = SkillRegistry()
    skill = registry.register(_ManifestSkill())
    manifest = registry.manifest_registry.get_manifest(skill.name)

    assert manifest is not None
    assert manifest.module_type == MODULE_TYPE_SKILL
    assert manifest.capabilities == ["test.skill"]
