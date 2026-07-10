from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class VoiceServiceResult:
    success: bool
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class VoiceInputAdapter:
    """Adapter boundary for future speech-to-text input providers."""

    def capture_input(self) -> VoiceServiceResult:
        raise NotImplementedError

    def get_status(self) -> VoiceServiceResult:
        raise NotImplementedError

    def get_capabilities(self) -> VoiceServiceResult:
        raise NotImplementedError


class VoiceOutputAdapter:
    """Adapter boundary for future text-to-speech output providers."""

    def speak(self, text: str) -> VoiceServiceResult:
        raise NotImplementedError

    def get_status(self) -> VoiceServiceResult:
        raise NotImplementedError

    def get_capabilities(self) -> VoiceServiceResult:
        raise NotImplementedError


class MockVoiceInputAdapter(VoiceInputAdapter):
    """Safe test/local input adapter that never touches microphone hardware."""

    def __init__(
        self,
        transcripts: Optional[Iterable[str]] = None,
        fail: bool = False,
        failure_message: str = "mock_input_failure",
        source: str = "mock_voice_input_adapter",
        voice_input: str = "mock",
        available: bool = True,
        placeholder: bool = False,
    ):
        self._transcripts = [str(transcript or "") for transcript in (transcripts or [])]
        self.fail = fail
        self.failure_message = failure_message
        self.source = source
        self.voice_input = voice_input
        self.available = available
        self.placeholder = placeholder
        self.capture_count = 0
        self.audio_hardware_accessed = False

    def capture_input(self) -> VoiceServiceResult:
        self.capture_count += 1
        if self.fail:
            return VoiceServiceResult(
                success=False,
                text="Mock voice input failed safely. No microphone was accessed.",
                data=self._base_data(transcript=""),
                error_message=self.failure_message,
                metadata=self._metadata(),
            )

        if not self.available:
            return VoiceServiceResult(
                success=False,
                text="Voice input is a placeholder. No microphone was accessed.",
                data=self._base_data(transcript="", voice_input="placeholder"),
                error_message="voice_input_unavailable",
                metadata=self._metadata(placeholder=True),
            )

        transcript = self._transcripts.pop(0) if self._transcripts else ""
        return VoiceServiceResult(
            success=True,
            text=(
                "Mock voice input captured text."
                if transcript
                else "Mock voice input captured no text."
            ),
            data=self._base_data(transcript=transcript),
            metadata=self._metadata(),
        )

    def get_status(self) -> VoiceServiceResult:
        if not self.available:
            return VoiceServiceResult(
                success=True,
                text="Voice input status: placeholder only. Microphone access is disabled.",
                data={
                    "status": "placeholder",
                    "source": self.source,
                    "voice_input": "placeholder",
                    "microphone": "disabled",
                    "stt": "not_configured",
                    "background_listening": "disabled",
                    "audio_hardware_access": "disabled",
                },
                metadata=self._metadata(placeholder=True),
            )
        return VoiceServiceResult(
            success=True,
            text="Mock voice input status: ready without microphone access.",
            data={
                "status": "mock",
                "source": self.source,
                "voice_input": self.voice_input,
                "queued_inputs": len(self._transcripts),
                "microphone": "disabled",
                "stt": "mock",
                "background_listening": "disabled",
                "audio_hardware_access": "disabled",
            },
            metadata=self._metadata(),
        )

    def get_capabilities(self) -> VoiceServiceResult:
        if not self.available:
            return VoiceServiceResult(
                success=True,
                text="Voice input capabilities: placeholder only.",
                data={
                    "source": self.source,
                    "voice_input": "placeholder",
                    "supported_input_modes": [],
                    "microphone": "disabled",
                    "stt": "disabled",
                    "wake_word": "disabled",
                    "background_listening": "disabled",
                    "audio_hardware_access": "disabled",
                },
                metadata=self._metadata(placeholder=True),
            )
        return VoiceServiceResult(
            success=True,
            text="Mock voice input capabilities discovered.",
            data={
                "source": self.source,
                "voice_input": self.voice_input,
                "supported_input_modes": ["mock_text"],
                "microphone": "disabled",
                "stt": "mock",
                "wake_word": "disabled",
                "background_listening": "disabled",
                "audio_hardware_access": "disabled",
            },
            metadata=self._metadata(),
        )

    def _base_data(self, transcript: str, voice_input: Optional[str] = None) -> Dict[str, Any]:
        return {
            "source": self.source,
            "transcript": transcript,
            "voice_input": voice_input or self.voice_input,
            "microphone": "disabled",
            "stt": "disabled" if not self.available else "mock",
            "wake_word": "disabled",
            "background_listening": "disabled",
            "audio_hardware_access": "disabled",
        }

    def _metadata(self, placeholder: Optional[bool] = None) -> Dict[str, Any]:
        return {
            "safe": True,
            "source": self.source,
            "mock": True,
            "placeholder": self.placeholder if placeholder is None else placeholder,
            "audio_hardware_accessed": self.audio_hardware_accessed,
        }


