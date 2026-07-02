import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping


VALID_ADAPTER_MODES = {"mock", "local", "real"}
DEFAULT_ADAPTER_TIMEOUT_SECONDS = 10.0


class AdapterConfigError(ValueError):
    """Raised when an adapter config is structurally invalid."""


class SecretValidationError(ValueError):
    """Raised when adapter config content appears to contain a raw secret."""


@dataclass(frozen=True)
class SecretScanIssue:
    path: str
    field: str
    message: str
    matched_kind: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "path": self.path,
            "field": self.field,
            "message": self.message,
            "matched_kind": self.matched_kind,
        }


class SecretsGuard:
    """Validates future external adapter config without accepting raw secrets."""

    SECRET_FIELD_NAMES = {
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "bearer_token",
        "client_secret",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
    RAW_SECRET_PATTERNS = (
        ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
        ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
        ("github_fine_grained_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
        ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
        ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
        ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    )
    PLACEHOLDER_MARKERS = (
        "placeholder",
        "replace_me",
        "example",
        "fake",
        "dummy",
        "your_",
        "test_only",
    )
    ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")

    def validate_adapter_config(self, config: "ExternalAdapterConfig") -> None:
        issues = self.adapter_config_issues(config)
        if issues:
            raise SecretValidationError(_format_secret_issues(issues))

    def adapter_config_issues(self, config: "ExternalAdapterConfig") -> List[SecretScanIssue]:
        issues: List[SecretScanIssue] = []
        env_name = (config.api_key_env_name or "").strip()
        path = f"adapters.{config.name}.api_key_env_name"

        if env_name and not self.is_placeholder(env_name):
            if self.looks_like_raw_secret(env_name):
                issues.append(
                    SecretScanIssue(
                        path=path,
                        field="api_key_env_name",
                        message="Adapter config must reference an environment variable, not a raw secret.",
                        matched_kind="raw_secret",
                    )
                )
            elif not self.looks_like_env_name(env_name):
                issues.append(
                    SecretScanIssue(
                        path=path,
                        field="api_key_env_name",
                        message="Adapter config api_key_env_name must be an environment variable name.",
                        matched_kind="invalid_env_name",
                    )
                )

        base_url = (config.base_url or "").strip()
        if base_url:
            issues.extend(self.scan_text(base_url, path=f"adapters.{config.name}.base_url", field="base_url"))

        return issues

    def validate_config_payload(self, payload: Mapping[str, Any], path: str = "config") -> None:
        issues = self.scan_mapping(payload, path=path)
        if issues:
            raise SecretValidationError(_format_secret_issues(issues))

    def scan_mapping(self, payload: Mapping[str, Any], path: str = "config") -> List[SecretScanIssue]:
        issues: List[SecretScanIssue] = []
        for key, value in payload.items():
            field = str(key)
            current_path = f"{path}.{field}" if path else field
            if isinstance(value, Mapping):
                issues.extend(self.scan_mapping(value, path=current_path))
                continue
            if isinstance(value, list):
                for index, item in enumerate(value):
                    item_path = f"{current_path}[{index}]"
                    if isinstance(item, Mapping):
                        issues.extend(self.scan_mapping(item, path=item_path))
                    elif isinstance(item, str):
                        issues.extend(self._scan_field_value(field, item, path=item_path))
                continue
            if isinstance(value, str):
                issues.extend(self._scan_field_value(field, value, path=current_path))
        return issues

    def scan_text(self, text: str, path: str = "content", field: str = "") -> List[SecretScanIssue]:
        if self.is_placeholder(text):
            return []

        issues = []
        for kind, pattern in self.RAW_SECRET_PATTERNS:
            if pattern.search(text or ""):
                issues.append(
                    SecretScanIssue(
                        path=path,
                        field=field,
                        message="Raw-looking secret detected.",
                        matched_kind=kind,
                    )
                )
        return issues

    def _scan_field_value(self, field: str, value: str, path: str) -> List[SecretScanIssue]:
        normalized_field = field.lower().replace("-", "_")
        if normalized_field == "api_key_env_name":
            clean_value = (value or "").strip()
            if not clean_value or self.is_placeholder(clean_value):
                return []
            if self.looks_like_raw_secret(clean_value):
                return [
                    SecretScanIssue(
                        path=path,
                        field=field,
                        message="Adapter config must reference an environment variable, not a raw secret.",
                        matched_kind="raw_secret",
                    )
                ]
            if not self.looks_like_env_name(clean_value):
                return [
                    SecretScanIssue(
                        path=path,
                        field=field,
                        message="Adapter config api_key_env_name must be an environment variable name.",
                        matched_kind="invalid_env_name",
                    )
                ]
            return []

        if self.is_placeholder(value):
            return []

        issues = self.scan_text(value, path=path, field=field)
        if normalized_field in self.SECRET_FIELD_NAMES and value.strip():
            issues.append(
                SecretScanIssue(
                    path=path,
                    field=field,
                    message="Secret-like config fields may only contain explicit placeholders.",
                    matched_kind="secret_field",
                )
            )
        return issues

    def is_placeholder(self, value: str) -> bool:
        cleaned = (value or "").strip()
        if not cleaned:
            return False
        lowered = cleaned.lower()
        if lowered.startswith("${") and lowered.endswith("}"):
            return True
        if lowered.startswith("<") and lowered.endswith(">"):
            return True
        return any(marker in lowered for marker in self.PLACEHOLDER_MARKERS)

    def looks_like_env_name(self, value: str) -> bool:
        return bool(self.ENV_NAME_PATTERN.fullmatch((value or "").strip()))

    def looks_like_raw_secret(self, value: str) -> bool:
        return bool(self.scan_text(value))


@dataclass(frozen=True)
class ExternalAdapterConfig:
    name: str
    enabled: bool = True
    mode: str = "mock"
    api_key_env_name: str = ""
    base_url: str = ""
    timeout_seconds: float = DEFAULT_ADAPTER_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        name = (self.name or "").strip()
        if not name:
            raise AdapterConfigError("Adapter config name is required.")

        mode = (self.mode or "").strip().lower()
        if mode not in VALID_ADAPTER_MODES:
            raise AdapterConfigError(f"Invalid adapter mode for {name}: {self.mode}")

        timeout_seconds = float(self.timeout_seconds)
        if timeout_seconds <= 0:
            raise AdapterConfigError("Adapter timeout_seconds must be greater than zero.")

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "api_key_env_name", (self.api_key_env_name or "").strip())
        object.__setattr__(self, "base_url", (self.base_url or "").strip())
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        SecretsGuard().validate_adapter_config(self)

    @classmethod
    def from_dict(cls, name: str, payload: Mapping[str, Any]) -> "ExternalAdapterConfig":
        if not isinstance(payload, Mapping):
            raise AdapterConfigError(f"Adapter config for {name} must be a mapping.")

        SecretsGuard().validate_config_payload({"adapter": dict(payload)}, path=f"adapters.{name}")
        return cls(
            name=name,
            enabled=bool(payload.get("enabled", True)),
            mode=str(payload.get("mode", "mock")),
            api_key_env_name=str(payload.get("api_key_env_name", "") or ""),
            base_url=str(payload.get("base_url", "") or ""),
            timeout_seconds=float(payload.get("timeout_seconds", DEFAULT_ADAPTER_TIMEOUT_SECONDS)),
        )

    @property
    def api_key_env_name_is_placeholder(self) -> bool:
        return SecretsGuard().is_placeholder(self.api_key_env_name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "enabled": bool(self.enabled),
            "mode": self.mode,
            "api_key_env_name": self.api_key_env_name,
            "api_key_env_name_placeholder": self.api_key_env_name_is_placeholder,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
        }


def load_adapter_configs(path: str | Path) -> Dict[str, ExternalAdapterConfig]:
    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise AdapterConfigError("Adapter config file must contain a JSON object.")

    SecretsGuard().validate_config_payload(payload, path=str(config_path))
    adapters = payload.get("adapters", {})
    if not isinstance(adapters, Mapping):
        raise AdapterConfigError("Adapter config file requires an adapters object.")

    return {
        str(name): ExternalAdapterConfig.from_dict(str(name), adapter_payload)
        for name, adapter_payload in adapters.items()
    }


def _format_secret_issues(issues: Iterable[SecretScanIssue]) -> str:
    return "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
