from __future__ import annotations

import re
from dataclasses import MISSING, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


CONTRACT_VERSION_V1 = "v1"
CONTRACT_VERSION_PATTERN = re.compile(r"^v[1-9][0-9]*$")

CONTRACT_MICROPHONE_CAPTURE_REQUEST = "microphone.capture.request"
CONTRACT_MICROPHONE_CAPTURE_RESULT = "microphone.capture.result"
CONTRACT_SPEECH_TO_TEXT_REQUEST = "speech_to_text.transcribe.request"
CONTRACT_SPEECH_TO_TEXT_RESULT = "speech_to_text.transcribe.result"
CONTRACT_TEXT_TO_SPEECH_REQUEST = "text_to_speech.synthesize.request"
CONTRACT_TEXT_TO_SPEECH_RESULT = "text_to_speech.synthesize.result"
CONTRACT_VOICE_COMMAND_REQUEST = "voice.command.request"
CONTRACT_VOICE_COMMAND_RESULT = "voice.command.result"
CONTRACT_CORE_EXECUTION_REQUEST = "core.execution.request"
CONTRACT_CORE_EXECUTION_RESULT = "core.execution.result"
CONTRACT_LIFECYCLE_EXECUTION_REQUEST = "lifecycle.execution.request"
CONTRACT_LIFECYCLE_EXECUTION_RESULT = "lifecycle.execution.result"
CONTRACT_VOICE_PIPELINE_REQUEST = "voice.pipeline.request"
CONTRACT_VOICE_PIPELINE_RESULT = "voice.pipeline.result"
CONTRACT_SINGLE_TURN_VOICE_REQUEST = "voice.single_turn.request"
CONTRACT_SINGLE_TURN_VOICE_RESULT = "voice.single_turn.result"
CONTRACT_MULTI_TURN_VOICE_SESSION_REQUEST = "voice.conversation_session.request"
CONTRACT_MULTI_TURN_VOICE_SESSION_RESULT = "voice.conversation_session.result"
CONTRACT_EVENT_PUBLICATION_ENVELOPE = "event.publication.envelope"

CONTRACT_REQUIRED_FIELDS = (
    "contract_name",
    "contract_version",
    "correlation_id",
    "created_at",
    "metadata",
)


def utc_contract_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_correlation_id(prefix: str = "corr") -> str:
    clean_prefix = str(prefix or "corr").strip() or "corr"
    return f"{clean_prefix}-{uuid4()}"


def is_valid_contract_version(version: str) -> bool:
    return bool(CONTRACT_VERSION_PATTERN.match(str(version or "").strip()))


@dataclass(frozen=True)
class ContractCompatibilityResult:
    success: bool
    status: str
    contract_name: str = ""
    contract_version: str = ""
    current_version: str = ""
    supported_versions: List[str] = field(default_factory=list)
    consumers: List[str] = field(default_factory=list)
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "current_version": self.current_version,
            "supported_versions": list(self.supported_versions),
            "consumers": list(self.consumers),
            "error_message": self.error_message,
            "metadata": _stable_data(self.metadata),
        }


@dataclass(frozen=True)
class ContractRegistration:
    contract_name: str
    current_version: str = CONTRACT_VERSION_V1
    supported_versions: List[str] = field(default_factory=lambda: [CONTRACT_VERSION_V1])
    consumers: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        clean_name = str(self.contract_name or "").strip()
        if not clean_name:
            raise ValueError("contract_name is required")
        object.__setattr__(self, "contract_name", clean_name)
        object.__setattr__(self, "current_version", _normalize_version(self.current_version))
        supported = _unique_versions(self.supported_versions or [self.current_version])
        if self.current_version not in supported:
            supported.append(self.current_version)
        object.__setattr__(self, "supported_versions", supported)
        object.__setattr__(
            self,
            "consumers",
            _unique_strings(self.consumers),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_name": self.contract_name,
            "current_version": self.current_version,
            "supported_versions": list(self.supported_versions),
            "consumers": list(self.consumers),
        }