class MockVoiceOutputAdapter(VoiceOutputAdapter):
    """Safe test/local output adapter that records text without playing audio."""

    def __init__(
        self,
        fail: bool = False,
        failure_message: str = "mock_output_failure",
        source: str = "mock_voice_output_adapter",
        voice_output: str = "mock",
        available: bool = True,
        placeholder: bool = False,
    ):
        self.fail = fail
        self.failure_message = failure_message
        self.source = source
        self.voice_output = voice_output
        self.available = available
        self.placeholder = placeholder
        self.spoken_texts: List[str] = []
        self.audio_hardware_accessed = False

    def speak(self, text: str) -> VoiceServiceResult:
        accepted_text = str(text or "")
        self.spoken_texts.append(accepted_text)
        if self.fail:
            return VoiceServiceResult(
                success=False,
                text="Mock voice output failed safely. No speaker audio was played.",
                data=self._base_data(accepted_text),
                error_message=self.failure_message,
                metadata=self._metadata(),
            )

        if not self.available:
            return VoiceServiceResult(
                success=True,
                text="Voice output accepted as placeholder. No speaker audio was played.",
                data=self._base_data(accepted_text, voice_output="placeholder"),
                metadata=self._metadata(placeholder=True),
            )

        return VoiceServiceResult(
            success=True,
            text="Mock voice output accepted text. No speaker audio was played.",
            data=self._base_data(accepted_text),
            metadata=self._metadata(),
        )

    def get_status(self) -> VoiceServiceResult:
        if not self.available:
            return VoiceServiceResult(
                success=True,
                text="Voice output status: placeholder only. Speaker access is disabled.",
                data={
                    "status": "placeholder",
                    "source": self.source,
                    "voice_output": "placeholder",
                    "speaker": "disabled",
                    "tts": "not_configured",
                    "audio_hardware_access": "disabled",
                },
                metadata=self._metadata(placeholder=True),
            )
        return VoiceServiceResult(
            success=True,
            text="Mock voice output status: ready without speaker access.",
            data={
                "status": "mock",
                "source": self.source,
                "voice_output": self.voice_output,
                "spoken_count": len(self.spoken_texts),
                "speaker": "disabled",
                "tts": "mock",
                "audio_hardware_access": "disabled",
            },
            metadata=self._metadata(),
        )

    def get_capabilities(self) -> VoiceServiceResult:
        if not self.available:
            return VoiceServiceResult(
                success=True,
                text="Voice output capabilities: placeholder only.",
                data={
                    "source": self.source,
                    "voice_output": "placeholder",
                    "supported_output_modes": [],
                    "speaker": "disabled",
                    "tts": "disabled",
                    "audio_hardware_access": "disabled",
                },
                metadata=self._metadata(placeholder=True),
            )
        return VoiceServiceResult(
            success=True,
            text="Mock voice output capabilities discovered.",
            data={
                "source": self.source,
                "voice_output": self.voice_output,
                "supported_output_modes": ["mock_text"],
                "speaker": "disabled",
                "tts": "mock",
                "audio_hardware_access": "disabled",
            },
            metadata=self._metadata(),
        )

    def _base_data(
        self,
        accepted_text: str,
        voice_output: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "source": self.source,
            "accepted_text": accepted_text,
            "voice_output": voice_output or self.voice_output,
            "speaker": "disabled",
            "tts": "disabled" if not self.available else "mock",
            "audio_hardware_access": "disabled",
        }

    def _metadata(self, placeholder: Optional[bool] = None) -> Dict[str, Any]:
        return {
            "safe": True,
            "source": self.source,
            "mock": True,
            "placeholder": self.placeholder if placeholder is None else placeholder,
            "audio_hardware_accessed": self.audio_hardware_accessed,
        }


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

    def __init__(self, adapter: Optional[VoiceInputAdapter] = None):
        self.adapter = adapter or MockVoiceInputAdapter(
            source="null_voice_input",
            voice_input="placeholder",
            available=False,
            placeholder=True,
        )

    def listen_once(self) -> VoiceServiceResult:
        return self.adapter.capture_input()

    def get_status(self) -> VoiceServiceResult:
        return self.adapter.get_status()

    def get_capabilities(self) -> VoiceServiceResult:
        return self.adapter.get_capabilities()

    @property
    def audio_hardware_accessed(self) -> bool:
        return bool(getattr(self.adapter, "audio_hardware_accessed", False))


class NullVoiceOutput(VoiceOutput):
    """Placeholder voice output that never touches speaker hardware."""

    def __init__(self, adapter: Optional[VoiceOutputAdapter] = None):
        self.adapter = adapter or MockVoiceOutputAdapter(
            source="null_voice_output",
            voice_output="placeholder",
            available=False,
            placeholder=True,
        )

    def speak(self, text: str) -> VoiceServiceResult:
        return self.adapter.speak(text)

    def get_status(self) -> VoiceServiceResult:
        return self.adapter.get_status()

    def get_capabilities(self) -> VoiceServiceResult:
        return self.adapter.get_capabilities()

    @property
    def audio_hardware_accessed(self) -> bool:
        return bool(getattr(self.adapter, "audio_hardware_accessed", False))


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
        input_adapter: VoiceInputAdapter | None = None,
        output_adapter: VoiceOutputAdapter | None = None,
    ):
        self.voice_input = voice_input or NullVoiceInput(adapter=input_adapter)
        self.voice_output = voice_output or NullVoiceOutput(adapter=output_adapter)

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
