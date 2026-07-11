from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.Contracts import (
    CONTRACT_CORE_EXECUTION_REQUEST,
    CONTRACT_CORE_EXECUTION_RESULT,
    CONTRACT_EVENT_PUBLICATION_ENVELOPE,
    CONTRACT_LIFECYCLE_EXECUTION_REQUEST,
    CONTRACT_LIFECYCLE_EXECUTION_RESULT,
    CONTRACT_MICROPHONE_CAPTURE_REQUEST,
    CONTRACT_MICROPHONE_CAPTURE_RESULT,
    CONTRACT_SPEECH_TO_TEXT_REQUEST,
    CONTRACT_SPEECH_TO_TEXT_RESULT,
    CONTRACT_VERSION_V1,
    CONTRACT_VOICE_COMMAND_REQUEST,
    CONTRACT_VOICE_COMMAND_RESULT,
    CONTRACT_VOICE_PIPELINE_REQUEST,
    CONTRACT_VOICE_PIPELINE_RESULT,
    DEFAULT_CONTRACT_REGISTRY,
    ContractRegistry,
    is_valid_contract_version,
)


MANIFEST_VERSION_V1 = "v1"
MODULE_TYPE_CITY = "city"
MODULE_TYPE_SKILL = "skill"
MODULE_TYPE_ADAPTER = "adapter"
MODULE_TYPE_SERVICE = "service"
MODULE_TYPES = (
    MODULE_TYPE_CITY,
    MODULE_TYPE_SKILL,
    MODULE_TYPE_ADAPTER,
    MODULE_TYPE_SERVICE,
)

PERMISSION_MICROPHONE_READ = "microphone.read"
PERMISSION_SPEAKER_WRITE = "speaker.write"
PERMISSION_CAMERA_READ = "camera.read"
PERMISSION_NETWORK_OUTBOUND = "network.outbound"
PERMISSION_FILESYSTEM_READ = "filesystem.read"
PERMISSION_FILESYSTEM_WRITE = "filesystem.write"
PERMISSION_PROCESS_LAUNCH = "process.launch"
PERMISSION_DEVICE_CONTROL = "device.control"
PERMISSION_GPIO_CONTROL = "gpio.control"
PERMISSIONS = (
    PERMISSION_MICROPHONE_READ,
    PERMISSION_SPEAKER_WRITE,
    PERMISSION_CAMERA_READ,
    PERMISSION_NETWORK_OUTBOUND,
    PERMISSION_FILESYSTEM_READ,
    PERMISSION_FILESYSTEM_WRITE,
    PERMISSION_PROCESS_LAUNCH,
    PERMISSION_DEVICE_CONTROL,
    PERMISSION_GPIO_CONTROL,
)

LIFECYCLE_OPERATION_START = "start"
LIFECYCLE_OPERATION_HEALTH_CHECK = "health_check"
LIFECYCLE_OPERATION_EXECUTE = "execute"
LIFECYCLE_OPERATION_STOP = "stop"
LIFECYCLE_OPERATION_RECOVER = "recover"
LIFECYCLE_OPERATIONS = (
    LIFECYCLE_OPERATION_START,
    LIFECYCLE_OPERATION_HEALTH_CHECK,
    LIFECYCLE_OPERATION_EXECUTE,
    LIFECYCLE_OPERATION_STOP,
    LIFECYCLE_OPERATION_RECOVER,
)

_MANIFEST_REQUIRED_FIELDS = {
    "module_name",
    "module_type",
    "module_version",
    "manifest_version",
    "description",
    "provider",
    "enabled_by_default",
    "capabilities",
    "consumed_contracts",
    "produced_contracts",
    "permissions",
    "lifecycle_support",
}


@dataclass(frozen=True)
class ManifestDependencies:
    required_capabilities: List[str] = field(default_factory=list)
    optional_capabilities: List[str] = field(default_factory=list)
    incompatible_capabilities: List[str] = field(default_factory=list)
    required_modules: List[str] = field(default_factory=list)
    optional_modules: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_capabilities",
            _normalize_capabilities(self.required_capabilities),
        )
        object.__setattr__(
            self,
            "optional_capabilities",
            _normalize_capabilities(self.optional_capabilities),
        )
        object.__setattr__(
            self,
            "incompatible_capabilities",
            _normalize_capabilities(self.incompatible_capabilities),
        )
        object.__setattr__(
            self,
            "required_modules",
            _normalize_module_names(self.required_modules),
        )
        object.__setattr__(
            self,
            "optional_modules",
            _normalize_module_names(self.optional_modules),
        )

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]]):
        data = dict(payload or {})
        return cls(
            required_capabilities=list(data.get("required_capabilities") or []),
            optional_capabilities=list(data.get("optional_capabilities") or []),
            incompatible_capabilities=list(data.get("incompatible_capabilities") or []),
            required_modules=list(data.get("required_modules") or []),
            optional_modules=list(data.get("optional_modules") or []),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "required_capabilities": list(self.required_capabilities),
            "optional_capabilities": list(self.optional_capabilities),
            "incompatible_capabilities": list(self.incompatible_capabilities),
            "required_modules": list(self.required_modules),
            "optional_modules": list(self.optional_modules),
        }


