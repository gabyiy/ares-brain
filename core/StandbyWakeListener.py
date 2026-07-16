from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import isfinite
import re
from threading import RLock
from typing import Any, Deque, Dict, Mapping, Optional, Protocol, Sequence, runtime_checkable

from core.AresIdentity import (
    DEFAULT_ARES_NAME_ALIASES,
    clean_spoken_phrase,
    expand_ares_alias_phrases,
    normalize_spoken_phrase,
    validate_ares_name_aliases,
)

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

DEFAULT_WAKE_PHRASE_ALIASES = DEFAULT_ARES_NAME_ALIASES
DEFAULT_WAKE_PHRASE_PREFIXES = ("", "hey", "hello", "wake up", "okay")
DEFAULT_WAKE_FILLER_PREFIXES: tuple[str, ...] = ()
DEFAULT_WAKE_PHRASES = tuple(
    " ".join(part for part in (prefix, alias) if part)
    for prefix in DEFAULT_WAKE_PHRASE_PREFIXES
    for alias in DEFAULT_WAKE_PHRASE_ALIASES
)
DEFAULT_WAKE_MICROPHONE_DEVICE = "plughw:2,0"
DEFAULT_WAKE_VOSK_MODEL = "models/vosk/vosk-model-small-en-us-0.15"
DEFAULT_WAKE_MINIMUM_CONFIDENCE = 0.55
DEFAULT_WAKE_MEDIUM_CONFIDENCE = 0.40

_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_PREFIX_PATTERN = re.compile(r"^[a-z0-9]+(?: [a-z0-9]+){0,2}$")


@dataclass(frozen=True)
class WakeLocalDiagnostics:
    """Ephemeral owner-terminal diagnostics; never included in runtime events."""

    raw_transcript: str = ""
    attempt_id: str = ""
    candidate_id: str = ""
    stream_generation: int = 0
    capture_valid: bool = False
    recognizer_invoked: bool = False
    infrastructure_failure: bool = False
    cleaned_transcript: str = ""
    normalized_transcript: str = ""
    selected_alias: str = ""
    selected_wake_phrase: str = ""
    canonical_wake_phrase: str = ""
    classification_path: str = ""
    classification_reason: str = ""
    collapsed_wake_representation: str = ""
    wake_vocabulary_only: bool = False
    wake_token_count: int = 0
    alias_repetition_count: int = 0
    maximum_prefix_repetition_count: int = 0
    classification: str = "rejected"
    rejection_reason: str = ""
    capture_duration_seconds: float = 0.0
    raw_capture_duration_seconds: float = 0.0
    assembled_duration_seconds: float = 0.0
    normalized_duration_seconds: float = 0.0
    whisper_input_duration_seconds: float = 0.0
    capture_stop_reason: str = ""
    whisper_status: str = ""
    whisper_exit_code: Optional[int] = None
    whisper_processing_time_seconds: float = 0.0
    wake_model_path: str = ""
    lifecycle_state: str = "STANDBY"
    retained_audio_path: str = ""
    cleanup_status: str = "not_required"
    recognizer_name: str = ""
    raw_recognition_result: str = ""
    recognition_status: str = ""
    recognition_confidence: Optional[float] = None
    recognition_confidence_available: bool = False
    minimum_word_confidence: Optional[float] = None
    mean_word_confidence: Optional[float] = None
    canonical_confidence: Optional[float] = None
    duplicate_collapse_used: bool = False
    recognition_processing_time_seconds: float = 0.0
    recognizer_model_path: str = ""
    stream_open_count: int = 0
    stream_close_count: int = 0
    calibration_count: int = 0
    candidate_number: int = 0
    pre_roll_frames_retained: int = 0
    expected_pre_roll_frames: int = 0
    beginning_clipped: bool = False
    first_speech_frame: int = 0
    terminal_silence_duration_seconds: float = 0.0
    terminal_quiet_frame_count: int = 0
    speech_frame_count: int = 0
    post_roll_frame_count: int = 0
    duplicate_pcm_frame_count: int = 0
    stale_pcm_frames_discarded: int = 0
    ambient_noise_floor: float = 0.0
    speech_start_threshold: float = 0.0
    speech_continue_threshold: float = 0.0
    speech_end_threshold: float = 0.0
    rms_trace: tuple[Dict[str, Any], ...] = ()
    trimmed_duration_seconds: float = 0.0
    leading_trimmed_seconds: float = 0.0
    trailing_trimmed_seconds: float = 0.0
    vad_transitions: tuple[Dict[str, Any], ...] = ()
    speech_to_activation_seconds: float = 0.0
    confidence_tier: str = ""
    confirmation_count: int = 0
    confirmation_required_count: int = 0
    stream_instance_id: str = ""
    alsa_handle_id: str = ""
    stream_open_reason: str = ""
    stream_close_reason: str = ""
    calibration_reason: str = ""
    ownership_handoff_source: str = ""
    ownership_handoff_destination: str = ""
    stream_open_reasons: tuple[str, ...] = ()
    stream_close_reasons: tuple[str, ...] = ()
    calibration_reasons: tuple[str, ...] = ()
    ownership_handoffs: tuple[str, ...] = ()