class ContractRegistry:
    """Central compatibility registry for public ARES request/result contracts."""

    def __init__(self):
        self._registrations: Dict[str, ContractRegistration] = {}

    def register(
        self,
        contract_name: str,
        current_version: str = CONTRACT_VERSION_V1,
        supported_versions: Optional[List[str]] = None,
        consumers: Optional[List[str]] = None,
    ) -> ContractRegistration:
        registration = ContractRegistration(
            contract_name=contract_name,
            current_version=current_version,
            supported_versions=supported_versions or [current_version],
            consumers=consumers or [],
        )
        existing = self._registrations.get(registration.contract_name)
        if existing is not None and existing != registration:
            raise ValueError(f"Incompatible duplicate contract registration: {contract_name}")
        self._registrations[registration.contract_name] = registration
        return registration

    def list_contracts(self) -> List[str]:
        return sorted(self._registrations)

    def supported_versions(self, contract_name: str) -> List[str]:
        registration = self._registrations.get(str(contract_name or "").strip())
        return list(registration.supported_versions) if registration else []

    def current_version(self, contract_name: str) -> str:
        registration = self._registrations.get(str(contract_name or "").strip())
        return registration.current_version if registration else ""

    def consumers(self, contract_name: str) -> List[str]:
        registration = self._registrations.get(str(contract_name or "").strip())
        return list(registration.consumers) if registration else []

    def is_compatible(self, contract_name: str, contract_version: str) -> bool:
        return self.validate(
            {
                "contract_name": contract_name,
                "contract_version": contract_version,
                "correlation_id": "",
                "created_at": utc_contract_timestamp(),
                "metadata": {},
            }
        ).success

    def validate(
        self,
        contract: Any,
        expected_contract_name: Optional[str] = None,
    ) -> ContractCompatibilityResult:
        payload = _contract_to_dict(contract)
        for field_name in CONTRACT_REQUIRED_FIELDS:
            if field_name not in payload:
                return _compatibility_error(
                    status="missing_contract_field",
                    error_message=f"missing_{field_name}",
                    payload=payload,
                    expected_contract_name=expected_contract_name,
                )

        contract_name = str(payload.get("contract_name") or "").strip()
        contract_version = str(payload.get("contract_version") or "").strip()
        if expected_contract_name and contract_name != expected_contract_name:
            return _compatibility_error(
                status="wrong_contract_type",
                error_message=f"expected:{expected_contract_name}:got:{contract_name}",
                payload=payload,
                expected_contract_name=expected_contract_name,
            )
        if not contract_name:
            return _compatibility_error(
                status="missing_contract_name",
                error_message="missing_contract_name",
                payload=payload,
                expected_contract_name=expected_contract_name,
            )
        if not contract_version:
            return _compatibility_error(
                status="missing_contract_version",
                error_message="missing_contract_version",
                payload=payload,
                expected_contract_name=expected_contract_name,
            )
        if not is_valid_contract_version(contract_version):
            return _compatibility_error(
                status="malformed_contract_version",
                error_message=f"malformed_contract_version:{contract_version}",
                payload=payload,
                expected_contract_name=expected_contract_name,
            )

        registration = self._registrations.get(contract_name)
        if registration is None:
            return _compatibility_error(
                status="unknown_contract",
                error_message=f"unknown_contract:{contract_name}",
                payload=payload,
                expected_contract_name=expected_contract_name,
            )
        if contract_version not in registration.supported_versions:
            return ContractCompatibilityResult(
                success=False,
                status="unsupported_contract_version",
                contract_name=contract_name,
                contract_version=contract_version,
                current_version=registration.current_version,
                supported_versions=list(registration.supported_versions),
                consumers=list(registration.consumers),
                error_message=f"unsupported_contract_version:{contract_name}:{contract_version}",
                metadata={
                    "safe": True,
                    "source": "contract_registry",
                    "metadata": _stable_data(payload.get("metadata") or {}),
                },
            )

        return ContractCompatibilityResult(
            success=True,
            status="compatible",
            contract_name=contract_name,
            contract_version=contract_version,
            current_version=registration.current_version,
            supported_versions=list(registration.supported_versions),
            consumers=list(registration.consumers),
            metadata={
                "safe": True,
                "source": "contract_registry",
                "metadata": _stable_data(payload.get("metadata") or {}),
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contracts": {
                name: self._registrations[name].to_dict()
                for name in sorted(self._registrations)
            }
        }


