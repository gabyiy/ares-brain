from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse


VOICE_PROFILE_SCHEMA_NAME = "ares.piper_voice_profiles"
VOICE_PROFILE_SCHEMA_VERSION = 1
DEFAULT_VOICE_PROFILE_CONFIG_PATH = Path("config/voice_profiles.json")
SUPPORTED_VOICE_ENGINES = frozenset({"piper"})
SUPPORTED_VOICE_GENDERS = frozenset({"female", "male", "neutral", "unknown"})
SUPPORTED_VOICE_QUALITIES = frozenset({"x_low", "low", "medium", "high"})

_PROFILE_FIELDS = frozenset(
    {
        "profile_id",
        "display_name",
        "engine",
        "language",
        "locale",
        "gender",
        "quality",
        "model_path",
        "config_path",
        "expected_sample_rate_hz",
        "enabled",
        "default",
        "description",
        "source_model_url",
        "source_config_url",
        "source_metadata_url",
        "expected_model_size_bytes",
        "expected_config_size_bytes",
        "model_md5",
        "config_md5",
        "minimum_model_size_bytes",
    }
)


class VoiceProfileError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


@dataclass(frozen=True)
class VoiceProfile:
    profile_id: str
    display_name: str
    engine: str
    language: str
    locale: str
    gender: str
    quality: str
    model_path: str
    config_path: str
    expected_sample_rate_hz: Optional[int] = None
    enabled: bool = True
    is_default: bool = False
    description: str = ""
    source_model_url: str = ""
    source_config_url: str = ""
    source_metadata_url: str = ""
    expected_model_size_bytes: Optional[int] = None
    expected_config_size_bytes: Optional[int] = None
    model_md5: str = ""
    config_md5: str = ""
    minimum_model_size_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        _validate_profile(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "VoiceProfile":
        if not isinstance(payload, dict):
            raise VoiceProfileError("malformed_profile", "Voice profile must be an object")
        unknown = sorted(set(payload) - _PROFILE_FIELDS)
        if unknown:
            raise VoiceProfileError(
                "malformed_profile",
                f"Unknown voice profile fields: {', '.join(unknown)}",
            )
        required = (
            "profile_id",
            "display_name",
            "engine",
            "language",
            "locale",
            "gender",
            "quality",
            "model_path",
            "config_path",
            "enabled",
            "default",
        )
        missing = [field_name for field_name in required if field_name not in payload]
        if missing:
            raise VoiceProfileError(
                "malformed_profile",
                f"Missing voice profile fields: {', '.join(missing)}",
            )
        if not isinstance(payload["enabled"], bool) or not isinstance(payload["default"], bool):
            raise VoiceProfileError(
                "malformed_profile",
                "Voice profile enabled/default fields must be booleans",
            )
        return cls(
            profile_id=payload["profile_id"],
            display_name=payload["display_name"],
            engine=payload["engine"],
            language=payload["language"],
            locale=payload["locale"],
            gender=payload["gender"],
            quality=payload["quality"],
            model_path=payload["model_path"],
            config_path=payload["config_path"],
            expected_sample_rate_hz=payload.get("expected_sample_rate_hz"),
            enabled=payload["enabled"],
            is_default=payload["default"],
            description=payload.get("description", ""),
            source_model_url=payload.get("source_model_url", ""),
            source_config_url=payload.get("source_config_url", ""),
            source_metadata_url=payload.get("source_metadata_url", ""),
            expected_model_size_bytes=payload.get("expected_model_size_bytes"),
            expected_config_size_bytes=payload.get("expected_config_size_bytes"),
            model_md5=payload.get("model_md5", ""),
            config_md5=payload.get("config_md5", ""),
            minimum_model_size_bytes=payload.get("minimum_model_size_bytes", 1_000_000),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "engine": self.engine,
            "language": self.language,
            "locale": self.locale,
            "gender": self.gender,
            "quality": self.quality,
            "model_path": self.model_path,
            "config_path": self.config_path,
            "expected_sample_rate_hz": self.expected_sample_rate_hz,
            "enabled": self.enabled,
            "default": self.is_default,
            "description": self.description,
            "source_model_url": self.source_model_url,
            "source_config_url": self.source_config_url,
            "source_metadata_url": self.source_metadata_url,
            "expected_model_size_bytes": self.expected_model_size_bytes,
            "expected_config_size_bytes": self.expected_config_size_bytes,
            "model_md5": self.model_md5,
            "config_md5": self.config_md5,
            "minimum_model_size_bytes": self.minimum_model_size_bytes,
        }


@dataclass(frozen=True)
class VoiceProfileHealthResult:
    success: bool
    status: str
    profile_id: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "profile_id": self.profile_id,
            "error_message": self.error_message,
            "data": dict(self.data),
        }