@dataclass(frozen=True)
class PlatformCompatibility:
    supported_operating_systems: List[str] = field(default_factory=list)
    supported_architectures: List[str] = field(default_factory=list)
    minimum_python_version: str = ""
    hardware_requirements: Dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "supported_operating_systems",
            _normalize_strings(self.supported_operating_systems),
        )
        object.__setattr__(
            self,
            "supported_architectures",
            _normalize_strings(self.supported_architectures),
        )
        object.__setattr__(
            self,
            "minimum_python_version",
            str(self.minimum_python_version or "").strip(),
        )
        object.__setattr__(
            self,
            "hardware_requirements",
            {
                str(name).strip(): bool(required)
                for name, required in dict(self.hardware_requirements or {}).items()
                if str(name).strip()
            },
        )

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]]):
        data = dict(payload or {})
        return cls(
            supported_operating_systems=list(data.get("supported_operating_systems") or []),
            supported_architectures=list(data.get("supported_architectures") or []),
            minimum_python_version=str(data.get("minimum_python_version") or ""),
            hardware_requirements=dict(data.get("hardware_requirements") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "supported_operating_systems": list(self.supported_operating_systems),
            "supported_architectures": list(self.supported_architectures),
            "minimum_python_version": self.minimum_python_version,
            "hardware_requirements": dict(self.hardware_requirements),
        }


@dataclass(frozen=True)
class CapabilityManifest:
    module_name: str
    module_type: str
    module_version: str
    manifest_version: str
    description: str
    provider: str
    enabled_by_default: bool
    capabilities: List[str] = field(default_factory=list)
    consumed_contracts: Dict[str, List[str]] = field(default_factory=dict)
    produced_contracts: Dict[str, List[str]] = field(default_factory=dict)
    dependencies: ManifestDependencies = field(default_factory=ManifestDependencies)
    platform: PlatformCompatibility = field(default_factory=PlatformCompatibility)
    permissions: List[str] = field(default_factory=list)
    lifecycle_support: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        module_name = _normalize_module_name(self.module_name)
        if not module_name:
            raise ValueError("module_name is required")
        object.__setattr__(self, "module_name", module_name)

        module_type = _normalize_string(self.module_type)
        if module_type not in MODULE_TYPES:
            raise ValueError(f"Unknown module_type: {self.module_type}")
        object.__setattr__(self, "module_type", module_type)

        module_version = _normalize_version(self.module_version, "module_version")
        manifest_version = _normalize_version(self.manifest_version, "manifest_version")
        object.__setattr__(self, "module_version", module_version)
        object.__setattr__(self, "manifest_version", manifest_version)

        description = str(self.description or "").strip()
        if not description:
            raise ValueError("description is required")
        object.__setattr__(self, "description", description)

        provider = _normalize_string(self.provider)
        if not provider:
            raise ValueError("provider is required")
        object.__setattr__(self, "provider", provider)

        object.__setattr__(self, "enabled_by_default", bool(self.enabled_by_default))
        object.__setattr__(self, "capabilities", _normalize_capabilities(self.capabilities))
        object.__setattr__(
            self,
            "consumed_contracts",
            _normalize_contract_map(self.consumed_contracts),
        )
        object.__setattr__(
            self,
            "produced_contracts",
            _normalize_contract_map(self.produced_contracts),
        )
        if not isinstance(self.dependencies, ManifestDependencies):
            object.__setattr__(
                self,
                "dependencies",
                ManifestDependencies.from_dict(self.dependencies),  # type: ignore[arg-type]
            )
        if not isinstance(self.platform, PlatformCompatibility):
            object.__setattr__(
                self,
                "platform",
                PlatformCompatibility.from_dict(self.platform),  # type: ignore[arg-type]
            )
        object.__setattr__(self, "permissions", _normalize_permissions(self.permissions))
        object.__setattr__(
            self,
            "lifecycle_support",
            _normalize_lifecycle_operations(self.lifecycle_support),
        )
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]):
        if not isinstance(payload, dict):
            raise ValueError("Manifest payload must be a dictionary")
        unknown_fields = sorted(set(payload) - (_MANIFEST_REQUIRED_FIELDS | {"dependencies", "platform", "metadata"}))
        if unknown_fields:
            raise ValueError(f"Unknown manifest fields: {', '.join(unknown_fields)}")
        for field_name in _MANIFEST_REQUIRED_FIELDS:
            if field_name not in payload:
                raise ValueError(f"Missing manifest field: {field_name}")
        return cls(
            module_name=payload["module_name"],
            module_type=payload["module_type"],
            module_version=payload["module_version"],
            manifest_version=payload["manifest_version"],
            description=payload["description"],
            provider=payload["provider"],
            enabled_by_default=bool(payload["enabled_by_default"]),
            capabilities=list(payload.get("capabilities") or []),
            consumed_contracts=dict(payload.get("consumed_contracts") or {}),
            produced_contracts=dict(payload.get("produced_contracts") or {}),
            dependencies=ManifestDependencies.from_dict(payload.get("dependencies")),
            platform=PlatformCompatibility.from_dict(payload.get("platform")),
            permissions=list(payload.get("permissions") or []),
            lifecycle_support=list(payload.get("lifecycle_support") or []),
            metadata=dict(payload.get("metadata") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_name": self.module_name,
            "module_type": self.module_type,
            "module_version": self.module_version,
            "manifest_version": self.manifest_version,
            "description": self.description,
            "provider": self.provider,
            "enabled_by_default": self.enabled_by_default,
            "capabilities": list(self.capabilities),
            "consumed_contracts": _stable_contract_map(self.consumed_contracts),
            "produced_contracts": _stable_contract_map(self.produced_contracts),
            "dependencies": self.dependencies.to_dict(),
            "platform": self.platform.to_dict(),
            "permissions": list(self.permissions),
            "lifecycle_support": list(self.lifecycle_support),
            "metadata": _stable_data(self.metadata),
        }


@dataclass(frozen=True)
class ManifestValidationResult:
    success: bool
    status: str
    module_name: str = ""
    text: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "module_name": self.module_name,
            "text": self.text,
            "error_message": self.error_message,
            "data": _stable_data(self.data),
            "metadata": _stable_data(self.metadata),
        }