@dataclass(frozen=True)
class VersionedContract:
    contract_name: str
    contract_version: str = CONTRACT_VERSION_V1
    correlation_id: str = field(default_factory=new_correlation_id)
    session_id: str = ""
    created_at: str = field(default_factory=utc_contract_timestamp)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clean_name = str(self.contract_name or "").strip()
        if not clean_name:
            raise ValueError("contract_name is required")
        object.__setattr__(self, "contract_name", clean_name)
        object.__setattr__(self, "contract_version", _normalize_version(self.contract_version))
        object.__setattr__(self, "correlation_id", str(self.correlation_id or "").strip())
        object.__setattr__(self, "session_id", str(self.session_id or "").strip())
        object.__setattr__(self, "created_at", str(self.created_at or "").strip() or utc_contract_timestamp())
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    def to_dict(self) -> Dict[str, Any]:
        return {
            field_info.name: _stable_data(getattr(self, field_info.name))
            for field_info in fields(self)
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]):
        if not isinstance(payload, dict):
            raise ValueError("Contract payload must be a dictionary")
        allowed_fields = {field_info.name for field_info in fields(cls)}
        unknown_fields = sorted(set(payload) - allowed_fields)
        if unknown_fields:
            raise ValueError(f"Unknown contract fields: {', '.join(unknown_fields)}")
        for field_name in CONTRACT_REQUIRED_FIELDS:
            if field_name not in payload:
                raise ValueError(f"Missing required contract field: {field_name}")
        values = {
            field_info.name: payload[field_info.name]
            for field_info in fields(cls)
            if field_info.name in payload
        }
        instance = cls(**values)
        default_name = _default_contract_name(cls)
        if default_name and instance.contract_name != default_name:
            raise ValueError(
                f"Wrong contract type: expected {default_name}, got {instance.contract_name}"
            )
        if instance.contract_version != CONTRACT_VERSION_V1:
            raise ValueError(
                f"Unsupported contract class version: {instance.contract_version}"
            )
        return instance


@dataclass(frozen=True)
class MicrophoneCaptureRequestV1(VersionedContract):
    contract_name: str = CONTRACT_MICROPHONE_CAPTURE_REQUEST
    timeout_seconds: Optional[float] = None
    cancel_requested: bool = False


@dataclass(frozen=True)
class MicrophoneCaptureResultV1(VersionedContract):
    contract_name: str = CONTRACT_MICROPHONE_CAPTURE_RESULT
    success: bool = False
    status: str = ""
    text: str = ""
    chunk: Optional[Dict[str, Any]] = None
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpeechToTextRequestV1(VersionedContract):
    contract_name: str = CONTRACT_SPEECH_TO_TEXT_REQUEST
    audio_chunk: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class SpeechToTextResultV1(VersionedContract):
    contract_name: str = CONTRACT_SPEECH_TO_TEXT_RESULT
    success: bool = False
    status: str = ""
    text: str = ""
    confidence: float = 0.0
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TextToSpeechRequestV1(VersionedContract):
    contract_name: str = CONTRACT_TEXT_TO_SPEECH_REQUEST
    text: str = ""
    language: str = "en_US"
    voice_id: str = ""
    voice_profile_id: str = ""
    speaking_rate: float = 1.0
    output_wav_path: str = ""
    timeout_seconds: Optional[float] = None
    playback_enabled: bool = False


@dataclass(frozen=True)
class TextToSpeechResultV1(VersionedContract):
    contract_name: str = CONTRACT_TEXT_TO_SPEECH_RESULT
    success: bool = False
    status: str = ""
    normalized_text: str = ""
    engine: str = ""
    voice_id: str = ""
    requested_voice_profile: str = ""
    resolved_voice_profile: str = ""
    voice_display_name: str = ""
    language: str = ""
    locale: str = ""
    gender: str = ""
    quality: str = ""
    model_path: str = ""
    config_path: str = ""
    generated_audio_path: str = ""
    duration_seconds: float = 0.0
    processing_time_seconds: float = 0.0
    playback_status: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceCommandRequestV1(VersionedContract):
    contract_name: str = CONTRACT_VOICE_COMMAND_REQUEST
    text: str = ""
    confidence: float = 0.0
    transcription: Dict[str, Any] = field(default_factory=dict)
    route: str = ""


