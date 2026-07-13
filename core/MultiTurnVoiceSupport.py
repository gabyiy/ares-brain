from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from typing import Any, Callable, Dict, List, Optional

from core.Contracts import (
    CONTRACT_MULTI_TURN_VOICE_SESSION_REQUEST,
    MultiTurnVoiceSessionRequestV1,
    MultiTurnVoiceSessionResultV1,
    new_correlation_id,
    utc_contract_timestamp,
    validate_contract,
)
from core.SingleTurnVoiceSupport import PIPELINE_CLEANUP_POLICIES


SESSION_CREATED = "created"
SESSION_STARTING = "starting"
SESSION_GREETING = "greeting"
SESSION_LISTENING = "listening"
SESSION_TRANSCRIBING = "transcribing"
SESSION_CHECKING_STOP_PHRASE = "checking_stop_phrase"
SESSION_PROCESSING = "processing"
SESSION_SYNTHESIZING = "synthesizing"
SESSION_SPEAKING = "speaking"
SESSION_WAITING = "waiting_between_turns"
SESSION_STOPPING = "stopping"
SESSION_COMPLETED = "completed"
SESSION_FAILED = "failed"
SESSION_CANCELLED = "cancelled"

SESSION_STATES = {
    SESSION_CREATED,
    SESSION_STARTING,
    SESSION_GREETING,
    SESSION_LISTENING,
    SESSION_TRANSCRIBING,
    SESSION_CHECKING_STOP_PHRASE,
    SESSION_PROCESSING,
    SESSION_SYNTHESIZING,
    SESSION_SPEAKING,
    SESSION_WAITING,
    SESSION_STOPPING,
    SESSION_COMPLETED,
    SESSION_FAILED,
    SESSION_CANCELLED,
}

DEFAULT_CONVERSATION_STOP_PHRASES = (
    "stop listening",
    "stop conversation",
    "end conversation",
    "goodbye Ares",
    "goodbye",
    "that is all",
    "exit conversation",
)

_ALLOWED_TRANSITIONS = {
    SESSION_CREATED: {SESSION_STARTING},
    SESSION_STARTING: {
        SESSION_GREETING,
        SESSION_LISTENING,
        SESSION_SPEAKING,
        SESSION_STOPPING,
    },
    SESSION_GREETING: {
        SESSION_LISTENING,
        SESSION_SPEAKING,
        SESSION_STOPPING,
    },
    SESSION_LISTENING: {
        SESSION_TRANSCRIBING,
        SESSION_CHECKING_STOP_PHRASE,
        SESSION_SPEAKING,
        SESSION_WAITING,
        SESSION_STOPPING,
    },
    SESSION_TRANSCRIBING: {
        SESSION_CHECKING_STOP_PHRASE,
        SESSION_PROCESSING,
        SESSION_SPEAKING,
        SESSION_WAITING,
        SESSION_STOPPING,
    },
    SESSION_CHECKING_STOP_PHRASE: {
        SESSION_PROCESSING,
        SESSION_SPEAKING,
        SESSION_WAITING,
        SESSION_STOPPING,
    },
    SESSION_PROCESSING: {
        SESSION_SYNTHESIZING,
        SESSION_SPEAKING,
        SESSION_WAITING,
        SESSION_STOPPING,
    },
    SESSION_SYNTHESIZING: {
        SESSION_SPEAKING,
        SESSION_WAITING,
        SESSION_STOPPING,
    },
    SESSION_SPEAKING: {SESSION_WAITING, SESSION_STOPPING},
    SESSION_WAITING: {SESSION_LISTENING, SESSION_SPEAKING, SESSION_STOPPING},
    SESSION_STOPPING: {SESSION_COMPLETED, SESSION_FAILED, SESSION_CANCELLED},
    SESSION_COMPLETED: set(),
    SESSION_FAILED: set(),
    SESSION_CANCELLED: set(),
}


class SessionStateTransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionStateTransition:
    from_state: str
    to_state: str
    reason: str
    timestamp: str
    elapsed_seconds: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass
class SessionStateMachine:
    clock: Callable[[], float]
    timestamp_factory: Callable[[], str] = utc_contract_timestamp
    state: str = SESSION_CREATED
    history: List[SessionStateTransition] = field(default_factory=list)
    _started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self._started_at = float(self.clock())
        self.history.append(
            SessionStateTransition(
                from_state="",
                to_state=SESSION_CREATED,
                reason="session_created",
                timestamp=self.timestamp_factory(),
                elapsed_seconds=0.0,
            )
        )

    def transition(self, to_state: str, reason: str = "") -> SessionStateTransition:
        clean_state = str(to_state or "").strip().lower()
        if clean_state not in SESSION_STATES:
            raise SessionStateTransitionError(f"unknown_session_state:{clean_state}")
        if clean_state == self.state:
            return self.history[-1]
        if clean_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise SessionStateTransitionError(
                f"illegal_session_transition:{self.state}:{clean_state}"
            )
        transition = SessionStateTransition(
            from_state=self.state,
            to_state=clean_state,
            reason=str(reason or ""),
            timestamp=self.timestamp_factory(),
            elapsed_seconds=round(max(0.0, float(self.clock()) - self._started_at), 6),
        )
        self.state = clean_state
        self.history.append(transition)
        return transition

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "history": [transition.to_dict() for transition in self.history],
        }