class VoiceProfileRegistry:
    """Validated local registry for replaceable Piper voice profiles."""

    def __init__(
        self,
        profiles: Iterable[VoiceProfile],
        project_root: str | Path,
        approved_model_directories: Optional[Iterable[str | Path]] = None,
        allow_external_paths: bool = False,
    ):
        self.project_root = Path(project_root).expanduser().resolve()
        self.allow_external_paths = bool(allow_external_paths)
        roots = list(approved_model_directories or [self.project_root / "models" / "piper"])
        self.approved_model_directories = tuple(
            _resolve_from_root(path, self.project_root) for path in roots
        )
        self._profiles: Dict[str, VoiceProfile] = {}
        for profile in profiles:
            if profile.profile_id in self._profiles:
                raise VoiceProfileError(
                    "duplicate_profile",
                    f"Duplicate voice profile: {profile.profile_id}",
                )
            self._validate_paths(profile)
            self._profiles[profile.profile_id] = profile
        if not self._profiles:
            raise VoiceProfileError("no_profiles", "At least one voice profile is required")
        defaults = [profile for profile in self._profiles.values() if profile.is_default]
        if len(defaults) > 1:
            raise VoiceProfileError("multiple_defaults", "More than one default voice profile")
        if not defaults:
            raise VoiceProfileError("no_default", "Exactly one default voice profile is required")
        if not defaults[0].enabled:
            raise VoiceProfileError("default_disabled", "Default voice profile must be enabled")

    @classmethod
    def from_dict(
        cls,
        payload: Dict[str, Any],
        project_root: str | Path,
        approved_model_directories: Optional[Iterable[str | Path]] = None,
        allow_external_paths: bool = False,
    ) -> "VoiceProfileRegistry":
        if not isinstance(payload, dict):
            raise VoiceProfileError("malformed_config", "Voice profile config must be an object")
        allowed_root_fields = {"schema_name", "schema_version", "profiles"}
        unknown = sorted(set(payload) - allowed_root_fields)
        if unknown:
            raise VoiceProfileError(
                "malformed_config",
                f"Unknown voice profile config fields: {', '.join(unknown)}",
            )
        if payload.get("schema_name") != VOICE_PROFILE_SCHEMA_NAME:
            raise VoiceProfileError("invalid_schema_name", "Invalid voice profile schema name")
        if payload.get("schema_version") != VOICE_PROFILE_SCHEMA_VERSION:
            raise VoiceProfileError("invalid_schema_version", "Unsupported voice profile schema version")
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list):
            raise VoiceProfileError("malformed_config", "Voice profiles must be a list")
        profiles = [VoiceProfile.from_dict(item) for item in raw_profiles]
        return cls(
            profiles,
            project_root=project_root,
            approved_model_directories=approved_model_directories,
            allow_external_paths=allow_external_paths,
        )

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        project_root: str | Path,
        approved_model_directories: Optional[Iterable[str | Path]] = None,
        allow_external_paths: bool = False,
    ) -> "VoiceProfileRegistry":
        config_path = Path(path).expanduser()
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError as error:
            raise VoiceProfileError("config_missing", f"Voice profile config missing: {config_path}") from error
        except (json.JSONDecodeError, OSError, UnicodeError) as error:
            raise VoiceProfileError(
                "malformed_config",
                f"Voice profile config could not be loaded: {error.__class__.__name__}",
            ) from error
        return cls.from_dict(
            payload,
            project_root=project_root,
            approved_model_directories=approved_model_directories,
            allow_external_paths=allow_external_paths,
        )

    def default_profile(self) -> VoiceProfile:
        return next(profile for profile in self._profiles.values() if profile.is_default)

    def resolve(self, requested_profile_id: str = "") -> VoiceProfile:
        clean_id = str(requested_profile_id or "").strip()
        profile = self._profiles.get(clean_id) if clean_id else self.default_profile()
        if profile is None:
            raise VoiceProfileError("unknown_profile", f"Unknown voice profile: {clean_id}")
        if not profile.enabled:
            raise VoiceProfileError("profile_disabled", f"Voice profile is disabled: {profile.profile_id}")
        return profile

    def get(self, profile_id: str) -> Optional[VoiceProfile]:
        return self._profiles.get(str(profile_id or "").strip())

    def list_profiles(self) -> List[VoiceProfile]:
        return [self._profiles[key] for key in sorted(self._profiles)]

    def model_path(self, profile: VoiceProfile) -> Path:
        return _resolve_from_root(profile.model_path, self.project_root)

    def config_path(self, profile: VoiceProfile) -> Path:
        return _resolve_from_root(profile.config_path, self.project_root)

    def profile_metadata(self, profile: VoiceProfile) -> Dict[str, Any]:
        health = self.validate_installed(profile)
        return {
            **profile.to_dict(),
            "resolved_model_path": str(self.model_path(profile)),
            "resolved_config_path": str(self.config_path(profile)),
            "installed": health.success,
            "installation_status": health.status,
        }

    def validate_installed(
        self,
        profile: VoiceProfile | str,
        verify_checksums: bool = False,
    ) -> VoiceProfileHealthResult:
        try:
            selected = self.resolve(profile) if isinstance(profile, str) else profile
        except VoiceProfileError as error:
            return _health_failure(error.code, str(profile), error.message)
        model_path = self.model_path(selected)
        config_path = self.config_path(selected)
        model = self.validate_model(selected, verify_checksums)
        if not model.success:
            return model
        config = self.validate_config(selected, verify_checksums)
        if not config.success:
            return config
        return VoiceProfileHealthResult(
            success=True,
            status="profile_healthy",
            profile_id=selected.profile_id,
            data={
                "model_path": str(model_path),
                "config_path": str(config_path),
                "model_size_bytes": model.data["size_bytes"],
                "config_size_bytes": config.data["size_bytes"],
                "sample_rate_hz": config.data["sample_rate_hz"],
            },
        )

    def validate_model(
        self,
        profile: VoiceProfile,
        verify_checksum: bool = False,
    ) -> VoiceProfileHealthResult:
        return validate_voice_model_file(self.model_path(profile), profile, verify_checksum)

    def validate_config(
        self,
        profile: VoiceProfile,
        verify_checksum: bool = False,
    ) -> VoiceProfileHealthResult:
        return validate_voice_config_file(self.config_path(profile), profile, verify_checksum)

    def _validate_paths(self, profile: VoiceProfile) -> None:
        if self.allow_external_paths:
            return
        for label, path in (
            ("model", self.model_path(profile)),
            ("config", self.config_path(profile)),
        ):
            if not any(_is_within(path, root) for root in self.approved_model_directories):
                raise VoiceProfileError(
                    "unapproved_profile_path",
                    f"Voice {label} path is outside approved directories: {path}",
                )


