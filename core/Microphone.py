from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional

from core.Contracts import (
    CONTRACT_MICROPHONE_CAPTURE_RESULT,
    CONTRACT_VERSION_V1,
    utc_contract_timestamp,
)


CancelCheck = Callable[[], bool]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AudioChunk:
    """Raw audio data boundary for future microphone implementations."""

    data: bytes
    sample_rate_hz: int = 16000
    channels: int = 1
    sample_width_bytes: int = 2
    timestamp: str = field(default_factory=_utc_timestamp)
    sequence_number: int = 0
    source: str = "mock_microphone_adapter"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", bytes(self.data or b""))
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.channels <= 0:
            raise ValueError("channels must be positive")
        if self.sample_width_bytes <= 0:
            raise ValueError("sample_width_bytes must be positive")

    @property
    def byte_count(self) -> int:
        return len(self.data)

    @property
    def duration_seconds(self) -> float:
        bytes_per_second = self.sample_rate_hz * self.channels * self.sample_width_bytes
        return self.byte_count / bytes_per_second

    def to_dict(self, include_bytes: bool = False) -> Dict[str, Any]:
        payload = {
            "byte_count": self.byte_count,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "sample_width_bytes": self.sample_width_bytes,
            "duration_seconds": self.duration_seconds,
            "timestamp": self.timestamp,
            "sequence_number": self.sequence_number,
            "source": self.source,
            "metadata": dict(self.metadata),
        }
        if include_bytes:
            payload["data"] = self.data
        return payload


@dataclass(frozen=True)
class MicrophoneResult:
    success: bool
    status: str
    text: str = ""
    chunk: Optional[AudioChunk] = None
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    contract_name: str = CONTRACT_MICROPHONE_CAPTURE_RESULT
    contract_version: str = CONTRACT_VERSION_V1
    correlation_id: str = ""
    session_id: str = ""
    created_at: str = field(default_factory=utc_contract_timestamp)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "success": self.success,
            "status": self.status,
            "text": self.text,
            "chunk": self.chunk.to_dict() if self.chunk else None,
            "error_message": self.error_message,
            "data": dict(self.data),
            "metadata": dict(self.metadata),
        }


class MicrophoneAdapter:
    """Interface for microphone providers, independent of STT engines."""

    def start(self) -> MicrophoneResult:
        raise NotImplementedError

    def stop(self) -> MicrophoneResult:
        raise NotImplementedError

    def read_chunk(
        self,
        timeout_seconds: Optional[float] = None,
        cancel_requested: Optional[CancelCheck | Any] = None,
    ) -> MicrophoneResult:
        raise NotImplementedError

    def get_status(self) -> MicrophoneResult:
        raise NotImplementedError

    def get_capabilities(self) -> MicrophoneResult:
        raise NotImplementedError


