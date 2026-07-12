import json
from pathlib import Path

import pytest

from core import (
    VoiceProfile,
    VoiceProfileError,
    VoiceProfileRegistry,
    load_voice_profile_registry,
)


def _profile(
    profile_id="en_US-hfc_male-medium",
    default=True,
    enabled=True,
    engine="piper",
    model_path="models/piper/male.onnx",
    config_path="models/piper/male.onnx.json",
):
    return VoiceProfile(
        profile_id=profile_id,
        display_name="ARES Male English",
        engine=engine,
        language="en",
        locale="en_US",
        gender="male",
        quality="medium",
        model_path=model_path,
        config_path=config_path,
        expected_sample_rate_hz=22050,
        enabled=enabled,
        is_default=default,
        source_model_url="https://voices.example/male.onnx",
        source_config_url="https://voices.example/male.onnx.json",
        minimum_model_size_bytes=4,
    )


def _write_installed(registry, profile):
    model = registry.model_path(profile)
    config = registry.config_path(profile)
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"valid model")
    config.write_text(
        json.dumps(
            {
                "audio": {"sample_rate": profile.expected_sample_rate_hz},
                "language": {"code": profile.locale},
            }
        ),
        encoding="utf-8",
    )


def test_default_config_resolves_official_male_ares_voice():
    registry = load_voice_profile_registry(project_root=Path.cwd())

    profile = registry.resolve()

    assert profile.profile_id == "en_US-hfc_male-medium"
    assert profile.display_name == "ARES Male English (HFC)"
    assert profile.gender == "male"
    assert profile.quality == "medium"
    assert profile.locale == "en_US"
    assert profile.expected_sample_rate_hz == 22050
    assert profile.model_path == "models/piper/en_US-hfc_male-medium.onnx"
    assert "rhasspy/piper-voices" in profile.source_model_url
    assert profile.source_metadata_url.endswith("hfc_male/medium/MODEL_CARD")


def test_registry_resolves_explicit_optional_amy_profile():
    registry = load_voice_profile_registry(project_root=Path.cwd())

    profile = registry.resolve("en_US-amy-low")

    assert profile.gender == "female"
    assert profile.quality == "low"
    assert profile.is_default is False


def test_registry_rejects_unknown_profile(tmp_path):
    registry = VoiceProfileRegistry([_profile()], project_root=tmp_path)

    with pytest.raises(VoiceProfileError, match="Unknown voice profile") as error:
        registry.resolve("missing")

    assert error.value.code == "unknown_profile"


def test_registry_rejects_disabled_profile_selection(tmp_path):
    registry = VoiceProfileRegistry(
        [
            _profile(),
            _profile(
                profile_id="disabled",
                default=False,
                enabled=False,
                model_path="models/piper/disabled.onnx",
                config_path="models/piper/disabled.onnx.json",
            ),
        ],
        project_root=tmp_path,
    )

    with pytest.raises(VoiceProfileError) as error:
        registry.resolve("disabled")

    assert error.value.code == "profile_disabled"


def test_registry_rejects_duplicate_profile_identifier(tmp_path):
    with pytest.raises(VoiceProfileError) as error:
        VoiceProfileRegistry([_profile(), _profile()], project_root=tmp_path)

    assert error.value.code == "duplicate_profile"


def test_registry_rejects_multiple_defaults(tmp_path):
    second = _profile(
        profile_id="second",
        model_path="models/piper/second.onnx",
        config_path="models/piper/second.onnx.json",
    )

    with pytest.raises(VoiceProfileError) as error:
        VoiceProfileRegistry([_profile(), second], project_root=tmp_path)

    assert error.value.code == "multiple_defaults"


def test_registry_rejects_missing_default(tmp_path):
    with pytest.raises(VoiceProfileError) as error:
        VoiceProfileRegistry([_profile(default=False)], project_root=tmp_path)

    assert error.value.code == "no_default"


def test_registry_reports_missing_model(tmp_path):
    registry = VoiceProfileRegistry([_profile()], project_root=tmp_path)
    profile = registry.default_profile()
    config = registry.config_path(profile)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        '{"audio": {"sample_rate": 22050}, "language": {"code": "en_US"}}',
        encoding="utf-8",
    )

    result = registry.validate_installed(profile)

    assert result.success is False
    assert result.status == "model_missing"


def test_registry_reports_missing_config(tmp_path):
    registry = VoiceProfileRegistry([_profile()], project_root=tmp_path)
    profile = registry.default_profile()
    model = registry.model_path(profile)
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"valid model")

    result = registry.validate_installed(profile)

    assert result.success is False
    assert result.status == "config_missing"


def test_registry_accepts_valid_installed_profile(tmp_path):
    registry = VoiceProfileRegistry([_profile()], project_root=tmp_path)
    profile = registry.default_profile()
    _write_installed(registry, profile)

    result = registry.validate_installed(profile)

    assert result.success is True
    assert result.data["sample_rate_hz"] == 22050


def test_profile_rejects_unsupported_engine():
    with pytest.raises(VoiceProfileError) as error:
        _profile(engine="cloud")

    assert error.value.code == "unsupported_engine"


@pytest.mark.parametrize(
    "changes",
    [
        {"language": "english"},
        {"locale": "en-us"},
        {"model_path": ""},
        {"config_path": ""},
    ],
)
def test_profile_rejects_malformed_required_fields(changes):
    payload = _profile().to_dict()
    payload.update(changes)

    with pytest.raises(VoiceProfileError):
        VoiceProfile.from_dict(payload)


def test_registry_rejects_model_paths_outside_approved_directory(tmp_path):
    with pytest.raises(VoiceProfileError) as error:
        VoiceProfileRegistry(
            [
                _profile(
                    model_path="../outside.onnx",
                    config_path="../outside.onnx.json",
                )
            ],
            project_root=tmp_path,
        )

    assert error.value.code == "unapproved_profile_path"


def test_registry_allows_external_paths_only_when_explicit(tmp_path):
    external = tmp_path.parent / "external_voice"
    registry = VoiceProfileRegistry(
        [
            _profile(
                model_path=str(external.with_suffix(".onnx")),
                config_path=str(external.with_suffix(".onnx.json")),
            )
        ],
        project_root=tmp_path,
        allow_external_paths=True,
    )

    assert registry.default_profile().profile_id == "en_US-hfc_male-medium"


def test_malformed_voice_profile_config_is_rejected(tmp_path):
    config = tmp_path / "voices.json"
    config.write_text("{not json", encoding="utf-8")

    with pytest.raises(VoiceProfileError) as error:
        VoiceProfileRegistry.from_file(config, project_root=tmp_path)

    assert error.value.code == "malformed_config"


def test_profile_serialization_is_deterministic():
    profile = _profile()

    assert profile.to_dict() == profile.to_dict()
    assert list(profile.to_dict())[:5] == [
        "profile_id",
        "display_name",
        "engine",
        "language",
        "locale",
    ]