@dataclass(frozen=True)
class VoiceCommandResultV1(VersionedContract):
    contract_name: str = CONTRACT_VOICE_COMMAND_RESULT
    success: bool = False
    status: str = ""
    text: str = ""
    response_text: str = ""
    route: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoreExecutionRequestV1(VersionedContract):
    contract_name: str = CONTRACT_CORE_EXECUTION_REQUEST
    capability: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoreExecutionResultV1(VersionedContract):
    contract_name: str = CONTRACT_CORE_EXECUTION_RESULT
    success: bool = False
    status: str = ""
    text: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleExecutionRequestV1(VersionedContract):
    contract_name: str = CONTRACT_LIFECYCLE_EXECUTION_REQUEST
    module_name: str = ""
    operation: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleExecutionResultV1(VersionedContract):
    contract_name: str = CONTRACT_LIFECYCLE_EXECUTION_RESULT
    success: bool = False
    status: str = ""
    state: str = ""
    text: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VoicePipelineRequestV1(VersionedContract):
    contract_name: str = CONTRACT_VOICE_PIPELINE_REQUEST
    timeout_seconds: Optional[float] = None


@dataclass(frozen=True)
class VoicePipelineResultV1(VersionedContract):
    contract_name: str = CONTRACT_VOICE_PIPELINE_RESULT
    success: bool = False
    status: str = ""
    text: str = ""
    response_text: str = ""
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SingleTurnVoiceRequestV1(VersionedContract):
    contract_name: str = CONTRACT_SINGLE_TURN_VOICE_REQUEST
    microphone_device: str = "hw:2,0"
    recording_duration_seconds: int = 5
    recording_output_path: str = "data/manual_voice_samples/single_turn_input.wav"
    language: str = "en"
    whisper_executable_path: str = "external/whisper.cpp/build/bin/whisper-cli"
    whisper_model_profile: str = "models/whisper/ggml-tiny.en.bin"
    minimum_rms: float = 0.0
    tts_voice_profile: str = ""
    speaker_device: str = "plughw:CARD=Device,DEV=0"
    playback_enabled: bool = False
    timeout_seconds: float = 300.0
    recording_timeout_seconds: Optional[float] = None
    transcription_timeout_seconds: Optional[float] = None
    brain_timeout_seconds: Optional[float] = None
    synthesis_timeout_seconds: Optional[float] = None
    playback_timeout_seconds: Optional[float] = None
    cleanup_policy: str = "delete_on_success"
    text_input: str = ""


@dataclass(frozen=True)
class SingleTurnVoiceResultV1(VersionedContract):
    contract_name: str = CONTRACT_SINGLE_TURN_VOICE_RESULT
    success: bool = False
    status: str = ""
    microphone_health_status: str = "not_checked"
    recording_status: str = "not_started"
    recorded_wav_path: str = ""
    recording_duration_seconds: float = 0.0
    peak_amplitude: int = 0
    rms_amplitude: float = 0.0
    transcription_status: str = "not_started"
    recognized_text: str = ""
    transcription_processing_time_seconds: float = 0.0
    brain_execution_status: str = "not_started"
    detected_intent: str = ""
    routed_skill: str = ""
    brain_text_response: str = ""
    brain_fallback_used: bool = False
    tts_status: str = "not_started"
    resolved_voice_profile: str = ""
    generated_speech_wav_path: str = ""
    tts_processing_time_seconds: float = 0.0
    playback_status: str = "not_requested"
    total_processing_time_seconds: float = 0.0
    error_stage: str = ""
    error_reason: str = ""
    simulated_input: bool = False
    data: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class MultiTurnVoiceSessionRequestV1(VersionedContract):
    contract_name: str = CONTRACT_MULTI_TURN_VOICE_SESSION_REQUEST
    microphone_device: str = "hw:2,0"
    speaker_device: str = "plughw:CARD=Device,DEV=0"
    recording_duration_seconds: int = 5
    recording_output_directory: str = "data/manual_voice_samples"
    language: str = "en"
    whisper_executable_path: str = "external/whisper.cpp/build/bin/whisper-cli"
    whisper_model_profile: str = "models/whisper/ggml-tiny.en.bin"
    minimum_rms: float = 0.0
    tts_voice_profile: str = ""
    playback_enabled: bool = False
    maximum_turns: int = 5
    maximum_session_duration_seconds: float = 180.0
    maximum_consecutive_failures: int = 3
    inter_turn_delay_seconds: float = 0.75
    silence_retry_enabled: bool = True
    blank_transcription_retry_enabled: bool = True
    stop_phrases: List[str] = field(default_factory=list)
    confirmation_stop_phrases: List[str] = field(default_factory=list)
    greeting_enabled: bool = True
    greeting_text: str = "Hello Gabriel. I am listening."
    closing_phrase_enabled: bool = True
    closing_phrase_text: str = "Goodbye Gabriel."
    cleanup_policy: str = "delete_on_success"
    per_turn_timeout_seconds: float = 300.0
    total_session_timeout_seconds: float = 180.0
    verbose_diagnostics: bool = False
    simulated_text_turns: List[str] = field(default_factory=list)
    interactive_text: bool = False


