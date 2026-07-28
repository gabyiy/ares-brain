from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from threading import RLock
import time
from typing import Any, Callable, Optional
from uuid import uuid4

from core.ActiveLifecycleAudioRecognizer import (
    ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
    ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
    ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
    ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
    ACTIVE_LIFECYCLE_CONFIRMATION_CANCELLED,
    ACTIVE_LIFECYCLE_CONFIRMATION_CONFIRMED,
    CANONICAL_SHUTDOWN_PHRASE,
    CANONICAL_STANDBY_PHRASE,
    DEFAULT_CANCELLATION_MINIMUM_CONFIDENCE,
    DEFAULT_CONFIRMATION_MINIMUM_CONFIDENCE,
    DEFAULT_SHUTDOWN_HIGH_CONFIDENCE,
    DEFAULT_SHUTDOWN_MEDIUM_CONFIDENCE,
    DEFAULT_STANDBY_HIGH_CONFIDENCE,
    DEFAULT_STANDBY_MEDIUM_CONFIDENCE,
    ActiveLifecycleAudioRecognizer,
)
from core.BrainRuntimeAdapters import (
    RuntimeInputResult,
    RuntimeOutputMessage,
    RuntimeOutputResult,
)
from core.Contracts import (
    SingleTurnVoiceRequestV1,
    new_correlation_id,
    utc_contract_timestamp,
)
from core.LifecycleControl import (
    LIFECYCLE_ACTION_NONE,
    normalize_active_lifecycle_command,
)
from core.Microphone import AudioChunk
from core.ResourceBudget import CancellationToken
from core.SingleTurnVoiceSupport import (
    FinalizedAudioHook,
    SingleTurnFinalizedAudioDecision,
    SingleTurnPreBrainDecision,
)


_INPUT_TIMEOUT_STATUSES = {
    "end_of_input",
    "no_speech_timeout",
    "silent_audio",
    "blank_transcription",
    "transcription_timeout",
    "transcription_rejected",
    "transcript_rejected",
}

ACTIVE_COMMAND_CAPTURE_PROFILE = "active_command_v1"
ACTIVE_COMMAND_MINIMUM_PRE_ROLL_SECONDS = 0.5
ACTIVE_COMMAND_TERMINAL_SILENCE_SECONDS = 0.9
DEFAULT_ACTIVE_LIFECYCLE_CONFIRMATION_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ActiveLifecyclePendingConfirmation:
    classification: str
    canonical_phrase: str
    session_id: str
    expires_at: float