@dataclass(frozen=True)
class ProviderSelectionResult:
    success: bool
    capability: str
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    selected_provider: str = ""
    reason: str = ""
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "capability": self.capability,
            "candidates": [_stable_data(candidate) for candidate in self.candidates],
            "selected_provider": self.selected_provider,
            "reason": self.reason,
            "error_message": self.error_message,
            "metadata": _stable_data(self.metadata),
        }


@dataclass(frozen=True)
class ManifestPolicy:
    enabled_modules: Dict[str, bool] = field(default_factory=dict)
    preferred_providers: Dict[str, str] = field(default_factory=dict)
    allowed_permissions: List[str] = field(default_factory=lambda: list(PERMISSIONS))

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "enabled_modules",
            {
                _normalize_module_name(name): bool(enabled)
                for name, enabled in dict(self.enabled_modules or {}).items()
                if _normalize_module_name(name)
            },
        )
        object.__setattr__(
            self,
            "preferred_providers",
            {
                _normalize_capability(capability): _normalize_module_name(provider)
                for capability, provider in dict(self.preferred_providers or {}).items()
                if _normalize_capability(capability) and _normalize_module_name(provider)
            },
        )
        object.__setattr__(
            self,
            "allowed_permissions",
            _normalize_permissions(self.allowed_permissions),
        )

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]]):
        data = dict(payload or {})
        return cls(
            enabled_modules=dict(data.get("enabled_modules") or {}),
            preferred_providers=dict(data.get("preferred_providers") or {}),
            allowed_permissions=list(data.get("allowed_permissions") or list(PERMISSIONS)),
        )

    def module_enabled(self, manifest: CapabilityManifest) -> bool:
        if manifest.module_name in self.enabled_modules:
            return self.enabled_modules[manifest.module_name]
        return manifest.enabled_by_default

    def preferred_provider(self, capability: str) -> str:
        return self.preferred_providers.get(_normalize_capability(capability), "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled_modules": dict(sorted(self.enabled_modules.items())),
            "preferred_providers": dict(sorted(self.preferred_providers.items())),
            "allowed_permissions": list(self.allowed_permissions),
        }