@dataclass(frozen=True)
class MultiTurnVoiceSessionResultV1(VersionedContract):
    contract_name: str = CONTRACT_MULTI_TURN_VOICE_SESSION_RESULT
    success: bool = False
    status: str = ""
    started_at: str = ""
    completed_at: str = ""
    total_duration_seconds: float = 0.0
    attempted_turns: int = 0
    successful_turns: int = 0
    failed_turns: int = 0
    silent_turns: int = 0
    blank_transcription_turns: int = 0
    stop_reason: str = ""
    recognized_stop_phrase: str = ""
    maximum_turns_reached: bool = False
    maximum_duration_reached: bool = False
    cancelled: bool = False
    fallback_responses_used: bool = False
    resource_cleanup_status: str = "not_started"
    final_state: str = "created"
    turn_summaries: List[Dict[str, Any]] = field(default_factory=list)
    state_history: List[Dict[str, Any]] = field(default_factory=list)
    error_stage: str = ""
    error_reason: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class EventPublicationEnvelopeV1(VersionedContract):
    contract_name: str = CONTRACT_EVENT_PUBLICATION_ENVELOPE
    source: str = ""
    type: str = ""
    priority: str = "normal"
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_contract_timestamp)


def build_default_contract_registry() -> ContractRegistry:
    registry = ContractRegistry()
    registry.register(
        CONTRACT_MICROPHONE_CAPTURE_REQUEST,
        consumers=["VoicePipeline", "MicrophoneAdapter"],
    )
    registry.register(
        CONTRACT_MICROPHONE_CAPTURE_RESULT,
        consumers=["VoicePipeline", "MicrophoneAdapter"],
    )
    registry.register(
        CONTRACT_SPEECH_TO_TEXT_REQUEST,
        consumers=["VoicePipeline", "SpeechToTextAdapter"],
    )
    registry.register(
        CONTRACT_SPEECH_TO_TEXT_RESULT,
        consumers=["VoicePipeline", "SpeechToTextAdapter", "VoiceCommandRouter"],
    )
    registry.register(
        CONTRACT_TEXT_TO_SPEECH_REQUEST,
        consumers=["TextToSpeechAdapter", "VoiceOutputAdapter"],
    )
    registry.register(
        CONTRACT_TEXT_TO_SPEECH_RESULT,
        consumers=["TextToSpeechAdapter", "VoiceOutputAdapter"],
    )
    registry.register(
        CONTRACT_VOICE_COMMAND_REQUEST,
        consumers=["VoiceCommandRouter", "CoreService"],
    )
    registry.register(
        CONTRACT_VOICE_COMMAND_RESULT,
        consumers=["VoiceCommandRouter", "VoicePipeline"],
    )
    registry.register(
        CONTRACT_CORE_EXECUTION_REQUEST,
        consumers=["CoreService", "ModuleLifecycleManager"],
    )
    registry.register(
        CONTRACT_CORE_EXECUTION_RESULT,
        consumers=["CoreService", "VoiceCommandRouter"],
    )
    registry.register(
        CONTRACT_LIFECYCLE_EXECUTION_REQUEST,
        consumers=["ModuleLifecycleManager", "CoreService"],
    )
    registry.register(
        CONTRACT_LIFECYCLE_EXECUTION_RESULT,
        consumers=["ModuleLifecycleManager", "CoreService"],
    )
    registry.register(
        CONTRACT_VOICE_PIPELINE_REQUEST,
        consumers=["VoicePipeline"],
    )
    registry.register(
        CONTRACT_VOICE_PIPELINE_RESULT,
        consumers=["VoicePipeline"],
    )
    registry.register(
        CONTRACT_SINGLE_TURN_VOICE_REQUEST,
        consumers=["SingleTurnVoicePipeline"],
    )
    registry.register(
        CONTRACT_SINGLE_TURN_VOICE_RESULT,
        consumers=["SingleTurnVoicePipeline", "CoreService"],
    )
    registry.register(
        CONTRACT_MULTI_TURN_VOICE_SESSION_REQUEST,
        consumers=["MultiTurnVoiceSession"],
    )
    registry.register(
        CONTRACT_MULTI_TURN_VOICE_SESSION_RESULT,
        consumers=["MultiTurnVoiceSession", "CoreService"],
    )
    registry.register(
        CONTRACT_EVENT_PUBLICATION_ENVELOPE,
        consumers=["EventBus", "CoreService", "EventHistoryStore"],
    )
    return registry