class MockMicrophoneAdapter(MicrophoneAdapter):
    """Deterministic microphone test double with no hardware access."""

    def __init__(
        self,
        chunks: Optional[Iterable[AudioChunk | bytes | bytearray]] = None,
        source: str = "mock_microphone_adapter",
        sample_rate_hz: int = 16000,
        channels: int = 1,
        sample_width_bytes: int = 2,
        available: bool = True,
        fail_start: bool = False,
        fail_stop: bool = False,
        fail_read: bool = False,
        failure_message: str = "mock_microphone_failure",
    ):
        self.source = source
        self.sample_rate_hz = sample_rate_hz
        self.channels = channels
        self.sample_width_bytes = sample_width_bytes
        self.available = available
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.fail_read = fail_read
        self.failure_message = failure_message
        self.started = False
        self.start_count = 0
        self.stop_count = 0
        self.read_count = 0
        self.audio_hardware_accessed = False
        self._chunks = [
            self._normalize_chunk(chunk, index + 1)
            for index, chunk in enumerate(chunks or [])
        ]

    def start(self) -> MicrophoneResult:
        self.start_count += 1
        if self.fail_start:
            return self._failure(
                status="start_failed",
                text="Mock microphone failed to start safely. No hardware was accessed.",
                error_message=self.failure_message,
            )
        if not self.available:
            return self._failure(
                status="unavailable",
                text="Mock microphone is unavailable. No hardware was accessed.",
                error_message="microphone_unavailable",
            )
        self.started = True
        return self._success(
            status="started",
            text="Mock microphone started. No hardware was accessed.",
        )

    def stop(self) -> MicrophoneResult:
        self.stop_count += 1
        if self.fail_stop:
            return self._failure(
                status="stop_failed",
                text="Mock microphone failed to stop safely. No hardware was accessed.",
                error_message=self.failure_message,
            )
        self.started = False
        return self._success(
            status="stopped",
            text="Mock microphone stopped. No hardware was accessed.",
        )

    def read_chunk(
        self,
        timeout_seconds: Optional[float] = None,
        cancel_requested: Optional[CancelCheck | Any] = None,
    ) -> MicrophoneResult:
        self.read_count += 1
        if _is_cancelled(cancel_requested):
            return self._failure(
                status="cancelled",
                text="Mock microphone read was cancelled safely.",
                error_message="microphone_read_cancelled",
                data={"timeout_seconds": timeout_seconds},
            )
        if self.fail_read:
            return self._failure(
                status="read_failed",
                text="Mock microphone read failed safely. No hardware was accessed.",
                error_message=self.failure_message,
                data={"timeout_seconds": timeout_seconds},
            )
        if not self.available:
            return self._failure(
                status="unavailable",
                text="Mock microphone is unavailable. No hardware was accessed.",
                error_message="microphone_unavailable",
                data={"timeout_seconds": timeout_seconds},
            )
        if not self.started:
            return self._failure(
                status="not_started",
                text="Mock microphone must be started before reading audio.",
                error_message="microphone_not_started",
                data={"timeout_seconds": timeout_seconds},
            )
        if not self._chunks:
            if timeout_seconds is not None:
                return self._failure(
                    status="timeout",
                    text=f"Mock microphone read timed out after {timeout_seconds} seconds.",
                    error_message="microphone_read_timeout",
                    data={"timeout_seconds": timeout_seconds},
                )
            return self._success(
                status="no_audio",
                text="Mock microphone has no queued audio chunk.",
                data={"timeout_seconds": timeout_seconds},
            )

        chunk = self._chunks.pop(0)
        return self._success(
            status="chunk",
            text="Mock microphone returned audio chunk.",
            chunk=chunk,
            data={
                "timeout_seconds": timeout_seconds,
                "remaining_chunks": len(self._chunks),
                "chunk": chunk.to_dict(),
            },
        )

    def get_status(self) -> MicrophoneResult:
        status = "started" if self.started else "stopped"
        if not self.available:
            status = "unavailable"
        return self._success(
            status=status,
            text="Mock microphone status discovered. No hardware was accessed.",
            data={
                "source": self.source,
                "status": status,
                "queued_chunks": len(self._chunks),
                "start_count": self.start_count,
                "stop_count": self.stop_count,
                "read_count": self.read_count,
                "microphone": "mock" if self.available else "disabled",
                "audio_hardware_access": "disabled",
            },
        )

    def get_capabilities(self) -> MicrophoneResult:
        return self._success(
            status="capabilities",
            text="Mock microphone capabilities discovered.",
            data={
                "source": self.source,
                "microphone": "mock" if self.available else "disabled",
                "supported_modes": ["mock_audio_chunks"] if self.available else [],
                "sample_rate_hz": self.sample_rate_hz,
                "channels": self.channels,
                "sample_width_bytes": self.sample_width_bytes,
                "timeout_handling": "safe_timeout_result",
                "cancellation": "supported",
                "audio_hardware_access": "disabled",
            },
        )

    def health_check(self) -> MicrophoneResult:
        if not self.available:
            return self._failure(
                status="unavailable",
                text="Mock microphone health check reports unavailable. No hardware was accessed.",
                error_message="microphone_unavailable",
            )
        if self.fail_start:
            return self._failure(
                status="failed",
                text="Mock microphone health check reports start failure.",
                error_message=self.failure_message,
            )
        return self._success(
            status="healthy",
            text="Mock microphone health check passed. No hardware was accessed.",
            data={
                "queued_chunks": len(self._chunks),
                "microphone": "mock",
                "audio_hardware_access": "disabled",
            },
        )

    def _normalize_chunk(
        self,
        chunk: AudioChunk | bytes | bytearray,
        sequence_number: int,
    ) -> AudioChunk:
        if isinstance(chunk, AudioChunk):
            return chunk
        return AudioChunk(
            data=bytes(chunk or b""),
            sample_rate_hz=self.sample_rate_hz,
            channels=self.channels,
            sample_width_bytes=self.sample_width_bytes,
            sequence_number=sequence_number,
            source=self.source,
        )

    def _success(
        self,
        status: str,
        text: str,
        chunk: Optional[AudioChunk] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> MicrophoneResult:
        return MicrophoneResult(
            success=True,
            status=status,
            text=text,
            chunk=chunk,
            data={**self._base_data(), **dict(data or {})},
            metadata=self._metadata(),
        )

    def _failure(
        self,
        status: str,
        text: str,
        error_message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> MicrophoneResult:
        return MicrophoneResult(
            success=False,
            status=status,
            text=text,
            error_message=error_message,
            data={**self._base_data(), **dict(data or {})},
            metadata=self._metadata(),
        )

    def _base_data(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "started": self.started,
            "available": self.available,
            "audio_hardware_access": "disabled",
        }

    def _metadata(self) -> Dict[str, Any]:
        return {
            "safe": True,
            "source": self.source,
            "mock": True,
            "audio_hardware_accessed": self.audio_hardware_accessed,
        }


def _is_cancelled(cancel_requested: Optional[CancelCheck | Any]) -> bool:
    if cancel_requested is None:
        return False
    is_set = getattr(cancel_requested, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(cancel_requested):
        return bool(cancel_requested())
    return bool(cancel_requested)