class CapabilityManifestRegistry:
    """Central manifest registry and pre-start validation gate."""

    def __init__(
        self,
        contract_registry: ContractRegistry = DEFAULT_CONTRACT_REGISTRY,
        policy: Optional[ManifestPolicy] = None,
    ):
        self.contract_registry = contract_registry
        self.policy = policy or ManifestPolicy()
        self._manifests: Dict[str, CapabilityManifest] = {}

    def register_manifest(self, manifest: CapabilityManifest | Dict[str, Any]) -> CapabilityManifest:
        parsed = manifest if isinstance(manifest, CapabilityManifest) else CapabilityManifest.from_dict(manifest)
        if parsed.module_name in self._manifests:
            raise ValueError(f"Duplicate module manifest: {parsed.module_name}")
        contract_result = self.validate_contract_compatibility(parsed)
        if not contract_result.success:
            raise ValueError(contract_result.error_message or contract_result.status)
        self._manifests[parsed.module_name] = parsed
        return parsed

    def unregister_manifest(self, module_name: str) -> bool:
        return self._manifests.pop(_normalize_module_name(module_name), None) is not None

    def get_manifest(self, module_name: str) -> Optional[CapabilityManifest]:
        return self._manifests.get(_normalize_module_name(module_name))

    def list_manifests(self) -> List[CapabilityManifest]:
        return [
            self._manifests[name]
            for name in sorted(self._manifests)
        ]

    def list_enabled_modules(self) -> List[str]:
        return [
            manifest.module_name
            for manifest in self.list_manifests()
            if self.policy.module_enabled(manifest)
        ]

    def list_modules_by_type(self, module_type: str) -> List[CapabilityManifest]:
        clean_type = _normalize_string(module_type)
        return [
            manifest
            for manifest in self.list_manifests()
            if manifest.module_type == clean_type
        ]

    def list_modules_by_capability(self, capability: str) -> List[CapabilityManifest]:
        clean_capability = _normalize_capability(capability)
        return [
            manifest
            for manifest in self.list_manifests()
            if clean_capability in manifest.capabilities
        ]

    def find_providers_for_capability(
        self,
        capability: str,
        available_modules: Optional[Sequence[str]] = None,
    ) -> List[CapabilityManifest]:
        clean_capability = _normalize_capability(capability)
        allowed_modules = (
            {_normalize_module_name(name) for name in available_modules}
            if available_modules is not None
            else None
        )
        candidates = [
            manifest
            for manifest in self.list_modules_by_capability(clean_capability)
            if allowed_modules is None or manifest.module_name in allowed_modules
        ]
        valid_candidates = [
            manifest
            for manifest in candidates
            if self.validate_manifest_requirements(manifest, clean_capability).success
        ]
        return _sort_provider_candidates(valid_candidates)

    def select_provider(
        self,
        capability: str,
        available_modules: Optional[Sequence[str]] = None,
    ) -> ProviderSelectionResult:
        clean_capability = _normalize_capability(capability)
        allowed_modules = (
            {_normalize_module_name(name) for name in available_modules}
            if available_modules is not None
            else None
        )
        raw_candidates = [
            manifest
            for manifest in self.list_modules_by_capability(clean_capability)
            if allowed_modules is None or manifest.module_name in allowed_modules
        ]
        candidates = self.find_providers_for_capability(
            clean_capability,
            available_modules=available_modules,
        )
        candidate_data = [candidate.to_dict() for candidate in candidates]
        preferred = self.policy.preferred_provider(clean_capability)
        if preferred:
            preferred_manifest = self.get_manifest(preferred)
            if preferred_manifest is None:
                return ProviderSelectionResult(
                    success=False,
                    capability=clean_capability,
                    candidates=candidate_data,
                    error_message=f"preferred_provider_missing:{preferred}",
                    metadata=_metadata("capability_manifest_registry"),
                )
            preferred_valid = self.validate_manifest_requirements(
                preferred_manifest,
                clean_capability,
            )
            if not preferred_valid.success:
                return ProviderSelectionResult(
                    success=False,
                    capability=clean_capability,
                    candidates=candidate_data,
                    error_message=preferred_valid.error_message or preferred_valid.status,
                    metadata={
                        **_metadata("capability_manifest_registry"),
                        "preferred_provider": preferred,
                    },
                )
            if available_modules is not None and preferred not in {
                _normalize_module_name(name)
                for name in available_modules
            }:
                return ProviderSelectionResult(
                    success=False,
                    capability=clean_capability,
                    candidates=candidate_data,
                    error_message=f"preferred_provider_unavailable:{preferred}",
                    metadata={
                        **_metadata("capability_manifest_registry"),
                        "preferred_provider": preferred,
                    },
                )
            return ProviderSelectionResult(
                success=True,
                capability=clean_capability,
                candidates=candidate_data,
                selected_provider=preferred,
                reason="preferred_provider",
                metadata=_metadata("capability_manifest_registry"),
            )

        if not candidates:
            if raw_candidates:
                validation = self.validate_manifest_requirements(
                    _sort_provider_candidates(raw_candidates)[0],
                    clean_capability,
                )
                return ProviderSelectionResult(
                    success=False,
                    capability=clean_capability,
                    candidates=[candidate.to_dict() for candidate in raw_candidates],
                    error_message=validation.error_message or validation.status,
                    metadata={
                        **_metadata("capability_manifest_registry"),
                        "validation": validation.to_dict(),
                    },
                )
            return ProviderSelectionResult(
                success=False,
                capability=clean_capability,
                candidates=[],
                error_message="no_valid_provider",
                metadata=_metadata("capability_manifest_registry"),
            )
        selected = candidates[0]
        return ProviderSelectionResult(
            success=True,
            capability=clean_capability,
            candidates=candidate_data,
            selected_provider=selected.module_name,
            reason="deterministic_order",
            metadata=_metadata("capability_manifest_registry"),
        )

    def validate_manifest_requirements(
        self,
        manifest_or_name: CapabilityManifest | str,
        required_capability: str = "",
        implementation: Any = None,
    ) -> ManifestValidationResult:
        manifest = (
            manifest_or_name
            if isinstance(manifest_or_name, CapabilityManifest)
            else self.get_manifest(manifest_or_name)
        )
        if manifest is None:
            return _manifest_error("manifest_missing", str(manifest_or_name or ""))

        clean_capability = _normalize_capability(required_capability)
        if not self.policy.module_enabled(manifest):
            return _manifest_error("module_disabled", manifest.module_name)
        if clean_capability and clean_capability not in manifest.capabilities:
            return _manifest_error(
                "capability_not_declared",
                manifest.module_name,
                data={"capability": clean_capability},
            )

        for validator in (
            self.validate_contract_compatibility,
            self.validate_dependencies,
            self.validate_permissions,
            self.validate_platform,
        ):
            result = validator(manifest)
            if not result.success:
                return result

        if implementation is not None:
            lifecycle = self.validate_lifecycle_implementation(manifest, implementation)
            if not lifecycle.success:
                return lifecycle

        return ManifestValidationResult(
            success=True,
            status="valid",
            module_name=manifest.module_name,
            text="Capability manifest validated.",
            data={"manifest": manifest.to_dict(), "capability": clean_capability},
            metadata=_metadata("capability_manifest_registry"),
        )

    def validate_contract_compatibility(
        self,
        manifest: CapabilityManifest,
    ) -> ManifestValidationResult:
        for direction, contract_map in (
            ("consumed", manifest.consumed_contracts),
            ("produced", manifest.produced_contracts),
        ):
            for contract_name, versions in contract_map.items():
                if contract_name not in self.contract_registry.list_contracts():
                    return _manifest_error(
                        "unknown_contract",
                        manifest.module_name,
                        error_message=f"unknown_contract:{contract_name}",
                        data={"direction": direction, "contract_name": contract_name},
                    )
                supported = self.contract_registry.supported_versions(contract_name)
                for version in versions:
                    if version not in supported:
                        return _manifest_error(
                            "unsupported_contract_version",
                            manifest.module_name,
                            error_message=f"unsupported_contract_version:{contract_name}:{version}",
                            data={
                                "direction": direction,
                                "contract_name": contract_name,
                                "contract_version": version,
                                "supported_versions": supported,
                            },
                        )
        return _manifest_ok(manifest.module_name, "contracts_valid")

    def validate_dependencies(self, manifest: CapabilityManifest) -> ManifestValidationResult:
        for capability in manifest.dependencies.required_capabilities:
            providers = [
                provider
                for provider in self.list_modules_by_capability(capability)
                if provider.module_name != manifest.module_name
                and self.policy.module_enabled(provider)
            ]
            if not providers:
                return _manifest_error(
                    "required_capability_missing",
                    manifest.module_name,
                    error_message=f"required_capability_missing:{capability}",
                    data={"capability": capability},
                )
        for module_name in manifest.dependencies.required_modules:
            dependency = self.get_manifest(module_name)
            if dependency is None or not self.policy.module_enabled(dependency):
                return _manifest_error(
                    "required_module_missing",
                    manifest.module_name,
                    error_message=f"required_module_missing:{module_name}",
                    data={"module": module_name},
                )
        for capability in manifest.dependencies.incompatible_capabilities:
            providers = [
                provider
                for provider in self.list_modules_by_capability(capability)
                if provider.module_name != manifest.module_name
                and self.policy.module_enabled(provider)
            ]
            if providers:
                return _manifest_error(
                    "incompatible_capability_present",
                    manifest.module_name,
                    error_message=f"incompatible_capability_present:{capability}",
                    data={
                        "capability": capability,
                        "providers": [provider.module_name for provider in providers],
                    },
                )
        return _manifest_ok(manifest.module_name, "dependencies_valid")

    def validate_permissions(self, manifest: CapabilityManifest) -> ManifestValidationResult:
        allowed = set(self.policy.allowed_permissions)
        denied = [
            permission
            for permission in manifest.permissions
            if permission not in allowed
        ]
        if denied:
            return _manifest_error(
                "permission_denied",
                manifest.module_name,
                error_message=f"permission_denied:{','.join(denied)}",
                data={"denied_permissions": denied, "allowed_permissions": list(allowed)},
            )
        return _manifest_ok(manifest.module_name, "permissions_valid")

    def validate_platform(self, manifest: CapabilityManifest) -> ManifestValidationResult:
        current_os = platform.system().lower()
        current_arch = platform.machine().lower()
        if manifest.platform.supported_operating_systems and current_os not in manifest.platform.supported_operating_systems:
            return _manifest_error(
                "platform_mismatch",
                manifest.module_name,
                error_message=f"platform_mismatch:os:{current_os}",
                data={
                    "current_os": current_os,
                    "supported_operating_systems": manifest.platform.supported_operating_systems,
                },
            )
        if manifest.platform.supported_architectures and current_arch not in manifest.platform.supported_architectures:
            return _manifest_error(
                "platform_mismatch",
                manifest.module_name,
                error_message=f"platform_mismatch:architecture:{current_arch}",
                data={
                    "current_architecture": current_arch,
                    "supported_architectures": manifest.platform.supported_architectures,
                },
            )
        if manifest.platform.minimum_python_version and not _python_version_supported(
            manifest.platform.minimum_python_version
        ):
            return _manifest_error(
                "platform_mismatch",
                manifest.module_name,
                error_message=f"platform_mismatch:python:{manifest.platform.minimum_python_version}",
                data={
                    "current_python": ".".join(str(part) for part in sys.version_info[:3]),
                    "minimum_python_version": manifest.platform.minimum_python_version,
                },
            )
        return _manifest_ok(manifest.module_name, "platform_valid")

    def validate_lifecycle_implementation(
        self,
        manifest: CapabilityManifest,
        implementation: Any,
    ) -> ManifestValidationResult:
        missing: List[str] = []
        for operation in manifest.lifecycle_support:
            if operation == LIFECYCLE_OPERATION_HEALTH_CHECK:
                if not any(
                    callable(getattr(implementation, method_name, None))
                    for method_name in ("health_check", "get_status", "status")
                ):
                    missing.append(operation)
                continue
            if operation in {LIFECYCLE_OPERATION_START, LIFECYCLE_OPERATION_STOP}:
                continue
            if operation == LIFECYCLE_OPERATION_EXECUTE:
                continue
            if operation == LIFECYCLE_OPERATION_RECOVER:
                continue
        if missing:
            return _manifest_error(
                "lifecycle_declaration_mismatch",
                manifest.module_name,
                error_message=f"lifecycle_declaration_mismatch:{','.join(missing)}",
                data={"missing_operations": missing},
            )
        return _manifest_ok(manifest.module_name, "lifecycle_valid")