def validate_contract(
    contract: Any,
    expected_contract_name: Optional[str] = None,
    registry: Optional[ContractRegistry] = None,
) -> ContractCompatibilityResult:
    if registry is None:
        registry = DEFAULT_CONTRACT_REGISTRY
    return registry.validate(contract, expected_contract_name=expected_contract_name)


def _compatibility_error(
    status: str,
    error_message: str,
    payload: Dict[str, Any],
    expected_contract_name: Optional[str],
) -> ContractCompatibilityResult:
    contract_name = str(payload.get("contract_name") or expected_contract_name or "")
    contract_version = str(payload.get("contract_version") or "")
    return ContractCompatibilityResult(
        success=False,
        status=status,
        contract_name=contract_name,
        contract_version=contract_version,
        error_message=error_message,
        metadata={
            "safe": True,
            "source": "contract_registry",
            "metadata": _stable_data(payload.get("metadata") or {}),
        },
    )


def _contract_to_dict(contract: Any) -> Dict[str, Any]:
    if isinstance(contract, dict):
        return dict(contract)
    to_dict = getattr(contract, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, dict):
            return dict(payload)
    return {
        "contract_name": getattr(contract, "contract_name", ""),
        "contract_version": getattr(contract, "contract_version", ""),
        "correlation_id": getattr(contract, "correlation_id", ""),
        "session_id": getattr(contract, "session_id", ""),
        "created_at": getattr(contract, "created_at", ""),
        "metadata": dict(getattr(contract, "metadata", {}) or {}),
    }


def _normalize_version(version: str) -> str:
    clean_version = str(version or "").strip()
    if not is_valid_contract_version(clean_version):
        raise ValueError(f"Invalid contract version: {version}")
    return clean_version


def _unique_versions(versions: List[str]) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for version in versions:
        clean_version = _normalize_version(version)
        if clean_version not in seen:
            seen.add(clean_version)
            normalized.append(clean_version)
    return normalized


def _unique_strings(values: Optional[List[str]]) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for value in values or []:
        clean_value = str(value or "").strip()
        if clean_value and clean_value not in seen:
            seen.add(clean_value)
            normalized.append(clean_value)
    return normalized


def _default_contract_name(cls: Any) -> str:
    field_info = cls.__dataclass_fields__.get("contract_name")  # type: ignore[attr-defined]
    if field_info is None:
        return ""
    if field_info.default is not MISSING:
        return str(field_info.default)
    return ""


def _stable_data(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"byte_count": len(value)}
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


DEFAULT_CONTRACT_REGISTRY = build_default_contract_registry()