def load_voice_profile_registry(
    config_path: str | Path = DEFAULT_VOICE_PROFILE_CONFIG_PATH,
    project_root: str | Path | None = None,
    allow_external_paths: bool = False,
) -> VoiceProfileRegistry:
    root = Path(project_root or Path(__file__).resolve().parent.parent).resolve()
    path = _resolve_from_root(config_path, root)
    return VoiceProfileRegistry.from_file(
        path,
        project_root=root,
        allow_external_paths=allow_external_paths,
    )


def _validate_profile(profile: VoiceProfile) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,63}", str(profile.profile_id or "")):
        raise VoiceProfileError("invalid_profile_id", "Invalid voice profile identifier")
    if not str(profile.display_name or "").strip():
        raise VoiceProfileError("invalid_display_name", "Voice display name is required")
    if profile.engine not in SUPPORTED_VOICE_ENGINES:
        raise VoiceProfileError("unsupported_engine", f"Unsupported voice engine: {profile.engine}")
    if not re.fullmatch(r"[a-z]{2,3}", str(profile.language or "")):
        raise VoiceProfileError("invalid_language", f"Invalid voice language: {profile.language}")
    if not re.fullmatch(r"[a-z]{2,3}_[A-Z]{2}", str(profile.locale or "")):
        raise VoiceProfileError("invalid_locale", f"Invalid voice locale: {profile.locale}")
    if not profile.locale.startswith(f"{profile.language}_"):
        raise VoiceProfileError("invalid_locale", "Voice locale does not match language")
    if profile.gender not in SUPPORTED_VOICE_GENDERS:
        raise VoiceProfileError("invalid_gender", f"Invalid voice gender: {profile.gender}")
    if profile.quality not in SUPPORTED_VOICE_QUALITIES:
        raise VoiceProfileError("invalid_quality", f"Invalid voice quality: {profile.quality}")
    if not str(profile.model_path or "").strip():
        raise VoiceProfileError("missing_model_path", "Voice model path is required")
    if not str(profile.config_path or "").strip():
        raise VoiceProfileError("missing_config_path", "Voice config path is required")
    if Path(profile.model_path).suffix.lower() != ".onnx":
        raise VoiceProfileError("invalid_model_path", "Voice model path must end in .onnx")
    if not str(profile.config_path).lower().endswith(".onnx.json"):
        raise VoiceProfileError("invalid_config_path", "Voice config path must end in .onnx.json")
    if profile.expected_sample_rate_hz is not None:
        if (
            isinstance(profile.expected_sample_rate_hz, bool)
            or not isinstance(profile.expected_sample_rate_hz, int)
            or not 8_000 <= profile.expected_sample_rate_hz <= 192_000
        ):
            raise VoiceProfileError("invalid_sample_rate", "Invalid expected sample rate")
    if (
        isinstance(profile.minimum_model_size_bytes, bool)
        or not isinstance(profile.minimum_model_size_bytes, int)
        or profile.minimum_model_size_bytes <= 0
    ):
        raise VoiceProfileError("invalid_model_size", "Minimum model size must be positive")
    for value, label in (
        (profile.expected_model_size_bytes, "expected_model_size_bytes"),
        (profile.expected_config_size_bytes, "expected_config_size_bytes"),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise VoiceProfileError("invalid_file_size", f"{label} must be positive")
    for digest, label in ((profile.model_md5, "model_md5"), (profile.config_md5, "config_md5")):
        if digest and not re.fullmatch(r"[a-f0-9]{32}", digest):
            raise VoiceProfileError("invalid_checksum", f"{label} must be a lowercase MD5 digest")
    for url, label in (
        (profile.source_model_url, "source_model_url"),
        (profile.source_config_url, "source_config_url"),
        (profile.source_metadata_url, "source_metadata_url"),
    ):
        if url and not _valid_https_url(url):
            raise VoiceProfileError("invalid_source_url", f"Invalid {label}")


def validate_voice_model_file(
    path: Path,
    profile: VoiceProfile,
    verify_checksum: bool,
) -> VoiceProfileHealthResult:
    try:
        if not path.exists() or not path.is_file():
            return _health_failure("model_missing", profile.profile_id, "Voice model is missing")
        size = path.stat().st_size
        if size < profile.minimum_model_size_bytes:
            return _health_failure("model_invalid", profile.profile_id, "Voice model is too small")
        if verify_checksum and profile.expected_model_size_bytes and size != profile.expected_model_size_bytes:
            return _health_failure("model_size_mismatch", profile.profile_id, "Voice model size mismatch")
        if not os.access(path, os.R_OK):
            return _health_failure("model_unreadable", profile.profile_id, "Voice model is unreadable")
        if verify_checksum and profile.model_md5 and _md5(path) != profile.model_md5:
            return _health_failure("model_checksum_mismatch", profile.profile_id, "Voice model checksum mismatch")
    except OSError as error:
        return _health_failure(
            "model_unreadable",
            profile.profile_id,
            f"Voice model could not be inspected: {error.__class__.__name__}",
        )
    return VoiceProfileHealthResult(True, "model_valid", profile.profile_id, data={"size_bytes": size})


def validate_voice_config_file(
    path: Path,
    profile: VoiceProfile,
    verify_checksum: bool,
) -> VoiceProfileHealthResult:
    try:
        if not path.exists() or not path.is_file():
            return _health_failure("config_missing", profile.profile_id, "Voice config is missing")
        size = path.stat().st_size
        if size <= 0 or not os.access(path, os.R_OK):
            return _health_failure("config_unreadable", profile.profile_id, "Voice config is unreadable")
        if verify_checksum and profile.expected_config_size_bytes and size != profile.expected_config_size_bytes:
            return _health_failure("config_size_mismatch", profile.profile_id, "Voice config size mismatch")
        if verify_checksum and profile.config_md5 and _md5(path) != profile.config_md5:
            return _health_failure("config_checksum_mismatch", profile.profile_id, "Voice config checksum mismatch")
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (json.JSONDecodeError, OSError, UnicodeError) as error:
        return _health_failure(
            "config_invalid",
            profile.profile_id,
            f"Voice config is invalid: {error.__class__.__name__}",
        )
    if not isinstance(payload, dict):
        return _health_failure("config_invalid", profile.profile_id, "Voice config must be an object")
    sample_rate = payload.get("audio", {}).get("sample_rate") if isinstance(payload.get("audio"), dict) else None
    language_code = payload.get("language", {}).get("code") if isinstance(payload.get("language"), dict) else None
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        return _health_failure("config_invalid", profile.profile_id, "Voice config sample rate is invalid")
    if profile.expected_sample_rate_hz and sample_rate != profile.expected_sample_rate_hz:
        return _health_failure("config_mismatch", profile.profile_id, "Voice config sample rate does not match profile")
    if language_code and language_code != profile.locale:
        return _health_failure("config_mismatch", profile.profile_id, "Voice config locale does not match profile")
    return VoiceProfileHealthResult(
        True,
        "config_valid",
        profile.profile_id,
        data={"size_bytes": size, "sample_rate_hz": sample_rate},
    )


def _health_failure(status: str, profile_id: str, message: str) -> VoiceProfileHealthResult:
    return VoiceProfileHealthResult(False, status, str(profile_id or ""), message)


def _resolve_from_root(path: str | Path, root: Path) -> Path:
    candidate = Path(path).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _valid_https_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and bool(parsed.netloc)


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