def load_manifest_policy(path: Path) -> ManifestPolicy:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Manifest policy config must be a JSON object")
    return ManifestPolicy.from_dict(data)


def build_service_manifest(
    module_name: str,
    capabilities: Optional[List[str]] = None,
    module_type: str = MODULE_TYPE_SERVICE,
    description: str = "ARES registered service.",
    provider: str = "ares",
    enabled_by_default: bool = True,
    consumed_contracts: Optional[Dict[str, List[str]]] = None,
    produced_contracts: Optional[Dict[str, List[str]]] = None,
    dependencies: Optional[ManifestDependencies] = None,
    permissions: Optional[List[str]] = None,
    lifecycle_support: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> CapabilityManifest:
    return CapabilityManifest(
        module_name=module_name,
        module_type=module_type,
        module_version=CONTRACT_VERSION_V1,
        manifest_version=MANIFEST_VERSION_V1,
        description=description,
        provider=provider,
        enabled_by_default=enabled_by_default,
        capabilities=capabilities or [],
        consumed_contracts=consumed_contracts
        or {
            CONTRACT_CORE_EXECUTION_REQUEST: [CONTRACT_VERSION_V1],
            CONTRACT_LIFECYCLE_EXECUTION_REQUEST: [CONTRACT_VERSION_V1],
        },
        produced_contracts=produced_contracts
        or {
            CONTRACT_CORE_EXECUTION_RESULT: [CONTRACT_VERSION_V1],
            CONTRACT_LIFECYCLE_EXECUTION_RESULT: [CONTRACT_VERSION_V1],
        },
        dependencies=dependencies or ManifestDependencies(),
        permissions=permissions or [],
        lifecycle_support=lifecycle_support
        or [
            LIFECYCLE_OPERATION_START,
            LIFECYCLE_OPERATION_EXECUTE,
            LIFECYCLE_OPERATION_STOP,
        ],
        metadata=metadata or {"source": "core_service"},
    )


def build_skill_manifest(
    skill_name: str,
    capabilities: Optional[List[str]] = None,
    description: str = "ARES registered skill.",
    provider: str = "ares",
    module_version: str = CONTRACT_VERSION_V1,
    enabled_by_default: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
) -> CapabilityManifest:
    """Build a manifest for a skill registered through SkillRegistry."""

    clean_skill = _normalize_module_name(skill_name)
    if not clean_skill:
        raise ValueError("module_name is required")
    return CapabilityManifest(
        module_name=clean_skill,
        module_type=MODULE_TYPE_SKILL,
        module_version=module_version,
        manifest_version=MANIFEST_VERSION_V1,
        description=description,
        provider=provider,
        enabled_by_default=enabled_by_default,
        capabilities=capabilities or [f"skill.{clean_skill}"],
        lifecycle_support=[LIFECYCLE_OPERATION_EXECUTE],
        metadata=metadata or {"source": "skill_registry"},
    )


def build_pc_service_manifest(capabilities: Optional[List[str]] = None) -> CapabilityManifest:
    return build_service_manifest(
        module_name="pc",
        module_type=MODULE_TYPE_CITY,
        capabilities=capabilities
        or [
            "pc.status",
            "pc.capabilities",
            "device.actions",
            "device.status",
            "device.apps",
            "device.open_app",
        ],
        description="Device/PC City service boundary.",
        lifecycle_support=[
            LIFECYCLE_OPERATION_START,
            LIFECYCLE_OPERATION_HEALTH_CHECK,
            LIFECYCLE_OPERATION_EXECUTE,
            LIFECYCLE_OPERATION_STOP,
        ],
        metadata={"source": "core_service", "city": "pc"},
    )


def build_voice_city_manifest(capabilities: Optional[List[str]] = None) -> CapabilityManifest:
    return build_service_manifest(
        module_name="voice",
        module_type=MODULE_TYPE_CITY,
        capabilities=capabilities
        or [
            "voice.status",
            "voice.capabilities",
            "voice.text_loop",
        ],
        description="Voice City placeholder service boundary.",
        consumed_contracts={
            CONTRACT_CORE_EXECUTION_REQUEST: [CONTRACT_VERSION_V1],
            CONTRACT_LIFECYCLE_EXECUTION_REQUEST: [CONTRACT_VERSION_V1],
            CONTRACT_VOICE_COMMAND_REQUEST: [CONTRACT_VERSION_V1],
        },
        produced_contracts={
            CONTRACT_CORE_EXECUTION_RESULT: [CONTRACT_VERSION_V1],
            CONTRACT_LIFECYCLE_EXECUTION_RESULT: [CONTRACT_VERSION_V1],
            CONTRACT_VOICE_COMMAND_RESULT: [CONTRACT_VERSION_V1],
        },
        dependencies=ManifestDependencies(
            required_capabilities=[
                "voice.capture",
                "voice.transcribe",
                "voice.output",
                "voice.command_route",
            ],
            optional_capabilities=["voice.session"],
        ),
        lifecycle_support=[
            LIFECYCLE_OPERATION_START,
            LIFECYCLE_OPERATION_HEALTH_CHECK,
            LIFECYCLE_OPERATION_EXECUTE,
            LIFECYCLE_OPERATION_STOP,
        ],
        metadata={"source": "core_service", "city": "voice"},
    )


def default_voice_related_manifests() -> List[CapabilityManifest]:
    return [
        CapabilityManifest(
            module_name="mock_microphone_adapter",
            module_type=MODULE_TYPE_ADAPTER,
            module_version=CONTRACT_VERSION_V1,
            manifest_version=MANIFEST_VERSION_V1,
            description="Mock microphone adapter for simulated Voice City input.",
            provider="ares",
            enabled_by_default=True,
            capabilities=["voice.capture"],
            consumed_contracts={CONTRACT_MICROPHONE_CAPTURE_REQUEST: [CONTRACT_VERSION_V1]},
            produced_contracts={CONTRACT_MICROPHONE_CAPTURE_RESULT: [CONTRACT_VERSION_V1]},
            platform=PlatformCompatibility(
                hardware_requirements={"microphone_required": False}
            ),
            permissions=[],
            lifecycle_support=[
                LIFECYCLE_OPERATION_START,
                LIFECYCLE_OPERATION_STOP,
            ],
            metadata={"source": "voice_city", "mock": True},
        ),
        CapabilityManifest(
            module_name="mock_speech_to_text_adapter",
            module_type=MODULE_TYPE_ADAPTER,
            module_version=CONTRACT_VERSION_V1,
            manifest_version=MANIFEST_VERSION_V1,
            description="Mock speech-to-text adapter for simulated Voice City transcription.",
            provider="ares",
            enabled_by_default=True,
            capabilities=["voice.transcribe"],
            consumed_contracts={CONTRACT_SPEECH_TO_TEXT_REQUEST: [CONTRACT_VERSION_V1]},
            produced_contracts={CONTRACT_SPEECH_TO_TEXT_RESULT: [CONTRACT_VERSION_V1]},
            platform=PlatformCompatibility(
                hardware_requirements={"microphone_required": False}
            ),
            permissions=[],
            lifecycle_support=[],
            metadata={"source": "voice_city", "mock": True},
        ),
        CapabilityManifest(
            module_name="mock_voice_output_adapter",
            module_type=MODULE_TYPE_ADAPTER,
            module_version=CONTRACT_VERSION_V1,
            manifest_version=MANIFEST_VERSION_V1,
            description="Mock voice output adapter for simulated Voice City responses.",
            provider="ares",
            enabled_by_default=True,
            capabilities=["voice.output"],
            consumed_contracts={CONTRACT_VOICE_PIPELINE_RESULT: [CONTRACT_VERSION_V1]},
            produced_contracts={CONTRACT_EVENT_PUBLICATION_ENVELOPE: [CONTRACT_VERSION_V1]},
            platform=PlatformCompatibility(
                hardware_requirements={"speaker_required": False}
            ),
            permissions=[],
            lifecycle_support=[],
            metadata={"source": "voice_city", "mock": True},
        ),
        CapabilityManifest(
            module_name="voice_command_router",
            module_type=MODULE_TYPE_SERVICE,
            module_version=CONTRACT_VERSION_V1,
            manifest_version=MANIFEST_VERSION_V1,
            description="Routes accepted transcriptions into the Voice City text loop.",
            provider="ares",
            enabled_by_default=True,
            capabilities=["voice.command_route"],
            consumed_contracts={
                CONTRACT_SPEECH_TO_TEXT_RESULT: [CONTRACT_VERSION_V1],
                CONTRACT_VOICE_COMMAND_REQUEST: [CONTRACT_VERSION_V1],
            },
            produced_contracts={CONTRACT_VOICE_COMMAND_RESULT: [CONTRACT_VERSION_V1]},
            permissions=[],
            lifecycle_support=[],
            metadata={"source": "voice_city"},
        ),
        CapabilityManifest(
            module_name="voice_session_skill",
            module_type=MODULE_TYPE_SKILL,
            module_version=CONTRACT_VERSION_V1,
            manifest_version=MANIFEST_VERSION_V1,
            description="Starts bounded mock voice sessions from text commands.",
            provider="ares",
            enabled_by_default=True,
            capabilities=["voice.session"],
            consumed_contracts={CONTRACT_CORE_EXECUTION_REQUEST: [CONTRACT_VERSION_V1]},
            produced_contracts={CONTRACT_CORE_EXECUTION_RESULT: [CONTRACT_VERSION_V1]},
            permissions=[],
            lifecycle_support=[],
            metadata={"source": "voice_city"},
        ),
    ]


def register_default_voice_manifests(registry: CapabilityManifestRegistry) -> None:
    for manifest in default_voice_related_manifests():
        if registry.get_manifest(manifest.module_name) is None:
            registry.register_manifest(manifest)


def _manifest_ok(module_name: str, status: str) -> ManifestValidationResult:
    return ManifestValidationResult(
        success=True,
        status=status,
        module_name=module_name,
        text="Capability manifest validation passed.",
        metadata=_metadata("capability_manifest_registry"),
    )


def _manifest_error(
    status: str,
    module_name: str,
    error_message: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> ManifestValidationResult:
    return ManifestValidationResult(
        success=False,
        status=status,
        module_name=_normalize_module_name(module_name),
        text="Capability manifest validation failed safely.",
        error_message=error_message or status,
        data=dict(data or {}),
        metadata=_metadata("capability_manifest_registry"),
    )


def _normalize_module_name(name: str) -> str:
    return "_".join(part for part in str(name or "").strip().lower().split() if part)


def _normalize_module_names(names: Sequence[str]) -> List[str]:
    return _unique(_normalize_module_name(name) for name in names)


def _normalize_capability(capability: str) -> str:
    return str(capability or "").strip().lower()


def _normalize_capabilities(capabilities: Sequence[str]) -> List[str]:
    return _unique(_normalize_capability(capability) for capability in capabilities)


def _normalize_string(value: str) -> str:
    return str(value or "").strip().lower()


def _normalize_strings(values: Sequence[str]) -> List[str]:
    return _unique(_normalize_string(value) for value in values)


def _normalize_version(version: str, label: str) -> str:
    clean_version = str(version or "").strip()
    if not is_valid_contract_version(clean_version):
        raise ValueError(f"Malformed {label}: {version}")
    return clean_version


def _normalize_contract_map(contract_map: Dict[str, Sequence[str]]) -> Dict[str, List[str]]:
    normalized: Dict[str, List[str]] = {}
    for contract_name, versions in dict(contract_map or {}).items():
        clean_name = str(contract_name or "").strip()
        if not clean_name:
            raise ValueError("contract name is required")
        normalized[clean_name] = _unique_versions(versions)
    return dict(sorted(normalized.items()))


def _normalize_permissions(permissions: Sequence[str]) -> List[str]:
    normalized = _unique(_normalize_string(permission) for permission in permissions)
    unknown = [permission for permission in normalized if permission not in PERMISSIONS]
    if unknown:
        raise ValueError(f"Unknown permission: {unknown[0]}")
    return normalized


def _normalize_lifecycle_operations(operations: Sequence[str]) -> List[str]:
    normalized = _unique(_normalize_string(operation) for operation in operations)
    unknown = [operation for operation in normalized if operation not in LIFECYCLE_OPERATIONS]
    if unknown:
        raise ValueError(f"Unknown lifecycle operation: {unknown[0]}")
    return normalized


def _unique_versions(versions: Sequence[str]) -> List[str]:
    normalized = _unique(str(version or "").strip() for version in versions)
    for version in normalized:
        if not is_valid_contract_version(version):
            raise ValueError(f"Malformed contract version: {version}")
    return normalized


def _unique(values: Sequence[str] | Any) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            normalized.append(clean)
    return normalized


def _stable_contract_map(contract_map: Dict[str, List[str]]) -> Dict[str, List[str]]:
    return {
        contract_name: list(contract_map[contract_name])
        for contract_name in sorted(contract_map)
    }


def _sort_provider_candidates(candidates: List[CapabilityManifest]) -> List[CapabilityManifest]:
    return sorted(
        candidates,
        key=lambda manifest: (
            manifest.provider,
            manifest.module_name,
            manifest.module_version,
        ),
    )


def _python_version_supported(minimum_version: str) -> bool:
    try:
        minimum = tuple(int(part) for part in minimum_version.split("."))
    except ValueError:
        return False
    current = tuple(sys.version_info[: len(minimum)])
    return current >= minimum


def _stable_data(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _stable_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stable_data(item) for item in value]
    if isinstance(value, set):
        return [_stable_data(item) for item in sorted(value, key=repr)]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _stable_data(to_dict())
    return repr(value)


def _metadata(source: str) -> Dict[str, Any]:
    return {"safe": True, "source": source}
