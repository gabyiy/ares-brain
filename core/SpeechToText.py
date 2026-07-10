from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

from core.Microphone import AudioChunk


@dataclass(frozen=True)
class TranscriptionResult:
    success: bool
    status: str
    text: str = ""
    confidence: float = 0.0
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", str(self.text or "").strip())
        confidence = max(0.0, min(1.0, float(self.confidence)))
        object.__setattr__(self, "confidence", confidence)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "text": self.text,
            "confidence": self.confidence,
            "error_message": self.error_message,
            "data": dict(self.data),
            "metadata": dict(self.metadata),
        }


class SpeechToTextAdapter:
    """Interface for converting audio chunks into text."""

    def transcribe(self, audio_chunk: AudioChunk) -> TranscriptionResult:
        raise NotImplementedError

    def get_status(self) -> TranscriptionResult:
        raise NotImplementedError

    def get_capabilities(self) -> TranscriptionResult:
        raise NotImplementedError


class MockSpeechToTextAdapter(SpeechToTextAdapter):
    """Deterministic STT test double with no speech engine or hardware access."""

    def __init__(
        self,
        transcripts: Optional[Iterable[str]] = None,
        confidence: float = 1.0,
        source: str = "mock_speech_to_text_adapter",
        fail: bool = False,
        failure_message: str = "mock_stt_failure",
        low_confidence_threshold: float = 0.5,
    ):
        self._transcripts = [str(transcript or "") for transcript in (transcripts or [])]
        self.confidence = confidence
        self.source = source
        self.fail = fail
        self.failure_message = failure_message
        self.low_confidence_threshold = low_confidence_threshold
        self.transcription_count = 0
        self.audio_hardware_accessed = False
        self.speech_engine_accessed = False

    def transcribe(self, audio_chunk: AudioChunk) -> TranscriptionResult:
        self.transcription_count += 1
        if self.fail:
            return self._failure(
                status="failed",
                text="Mock speech-to-text failed safely.",
                error_message=self.failure_message,
                audio_chunk=audio_chunk,
            )
        if audio_chunk.byte_count == 0:
            return self._success(
                status="empty_audio",
                text="",
                confidence=0.0,
                audio_chunk=audio_chunk,
                message="Mock speech-to-text received empty audio.",
            )

        transcript = self._transcripts.pop(0) if self._transcripts else ""
        confidence = max(0.0, min(1.0, float(self.confidence)))
        if not transcript:
            return self._success(
                status="no_transcription",
                text="",
                confidence=confidence,
                audio_chunk=audio_chunk,
                message="Mock speech-to-text produced no transcription.",
            )
        if confidence < self.low_confidence_threshold:
            return self._success(
                status="low_confidence",
                text=transcript,
                confidence=confidence,
                audio_chunk=audio_chunk,
                message="Mock speech-to-text produced a low-confidence transcription.",
            )
        return self._success(
            status="transcribed",
            text=transcript,
            confidence=confidence,
            audio_chunk=audio_chunk,
            message="Mock speech-to-text transcribed audio.",
        )

    def get_status(self) -> TranscriptionResult:
        return self._success(
            status="mock",
            text="",
            confidence=1.0,
            audio_chunk=None,
            message="Mock speech-to-text status discovered.",
            extra_data={
                "queued_transcripts": len(self._transcripts),
                "transcription_count": self.transcription_count,
                "stt": "mock",
            },
        )

    def get_capabilities(self) -> TranscriptionResult:
        return self._success(
            status="capabilities",
            text="",
            confidence=1.0,
            audio_chunk=None,
            message="Mock speech-to-text capabilities discovered.",
            extra_data={
                "supported_input": "AudioChunk",
                "supported_modes": ["mock_transcription"],
                "confidence": "supported",
                "empty_audio_handling": "safe_empty_result",
                "speech_engine": "disabled",
            },
        )

    def _success(
        self,
        status: str,
        text: str,
        confidence: float,
        audio_chunk: Optional[AudioChunk],
        message: str,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> TranscriptionResult:
        return TranscriptionResult(
            success=True,
            status=status,
            text=text,
            confidence=confidence,
            data={
                **self._base_data(audio_chunk),
                "message": message,
                **dict(extra_data or {}),
            },
            metadata=self._metadata(),
        )

    def _failure(
        self,
        status: str,
        text: str,
        error_message: str,
        audio_chunk: AudioChunk,
    ) -> TranscriptionResult:
        return TranscriptionResult(
            success=False,
            status=status,
            text="",
            confidence=0.0,
            error_message=error_message,
            data=self._base_data(audio_chunk),
            metadata=self._metadata(),
        )

    def _base_data(self, audio_chunk: Optional[AudioChunk]) -> Dict[str, Any]:
        return {
            "source": self.source,
            "audio_chunk": audio_chunk.to_dict() if audio_chunk else None,
            "audio_hardware_access": "disabled",
            "speech_engine_access": "disabled",
            "internet": "disabled",
        }

    def _metadata(self) -> Dict[str, Any]:
        return {
            "safe": True,
            "source": self.source,
            "mock": True,
            "audio_hardware_accessed": self.audio_hardware_accessed,
            "speech_engine_accessed": self.speech_engine_accessed,
        }
