from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import isfinite
import re
from threading import RLock
from typing import Any, Deque, Dict, Mapping, Optional, Protocol, Sequence, runtime_checkable
import unicodedata

from core.Contracts import (
    StandbyListenResultV1,
    WakeDetectionResultV1,
    WakeListenerRequestV1,
    WakeListenerResultV1,
    WakeListenerSnapshotV1,
    new_correlation_id,
)


WAKE_LISTENER_STOPPED = "stopped"
WAKE_LISTENER_READY = "ready"
WAKE_LISTENER_LISTENING = "listening"
WAKE_LISTENER_CANCELLING = "cancelling"
WAKE_LISTENER_ERROR = "error"

WAKE_STATUS_NO_SPEECH = "no_speech"
WAKE_STATUS_NON_WAKE_SPEECH = "non_wake_speech"
WAKE_STATUS_DETECTED = "wake_detected"
WAKE_STATUS_CONTROL_DETECTED = "control_detected"
WAKE_STATUS_CANCELLED = "cancelled"
WAKE_STATUS_FAILED = "failed"

WAKE_CATEGORY_ACTIVATION = "activation"
WAKE_CATEGORY_STANDBY = "standby"
WAKE_CATEGORY_SHUTDOWN = "shutdown"
WAKE_CATEGORY_NON_WAKE = "non_wake"

DEFAULT_WAKE_PHRASES = ("ares", "hey ares", "hello ares", "wake up ares")
DEFAULT_WAKE_FILLER_PREFIXES = ("ok", "okay")
DEFAULT_WAKE_MICROPHONE_DEVICE = "plughw:2,0"
DEFAULT_WAKE_WHISPER_COMMAND = "external/whisper.cpp/build/bin/whisper-cli"
DEFAULT_WAKE_WHISPER_MODEL = "models/whisper/ggml-tiny.en.bin"

_PHRASE_COMPONENT = re.compile(r"[^a-z0-9]+")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class WakeListenerConfig:
    enabled: bool = True
    microphone_device: str = DEFAULT_WAKE_MICROPHONE_DEVICE
    whisper_command: str = DEFAULT_WAKE_WHISPER_COMMAND
    whisper_model: str = DEFAULT_WAKE_WHISPER_MODEL
    language: str = "en"
    wake_phrases: tuple[str, ...] = DEFAULT_WAKE_PHRASES
    filler_prefixes: tuple[str, ...] = DEFAULT_WAKE_FILLER_PREFIXES
    calibration_enabled: bool = True
    calibration_duration_seconds: float = 0.75
    speech_start_rms: float = 200.0
    speech_continue_rms: float = 160.0
    silence_rms: float = 120.0
    minimum_speech_start_rms: float = 200.0
    maximum_speech_start_rms: float = 1200.0
    minimum_speech_continue_rms: float = 140.0
    maximum_speech_continue_rms: float = 900.0
    minimum_silence_rms: float = 80.0
    maximum_silence_rms: float = 600.0
    required_speech_frames: int = 3
    required_continue_frames: int = 3
    required_silence_frames: int = 5
    speech_wait_timeout_seconds: float = 3.0
    maximum_utterance_seconds: float = 3.0
    silence_duration_seconds: float = 0.7
    pre_roll_seconds: float = 0.2
    frame_duration_ms: int = 20
    frame_read_timeout_seconds: float = 1.0
    playback_settle_delay_seconds: float = 0.35
    consecutive_failure_limit: int = 3
    retry_delay_seconds: float = 0.25
    retain_diagnostic_audio: bool = False
    diagnostic_output_directory: str = "data/runtime/wake_audio"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if not isinstance(self.calibration_enabled, bool):
            raise ValueError("calibration_enabled must be a boolean")
        if not isinstance(self.retain_diagnostic_audio, bool):
            raise ValueError("retain_diagnostic_audio must be a boolean")
        object.__setattr__(self, "microphone_device", _safe_path_text(self.microphone_device, "microphone_device"))
        object.__setattr__(self, "whisper_command", _safe_path_text(self.whisper_command, "whisper_command"))
        object.__setattr__(self, "whisper_model", _safe_path_text(self.whisper_model, "whisper_model"))
        object.__setattr__(
            self,
            "diagnostic_output_directory",
            _safe_path_text(self.diagnostic_output_directory, "diagnostic_output_directory"),
        )
        language = str(self.language or "").strip().lower()
        if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2})?", language):
            raise ValueError("language must be a bounded language or locale code")
        object.__setattr__(self, "language", language)
        object.__setattr__(self, "wake_phrases", _validated_phrases(self.wake_phrases, "wake_phrases"))
        object.__setattr__(
            self,
            "filler_prefixes",
            _validated_phrases(self.filler_prefixes, "filler_prefixes", allow_empty=True),
        )
        numeric_bounds = {
            "calibration_duration_seconds": (0.0, 3.0),
            "speech_start_rms": (1.0, 32767.0),
            "speech_continue_rms": (1.0, 32767.0),
            "silence_rms": (1.0, 32767.0),
            "minimum_speech_start_rms": (1.0, 32767.0),
            "maximum_speech_start_rms": (1.0, 32767.0),
            "minimum_speech_continue_rms": (1.0, 32767.0),
            "maximum_speech_continue_rms": (1.0, 32767.0),
            "minimum_silence_rms": (1.0, 32767.0),
            "maximum_silence_rms": (1.0, 32767.0),
            "speech_wait_timeout_seconds": (0.1, 10.0),
            "maximum_utterance_seconds": (0.25, 5.0),
            "silence_duration_seconds": (0.1, 2.0),
            "pre_roll_seconds": (0.0, 0.75),
            "frame_read_timeout_seconds": (0.05, 3.0),
            "playback_settle_delay_seconds": (0.0, 3.0),
            "retry_delay_seconds": (0.0, 5.0),
        }
        for name, (minimum, maximum) in numeric_bounds.items():
            object.__setattr__(self, name, _bounded_number(getattr(self, name), name, minimum, maximum))
        for name, minimum, maximum in (
            ("required_speech_frames", 1, 20),
            ("required_continue_frames", 1, 20),
            ("required_silence_frames", 1, 50),
            ("frame_duration_ms", 10, 40),
            ("consecutive_failure_limit", 1, 10),
        ):
            object.__setattr__(self, name, _bounded_integer(getattr(self, name), name, minimum, maximum))
        if not self.speech_start_rms > self.speech_continue_rms >= self.silence_rms:
            raise ValueError("RMS thresholds must satisfy speech_start > speech_continue >= silence")
        for minimum_name, maximum_name in (
            ("minimum_speech_start_rms", "maximum_speech_start_rms"),
            ("minimum_speech_continue_rms", "maximum_speech_continue_rms"),
            ("minimum_silence_rms", "maximum_silence_rms"),
        ):
            if getattr(self, minimum_name) > getattr(self, maximum_name):
                raise ValueError(f"{minimum_name} cannot exceed {maximum_name}")
        if self.calibration_enabled and self.calibration_duration_seconds <= 0:
            raise ValueError("calibration_duration_seconds must be positive when calibration is enabled")

    @classmethod
    def from_mapping(cls, value: Optional["WakeListenerConfig | Mapping[str, Any]"] = None) -> "WakeListenerConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("standby_wake_listener configuration must be a mapping")
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValueError(f"Unknown standby_wake_listener configuration fields: {', '.join(unknown)}")
        return cls(**dict(value))

    def to_dict(self) -> Dict[str, Any]:
        return {
            name: list(value) if isinstance(value, tuple) else value
            for name, value in self.__dict__.items()
        }