@dataclass(frozen=True)
class WakeAttemptResult:
    """One immutable, internally consistent standby candidate outcome."""

    attempt_id: str
    candidate_id: str
    stream_instance_id: str
    stream_generation: int
    candidate_number: int
    capture_valid: bool
    recognizer_invoked: bool
    infrastructure_failure: bool
    lifecycle_state_before: str
    lifecycle_state_after: str
    cleanup_status: str
    result: StandbyListenResultV1
    diagnostics: Optional[WakeLocalDiagnostics] = None

    def __post_init__(self) -> None:
        if not self.attempt_id or not self.candidate_id:
            raise ValueError("wake attempt and candidate identifiers are required")
        if self.stream_generation < 0 or self.candidate_number < 0:
            raise ValueError("wake attempt generation and candidate number cannot be negative")
        if self.result.attempt_id != self.attempt_id:
            raise ValueError("wake result attempt ID does not match its attempt")
        if self.result.candidate_id != self.candidate_id:
            raise ValueError("wake result candidate ID does not match its attempt")
        if self.result.stream_generation != self.stream_generation:
            raise ValueError("wake result stream generation does not match its attempt")
        if self.result.candidate_number != self.candidate_number:
            raise ValueError("wake result candidate number does not match its attempt")
        if self.result.stream_instance_id != self.stream_instance_id:
            raise ValueError("wake result stream instance does not match its attempt")
        if self.result.capture_valid != self.capture_valid:
            raise ValueError("wake result capture validity does not match its attempt")
        if self.result.recognizer_invoked != self.recognizer_invoked:
            raise ValueError("wake result recognizer state does not match its attempt")
        if self.result.infrastructure_failure != self.infrastructure_failure:
            raise ValueError("wake result infrastructure state does not match its attempt")
        if self.result.cleanup_status != self.cleanup_status:
            raise ValueError("wake result cleanup status does not match its attempt")
        if self.diagnostics is not None:
            if self.diagnostics.attempt_id != self.attempt_id:
                raise ValueError("wake diagnostics attempt ID does not match")
            if self.diagnostics.candidate_id != self.candidate_id:
                raise ValueError("wake diagnostics candidate ID does not match")
            if self.diagnostics.stream_generation != self.stream_generation:
                raise ValueError("wake diagnostics stream generation does not match")
            if self.diagnostics.capture_valid != self.capture_valid:
                raise ValueError("wake diagnostics capture validity does not match")
            if self.diagnostics.recognizer_invoked != self.recognizer_invoked:
                raise ValueError("wake diagnostics recognizer state does not match")
            if self.diagnostics.infrastructure_failure != self.infrastructure_failure:
                raise ValueError("wake diagnostics infrastructure state does not match")
        if not self.capture_valid:
            if self.recognizer_invoked:
                raise ValueError("wake recognizer cannot run for invalid audio")
            if self.result.recognition_confidence is not None:
                raise ValueError("invalid wake audio cannot carry recognition confidence")
            if self.diagnostics is not None and (
                self.diagnostics.raw_transcript
                or self.diagnostics.raw_recognition_result
            ):
                raise ValueError("invalid wake audio cannot carry recognition text")
        if self.recognizer_invoked:
            if self.result.duration_seconds <= 0:
                raise ValueError("wake recognition requires non-empty audio")
            if (
                self.result.sample_rate_hz != 16000
                or self.result.channels != 1
                or self.result.sample_width_bytes != 2
            ):
                raise ValueError("wake recognition requires canonical PCM audio")
        if self.result.wake_detected and not self.recognizer_invoked:
            raise ValueError("wake detection requires recognizer invocation")