class ActiveLifecycleAudioTurnController:
    """Stateful policy boundary around finalized-audio lifecycle evidence.

    It never captures audio, executes a transition, calls CoreService, or owns
    output.  It only decides whether the already-finalized active-turn WAV may
    bypass Whisper, requires a bounded next-turn confirmation, or must continue
    through the ordinary Whisper route.
    """

    def __init__(
        self,
        *,
        recognizer: ActiveLifecycleAudioRecognizer,
        session_id_provider: Callable[[], str],
        lifecycle_state_provider: Callable[[], str],
        clock: Callable[[], float] = time.monotonic,
        confirmation_timeout_seconds: float = (
            DEFAULT_ACTIVE_LIFECYCLE_CONFIRMATION_TIMEOUT_SECONDS
        ),
    ) -> None:
        if not callable(getattr(recognizer, "recognize_wav", None)):
            raise ValueError("recognizer must support recognize_wav")
        if not callable(getattr(recognizer, "recognize_confirmation_wav", None)):
            raise ValueError("recognizer must support recognize_confirmation_wav")
        if not callable(session_id_provider):
            raise ValueError("session_id_provider must be callable")
        if not callable(lifecycle_state_provider):
            raise ValueError("lifecycle_state_provider must be callable")
        if not callable(clock):
            raise ValueError("clock must be callable")
        if isinstance(confirmation_timeout_seconds, bool) or not isinstance(
            confirmation_timeout_seconds, (int, float)
        ):
            raise ValueError("confirmation_timeout_seconds must be numeric")
        timeout = float(confirmation_timeout_seconds)
        if not 1.0 <= timeout <= 60.0:
            raise ValueError(
                "confirmation_timeout_seconds must be between 1 and 60"
            )
        self.recognizer = recognizer
        self.session_id_provider = session_id_provider
        self.lifecycle_state_provider = lifecycle_state_provider
        self.clock = clock
        self.confirmation_timeout_seconds = timeout
        self._lock = RLock()
        self._pending: Optional[ActiveLifecyclePendingConfirmation] = None
        self._closed = False

    def __call__(self, audio_chunk: AudioChunk) -> SingleTurnFinalizedAudioDecision:
        if not isinstance(audio_chunk, AudioChunk):
            raise TypeError("finalized active audio must be an AudioChunk")
        audio_path = str(
            dict(audio_chunk.metadata or {}).get("final_whisper_input_path")
            or dict(audio_chunk.metadata or {}).get("wav_path")
            or ""
        )
        state = str(self.lifecycle_state_provider() or "").strip().upper()
        session_id = str(self.session_id_provider() or "").strip()
        now = float(self.clock())
        with self._lock:
            if self._closed:
                return self._fallback_decision(
                    rejection_reason="active_lifecycle_controller_closed",
                )
            pending = self._pending

        if state != "ACTIVE" or not session_id:
            self.reset("active_state_or_session_unavailable")
            return self._fallback_decision(
                rejection_reason="active_state_and_session_required",
                pending_clear_reason=(
                    "state_changed" if state != "ACTIVE" else "session_missing"
                ),
            )

        if pending is not None:
            if pending.session_id != session_id:
                self.reset("pending_session_changed")
                return self._fallback_decision(
                    rejection_reason="pending_confirmation_session_changed",
                    pending_clear_reason="session_changed",
                )
            if now >= pending.expires_at:
                self.reset("pending_confirmation_expired")
                return self._fallback_decision(
                    rejection_reason="pending_confirmation_expired",
                    pending_clear_reason="expired",
                )
            return self._recognize_confirmation(audio_path, pending)

        try:
            recognition = self.recognizer.recognize_wav(audio_path)
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            return self._fallback_decision(
                rejection_reason=(
                    f"active_lifecycle_recognizer_error:{error.__class__.__name__}:"
                    f"{str(error)[:160]}"
                ),
            )
        payload = _active_lifecycle_recognition_payload(recognition)
        classification = str(getattr(recognition, "classification", "") or "")
        canonical_phrase = str(
            getattr(recognition, "canonical_phrase", "") or ""
        )
        confirmation_required = bool(
            getattr(recognition, "confirmation_required", False)
        )
        proposed = str(
            getattr(recognition, "proposed_classification", "") or ""
        )
        confidence = _finite_lifecycle_confidence(
            getattr(recognition, "confidence", None)
        )
        confidence_available = bool(
            getattr(recognition, "confidence_available", False)
        )
        confidence_tier = str(
            getattr(recognition, "confidence_tier", "") or ""
        ).strip().lower()
        selected_action = str(
            getattr(recognition, "selected_lifecycle_action", "") or ""
        )
        expected_canonical = _canonical_lifecycle_phrase(classification)
        high_threshold = _lifecycle_action_confidence_threshold(
            self.recognizer,
            classification,
            tier="high",
        )
        high_confidence_authorized = (
            bool(expected_canonical)
            and canonical_phrase == expected_canonical
            and selected_action == classification
            and not confirmation_required
            and confidence_available
            and confidence is not None
            and confidence >= high_threshold
            and confidence_tier == "high"
        )
        if (
            classification
            in {
                ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
                ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
            }
            and high_confidence_authorized
        ):
            payload.update(
                {
                    "audio_checked": True,
                    "lifecycle_authorized": True,
                    "selected_lifecycle_action": classification,
                    "whisper_fallback_required": False,
                }
            )
            return _active_lifecycle_decision(
                handled=True,
                status="active_lifecycle_audio_authorized",
                canonical_text=canonical_phrase,
                payload=payload,
            )
        proposed_canonical = _canonical_lifecycle_phrase(proposed)
        medium_threshold = _lifecycle_action_confidence_threshold(
            self.recognizer,
            proposed,
            tier="medium",
        )
        medium_confidence_confirmation = (
            confirmation_required
            and classification == ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN
            and proposed in {
                ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
                ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
            }
            and bool(proposed_canonical)
            and canonical_phrase == proposed_canonical
            and confidence_available
            and confidence is not None
            and confidence >= medium_threshold
            and confidence_tier == "medium"
        )
        if medium_confidence_confirmation:
            pending = ActiveLifecyclePendingConfirmation(
                classification=proposed,
                canonical_phrase=canonical_phrase,
                session_id=session_id,
                expires_at=now + self.confirmation_timeout_seconds,
            )
            with self._lock:
                self._pending = pending
            payload.update(
                {
                    "audio_checked": True,
                    "lifecycle_authorized": False,
                    "selected_lifecycle_action": "none",
                    "whisper_fallback_required": False,
                    "pending_confirmation": True,
                    "pending_expires_at": pending.expires_at,
                }
            )
            return _active_lifecycle_decision(
                handled=True,
                status="active_lifecycle_audio_confirmation_required",
                canonical_text="",
                payload=payload,
            )
        if classification in {
            ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
            ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
        }:
            payload["rejection_reason"] = (
                "controller_high_confidence_authorization_invariant_failed"
            )
        elif confirmation_required:
            payload["rejection_reason"] = (
                "controller_medium_confidence_confirmation_invariant_failed"
            )
        payload.update(
            {
                "audio_checked": True,
                "lifecycle_authorized": False,
                "selected_lifecycle_action": "none",
                "whisper_fallback_required": True,
            }
        )
        return _active_lifecycle_decision(
            handled=False,
            status="active_lifecycle_audio_whisper_fallback",
            canonical_text="",
            payload=payload,
        )

    def pending_confirmation(self) -> Optional[ActiveLifecyclePendingConfirmation]:
        with self._lock:
            return self._pending

    def reset(self, reason: str = "reset") -> None:
        del reason
        with self._lock:
            self._pending = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending = None
        close = getattr(self.recognizer, "close", None)
        if not callable(close):
            close = getattr(getattr(self.recognizer, "backend", None), "close", None)
        if callable(close):
            try:
                close()
            except (OSError, RuntimeError, TypeError, ValueError):
                pass

    def _recognize_confirmation(
        self,
        audio_path: str,
        pending: ActiveLifecyclePendingConfirmation,
    ) -> SingleTurnFinalizedAudioDecision:
        try:
            confirmation = self.recognizer.recognize_confirmation_wav(
                audio_path,
                expected_classification=pending.classification,
            )
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            self.reset("confirmation_recognizer_error")
            return self._fallback_decision(
                rejection_reason=(
                    f"active_lifecycle_confirmation_error:{error.__class__.__name__}:"
                    f"{str(error)[:160]}"
                ),
                pending_clear_reason="confirmation_error",
            )
        self.reset("confirmation_turn_consumed")
        payload = _active_lifecycle_confirmation_payload(confirmation)
        disposition = str(getattr(confirmation, "disposition", "") or "")
        confidence = _finite_lifecycle_confidence(
            getattr(confirmation, "confidence", None)
        )
        confidence_available = bool(
            getattr(confirmation, "confidence_available", False)
        )
        expected_classification = str(
            getattr(confirmation, "expected_classification", "") or ""
        )
        pending_canonical_is_valid = (
            pending.canonical_phrase
            == _canonical_lifecycle_phrase(pending.classification)
        )
        confirmation_threshold = _recognizer_confidence_threshold(
            self.recognizer,
            "confirmation_minimum_confidence",
            DEFAULT_CONFIRMATION_MINIMUM_CONFIDENCE,
            prevent_weakening=True,
        )
        cancellation_threshold = _recognizer_confidence_threshold(
            self.recognizer,
            "cancellation_minimum_confidence",
            DEFAULT_CANCELLATION_MINIMUM_CONFIDENCE,
        )
        confirmation_evidence_is_valid = (
            confidence_available
            and confidence is not None
            and expected_classification == pending.classification
            and pending_canonical_is_valid
        )
        if (
            disposition == ACTIVE_LIFECYCLE_CONFIRMATION_CONFIRMED
            and confirmation_evidence_is_valid
            and confidence >= confirmation_threshold
        ):
            payload.update(
                {
                    "audio_checked": True,
                    "classification": pending.classification,
                    "canonical_phrase": pending.canonical_phrase,
                    "lifecycle_authorized": True,
                    "selected_lifecycle_action": pending.classification,
                    "confirmation_required": False,
                    "confirmation_disposition": disposition,
                    "whisper_fallback_required": False,
                }
            )
            return _active_lifecycle_decision(
                handled=True,
                status="active_lifecycle_audio_confirmation_confirmed",
                canonical_text=pending.canonical_phrase,
                payload=payload,
            )
        if (
            disposition == ACTIVE_LIFECYCLE_CONFIRMATION_CANCELLED
            and confirmation_evidence_is_valid
            and confidence >= cancellation_threshold
        ):
            payload.update(
                {
                    "audio_checked": True,
                    "classification": "uncertain",
                    "canonical_phrase": pending.canonical_phrase,
                    "lifecycle_authorized": False,
                    "selected_lifecycle_action": "none",
                    "confirmation_required": False,
                    "confirmation_disposition": disposition,
                    "whisper_fallback_required": False,
                    "pending_clear_reason": "owner_cancelled",
                }
            )
            return _active_lifecycle_decision(
                handled=True,
                status="active_lifecycle_audio_confirmation_cancelled",
                canonical_text="",
                payload=payload,
            )
        if disposition in {
            ACTIVE_LIFECYCLE_CONFIRMATION_CONFIRMED,
            ACTIVE_LIFECYCLE_CONFIRMATION_CANCELLED,
        }:
            payload["rejection_reason"] = (
                "controller_confirmation_authorization_invariant_failed"
            )
        payload.update(
            {
                "audio_checked": True,
                "classification": "ordinary",
                "canonical_phrase": "",
                "lifecycle_authorized": False,
                "selected_lifecycle_action": "none",
                "confirmation_required": False,
                "confirmation_disposition": disposition or "unmatched",
                "whisper_fallback_required": True,
                "pending_clear_reason": "confirmation_unmatched",
            }
        )
        return _active_lifecycle_decision(
            handled=False,
            status="active_lifecycle_audio_confirmation_unmatched",
            canonical_text="",
            payload=payload,
        )

    def _fallback_decision(
        self,
        *,
        rejection_reason: str,
        pending_clear_reason: str = "",
    ) -> SingleTurnFinalizedAudioDecision:
        payload = {
            "audio_checked": True,
            "classification": ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
            "canonical_phrase": "",
            "recognized_text": "",
            "recognized_tokens": [],
            "confidence": None,
            "confidence_available": False,
            "recognition_backend": "active_lifecycle_controller",
            "rejection_reason": str(rejection_reason or "")[:240],
            "confidence_tier": "missing",
            "confirmation_required": False,
            "proposed_classification": "",
            "lifecycle_authorized": False,
            "selected_lifecycle_action": "none",
            "whisper_fallback_required": True,
            "pending_clear_reason": pending_clear_reason,
        }
        return _active_lifecycle_decision(
            handled=False,
            status="active_lifecycle_audio_whisper_fallback",
            canonical_text="",
            payload=payload,
        )


