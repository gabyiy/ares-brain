from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class VoiceServiceResult:
    success: bool
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceStatus:
    status: str = "placeholder"
    available_actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "source": "voice_service",
            "available_actions": list(self.available_actions),
            "checks": {
                "audio_hardware_access": "disabled",
                "microphone": "disabled",
                "speaker": "disabled",
                "stt": "not_configured",
                "tts": "not_configured",
                "wake_word": "not_configured",
                "background_listening": "disabled",
                "internet": "disabled",
                "gpt": "disabled",
            },
        }


@dataclass(frozen=True)
class VoiceCapabilities:
    supported_voice_actions: List[Dict[str, Any]] = field(default_factory=list)
    supported_input_modes: List[str] = field(default_factory=list)
    supported_output_modes: List[str] = field(default_factory=list)
    available_status_providers: List[str] = field(default_factory=lambda: ["voice_status"])
    available_services: List[str] = field(
        default_factory=lambda: ["voice_service", "placeholder_voice_service"]
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": "voice_service",
            "supported_voice_actions": [
                dict(action) for action in self.supported_voice_actions
            ],
            "supported_input_modes": list(self.supported_input_modes),
            "supported_output_modes": list(self.supported_output_modes),
            "available_status_providers": list(self.available_status_providers),
            "available_services": list(self.available_services),
            "safeguards": {
                "audio_hardware_access": "disabled",
                "microphone": "disabled",
                "speaker": "disabled",
                "stt": "disabled",
                "tts": "disabled",
                "wake_word": "disabled",
                "background_listening": "disabled",
                "internet": "disabled",
                "gpt": "disabled",
            },
        }


class VoiceService:
    def get_status(self) -> VoiceServiceResult:
        raise NotImplementedError

    def get_capabilities(self) -> VoiceServiceResult:
        raise NotImplementedError

    def status(self) -> VoiceServiceResult:
        return self.get_status()


class PlaceholderVoiceService(VoiceService):
    """Voice City skeleton with no audio hardware access or background work."""

    def __init__(self):
        self.audio_hardware_accessed = False

    def get_status(self) -> VoiceServiceResult:
        status = VoiceStatus()
        return VoiceServiceResult(
            success=True,
            text="Voice City status: placeholder only. Audio hardware access is disabled.",
            data=status.to_dict(),
            metadata={
                "safe": True,
                "source": "voice_service",
                "placeholder": True,
                "audio_hardware_accessed": self.audio_hardware_accessed,
            },
        )

    def get_capabilities(self) -> VoiceServiceResult:
        capabilities = VoiceCapabilities()
        return VoiceServiceResult(
            success=True,
            text="Voice City capabilities discovered.",
            data=capabilities.to_dict(),
            metadata={
                "safe": True,
                "source": "voice_service",
                "placeholder": True,
                "audio_hardware_accessed": self.audio_hardware_accessed,
            },
        )