@dataclass(frozen=True)
class WakeListenerConfig:
    enabled: bool = True
    microphone_device: str = DEFAULT_WAKE_MICROPHONE_DEVICE
    vosk_model_path: str = DEFAULT_WAKE_VOSK_MODEL
    minimum_recognition_confidence: float = DEFAULT_WAKE_MINIMUM_CONFIDENCE
    medium_recognition_confidence: float = DEFAULT_WAKE_MEDIUM_CONFIDENCE
    allow_exact_wake_without_confidence: bool = True
    medium_confidence_confirmation_count: int = 2
    medium_confidence_window_seconds: float = 8.0
    language: str = "en"
    wake_phrase_aliases: tuple[str, ...] = DEFAULT_WAKE_PHRASE_ALIASES
    wake_phrase_prefixes: tuple[str, ...] = DEFAULT_WAKE_PHRASE_PREFIXES
    filler_prefixes: tuple[str, ...] = DEFAULT_WAKE_FILLER_PREFIXES
    calibration_enabled: bool = True
    calibration_duration_seconds: float = 0.6
    calibration_maximum_seconds: float = 1.8
    recalibration_interval_seconds: float = 300.0
    speech_start_rms: float = 200.0
    speech_continue_rms: float = 160.0
    silence_rms: float = 120.0
    minimum_speech_start_rms: float = 200.0
    maximum_speech_start_rms: float = 1200.0
    minimum_speech_continue_rms: float = 160.0
    maximum_speech_continue_rms: float = 900.0
    minimum_silence_rms: float = 120.0
    maximum_silence_rms: float = 600.0
    required_speech_frames: int = 2
    required_continue_frames: int = 3
    required_silence_frames: int = 5
    speech_wait_timeout_seconds: float = 3.0
    maximum_utterance_seconds: float = 1.6
    minimum_speech_duration_seconds: float = 0.08
    silence_duration_seconds: float = 0.55
    pre_roll_seconds: float = 0.4
    speech_end_padding_seconds: float = 0.12
    trim_leading_padding_seconds: float = 0.24
    trim_trailing_padding_seconds: float = 0.20
    maximum_duplicate_collapse_audio_seconds: float = 1.4
    diagnostic_rms_interval_frames: int = 5
    frame_duration_ms: int = 20
    frame_read_timeout_seconds: float = 1.0
    playback_settle_delay_seconds: float = 0.35
    consecutive_failure_limit: int = 3
    retry_delay_seconds: float = 0.25
    duration_tolerance_seconds: float = 0.05
    diagnostic_wake: bool = False
    retain_diagnostic_audio: bool = False
    maximum_retained_candidates: int = 1
    diagnostic_output_directory: str = "data/runtime/wake_audio"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if not isinstance(self.calibration_enabled, bool):
            raise ValueError("calibration_enabled must be a boolean")
        if not isinstance(self.diagnostic_wake, bool):
            raise ValueError("diagnostic_wake must be a boolean")
        if not isinstance(self.retain_diagnostic_audio, bool):
            raise ValueError("retain_diagnostic_audio must be a boolean")
        if not isinstance(self.allow_exact_wake_without_confidence, bool):
            raise ValueError("allow_exact_wake_without_confidence must be a boolean")
        if self.retain_diagnostic_audio and not self.diagnostic_wake:
            raise ValueError("retain_diagnostic_audio requires diagnostic_wake")
        object.__setattr__(self, "microphone_device", _safe_path_text(self.microphone_device, "microphone_device"))
        object.__setattr__(
            self,
            "vosk_model_path",
            _safe_path_text(self.vosk_model_path, "vosk_model_path"),
        )
        object.__setattr__(
            self,
            "diagnostic_output_directory",
            _safe_path_text(self.diagnostic_output_directory, "diagnostic_output_directory"),
        )
        language = str(self.language or "").strip().lower()
        if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2})?", language):
            raise ValueError("language must be a bounded language or locale code")
        object.__setattr__(self, "language", language)
        object.__setattr__(
            self,
            "wake_phrase_aliases",
            _validated_aliases(self.wake_phrase_aliases),
        )
        object.__setattr__(
            self,
            "wake_phrase_prefixes",
            _validated_prefixes(self.wake_phrase_prefixes, "wake_phrase_prefixes"),
        )
        object.__setattr__(
            self,
            "filler_prefixes",
            _validated_phrases(self.filler_prefixes, "filler_prefixes", allow_empty=True),
        )
        numeric_bounds = {
            "calibration_duration_seconds": (0.0, 3.0),
            "calibration_maximum_seconds": (0.1, 5.0),
            "recalibration_interval_seconds": (0.0, 3600.0),
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
            "minimum_speech_duration_seconds": (0.02, 0.5),
            "silence_duration_seconds": (0.1, 2.0),
            "pre_roll_seconds": (0.0, 0.75),
            "speech_end_padding_seconds": (0.0, 0.5),
            "trim_leading_padding_seconds": (0.0, 0.5),
            "trim_trailing_padding_seconds": (0.0, 0.5),
            "maximum_duplicate_collapse_audio_seconds": (0.25, 2.0),
            "frame_read_timeout_seconds": (0.05, 3.0),
            "playback_settle_delay_seconds": (0.0, 3.0),
            "retry_delay_seconds": (0.0, 5.0),
            "duration_tolerance_seconds": (0.0, 0.5),
            "minimum_recognition_confidence": (0.4, 1.0),
            "medium_recognition_confidence": (0.1, 0.9),
            "medium_confidence_window_seconds": (1.0, 30.0),
        }
        for name, (minimum, maximum) in numeric_bounds.items():
            object.__setattr__(self, name, _bounded_number(getattr(self, name), name, minimum, maximum))
        for name, minimum, maximum in (
            ("required_speech_frames", 1, 20),
            ("required_continue_frames", 1, 20),
            ("required_silence_frames", 1, 50),
            ("frame_duration_ms", 10, 40),
            ("consecutive_failure_limit", 1, 10),
            ("maximum_retained_candidates", 1, 3),
            ("medium_confidence_confirmation_count", 2, 3),
            ("diagnostic_rms_interval_frames", 1, 50),
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
        if self.calibration_maximum_seconds < self.calibration_duration_seconds:
            raise ValueError(
                "calibration_maximum_seconds cannot be below calibration_duration_seconds"
            )
        if self.medium_recognition_confidence >= self.minimum_recognition_confidence:
            raise ValueError(
                "medium_recognition_confidence must be below minimum_recognition_confidence"
            )
        if self.speech_end_padding_seconds >= self.silence_duration_seconds:
            raise ValueError(
                "speech_end_padding_seconds must be below silence_duration_seconds"
            )
        if self.minimum_speech_duration_seconds >= self.maximum_utterance_seconds:
            raise ValueError(
                "minimum_speech_duration_seconds must be below maximum_utterance_seconds"
            )
        if (
            self.maximum_duplicate_collapse_audio_seconds
            > self.maximum_utterance_seconds + self.pre_roll_seconds
        ):
            raise ValueError(
                "maximum_duplicate_collapse_audio_seconds exceeds the wake capture bound"
            )

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

    @property
    def wake_phrases(self) -> tuple[str, ...]:
        return build_accepted_wake_phrases(
            self.wake_phrase_aliases,
            self.wake_phrase_prefixes,
            self.filler_prefixes,
        )


@runtime_checkable
class StandbyWakeListener(Protocol):
    config: WakeListenerConfig

    def start(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        ...

    def listen_once(self, request: WakeListenerRequestV1) -> StandbyListenResultV1:
        ...

    def listen_attempt(self, request: WakeListenerRequestV1) -> WakeAttemptResult:
        ...

    def completed_attempt(self, attempt_id: str) -> Optional[WakeAttemptResult]:
        ...

    def complete_attempt_lifecycle(
        self,
        attempt_id: str,
        lifecycle_state_after: str,
    ) -> Optional[WakeAttemptResult]:
        ...

    def enter_standby(
        self,
        *,
        runtime_id: str = "",
        reason: str = "standby_entered",
        handoff_source: str = "",
    ) -> WakeListenerResultV1:
        ...

    def leave_standby(
        self,
        reason: str = "leaving_standby",
        *,
        handoff_destination: str = "active_command",
    ) -> WakeListenerResultV1:
        ...

    def cancel(self, reason: str = "cancelled") -> WakeListenerResultV1:
        ...

    def stop(self, reason: str = "stopped") -> WakeListenerResultV1:
        ...

    def snapshot(self, *, runtime_id: str = "") -> WakeListenerSnapshotV1:
        ...

    def health(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        ...


def clean_wake_transcript(text: str) -> str:
    return clean_spoken_phrase(text)


def normalize_wake_phrase(text: str) -> str:
    return normalize_spoken_phrase(text)


def build_accepted_wake_phrases(
    aliases: Sequence[str] = DEFAULT_WAKE_PHRASE_ALIASES,
    prefixes: Sequence[str] = DEFAULT_WAKE_PHRASE_PREFIXES,
    filler_prefixes: Sequence[str] = DEFAULT_WAKE_FILLER_PREFIXES,
) -> tuple[str, ...]:
    normalized_aliases = _validated_aliases(aliases)
    normalized_prefixes = _validated_prefixes(prefixes, "wake_phrase_prefixes")
    normalized_fillers = _validated_phrases(
        filler_prefixes,
        "filler_prefixes",
        allow_empty=True,
    )
    phrases = [
        _join_wake_phrase(prefix, alias)
        for prefix in (*normalized_prefixes, *normalized_fillers)
        for alias in normalized_aliases
    ]
    return tuple(dict.fromkeys(phrases))


def classify_wake_transcript(
    transcript: str,
    *,
    wake_phrase_aliases: Sequence[str] = DEFAULT_WAKE_PHRASE_ALIASES,
    wake_phrase_prefixes: Sequence[str] = DEFAULT_WAKE_PHRASE_PREFIXES,
    filler_prefixes: Sequence[str] = DEFAULT_WAKE_FILLER_PREFIXES,
    standby_phrases: Sequence[str] = (),
    shutdown_phrases: Sequence[str] = (),
    correlation_id: str = "",
    runtime_id: str = "",
) -> WakeDetectionResultV1:
    normalized = normalize_wake_phrase(transcript)
    aliases = _validated_aliases(wake_phrase_aliases)
    prefixes = _validated_prefixes(wake_phrase_prefixes, "wake_phrase_prefixes")
    fillers = _validated_phrases(filler_prefixes, "filler_prefixes", allow_empty=True)
    phrase_map = _wake_phrase_map(aliases, prefixes, fillers)
    wake = set(phrase_map)
    standby = set(_validated_phrases(standby_phrases, "standby_phrases", allow_empty=True))
    shutdown = set(_validated_phrases(shutdown_phrases, "shutdown_phrases", allow_empty=True))
    if wake & standby or wake & shutdown or standby & shutdown:
        raise ValueError("wake, standby, and shutdown phrases must not overlap")
    category = WAKE_CATEGORY_NON_WAKE
    matched = ""
    selected_alias = ""
    selected_phrase = ""
    canonical_phrase = ""
    classification_path = ""
    classification_reason = ""
    collapsed_representation = ""
    wake_vocabulary_only = False
    wake_token_count = 0
    alias_repetition_count = 0
    maximum_prefix_repetition_count = 0
    rejection_reason = ""
    detected = False
    if normalized in shutdown:
        category, matched = WAKE_CATEGORY_SHUTDOWN, normalized
        classification_path = "control"
        classification_reason = "matched_shutdown_phrase"
    elif normalized in standby:
        category, matched = WAKE_CATEGORY_STANDBY, normalized
        classification_path = "control"
        classification_reason = "matched_standby_phrase"
    elif normalized in phrase_map:
        selected_alias, selected_phrase, canonical_phrase = phrase_map[normalized]
        category, matched, detected = WAKE_CATEGORY_ACTIVATION, selected_phrase, True
        classification_path = "exact"
        classification_reason = "accepted_exact_wake_phrase"
        collapsed_representation = canonical_phrase
        wake_vocabulary_only = True
        wake_token_count = len(normalized.split())
        alias_repetition_count = 1
        maximum_prefix_repetition_count = 1 if wake_token_count > 1 else 0
    else:
        tokens = tuple(normalized.split())
        classification_path = "exact" if normalized else "none"
        classification_reason = (
            "exact_wake_phrase_not_matched" if normalized else "empty_wake_transcript"
        )
        wake_token_count = len(tokens)
        wake_vocabulary_only = False
        rejection_reason = classification_reason
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
        normalized_wake_phrase=canonical_phrase if detected else "",
        matched_phrase=matched,
        selected_alias=selected_alias,
        selected_wake_phrase=selected_phrase,
        canonical_wake_phrase=canonical_phrase,
        classification_path=classification_path,
        classification_reason=classification_reason,
        collapsed_wake_representation=collapsed_representation,
        wake_vocabulary_only=wake_vocabulary_only,
        wake_token_count=wake_token_count,
        alias_repetition_count=alias_repetition_count,
        maximum_prefix_repetition_count=maximum_prefix_repetition_count,
        rejection_reason=rejection_reason,
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
        self._stream_active = False
        self._stream_open_count = 0
        self._stream_close_count = 0
        self._calibration_count = 0
        self._last_stop_reason = ""
        self._last_open_reason = ""
        self._last_close_reason = ""
        self._last_calibration_reason = ""
        self._last_handoff_source = ""
        self._last_handoff_destination = ""
        self._stream_instance_id = ""
        self.last_result: Optional[StandbyListenResultV1] = None

    def push(self, item: Optional[str] | StandbyListenResultV1 | WakeDetectionResultV1) -> None:
        with self._lock:
            self._items.append(item)

    def start(self, *, runtime_id: str = "") -> WakeListenerResultV1:
        with self._lock:
            self._runtime_id = str(runtime_id or self._runtime_id)
            self._cancelled = False
            self._state = WAKE_LISTENER_READY
        self.enter_standby(runtime_id=self._runtime_id)
        return self._lifecycle(True, "started")

    def enter_standby(
        self,
        *,
        runtime_id: str = "",
        reason: str = "standby_entered",
        handoff_source: str = "",
    ) -> WakeListenerResultV1:
        with self._lock:
            if runtime_id:
                self._runtime_id = str(runtime_id)
            if self._state == WAKE_LISTENER_STOPPED:
                return self._lifecycle(False, "listener_not_started")
            if not self._stream_active:
                self._stream_active = True
                self._stream_open_count += 1
                self._calibration_count += 1
                self._stream_instance_id = f"queued-stream-{self._stream_open_count}"
                self._last_open_reason = str(reason or "standby_entered")[:80]
                self._last_calibration_reason = (
                    f"{self._last_open_reason}:initial_calibration"
                )[:96]
                self._last_handoff_source = str(handoff_source or "")[:64]
                self._last_handoff_destination = (
                    "queued_standby" if handoff_source else ""
                )
            return self._lifecycle(True, "standby_stream_ready")

    def leave_standby(
        self,
        reason: str = "leaving_standby",
        *,
        handoff_destination: str = "active_command",
    ) -> WakeListenerResultV1:
        with self._lock:
            if self._stream_active:
                self._stream_active = False
                self._stream_close_count += 1
            self._last_stop_reason = str(reason or "leaving_standby")[:80]
            self._last_close_reason = self._last_stop_reason
            self._last_handoff_source = "queued_standby"
            self._last_handoff_destination = str(handoff_destination or "")[:64]
            return self._lifecycle(True, "standby_stream_closed")

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
                    wake_phrase_aliases=(
                        request.wake_phrase_aliases or self.config.wake_phrase_aliases
                    ),
                    wake_phrase_prefixes=(
                        request.wake_phrase_prefixes or self.config.wake_phrase_prefixes
                    ),
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
            if self._stream_active:
                self._stream_active = False
                self._stream_close_count += 1
            self._cancelled = True
            self._state = WAKE_LISTENER_CANCELLING
            self._last_stop_reason = str(reason or "cancelled")[:80]
            return self._lifecycle(True, "cancelled")

    def stop(self, reason: str = "stopped") -> WakeListenerResultV1:
        with self._lock:
            if self._stream_active:
                self._stream_active = False
                self._stream_close_count += 1
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
                stream_open_count=self._stream_open_count,
                stream_close_count=self._stream_close_count,
                calibration_count=self._calibration_count,
                candidate_count=self._listen_count,
                stream_active=self._stream_active,
                capture_owner="queued_standby" if self._stream_active else "",
                stream_instance_id=self._stream_instance_id,
                alsa_handle_id=f"{self._stream_instance_id}-handle" if self._stream_instance_id else "",
                stream_open_reason=self._last_open_reason,
                stream_close_reason=self._last_close_reason,
                calibration_reason=self._last_calibration_reason,
                ownership_handoff_source=self._last_handoff_source,
                ownership_handoff_destination=self._last_handoff_destination,
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
        selected_alias=detection.selected_alias,
        selected_wake_phrase=detection.selected_wake_phrase,
        canonical_wake_phrase=detection.canonical_wake_phrase,
        classification_path=detection.classification_path,
        classification_reason=detection.classification_reason,
        collapsed_wake_representation=detection.collapsed_wake_representation,
        wake_vocabulary_only=detection.wake_vocabulary_only,
        wake_token_count=detection.wake_token_count,
        alias_repetition_count=detection.alias_repetition_count,
        maximum_prefix_repetition_count=detection.maximum_prefix_repetition_count,
        rejection_reason=detection.rejection_reason,
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
    wake_phrase_aliases: Sequence[str] = (),
) -> None:
    wake = set(_validated_phrases(wake_phrases, "wake_phrases"))
    standby = set(
        expand_control_phrase_aliases(standby_phrases, wake_phrase_aliases)
        if wake_phrase_aliases
        else _validated_phrases(standby_phrases, "standby_phrases")
    )
    shutdown = set(
        expand_control_phrase_aliases(shutdown_phrases, wake_phrase_aliases)
        if wake_phrase_aliases
        else _validated_phrases(shutdown_phrases, "shutdown_phrases")
    )
    if wake & standby:
        raise ValueError("wake and standby phrases must not overlap")
    if wake & shutdown:
        raise ValueError("wake and shutdown phrases must not overlap")
    if standby & shutdown:
        raise ValueError("standby and shutdown phrases must not overlap")


def expand_control_phrase_aliases(
    phrases: Sequence[str],
    aliases: Sequence[str],
) -> tuple[str, ...]:
    """Expand only complete alias tokens inside configured control phrases."""

    normalized_phrases = _validated_phrases(phrases, "control_phrases", allow_empty=True)
    return expand_ares_alias_phrases(normalized_phrases, aliases)


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


def _validated_aliases(values: Sequence[str]) -> tuple[str, ...]:
    try:
        return validate_ares_name_aliases(values)
    except ValueError as error:
        raise ValueError(str(error).replace("ares_name_aliases", "wake_phrase_aliases")) from error


def _validated_prefixes(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of prefixes")
    normalized = tuple(normalize_wake_phrase(value) for value in values)
    if not normalized:
        raise ValueError(f"{field_name} must contain at least one prefix")
    if len(normalized) > 8:
        raise ValueError(f"{field_name} may contain at most 8 prefixes")
    if any(value and (len(value) > 24 or not _PREFIX_PATTERN.fullmatch(value)) for value in normalized):
        raise ValueError(f"{field_name} contains an unsafe or oversized prefix")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} contains duplicates after normalization")
    return normalized


def _join_wake_phrase(prefix: str, alias: str) -> str:
    return " ".join(part for part in (prefix, alias) if part)


def _wake_phrase_map(
    aliases: Sequence[str],
    prefixes: Sequence[str],
    filler_prefixes: Sequence[str],
) -> Dict[str, tuple[str, str, str]]:
    canonical_alias = aliases[0]
    accepted: Dict[str, tuple[str, str, str]] = {}
    for prefix in prefixes:
        for alias in aliases:
            phrase = _join_wake_phrase(prefix, alias)
            accepted[phrase] = (alias, phrase, canonical_alias)
    for prefix in filler_prefixes:
        for alias in aliases:
            phrase = _join_wake_phrase(prefix, alias)
            accepted[phrase] = (alias, phrase, canonical_alias)
    return accepted


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