def _active_lifecycle_decision(
    *,
    handled: bool,
    status: str,
    canonical_text: str,
    payload: dict[str, Any],
) -> SingleTurnFinalizedAudioDecision:
    return SingleTurnFinalizedAudioDecision(
        handled=handled,
        continue_to_whisper=not handled,
        status=status,
        canonical_text=canonical_text,
        data={"active_lifecycle_audio": dict(payload)},
    )


def _canonical_lifecycle_phrase(classification: str) -> str:
    return {
        ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY: CANONICAL_STANDBY_PHRASE,
        ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN: CANONICAL_SHUTDOWN_PHRASE,
    }.get(str(classification or ""), "")


def _finite_lifecycle_confidence(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    confidence = float(value)
    return confidence if isfinite(confidence) else None


def _recognizer_confidence_threshold(
    recognizer: Any,
    attribute: str,
    default: float,
    *,
    prevent_weakening: bool = False,
) -> float:
    threshold = _finite_lifecycle_confidence(getattr(recognizer, attribute, None))
    if threshold is None or not 0.0 <= threshold <= 1.0:
        return float(default)
    return max(float(default), threshold) if prevent_weakening else threshold


def _lifecycle_action_confidence_threshold(
    recognizer: Any,
    classification: str,
    *,
    tier: str,
) -> float:
    policy = {
        (ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY, "high"): (
            "standby_high_confidence",
            DEFAULT_STANDBY_HIGH_CONFIDENCE,
        ),
        (ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY, "medium"): (
            "standby_medium_confidence",
            DEFAULT_STANDBY_MEDIUM_CONFIDENCE,
        ),
        (ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN, "high"): (
            "shutdown_high_confidence",
            DEFAULT_SHUTDOWN_HIGH_CONFIDENCE,
        ),
        (ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN, "medium"): (
            "shutdown_medium_confidence",
            DEFAULT_SHUTDOWN_MEDIUM_CONFIDENCE,
        ),
    }
    attribute, default = policy.get(
        (str(classification or ""), str(tier or "")),
        ("", 1.0),
    )
    if not attribute:
        return 1.0
    return _recognizer_confidence_threshold(
        recognizer,
        attribute,
        default,
        prevent_weakening=True,
    )


def _active_lifecycle_recognition_payload(recognition: Any) -> dict[str, Any]:
    return {
        "classification": str(getattr(recognition, "classification", "") or ""),
        "canonical_phrase": str(
            getattr(recognition, "canonical_phrase", "") or ""
        ),
        "recognized_text": str(getattr(recognition, "recognized_text", "") or ""),
        "recognized_tokens": [
            str(token or "")
            for token in tuple(getattr(recognition, "recognized_tokens", ()) or ())
        ],
        "confidence": getattr(recognition, "confidence", None),
        "confidence_available": bool(
            getattr(recognition, "confidence_available", False)
        ),
        "recognition_backend": str(
            getattr(recognition, "recognition_backend", "") or ""
        ),
        "rejection_reason": str(
            getattr(recognition, "rejection_reason", "") or ""
        ),
        "confidence_tier": str(
            getattr(recognition, "confidence_tier", "") or ""
        ),
        "confirmation_required": bool(
            getattr(recognition, "confirmation_required", False)
        ),
        "proposed_classification": str(
            getattr(recognition, "proposed_classification", "") or ""
        ),
    }


def _active_lifecycle_confirmation_payload(confirmation: Any) -> dict[str, Any]:
    return {
        "classification": "uncertain",
        "canonical_phrase": "",
        "recognized_text": str(
            getattr(confirmation, "recognized_text", "") or ""
        ),
        "recognized_tokens": [
            str(token or "")
            for token in tuple(getattr(confirmation, "recognized_tokens", ()) or ())
        ],
        "confidence": getattr(confirmation, "confidence", None),
        "confidence_available": bool(
            getattr(confirmation, "confidence_available", False)
        ),
        "recognition_backend": str(
            getattr(confirmation, "recognition_backend", "") or ""
        ),
        "rejection_reason": str(
            getattr(confirmation, "rejection_reason", "") or ""
        ),
        "confidence_tier": "confirmation",
        "confirmation_required": False,
        "proposed_classification": str(
            getattr(confirmation, "expected_classification", "") or ""
        ),
        "expected_classification": str(
            getattr(confirmation, "expected_classification", "") or ""
        ),
        "confirmation_disposition": str(
            getattr(confirmation, "disposition", "") or ""
        ),
    }


def active_command_capture_request(
    request: SingleTurnVoiceRequestV1,
) -> SingleTurnVoiceRequestV1:
    """Apply the production ACTIVE recorder's non-threshold timing profile.

    Thresholds and ALSA format stay untouched.  The profile only guarantees a
    full rolling lead-in and a natural terminal-quiet window, and is shared by
    the foreground runtime and its bounded hardware diagnostic.
    """

    if not isinstance(request, SingleTurnVoiceRequestV1):
        raise TypeError("single turn voice request required")
    return replace(
        request,
        pre_roll_seconds=max(
            ACTIVE_COMMAND_MINIMUM_PRE_ROLL_SECONDS,
            float(request.pre_roll_seconds),
        ),
        silence_duration_seconds=ACTIVE_COMMAND_TERMINAL_SILENCE_SECONDS,
        metadata={
            **dict(request.metadata or {}),
            "capture_profile": ACTIVE_COMMAND_CAPTURE_PROFILE,
            "active_command_capture": True,
            "canonical_pcm": "16000_hz_mono_s16_le",
        },
    )


@dataclass(frozen=True)
class ActiveCommandLocalDiagnostics:
    """Ephemeral owner-terminal details; never published or persisted."""

    raw_transcript: str = ""
    cleaned_transcript: str = ""
    alias_canonicalized_transcript: str = ""
    lifecycle_normalized_transcript: str = ""
    matched_assistant_alias: str = ""
    assistant_alias_type: str = ""
    assistant_alias_removed: str = ""
    alias_position: str = "none"
    canonical_name: str = ""
    negation_detected: bool = False
    lifecycle_classification: str = "ordinary"
    selected_lifecycle_action: str = "none"
    matched_lifecycle_phrase: str = ""
    lifecycle_rejection_reason: str = ""
    core_service_bypassed: bool = False
    activation_handler_called: bool = False
    lifecycle_state_before: str = ""
    lifecycle_state_after: str = ""
    session_id_before: str = ""
    session_id_after: str = ""
    capture_stop_reason: str = ""
    audio_capture_start_reason: str = ""
    first_speech_frame: int = 0
    last_speech_frame: int = 0
    pre_roll_frames_retained: int = 0
    expected_pre_roll_frames: int = 0
    beginning_clipped: str = "not_applicable"
    raw_capture_duration_seconds: float = 0.0
    finalized_candidate_duration_seconds: float = 0.0
    leading_audio_trimmed_seconds: float = 0.0
    trailing_audio_trimmed_seconds: float = 0.0
    whisper_processing_duration_seconds: float = 0.0
    terminal_silence_status: str = "unknown"
    audio_finalization_started_at: str = ""
    audio_finalization_completed_at: str = ""
    wav_path: str = ""
    wav_byte_size: int = 0
    wav_sample_rate_hz: int = 0
    wav_channels: int = 0
    wav_sample_width_bytes: int = 0
    transcription_backend: str = "whisper.cpp"
    transcription_started_at: str = ""
    transcription_completed_at: str = ""
    transcription_status: str = "not_started"
    transcription_timeout_seconds: float = 0.0
    whisper_process_pid: int = 0
    whisper_process_group_id: int = 0
    whisper_process_exit_code: Optional[int] = None
    whisper_process_elapsed_seconds: float = 0.0
    whisper_process_terminated: bool = False
    whisper_process_killed: bool = False
    whisper_process_reaped: bool = False
    whisper_process_cleanup_completed: bool = False
    whisper_output_handles_closed: bool = False
    transcript_parsing_status: str = "not_started"
    routing_started_at: str = ""
    routing_completed_at: str = ""
    temporary_audio_cleanup_status: str = "unknown"
    microphone_gate_released_before_inference: bool = False
    pipeline_status: str = "not_started"
    runtime_terminal: bool = False
    runtime_terminal_reason: str = "not_terminal"


class VoiceRuntimeGate:
    """Serialized microphone/speaker exclusion with a bounded post-playback settle."""

    def __init__(
        self,
        *,
        settle_delay_seconds: float = 0.35,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(settle_delay_seconds, bool) or not isinstance(
            settle_delay_seconds, (int, float)
        ):
            raise ValueError("settle_delay_seconds must be numeric")
        if not 0.0 <= float(settle_delay_seconds) <= 3.0:
            raise ValueError("settle_delay_seconds must be between 0 and 3")
        self.settle_delay_seconds = float(settle_delay_seconds)
        self.clock = clock
        self.sleeper = sleeper
        self._lock = RLock()
        self._capture_owner = ""
        self._playback_owner = ""
        self._last_playback_completed_at: Optional[float] = None

    def wait_for_capture(self, *, timeout_seconds: float) -> bool:
        timeout = max(0.0, float(timeout_seconds))
        started = self.clock()
        while True:
            with self._lock:
                if self._playback_owner:
                    remaining = timeout - max(0.0, self.clock() - started)
                elif self._last_playback_completed_at is None:
                    return True
                else:
                    settle_remaining = self.settle_delay_seconds - max(
                        0.0, self.clock() - self._last_playback_completed_at
                    )
                    if settle_remaining <= 0:
                        return True
                    remaining = min(settle_remaining, timeout - max(0.0, self.clock() - started))
            if remaining <= 0:
                return False
            self.sleeper(remaining)

    def begin_capture(self, owner: str) -> None:
        clean_owner = str(owner or "capture").strip()[:64]
        with self._lock:
            if self._playback_owner:
                raise RuntimeError("speaker_playback_active_during_capture")
            if self._capture_owner and self._capture_owner != clean_owner:
                raise RuntimeError("microphone_capture_already_active")
            self._capture_owner = clean_owner

    def end_capture(self, owner: str = "") -> None:
        with self._lock:
            if not owner or self._capture_owner == str(owner)[:64]:
                self._capture_owner = ""

    def begin_playback(self, owner: str) -> None:
        clean_owner = str(owner or "playback").strip()[:64]
        with self._lock:
            if self._capture_owner:
                raise RuntimeError("microphone_capture_active_during_playback")
            if self._playback_owner and self._playback_owner != clean_owner:
                raise RuntimeError("speaker_playback_already_active")
            self._playback_owner = clean_owner

    def end_playback(self, owner: str = "") -> None:
        with self._lock:
            if not owner or self._playback_owner == str(owner)[:64]:
                self._playback_owner = ""
                self._last_playback_completed_at = self.clock()

    def reset(self) -> None:
        with self._lock:
            self._capture_owner = ""
            self._playback_owner = ""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "capture_active": bool(self._capture_owner),
                "capture_owner": self._capture_owner,
                "playback_active": bool(self._playback_owner),
                "playback_owner": self._playback_owner,
                "settle_delay_seconds": self.settle_delay_seconds,
                "settling": (
                    self._last_playback_completed_at is not None
                    and max(0.0, self.clock() - self._last_playback_completed_at)
                    < self.settle_delay_seconds
                ),
                "safe": True,
            }


class SingleTurnPipelineRuntimeInputAdapter:
    """Uses SingleTurnVoicePipeline only for one active-session capture/transcription."""

    def __init__(
        self,
        *,
        pipeline: Any,
        base_request: SingleTurnVoiceRequestV1,
        session_id_provider: Callable[[], str],
        lifecycle_state_provider: Optional[Callable[[], str]] = None,
        voice_io_gate: Optional[VoiceRuntimeGate] = None,
        active_lifecycle_audio_controller: Optional[
            ActiveLifecycleAudioTurnController
        ] = None,
        finalized_audio_hook: Optional[FinalizedAudioHook] = None,
        diagnostic_callback: Optional[
            Callable[[ActiveCommandLocalDiagnostics], None]
        ] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not callable(getattr(pipeline, "run_once", None)):
            raise ValueError("pipeline must support run_once")
        if not callable(session_id_provider):
            raise ValueError("session_id_provider must be callable")
        if lifecycle_state_provider is not None and not callable(lifecycle_state_provider):
            raise ValueError("lifecycle_state_provider must be callable")
        if (
            active_lifecycle_audio_controller is not None
            and finalized_audio_hook is not None
        ):
            raise ValueError(
                "provide either active_lifecycle_audio_controller or finalized_audio_hook"
            )
        selected_audio_hook = (
            active_lifecycle_audio_controller or finalized_audio_hook
        )
        if selected_audio_hook is not None and not callable(selected_audio_hook):
            raise ValueError("finalized_audio_hook must be callable")
        self.pipeline = pipeline
        self.base_request = base_request
        self.session_id_provider = session_id_provider
        self.lifecycle_state_provider = lifecycle_state_provider or (lambda: "")
        self.voice_io_gate = voice_io_gate or VoiceRuntimeGate(settle_delay_seconds=0.0)
        self.active_lifecycle_audio_controller = active_lifecycle_audio_controller
        self.finalized_audio_hook = selected_audio_hook
        self.diagnostic_callback = diagnostic_callback
        self.status_callback = status_callback
        self._lock = RLock()
        self._current_token: Optional[CancellationToken] = None
        self._closed = False
        self.last_result: Any = None
        self.last_diagnostics: Optional[ActiveCommandLocalDiagnostics] = None
        self.capture_count = 0
        self._routing_started_at = ""

    def wait_for_input(self, timeout_seconds: float) -> RuntimeInputResult:
        with self._lock:
            if self._closed:
                return RuntimeInputResult(
                    status="end_of_input",
                    metadata={
                        "safe": True,
                        "source": "single_turn_voice_pipeline",
                        "runtime_terminal": False,
                        "input_scope": "active_command",
                    },
                )
            self.last_diagnostics = None
            self._routing_started_at = ""
        lifecycle_state_before = _provided_text(self.lifecycle_state_provider)
        session_id_before = _provided_text(self.session_id_provider)
        emitted_statuses: set[str] = set()

        def emit_once(message: str) -> None:
            if message in emitted_statuses:
                return
            emitted_statuses.add(message)
            self._emit_status(message)

        if not self.voice_io_gate.wait_for_capture(timeout_seconds=max(0.0, float(timeout_seconds))):
            return RuntimeInputResult.timeout()
        correlation = new_correlation_id("runtime-voice-command")
        output_path = _unique_runtime_input_path(self.base_request.recording_output_path)
        capture_base = active_command_capture_request(self.base_request)
        request = replace(
            capture_base,
            correlation_id=correlation,
            session_id=session_id_before,
            recording_output_path=str(output_path),
            text_input="",
            playback_enabled=False,
            metadata={
                **dict(capture_base.metadata or {}),
                "source": "brain_runtime_active_voice_input",
                "runtime_transport_only": True,
            },
        )
        token = CancellationToken(task_id=f"runtime-voice-input:{correlation}")
        captured: dict[str, str] = {}

        def transport_intercept(text: str) -> SingleTurnPreBrainDecision:
            captured["text"] = str(text or "").strip()
            return SingleTurnPreBrainDecision(
                handled=True,
                status="runtime_transport_captured",
                continue_to_output=False,
                data={"transport_only": True},
            )

        def lifecycle_intercept(raw_text: str) -> SingleTurnPreBrainDecision:
            # Active capture is intentionally classified with ACTIVE semantics.
            # Wake activation is a STANDBY concern; here a name-only phrase is
            # attention-only and optional edge addressing is removed before
            # exact lifecycle matching.
            lifecycle = normalize_active_lifecycle_command(raw_text)
            if lifecycle.action == LIFECYCLE_ACTION_NONE:
                return SingleTurnPreBrainDecision(
                    handled=False,
                    status="not_lifecycle_command",
                    data={
                        "transport_only": True,
                        "lifecycle_action": lifecycle.action,
                    },
                )
            captured["text"] = str(raw_text or "").strip()
            return SingleTurnPreBrainDecision(
                handled=True,
                status="runtime_lifecycle_transport_captured",
                continue_to_output=False,
                data={
                    "transport_only": True,
                    "lifecycle_action": lifecycle.action,
                    "lifecycle_normalized_transcript": (
                        lifecycle.canonicalized_transcript
                    ),
                    "canonical_name": lifecycle.canonical_name,
                },
            )

        with self._lock:
            self._current_token = token
            self.capture_count += 1
        unsubscribe: Optional[Callable[[], None]] = None
        unsubscribe_capture_ready: Optional[Callable[[], None]] = None
        capture_gate_owned = False
        capture_ready_details: dict[str, Any] = {}

        add_capture_ready_observer = getattr(
            self.pipeline,
            "add_capture_ready_observer",
            None,
        )
        capture_ready_observable = callable(add_capture_ready_observer)

        def observe_capture_ready(details: dict[str, Any]) -> None:
            capture_ready_details.update(dict(details or {}))
            emit_once("ARES is waiting for your command...")
            emit_once("Active microphone capture started")

        def observe_stage(_index: int, _total: int, label: str, status: str) -> None:
            nonlocal capture_gate_owned
            normalized_label = str(label or "").strip().casefold()
            normalized_status = str(status or "").strip().casefold()
            if normalized_label == "recording" and normalized_status == "running":
                # Test/fallback pipelines without the frame-safe callback still
                # announce at the closest available stage.  The production
                # pipeline announces only after ALSA is open and calibration is
                # complete through observe_capture_ready above.
                if not capture_ready_observable:
                    observe_capture_ready(
                        {"capture_start_reason": "recording_stage_ready"}
                    )
            elif normalized_label == "recording" and normalized_status == "completed":
                if capture_gate_owned:
                    self.voice_io_gate.end_capture("active_command")
                    capture_gate_owned = False
                emit_once("Speech detected")
                emit_once("Command captured")
            elif normalized_label == "transcribing" and normalized_status == "running":
                emit_once("Transcribing command")

        add_observer = getattr(self.pipeline, "add_stage_observer", None)
        if callable(add_observer):
            unsubscribe = add_observer(observe_stage)
        if capture_ready_observable:
            unsubscribe_capture_ready = add_capture_ready_observer(
                observe_capture_ready
            )
        try:
            self.voice_io_gate.begin_capture("active_command")
            capture_gate_owned = True
            run_arguments: dict[str, Any] = {
                "cancellation_token": token,
                "pre_brain_hook": transport_intercept,
                "raw_transcript_hook": lifecycle_intercept,
            }
            if self.finalized_audio_hook is not None:
                run_arguments["finalized_audio_hook"] = self.finalized_audio_hook
            result = self.pipeline.run_once(request, **run_arguments)
            self.last_result = result
        except KeyboardInterrupt:
            token.cancel("keyboard_interrupt")
            self.last_diagnostics = ActiveCommandLocalDiagnostics(
                lifecycle_state_before=lifecycle_state_before,
                lifecycle_state_after=_provided_text(self.lifecycle_state_provider),
                session_id_before=session_id_before,
                session_id_after=_provided_text(self.session_id_provider),
                pipeline_status="cancelled",
                runtime_terminal=False,
                runtime_terminal_reason="not_terminal",
            )
            self._emit_local_diagnostics()
            return RuntimeInputResult.cancelled()
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            self.last_diagnostics = ActiveCommandLocalDiagnostics(
                lifecycle_state_before=lifecycle_state_before,
                lifecycle_state_after=_provided_text(self.lifecycle_state_provider),
                session_id_before=session_id_before,
                session_id_after=_provided_text(self.session_id_provider),
                pipeline_status="pipeline_exception",
                runtime_terminal=False,
                runtime_terminal_reason="not_terminal",
            )
            self._emit_local_diagnostics()
            return RuntimeInputResult.failed(
                "active_voice_pipeline_exception",
                f"{error.__class__.__name__}:{str(error)[:120]}",
            )
        finally:
            if unsubscribe is not None:
                unsubscribe()
            if unsubscribe_capture_ready is not None:
                unsubscribe_capture_ready()
            if capture_gate_owned:
                self.voice_io_gate.end_capture("active_command")
            with self._lock:
                self._current_token = None
        status = str(getattr(result, "status", "") or "")
        lifecycle_audio_metadata = _active_lifecycle_runtime_metadata(result)
        finalized_audio_decision = _finalized_audio_decision_contract(result)
        capture_diagnostics = _active_command_diagnostics(result)
        self.last_diagnostics = replace(
            capture_diagnostics,
            audio_capture_start_reason=(
                capture_diagnostics.audio_capture_start_reason
                or str(capture_ready_details.get("capture_start_reason") or "")
            ),
            lifecycle_state_before=lifecycle_state_before,
            lifecycle_state_after=_provided_text(self.lifecycle_state_provider),
            session_id_before=session_id_before,
            session_id_after=_provided_text(self.session_id_provider),
        )
        if status == "cancelled" or str(getattr(result, "error_stage", "")) == "cancellation":
            self._emit_local_diagnostics()
            return RuntimeInputResult.cancelled()
        text = captured.get("text") or str(getattr(result, "recognized_text", "") or "").strip()
        if finalized_audio_decision.get("handled") and bool(
            getattr(result, "success", False)
        ):
            emit_once("Speech detected")
            emit_once("Command captured")
            if text:
                emit_once("Processing command")
                self._routing_started_at = utc_contract_timestamp()
                self.last_diagnostics = replace(
                    self.last_diagnostics,
                    routing_started_at=self._routing_started_at,
                )
            else:
                self._emit_local_diagnostics()
            return RuntimeInputResult(
                status="input",
                text=text,
                metadata={
                    "safe": True,
                    "source": "single_turn_voice_pipeline",
                    "recognized_length": len(text),
                    "capture_stop_reason": _capture_stop_reason(result),
                    "runtime_terminal": False,
                    "contains_audio": False,
                    **lifecycle_audio_metadata,
                },
            )
        if text and bool(getattr(result, "success", False)):
            emit_once("Speech detected")
            emit_once("Command captured")
            emit_once("Transcribing command")
            emit_once("Processing command")
            self._routing_started_at = utc_contract_timestamp()
            self.last_diagnostics = replace(
                self.last_diagnostics,
                routing_started_at=self._routing_started_at,
            )
            return RuntimeInputResult(
                status="input",
                text=text,
                metadata={
                    "safe": True,
                    "source": "single_turn_voice_pipeline",
                    "recognized_length": len(text),
                    "capture_stop_reason": _capture_stop_reason(result),
                    "raw_capture_duration_seconds": (
                        self.last_diagnostics.raw_capture_duration_seconds
                    ),
                    "finalized_candidate_duration_seconds": (
                        self.last_diagnostics.finalized_candidate_duration_seconds
                    ),
                    "whisper_processing_duration_seconds": (
                        self.last_diagnostics.whisper_processing_duration_seconds
                    ),
                    "contains_audio": False,
                    **lifecycle_audio_metadata,
                },
            )
        if not text and bool(getattr(result, "success", False)):
            emit_once("No command heard; still active")
            self._emit_local_diagnostics()
            return RuntimeInputResult(
                status="timeout",
                metadata={
                    "safe": True,
                    "source": "single_turn_voice_pipeline",
                    "capture_status": status or "empty_transcript",
                    "runtime_terminal": False,
                    "contains_audio": False,
                    **lifecycle_audio_metadata,
                },
            )
        if status in _INPUT_TIMEOUT_STATUSES or str(getattr(result, "error_stage", "")) in {
            "recording_validation",
            "transcription",
            "transcript_normalization",
        }:
            error_reason = str(
                getattr(result, "error_reason", "") or status or "transcription_failed"
            )
            if str(getattr(result, "error_stage", "")) == "transcription":
                if "timeout" in error_reason.casefold():
                    timeout = float(
                        getattr(self.base_request, "transcription_timeout_seconds", 0.0)
                        or 0.0
                    )
                    emit_once(
                        "Command transcription timeout handled"
                        + (f" after {timeout:g} seconds" if timeout else "")
                        + "; ARES remains active"
                    )
                    gate_released = not bool(
                        self.voice_io_gate.snapshot().get("capture_active")
                    )
                    emit_once(
                        "Microphone gate released: "
                        + ("yes" if gate_released else "no")
                    )
                    emit_once(
                        "Temporary audio cleanup: "
                        + self.last_diagnostics.temporary_audio_cleanup_status
                    )
                    emit_once("ARES: I could not transcribe that. Please try again.")
                else:
                    emit_once(
                        f"Command transcription failed: {error_reason[:120]}; still active"
                    )
            emit_once("No command heard; still active")
            self._emit_local_diagnostics()
            return RuntimeInputResult(
                status="timeout",
                metadata={
                    "safe": True,
                    "source": "single_turn_voice_pipeline",
                    "capture_status": status,
                    "transcription_failure_type": (
                        "transcription_timeout"
                        if status == "transcription_timeout"
                        or "timeout" in error_reason.casefold()
                        else "transcription_failed"
                    ),
                    "retryable": True,
                    "microphone_gate_released": not bool(
                        self.voice_io_gate.snapshot().get("capture_active")
                    ),
                    "temporary_audio_cleanup_status": (
                        self.last_diagnostics.temporary_audio_cleanup_status
                    ),
                    "runtime_terminal": False,
                    "contains_audio": False,
                    **lifecycle_audio_metadata,
                },
            )
        self._emit_local_diagnostics()
        return RuntimeInputResult(
            status="failed",
            error_code="active_voice_pipeline_failed",
            error_message=str(
                getattr(result, "error_reason", "")
                or status
                or "voice input failed"
            )[:160],
            metadata=lifecycle_audio_metadata,
        )

    def _emit_status(self, message: str) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(str(message))
        except (OSError, RuntimeError, TypeError, ValueError):
            return

    def _emit_local_diagnostics(self) -> None:
        if self.diagnostic_callback is None or self.last_diagnostics is None:
            return
        try:
            self.diagnostic_callback(self.last_diagnostics)
        except (OSError, RuntimeError, TypeError, ValueError):
            return

    def record_runtime_result(
        self,
        *,
        runtime_result: Any,
        lifecycle_state_before: str,
        session_id_before: str,
    ) -> None:
        """Complete local diagnostics after Capital/Core classification."""

        with self._lock:
            diagnostics = self.last_diagnostics
        if diagnostics is None:
            return
        category = str(getattr(runtime_result, "command_category", "") or "ordinary")
        data = dict(getattr(runtime_result, "data", {}) or {})
        lifecycle_data = dict(
            data.get("lifecycle_command")
            or data.get("lifecycle_normalization")
            or {}
        )
        action = str(
            lifecycle_data.get("action")
            or data.get("lifecycle_action")
            or (
                category
                if category
                in {"activation", "attention_only", "standby", "shutdown"}
                else "none"
            )
        )
        lifecycle_normalized = str(
            lifecycle_data["normalized_transcript"]
            if "normalized_transcript" in lifecycle_data
            else diagnostics.cleaned_transcript
        )
        lifecycle_canonicalized = str(
            lifecycle_data["canonicalized_transcript"]
            if "canonicalized_transcript" in lifecycle_data
            else getattr(runtime_result, "normalized_input", "")
            or lifecycle_normalized
        )
        lifecycle_cleaned = str(
            lifecycle_data["cleaned_transcript"]
            if "cleaned_transcript" in lifecycle_data
            else diagnostics.cleaned_transcript
        )
        canonical_name = str(lifecycle_data.get("canonical_name") or "")
        if (
            not canonical_name
            and action in {"activation", "attention_only", "standby", "shutdown"}
            and "ares" in lifecycle_canonicalized.split()
        ):
            canonical_name = "ares"
        current_state = str(
            getattr(runtime_result, "current_lifecycle_state", "") or ""
        )
        runtime_terminal = current_state == "STOPPED"
        terminal_reason = _diagnostic_terminal_reason(
            str(getattr(runtime_result, "stop_reason", "") or ""),
            runtime_terminal=runtime_terminal,
        )
        completed = replace(
            diagnostics,
            cleaned_transcript=lifecycle_cleaned,
            alias_canonicalized_transcript=lifecycle_canonicalized,
            lifecycle_normalized_transcript=lifecycle_normalized,
            matched_assistant_alias=str(
                lifecycle_data.get("matched_alias") or ""
            ),
            assistant_alias_type=str(
                lifecycle_data.get("alias_type") or ""
            ),
            assistant_alias_removed=str(
                lifecycle_data.get("assistant_alias_removed") or ""
            ),
            alias_position=str(
                lifecycle_data.get("alias_position") or "none"
            ),
            canonical_name=canonical_name,
            negation_detected=bool(
                lifecycle_data.get("negation_detected", False)
            ),
            lifecycle_classification=category,
            selected_lifecycle_action=action,
            matched_lifecycle_phrase=str(
                lifecycle_data.get("matched_phrase") or ""
            ),
            lifecycle_rejection_reason=str(
                lifecycle_data.get("rejection_reason") or ""
            ),
            core_service_bypassed=bool(
                data.get("core_service_bypassed")
                or category
                in {"activation", "attention_only", "standby", "shutdown"}
            ),
            activation_handler_called=category == "activation",
            lifecycle_state_before=str(lifecycle_state_before or ""),
            lifecycle_state_after=current_state,
            session_id_before=str(session_id_before or ""),
            session_id_after=str(getattr(runtime_result, "session_id", "") or ""),
            routing_started_at=(
                diagnostics.routing_started_at or self._routing_started_at
            ),
            routing_completed_at=utc_contract_timestamp(),
            runtime_terminal=runtime_terminal,
            runtime_terminal_reason=terminal_reason,
        )
        with self._lock:
            self.last_diagnostics = completed
        self._emit_local_diagnostics()

    def release_active_resources(self) -> None:
        with self._lock:
            token = self._current_token
        if token is not None:
            token.cancel("returning_to_standby")
        stop = getattr(self.pipeline, "stop", None)
        if callable(stop):
            try:
                stop(self.base_request)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        self.voice_io_gate.end_capture()
        if self.active_lifecycle_audio_controller is not None:
            self.active_lifecycle_audio_controller.reset(
                "active_transport_resources_released"
            )

    def cancel_pending_lifecycle_confirmation(
        self,
        reason: str = "runtime_cancelled_confirmation",
    ) -> None:
        controller = self.active_lifecycle_audio_controller
        if controller is not None:
            controller.reset(reason)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.release_active_resources()
        if self.active_lifecycle_audio_controller is not None:
            self.active_lifecycle_audio_controller.close()


class SingleTurnPipelineRuntimeOutputAdapter:
    """Speaks runtime output through the verified local TTS/speaker pipeline."""

    def __init__(
        self,
        *,
        pipeline: Any,
        base_request: SingleTurnVoiceRequestV1,
        voice_io_gate: Optional[VoiceRuntimeGate] = None,
        output_func: Optional[Callable[[str], None]] = print,
    ) -> None:
        if not callable(getattr(pipeline, "run_local_output", None)):
            raise ValueError("pipeline must support run_local_output")
        self.pipeline = pipeline
        self.base_request = base_request
        self.voice_io_gate = voice_io_gate or VoiceRuntimeGate(settle_delay_seconds=0.0)
        self.output_func = output_func
        self._lock = RLock()
        self._closed = False
        self.last_result: Any = None
        self.playback_count = 0

    def write(self, message: RuntimeOutputMessage) -> RuntimeOutputResult:
        if not isinstance(message, RuntimeOutputMessage):
            return RuntimeOutputResult(False, "malformed_output", "malformed_output", "invalid message")
        with self._lock:
            if self._closed:
                return RuntimeOutputResult(False, "output_closed", "output_closed", "voice output is closed")
        request = replace(
            self.base_request,
            correlation_id=message.correlation_id or new_correlation_id("runtime-voice-output"),
            session_id=message.session_id,
            recording_output_path=str(_unique_runtime_output_path(self.base_request.recording_output_path)),
            text_input=message.text,
            playback_enabled=True,
            cleanup_policy="delete_on_success",
            diagnostic_audio=False,
            metadata={
                **dict(self.base_request.metadata or {}),
                "source": "brain_runtime_voice_output",
                "output_category": message.category,
                "captured_audio_playback": False,
            },
        )
        token = CancellationToken(
            task_id=f"runtime-voice-output:{message.correlation_id or 'response'}"
        )
        try:
            self.voice_io_gate.begin_playback("runtime_response")
            result = self.pipeline.run_local_output(request, message.text, cancellation_token=token)
            self.last_result = result
            self.playback_count += 1
        except KeyboardInterrupt:
            token.cancel("keyboard_interrupt")
            return RuntimeOutputResult(False, "cancelled", "output_cancelled", "voice output cancelled")
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            return RuntimeOutputResult(
                False,
                "output_failed",
                "voice_output_exception",
                f"{error.__class__.__name__}:{str(error)[:120]}",
            )
        finally:
            self.voice_io_gate.end_playback("runtime_response")
        if self.output_func is not None:
            try:
                self.output_func(message.text)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        if bool(getattr(result, "success", False)):
            return RuntimeOutputResult(True, "spoken")
        return RuntimeOutputResult(
            False,
            "output_failed",
            "voice_pipeline_output_failed",
            str(getattr(result, "error_reason", "") or getattr(result, "status", "") or "voice output failed")[:160],
        )

    def release_active_resources(self) -> None:
        stop = getattr(self.pipeline, "stop", None)
        if callable(stop):
            try:
                stop(self.base_request)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        self.voice_io_gate.end_playback()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.release_active_resources()


def _unique_runtime_input_path(base_path: str) -> Path:
    base = Path(base_path).expanduser()
    return base.parent / f"runtime-command-{uuid4().hex}.wav"


def _unique_runtime_output_path(base_path: str) -> Path:
    base = Path(base_path).expanduser()
    return base.parent / f"runtime-response-{uuid4().hex}.wav"


def _capture_stop_reason(result: Any) -> str:
    data = dict(getattr(result, "data", {}) or {})
    recording = dict(data.get("recording") or {})
    return str(
        recording.get("stop_reason")
        or data.get("capture_stop_reason")
        or getattr(result, "recording_status", "")
        or ""
    )[:80]


def _finalized_audio_decision_contract(result: Any) -> dict[str, Any]:
    data = dict(getattr(result, "data", {}) or {})
    value = data.get("finalized_audio_decision")
    return dict(value) if isinstance(value, dict) else {}


def _active_lifecycle_runtime_metadata(result: Any) -> dict[str, Any]:
    """Flatten private recognition evidence through RuntimeInputResult's safe boundary."""

    decision = _finalized_audio_decision_contract(result)
    decision_data = decision.get("data")
    if not isinstance(decision_data, dict):
        return {}
    payload = decision_data.get("active_lifecycle_audio")
    if not isinstance(payload, dict):
        return {}
    status = str(decision.get("status") or "")
    authorized = bool(payload.get("lifecycle_authorized", False))
    action = str(
        payload.get("selected_lifecycle_action") if authorized else ""
    )
    classification = str(payload.get("classification") or "")
    proposed = str(payload.get("proposed_classification") or "")
    control = ""
    prompt = ""
    if status == "active_lifecycle_audio_confirmation_required":
        control = "confirmation_required"
        proposed_action = proposed or classification
        prompt = (
            "Did you say shutdown Ares?"
            if proposed_action == ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN
            else "Did you say goodbye Ares?"
        )
    elif status == "active_lifecycle_audio_confirmation_cancelled":
        control = "confirmation_cancelled"
    recognized_text = str(payload.get("recognized_text") or "")[:160]
    token_values = payload.get("recognized_tokens") or []
    if not isinstance(token_values, (list, tuple)):
        token_values = []
    recognized_tokens = " ".join(
        str(token or "").strip() for token in token_values if str(token or "").strip()
    )[:160]
    metadata: dict[str, Any] = {
        "active_lifecycle_audio_checked": bool(payload.get("audio_checked", True)),
        "active_lifecycle_audio_authorized": authorized,
        "active_lifecycle_audio_authorized_action": action,
        "active_lifecycle_classification": classification,
        "active_lifecycle_canonical_phrase": str(
            payload.get("canonical_phrase") or ""
        )[:160],
        "active_lifecycle_recognized_text": recognized_text,
        "active_lifecycle_recognized_tokens": recognized_tokens,
        "active_lifecycle_recognition_backend": str(
            payload.get("recognition_backend") or ""
        )[:160],
        "active_lifecycle_rejection_reason": str(
            payload.get("rejection_reason") or ""
        )[:160],
        "active_lifecycle_confidence_tier": str(
            payload.get("confidence_tier") or ""
        )[:160],
        "active_lifecycle_proposed_action": proposed,
        "active_lifecycle_proposed_classification": proposed,
        "active_lifecycle_control": control,
        "active_lifecycle_confirmation_prompt": prompt,
        "active_lifecycle_owner_activity": bool(
            recognized_text or recognized_tokens
        ),
        "active_lifecycle_whisper_fallback": bool(
            payload.get("whisper_fallback_required", False)
        ),
        "active_lifecycle_decision_status": status,
        "active_lifecycle_confirmation_disposition": str(
            payload.get("confirmation_disposition") or ""
        )[:160],
        "active_lifecycle_pending_clear_reason": str(
            payload.get("pending_clear_reason") or ""
        )[:160],
    }
    confidence = _finite_lifecycle_confidence(payload.get("confidence"))
    if confidence is not None:
        metadata["active_lifecycle_confidence"] = confidence
    return metadata


def _active_command_diagnostics(result: Any) -> ActiveCommandLocalDiagnostics:
    data = dict(getattr(result, "data", {}) or {})
    recording = dict(data.get("recording") or {})
    recording_data = dict(recording.get("data") or {})
    audio_finalization = dict(data.get("audio_finalization") or {})
    transcription_contract = dict(data.get("transcription") or {})
    transcription = dict(transcription_contract.get("data") or {})
    process = dict(transcription.get("process") or {})
    process_metadata = dict(process.get("metadata") or {})
    transcription_boundary = dict(data.get("transcription_boundary") or {})
    cleanup = dict(data.get("cleanup") or {})
    stop_reason = _capture_stop_reason(result)
    raw_duration = float(recording.get("raw_duration_seconds", 0.0) or 0.0)
    candidate_duration = float(
        recording.get("normalized_duration_seconds", 0.0)
        or recording.get("assembled_duration_seconds", 0.0)
        or getattr(result, "recording_duration_seconds", 0.0)
        or 0.0
    )
    wav_path = str(
        audio_finalization.get("wav_path")
        or transcription.get("audio_path")
        or getattr(result, "recorded_wav_path", "")
        or ""
    )
    removed = {str(path) for path in list(cleanup.get("removed") or [])}
    preserved = {str(path) for path in list(cleanup.get("preserved") or [])}
    cleanup_status = (
        "removed"
        if wav_path and wav_path in removed
        else "preserved"
        if wav_path and wav_path in preserved
        else "not_applicable"
        if not wav_path
        else "missing_after_cleanup"
        if not Path(wav_path).exists()
        else "present"
    )
    return ActiveCommandLocalDiagnostics(
        raw_transcript=str(getattr(result, "raw_transcript", "") or ""),
        cleaned_transcript=str(
            getattr(result, "cleaned_transcript", "")
            or getattr(result, "recognized_text", "")
            or ""
        ),
        capture_stop_reason=stop_reason,
        audio_capture_start_reason=str(
            recording_data.get("capture_start_reason") or ""
        ),
        first_speech_frame=int(
            recording.get("first_speech_frame")
            or recording_data.get("first_speech_frame")
            or 0
        ),
        last_speech_frame=int(
            recording.get("last_speech_frame")
            or recording_data.get("last_speech_frame")
            or 0
        ),
        pre_roll_frames_retained=int(
            recording.get("pre_roll_frames_retained")
            or recording_data.get("pre_roll_frames_retained")
            or 0
        ),
        expected_pre_roll_frames=int(
            recording_data.get("expected_pre_roll_frames")
            or recording_data.get("pre_roll_frames")
            or 0
        ),
        beginning_clipped=str(
            recording_data.get("beginning_clipped") or "not_applicable"
        ),
        raw_capture_duration_seconds=raw_duration,
        finalized_candidate_duration_seconds=candidate_duration,
        leading_audio_trimmed_seconds=float(
            recording_data.get("leading_audio_trimmed_seconds")
            or recording.get("leading_silence_trimmed_seconds")
            or 0.0
        ),
        trailing_audio_trimmed_seconds=float(
            recording_data.get("trailing_audio_trimmed_seconds")
            or recording.get("trailing_silence_trimmed_seconds")
            or 0.0
        ),
        whisper_processing_duration_seconds=float(
            getattr(result, "transcription_processing_time_seconds", 0.0) or 0.0
        ),
        terminal_silence_status=(
            "confirmed_terminal_silence"
            if stop_reason == "completed_after_silence"
            else "maximum_duration_before_terminal_silence"
            if stop_reason == "maximum_duration_reached"
            else stop_reason or "unknown"
        ),
        audio_finalization_started_at=str(
            audio_finalization.get("started_at") or ""
        ),
        audio_finalization_completed_at=str(
            audio_finalization.get("completed_at") or ""
        ),
        wav_path=wav_path,
        wav_byte_size=int(audio_finalization.get("wav_byte_size", 0) or 0),
        wav_sample_rate_hz=int(
            audio_finalization.get("sample_rate_hz", 0) or 0
        ),
        wav_channels=int(audio_finalization.get("channels", 0) or 0),
        wav_sample_width_bytes=int(
            audio_finalization.get("sample_width_bytes", 0) or 0
        ),
        transcription_backend=str(
            transcription.get("transcription_backend") or "whisper.cpp"
        ),
        transcription_started_at=str(
            transcription.get("transcription_started_at")
            or transcription_boundary.get("started_at")
            or ""
        ),
        transcription_completed_at=str(
            transcription.get("transcription_completed_at")
            or transcription_boundary.get("completed_at")
            or ""
        ),
        transcription_status=str(
            transcription_contract.get("status")
            or getattr(result, "transcription_status", "")
            or "not_started"
        ),
        transcription_timeout_seconds=float(
            transcription.get("transcription_timeout_seconds")
            or transcription_boundary.get("timeout_seconds")
            or 0.0
        ),
        whisper_process_pid=int(process_metadata.get("pid", 0) or 0),
        whisper_process_group_id=int(process_metadata.get("pgid", 0) or 0),
        whisper_process_exit_code=(
            int(process.get("returncode"))
            if process.get("returncode") is not None
            else None
        ),
        whisper_process_elapsed_seconds=float(
            process_metadata.get("elapsed_seconds", 0.0) or 0.0
        ),
        whisper_process_terminated=bool(process_metadata.get("terminated")),
        whisper_process_killed=bool(process_metadata.get("killed")),
        whisper_process_reaped=bool(process_metadata.get("reaped")),
        whisper_process_cleanup_completed=bool(
            process_metadata.get("cleanup_completed")
        ),
        whisper_output_handles_closed=bool(
            process_metadata.get("output_handles_closed")
        ),
        transcript_parsing_status=str(
            transcription.get("transcript_parsing_status") or "not_started"
        ),
        temporary_audio_cleanup_status=cleanup_status,
        microphone_gate_released_before_inference=bool(
            transcription_boundary.get("microphone_capture_released")
        ),
        pipeline_status=str(getattr(result, "status", "") or "unknown"),
    )


def _provided_text(provider: Callable[[], str]) -> str:
    try:
        return str(provider() or "")
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""


def _diagnostic_terminal_reason(reason: str, *, runtime_terminal: bool) -> str:
    clean = str(reason or "").strip()
    if not runtime_terminal:
        return "not_terminal"
    if clean in {"explicit_shutdown_command", "owner_shutdown_phrase"}:
        return "explicit_shutdown_command"
    if clean in {"input_cancelled", "owner_cancellation"}:
        return "owner_cancellation"
    return clean or "unrecoverable_failure"