@runtime_checkable
class StandbyWakeListener(Protocol):
    config: WakeListenerConfig

    def start(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        ...

    def listen_once(self, request: WakeListenerRequestV1) -> StandbyListenResultV1:
        ...

    def cancel(self, reason: str = "cancelled") -> WakeListenerResultV1:
        ...

    def stop(self, reason: str = "stopped") -> WakeListenerResultV1:
        ...

    def snapshot(self, *, runtime_id: str = "") -> WakeListenerSnapshotV1:
        ...

    def health(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        ...


def normalize_wake_phrase(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    normalized = normalized.replace("’", "'").replace("`", "'")
    return _PHRASE_COMPONENT.sub(" ", normalized).strip()


def classify_wake_transcript(
    transcript: str,
    *,
    wake_phrases: Sequence[str] = DEFAULT_WAKE_PHRASES,
    filler_prefixes: Sequence[str] = DEFAULT_WAKE_FILLER_PREFIXES,
    standby_phrases: Sequence[str] = (),
    shutdown_phrases: Sequence[str] = (),
    correlation_id: str = "",
    runtime_id: str = "",
) -> WakeDetectionResultV1:
    normalized = normalize_wake_phrase(transcript)
    wake = set(_validated_phrases(wake_phrases, "wake_phrases"))
    standby = set(_validated_phrases(standby_phrases, "standby_phrases", allow_empty=True))
    shutdown = set(_validated_phrases(shutdown_phrases, "shutdown_phrases", allow_empty=True))
    fillers = set(_validated_phrases(filler_prefixes, "filler_prefixes", allow_empty=True))
    if wake & standby or wake & shutdown or standby & shutdown:
        raise ValueError("wake, standby, and shutdown phrases must not overlap")
    category = WAKE_CATEGORY_NON_WAKE
    matched = ""
    detected = False
    if normalized in shutdown:
        category, matched = WAKE_CATEGORY_SHUTDOWN, normalized
    elif normalized in standby:
        category, matched = WAKE_CATEGORY_STANDBY, normalized
    elif normalized in wake:
        category, matched, detected = WAKE_CATEGORY_ACTIVATION, normalized, True
    elif "ares" in wake:
        filler_matches = {f"{prefix} ares" for prefix in fillers}
        if normalized in filler_matches:
            category, matched, detected = WAKE_CATEGORY_ACTIVATION, "ares", True
    status = (
        WAKE_STATUS_DETECTED
        if detected
        else WAKE_STATUS_CONTROL_DETECTED
        if category in {WAKE_CATEGORY_STANDBY, WAKE_CATEGORY_SHUTDOWN}
        else WAKE_STATUS_NON_WAKE_SPEECH
    )
    return WakeDetectionResultV1(
        success=True,
        status=status,
        runtime_id=runtime_id,
        lifecycle_state="STANDBY",
        speech_detected=bool(normalized),
        wake_detected=detected,
        command_category=category,
        normalized_wake_phrase=matched if detected else "",
        matched_phrase=matched,
        transcript_length=len(str(transcript or "").strip()),
        correlation_id=correlation_id or new_correlation_id("wake-detect"),
        metadata={"safe": True, "contains_transcript": False},
    )


class QueuedStandbyWakeListener:
    """Deterministic foreground listener for runtime and CI verification."""

    def __init__(
        self,
        items: Optional[Sequence[Optional[str] | StandbyListenResultV1 | WakeDetectionResultV1]] = None,
        *,
        config: Optional[WakeListenerConfig | Mapping[str, Any]] = None,
    ) -> None:
        self.config = WakeListenerConfig.from_mapping(config)
        self._items: Deque[Optional[str] | StandbyListenResultV1 | WakeDetectionResultV1] = deque(items or ())
        self._lock = RLock()
        self._state = WAKE_LISTENER_STOPPED
        self._cancelled = False
        self._runtime_id = ""
        self._listen_count = 0
        self._speech_count = 0
        self._wake_count = 0
        self._failure_count = 0
        self._last_stop_reason = ""
        self.last_result: Optional[StandbyListenResultV1] = None

    def push(self, item: Optional[str] | StandbyListenResultV1 | WakeDetectionResultV1) -> None:
        with self._lock:
            self._items.append(item)

    def start(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        with self._lock:
            self._runtime_id = str(runtime_id or self._runtime_id)
            self._cancelled = False
            self._state = WAKE_LISTENER_READY
            return self._lifecycle(True, "started")

    def health(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        with self._lock:
            if runtime_id:
                self._runtime_id = str(runtime_id)
            return self._lifecycle(self._state != WAKE_LISTENER_ERROR, "healthy")

    def listen_once(self, request: WakeListenerRequestV1) -> StandbyListenResultV1:
        with self._lock:
            self._listen_count += 1
            if self._state == WAKE_LISTENER_STOPPED:
                return self._listen_failure(request, "listener_not_started", "wake listener is stopped")
            if self._cancelled:
                return StandbyListenResultV1(
                    success=False,
                    status=WAKE_STATUS_CANCELLED,
                    runtime_id=request.runtime_id,
                    lifecycle_state=request.lifecycle_state,
                    listener_state=WAKE_LISTENER_CANCELLING,
                    stop_reason="cancelled",
                    error_code="wake_listener_cancelled",
                    correlation_id=request.correlation_id,
                    metadata={"safe": True},
                )
            self._state = WAKE_LISTENER_LISTENING
            item = self._items.popleft() if self._items else None
        try:
            if isinstance(item, StandbyListenResultV1):
                result = item
            elif isinstance(item, WakeDetectionResultV1):
                result = _listen_result_from_detection(item, request)
            elif isinstance(item, str):
                detection = classify_wake_transcript(
                    item,
                    wake_phrases=request.wake_phrases or self.config.wake_phrases,
                    filler_prefixes=self.config.filler_prefixes,
                    standby_phrases=request.standby_phrases,
                    shutdown_phrases=request.shutdown_phrases,
                    correlation_id=request.correlation_id,
                    runtime_id=request.runtime_id,
                )
                result = _listen_result_from_detection(detection, request)
            elif item is None:
                result = StandbyListenResultV1(
                    success=True,
                    status=WAKE_STATUS_NO_SPEECH,
                    runtime_id=request.runtime_id,
                    lifecycle_state=request.lifecycle_state,
                    listener_state=WAKE_LISTENER_READY,
                    stop_reason="no_speech_timeout",
                    correlation_id=request.correlation_id,
                    metadata={"safe": True, "contains_transcript": False},
                )
            else:
                result = self._listen_failure(request, "malformed_queued_wake_result", "unsupported queued wake result")
            with self._lock:
                if result.speech_detected:
                    self._speech_count += 1
                if result.wake_detected:
                    self._wake_count += 1
                if not result.success:
                    self._failure_count += 1
                else:
                    self._failure_count = 0
                self.last_result = result
            return result
        finally:
            with self._lock:
                if self._state != WAKE_LISTENER_STOPPED:
                    self._state = WAKE_LISTENER_READY

    def cancel(self, reason: str = "cancelled") -> WakeListenerResultV1:
        with self._lock:
            self._cancelled = True
            self._state = WAKE_LISTENER_CANCELLING
            self._last_stop_reason = str(reason or "cancelled")[:80]
            return self._lifecycle(True, "cancelled")

    def stop(self, reason: str = "stopped") -> WakeListenerResultV1:
        with self._lock:
            self._cancelled = True
            self._state = WAKE_LISTENER_STOPPED
            self._last_stop_reason = str(reason or "stopped")[:80]
            return self._lifecycle(True, "stopped")

    close = stop

    def snapshot(self, *, runtime_id: str = "") -> WakeListenerSnapshotV1:
        with self._lock:
            return WakeListenerSnapshotV1(
                runtime_id=runtime_id or self._runtime_id,
                listener_state=self._state,
                started=self._state != WAKE_LISTENER_STOPPED,
                listening=self._state == WAKE_LISTENER_LISTENING,
                cancelled=self._cancelled,
                listen_count=self._listen_count,
                speech_candidate_count=self._speech_count,
                wake_detection_count=self._wake_count,
                consecutive_failure_count=self._failure_count,
                last_stop_reason=self._last_stop_reason,
                metadata={"safe": True},
            )

    def _lifecycle(self, success: bool, status: str) -> WakeListenerResultV1:
        return WakeListenerResultV1(
            success=success,
            status=status,
            runtime_id=self._runtime_id,
            listener_state=self._state,
            error_code="" if success else status,
            correlation_id=new_correlation_id("wake-listener"),
            metadata={"safe": True},
        )

    def _listen_failure(self, request: WakeListenerRequestV1, code: str, message: str) -> StandbyListenResultV1:
        return StandbyListenResultV1(
            success=False,
            status=WAKE_STATUS_FAILED,
            runtime_id=request.runtime_id,
            lifecycle_state=request.lifecycle_state,
            listener_state=self._state,
            error_code=code,
            error_message=message[:160],
            correlation_id=request.correlation_id,
            metadata={"safe": True},
        )


def _listen_result_from_detection(
    detection: WakeDetectionResultV1,
    request: WakeListenerRequestV1,
) -> StandbyListenResultV1:
    return StandbyListenResultV1(
        success=detection.success,
        status=detection.status,
        runtime_id=request.runtime_id,
        lifecycle_state=request.lifecycle_state,
        listener_state=WAKE_LISTENER_READY,
        speech_detected=detection.speech_detected,
        wake_detected=detection.wake_detected,
        command_category=detection.command_category,
        normalized_wake_phrase=detection.normalized_wake_phrase,
        matched_phrase=detection.matched_phrase,
        stop_reason=detection.status,
        error_code=detection.error_code,
        error_message=detection.error_message,
        correlation_id=request.correlation_id,
        metadata={"safe": True, "contains_transcript": False},
    )


def validate_wake_control_phrases(
    wake_phrases: Sequence[str],
    standby_phrases: Sequence[str],
    shutdown_phrases: Sequence[str],
) -> None:
    wake = set(_validated_phrases(wake_phrases, "wake_phrases"))
    standby = set(_validated_phrases(standby_phrases, "standby_phrases"))
    shutdown = set(_validated_phrases(shutdown_phrases, "shutdown_phrases"))
    if wake & standby:
        raise ValueError("wake and standby phrases must not overlap")
    if wake & shutdown:
        raise ValueError("wake and shutdown phrases must not overlap")
    if standby & shutdown:
        raise ValueError("standby and shutdown phrases must not overlap")


def _validated_phrases(
    values: Sequence[str],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of phrases")
    normalized = tuple(normalize_wake_phrase(value) for value in values)
    if any(not value or len(value) > 64 for value in normalized):
        raise ValueError(f"{field_name} contains an empty or oversized phrase")
    if len(normalized) > 16:
        raise ValueError(f"{field_name} may contain at most 16 phrases")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} contains duplicates after normalization")
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} must contain at least one phrase")
    return normalized


def _safe_path_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 1024 or _CONTROL_CHARACTER.search(text):
        raise ValueError(f"{field_name} must be a non-empty safe path or command")
    return text


def _bounded_number(value: Any, field_name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ValueError(f"{field_name} must be a finite number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return number


def _bounded_integer(value: Any, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be an integer between {minimum} and {maximum}")
    return value