class StopPhraseMatcher:
    def __init__(self, phrases: List[str] | tuple[str, ...]):
        normalized: Dict[str, str] = {}
        for phrase in phrases:
            clean = normalize_stop_phrase(phrase)
            if clean:
                normalized.setdefault(clean, str(phrase).strip())
        if not normalized:
            raise ValueError("at_least_one_stop_phrase_is_required")
        self._phrases = normalized

    def match(self, text: str) -> str:
        return self._phrases.get(normalize_stop_phrase(text), "")

    def phrases(self) -> List[str]:
        return [self._phrases[key] for key in sorted(self._phrases)]


def normalize_stop_phrase(text: str) -> str:
    normalized = re.sub(r"[^\w\s]", " ", str(text or "").casefold().replace("_", " "))
    return " ".join(normalized.split())


def validated_multi_turn_request(
    request: MultiTurnVoiceSessionRequestV1 | Dict[str, Any],
) -> MultiTurnVoiceSessionRequestV1:
    if isinstance(request, dict):
        request = MultiTurnVoiceSessionRequestV1.from_dict(request)
    if not isinstance(request, MultiTurnVoiceSessionRequestV1):
        raise ValueError("multi_turn_voice_session_request_required")
    compatibility = validate_contract(
        request,
        expected_contract_name=CONTRACT_MULTI_TURN_VOICE_SESSION_REQUEST,
    )
    if not compatibility.success:
        raise ValueError(compatibility.error_message or compatibility.status)
    if not 1 <= int(request.maximum_turns) <= 50:
        raise ValueError("maximum_turns_out_of_range")
    if not 1 <= int(request.maximum_consecutive_failures) <= 20:
        raise ValueError("maximum_consecutive_failures_out_of_range")
    if not 1 <= int(request.recording_duration_seconds) <= 60:
        raise ValueError("recording_duration_seconds_out_of_range")
    if not 1.0 <= float(request.maximum_session_duration_seconds) <= 3600.0:
        raise ValueError("maximum_session_duration_seconds_out_of_range")
    if not 1.0 <= float(request.total_session_timeout_seconds) <= 3600.0:
        raise ValueError("total_session_timeout_seconds_out_of_range")
    if not 0.1 <= float(request.per_turn_timeout_seconds) <= 1800.0:
        raise ValueError("per_turn_timeout_seconds_out_of_range")
    if not 0.0 <= float(request.inter_turn_delay_seconds) <= 30.0:
        raise ValueError("inter_turn_delay_seconds_out_of_range")
    if float(request.minimum_rms) < 0:
        raise ValueError("minimum_rms_must_be_non_negative")
    if request.cleanup_policy not in PIPELINE_CLEANUP_POLICIES:
        raise ValueError("invalid_cleanup_policy")
    if request.interactive_text and request.simulated_text_turns:
        raise ValueError("interactive_text_and_simulated_turns_are_mutually_exclusive")
    if request.greeting_enabled and not request.greeting_text.strip():
        raise ValueError("greeting_text_is_required_when_enabled")
    if request.closing_phrase_enabled and not request.closing_phrase_text.strip():
        raise ValueError("closing_phrase_text_is_required_when_enabled")
    if not str(request.language or "").strip():
        raise ValueError("language_is_required")
    if not str(request.recording_output_directory or "").strip():
        raise ValueError("recording_output_directory_is_required")
    if any(len(str(turn)) > 10000 for turn in request.simulated_text_turns):
        raise ValueError("simulated_text_turn_too_large")

    phrases = list(request.stop_phrases or DEFAULT_CONVERSATION_STOP_PHRASES)
    phrases.extend(request.confirmation_stop_phrases or [])
    matcher = StopPhraseMatcher(phrases)
    session_id = request.session_id or new_correlation_id("voice-session")
    return replace(
        request,
        session_id=session_id,
        stop_phrases=matcher.phrases(),
        confirmation_stop_phrases=[],
        simulated_text_turns=[str(turn) for turn in request.simulated_text_turns],
        metadata=dict(request.metadata or {}),
    )


def multi_turn_contract_failure(
    request: Any,
    error_message: str,
) -> MultiTurnVoiceSessionResultV1:
    payload = request if isinstance(request, dict) else getattr(request, "to_dict", lambda: {})()
    return MultiTurnVoiceSessionResultV1(
        success=False,
        status="contract_rejected",
        correlation_id=str(dict(payload or {}).get("correlation_id") or ""),
        session_id=str(dict(payload or {}).get("session_id") or ""),
        final_state=SESSION_FAILED,
        error_stage="contract_validation",
        error_reason=str(error_message or "contract_rejected"),
        resource_cleanup_status="not_started",
        metadata={"safe": True, "source": "multi_turn_voice_session"},
    )


def session_elapsed(clock: Callable[[], float], started_at: float) -> float:
    return round(max(0.0, float(clock()) - float(started_at)), 6)
