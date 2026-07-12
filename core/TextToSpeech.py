from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from core.Contracts import TextToSpeechRequestV1, TextToSpeechResultV1


TTS_STATUS_READY = "ready"
TTS_STATUS_UNAVAILABLE = "unavailable"
TTS_STATUS_EMPTY_TEXT = "empty_text"
TTS_STATUS_TEXT_TOO_LONG = "text_too_long"


@dataclass(frozen=True)
class TextToSpeechSynthesisOptions:
    text: str
    language: str = "en_US"
    voice_id: str = ""
    voice_profile_id: str = ""
    speaking_rate: float = 1.0
    output_wav_path: str = ""
    timeout_seconds: float | None = None
    playback_enabled: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_request(self) -> TextToSpeechRequestV1:
        return TextToSpeechRequestV1(
            text=self.text,
            language=self.language,
            voice_id=self.voice_id,
            voice_profile_id=self.voice_profile_id,
            speaking_rate=self.speaking_rate,
            output_wav_path=self.output_wav_path,
            timeout_seconds=self.timeout_seconds,
            playback_enabled=self.playback_enabled,
            metadata=dict(self.metadata),
        )


class TextToSpeechAdapter:
    """Interface for local/offline text-to-speech engines."""

    def start(self) -> TextToSpeechResultV1:
        raise NotImplementedError

    def stop(self) -> TextToSpeechResultV1:
        raise NotImplementedError

    def synthesize(self, request: TextToSpeechRequestV1) -> TextToSpeechResultV1:
        raise NotImplementedError

    def execute(self, request: Any) -> TextToSpeechResultV1:
        payload = getattr(request, "payload", request)
        if isinstance(payload, TextToSpeechRequestV1):
            return self.synthesize(payload)
        if isinstance(payload, dict):
            return self.synthesize(TextToSpeechRequestV1(**payload))
        raise ValueError("Unsupported text-to-speech execution request")

    def health_check(self) -> TextToSpeechResultV1:
        raise NotImplementedError

    def get_status(self) -> TextToSpeechResultV1:
        raise NotImplementedError

    def get_capabilities(self) -> TextToSpeechResultV1:
        raise NotImplementedError


class MockTextToSpeechAdapter(TextToSpeechAdapter):
    """Deterministic TTS test double with no speaker or speech engine access."""

    def __init__(
        self,
        source: str = "mock_text_to_speech_adapter",
        voice_id: str = "mock_voice",
        fail: bool = False,
        failure_message: str = "mock_tts_failure",
    ):
        self.source = source
        self.voice_id = voice_id
        self.fail = fail
        self.failure_message = failure_message
        self.started = False
        self.synthesis_count = 0
        self.speech_engine_accessed = False
        self.audio_hardware_accessed = False

    def start(self) -> TextToSpeechResultV1:
        self.started = True
        return self._result(True, "started", "Mock TTS adapter started.")

    def stop(self) -> TextToSpeechResultV1:
        self.started = False
        return self._result(True, "stopped", "Mock TTS adapter stopped.")

    def synthesize(self, request: TextToSpeechRequestV1) -> TextToSpeechResultV1:
        self.synthesis_count += 1
        normalized = _normalize_tts_text(request.text)
        if self.fail:
            return self._result(
                False,
                "failed",
                "Mock TTS failed safely.",
                normalized_text=normalized,
                error_message=self.failure_message,
                request=request,
            )
        if not normalized:
            return self._result(
                False,
                TTS_STATUS_EMPTY_TEXT,
                "Text-to-speech rejected empty text.",
                error_message="empty_text",
                request=request,
            )
        return self._result(
            True,
            "synthesized",
            "Mock TTS synthesized text without audio output.",
            normalized_text=normalized,
            request=request,
            data={"playback_enabled": request.playback_enabled},
        )

    def health_check(self) -> TextToSpeechResultV1:
        if self.fail:
            return self._result(False, "failed", "Mock TTS health failed.", self.failure_message)
        return self._result(True, "healthy", "Mock TTS health check passed.")

    def get_status(self) -> TextToSpeechResultV1:
        return self._result(
            True,
            "started" if self.started else "stopped",
            "Mock TTS status discovered.",
            data={"started": self.started, "synthesis_count": self.synthesis_count},
        )

    def get_capabilities(self) -> TextToSpeechResultV1:
        return self._result(
            True,
            "capabilities",
            "Mock TTS capabilities discovered.",
            data={
                "supported_modes": ["mock_tts"],
                "playback": "disabled",
                "engine": "mock",
            },
        )

    def _result(
        self,
        success: bool,
        status: str,
        text: str,
        error_message: str = "",
        normalized_text: str = "",
        request: TextToSpeechRequestV1 | None = None,
        data: Dict[str, Any] | None = None,
    ) -> TextToSpeechResultV1:
        return TextToSpeechResultV1(
            success=success,
            status=status,
            normalized_text=normalized_text,
            engine="mock",
            voice_id=self.voice_id,
            playback_status="disabled",
            error_message=error_message,
            data={
                "message": text,
                "source": self.source,
                "request": request.to_dict() if request else None,
                **dict(data or {}),
            },
            metadata={
                "safe": True,
                "source": self.source,
                "mock": True,
                "speaker": "disabled",
                "speech_engine_accessed": self.speech_engine_accessed,
                "audio_hardware_accessed": self.audio_hardware_accessed,
            },
        )


def _normalize_tts_text(text: str) -> str:
    return " ".join(str(text or "").split())
