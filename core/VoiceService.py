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
    voice_input: Dict[str, Any] = field(default_factory=dict)
    voice_output: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "source": "voice_service",
            "available_actions": list(self.available_actions),
            "voice_input": dict(self.voice_input),
            "voice_output": dict(self.voice_output),
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
    voice_input: str = "placeholder"
    voice_output: str = "placeholder"
    supported_voice_actions: List[Dict[str, Any]] = field(default_factory=list)
    supported_input_modes: List[str] = field(default_factory=list)
    supported_output_modes: List[str] = field(default_factory=list)
    input_capabilities: Dict[str, Any] = field(default_factory=dict)
    output_capabilities: Dict[str, Any] = field(default_factory=dict)
    available_status_providers: List[str] = field(default_factory=lambda: ["voice_status"])
    available_services: List[str] = field(
        default_factory=lambda: ["voice_service", "placeholder_voice_service"]
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": "voice_service",
            "voice_input": self.voice_input,
            "voice_output": self.voice_output,
            "supported_voice_actions": [
                dict(action) for action in self.supported_voice_actions
            ],
            "supported_input_modes": list(self.supported_input_modes),
            "supported_output_modes": list(self.supported_output_modes),
            "input_capabilities": dict(self.input_capabilities),
            "output_capabilities": dict(self.output_capabilities),
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


class VoiceInput:
    def listen_once(self) -> VoiceServiceResult:
        raise NotImplementedError

    def get_status(self) -> VoiceServiceResult:
        raise NotImplementedError

    def get_capabilities(self) -> VoiceServiceResult:
        raise NotImplementedError


class VoiceOutput:
    def speak(self, text: str) -> VoiceServiceResult:
        raise NotImplementedError

    def get_status(self) -> VoiceServiceResult:
        raise NotImplementedError

    def get_capabilities(self) -> VoiceServiceResult:
        raise NotImplementedError


class NullVoiceInput(VoiceInput):
    """Placeholder voice input that never touches microphone hardware."""

    def __init__(self):
        self.audio_hardware_accessed = False

    def listen_once(self) -> VoiceServiceResult:
        return VoiceServiceResult(
            success=False,
            text="Voice input is a placeholder. No microphone was accessed.",
            data={
                "source": "null_voice_input",
                "transcript": "",
                "voice_input": "placeholder",
                "microphone": "disabled",
                "stt": "disabled",
                "background_listening": "disabled",
                "audio_hardware_access": "disabled",
            },
            error_message="voice_input_unavailable",
            metadata={
                "safe": True,
                "source": "null_voice_input",
                "placeholder": True,
                "audio_hardware_accessed": self.audio_hardware_accessed,
            },
        )

    def get_status(self) -> VoiceServiceResult:
        return VoiceServiceResult(
            success=True,
            text="Voice input status: placeholder only. Microphone access is disabled.",
            data={
                "status": "placeholder",
                "source": "null_voice_input",
                "voice_input": "placeholder",
                "microphone": "disabled",
                "stt": "not_configured",
                "background_listening": "disabled",
                "audio_hardware_access": "disabled",
            },
            metadata={
                "safe": True,
                "source": "null_voice_input",
                "placeholder": True,
                "audio_hardware_accessed": self.audio_hardware_accessed,
            },
        )

    def get_capabilities(self) -> VoiceServiceResult:
        return VoiceServiceResult(
            success=True,
            text="Voice input capabilities: placeholder only.",
            data={
                "source": "null_voice_input",
                "voice_input": "placeholder",
                "supported_input_modes": [],
                "microphone": "disabled",
                "stt": "disabled",
                "wake_word": "disabled",
                "background_listening": "disabled",
                "audio_hardware_access": "disabled",
            },
            metadata={
                "safe": True,
                "source": "null_voice_input",
                "placeholder": True,
                "audio_hardware_accessed": self.audio_hardware_accessed,
            },
        )


class NullVoiceOutput(VoiceOutput):
    """Placeholder voice output that never touches speaker hardware."""

    def __init__(self):
        self.audio_hardware_accessed = False

    def speak(self, text: str) -> VoiceServiceResult:
        accepted_text = str(text or "")
        return VoiceServiceResult(
            success=True,
            text="Voice output accepted as placeholder. No speaker audio was played.",
            data={
                "source": "null_voice_output",
                "accepted_text": accepted_text,
                "voice_output": "placeholder",
                "speaker": "disabled",
                "tts": "disabled",
                "audio_hardware_access": "disabled",
            },
            metadata={
                "safe": True,
                "source": "null_voice_output",
                "placeholder": True,
                "audio_hardware_accessed": self.audio_hardware_accessed,
            },
        )

    def get_status(self) -> VoiceServiceResult:
        return VoiceServiceResult(
            success=True,
            text="Voice output status: placeholder only. Speaker access is disabled.",
            data={
                "status": "placeholder",
                "source": "null_voice_output",
                "voice_output": "placeholder",
                "speaker": "disabled",
                "tts": "not_configured",
                "audio_hardware_access": "disabled",
            },
            metadata={
                "safe": True,
                "source": "null_voice_output",
                "placeholder": True,
                "audio_hardware_accessed": self.audio_hardware_accessed,
            },
        )

    def get_capabilities(self) -> VoiceServiceResult:
        return VoiceServiceResult(
            success=True,
            text="Voice output capabilities: placeholder only.",
            data={
                "source": "null_voice_output",
                "voice_output": "placeholder",
                "supported_output_modes": [],
                "speaker": "disabled",
                "tts": "disabled",
                "audio_hardware_access": "disabled",
            },
            metadata={
                "safe": True,
                "source": "null_voice_output",
                "placeholder": True,
                "audio_hardware_accessed": self.audio_hardware_accessed,
            },
        )


class VoiceService:
    def get_status(self) -> VoiceServiceResult:
        raise NotImplementedError

    def get_capabilities(self) -> VoiceServiceResult:
        raise NotImplementedError

    def status(self) -> VoiceServiceResult:
        return self.get_status()


class PlaceholderVoiceService(VoiceService):
    """Voice City skeleton with no audio hardware access or background work."""

    def __init__(
        self,
        voice_input: VoiceInput | None = None,
        voice_output: VoiceOutput | None = None,
    ):
        self.voice_input = voice_input or NullVoiceInput()
        self.voice_output = voice_output or NullVoiceOutput()

    @property
    def audio_hardware_accessed(self) -> bool:
        return bool(
            getattr(self.voice_input, "audio_hardware_accessed", False)
            or getattr(self.voice_output, "audio_hardware_accessed", False)
        )

    def get_status(self) -> VoiceServiceResult:
        input_status = self.voice_input.get_status()
        output_status = self.voice_output.get_status()
        status = VoiceStatus(
            voice_input=dict(input_status.data),
            voice_output=dict(output_status.data),
        )
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
        input_capabilities = self.voice_input.get_capabilities()
        output_capabilities = self.voice_output.get_capabilities()
        capabilities = VoiceCapabilities(
            input_capabilities=dict(input_capabilities.data),
            output_capabilities=dict(output_capabilities.data),
        )
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
