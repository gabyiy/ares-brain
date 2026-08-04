from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib
import json
from math import isfinite
import multiprocessing
import os
from pathlib import Path
import re
import select
import signal
import socket
import struct
from threading import Lock, RLock
import time
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import uuid4
import wave

from core.AresIdentity import clean_spoken_phrase, normalize_spoken_phrase
from core.StandbyWakeListener import DEFAULT_WAKE_VOSK_MODEL


ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY = "standby"
ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN = "shutdown"
ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY = "ordinary"
ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN = "uncertain"

ACTIVE_LIFECYCLE_CONFIRMATION_CONFIRMED = "confirmed"
ACTIVE_LIFECYCLE_CONFIRMATION_CANCELLED = "cancelled"
ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED = "unmatched"

VOSK_ACTIVE_LIFECYCLE_BACKEND = "vosk_active_lifecycle_constrained_grammar"
VOSK_LIFECYCLE_WORKER_PROTOCOL = "ares.active_lifecycle_worker v1"

_VOSK_LIFECYCLE_MESSAGE_HEADER_BYTES = 4
_VOSK_LIFECYCLE_MAX_MESSAGE_BYTES = 32 * 1024
_VOSK_LIFECYCLE_WORKER_READY_SEND_SECONDS = 1.0
_VOSK_LIFECYCLE_WORKER_REQUEST_WAIT_SECONDS = 10.0
_VOSK_LIFECYCLE_WORKER_RESULT_SEND_SECONDS = 1.0
_VOSK_LIFECYCLE_WORKER_STOP_WAIT_SECONDS = 1.0

DEFAULT_STANDBY_HIGH_CONFIDENCE = 0.70
DEFAULT_STANDBY_MEDIUM_CONFIDENCE = 0.50
DEFAULT_SHUTDOWN_HIGH_CONFIDENCE = 0.78
DEFAULT_SHUTDOWN_MEDIUM_CONFIDENCE = 0.60
DEFAULT_CONFIRMATION_MINIMUM_CONFIDENCE = 0.80
DEFAULT_CANCELLATION_MINIMUM_CONFIDENCE = 0.50
DEFAULT_ACTIVE_LIFECYCLE_TIMEOUT_SECONDS = 5.0

MINIMUM_SAFE_STANDBY_HIGH_CONFIDENCE = 0.60
MINIMUM_SAFE_STANDBY_MEDIUM_CONFIDENCE = 0.40
MINIMUM_SAFE_SHUTDOWN_HIGH_CONFIDENCE = 0.70
MINIMUM_SAFE_SHUTDOWN_MEDIUM_CONFIDENCE = 0.50
MINIMUM_SAFE_CONFIRMATION_CONFIDENCE = 0.75

CANONICAL_STANDBY_PHRASE = "goodbye ares"
CANONICAL_SHUTDOWN_PHRASE = "shutdown ares"

_ACTIVE_LIFECYCLE_ASSISTANT_SLOT = "{assistant}"

# These forms belong only to constrained ACTIVE lifecycle recognition.  They
# do not expand standby wake aliases or the ordinary transcript alias policy.
# Multi-token ``r s`` remains one bounded assistant-name form.
DEFAULT_ACTIVE_LIFECYCLE_ASSISTANT_ALIAS_FORMS = (
    "ares",
    "aris",
    "aries",
    "arris",
    "rs",
    "r s",
)


@dataclass(frozen=True)
class _ActiveLifecycleCommandTemplate:
    classification: str
    tokens: tuple[str, ...]
    canonicalized_tokens: tuple[str, ...]

    @property
    def alias_position(self) -> str:
        slot = self.tokens.index(_ACTIVE_LIFECYCLE_ASSISTANT_SLOT)
        return "prefix" if slot == 0 else "suffix"


# This is the single structural authority for both the Vosk action grammar and
# post-recognition alias canonicalization. Every template is a complete command
# with exactly one assistant-name slot at an edge. Phrase segmentation variants
# map to one stable transcript before exact lifecycle classification.
_ACTIVE_LIFECYCLE_COMMAND_TEMPLATES = (
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
        ("goodbye", _ACTIVE_LIFECYCLE_ASSISTANT_SLOT),
        ("goodbye", "ares"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
        ("good", "bye", _ACTIVE_LIFECYCLE_ASSISTANT_SLOT),
        ("goodbye", "ares"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
        ("bye", _ACTIVE_LIFECYCLE_ASSISTANT_SLOT),
        ("bye", "ares"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
        ("go", "standby", _ACTIVE_LIFECYCLE_ASSISTANT_SLOT),
        ("go", "standby", "ares"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
        ("go", "to", "standby", _ACTIVE_LIFECYCLE_ASSISTANT_SLOT),
        ("go", "to", "standby", "ares"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
        ("standby", _ACTIVE_LIFECYCLE_ASSISTANT_SLOT),
        ("standby", "ares"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
        ("sleep", _ACTIVE_LIFECYCLE_ASSISTANT_SLOT),
        ("sleep", "ares"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
        ("shutdown", _ACTIVE_LIFECYCLE_ASSISTANT_SLOT),
        ("shutdown", "ares"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
        ("shut", "down", _ACTIVE_LIFECYCLE_ASSISTANT_SLOT),
        ("shutdown", "ares"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
        (_ACTIVE_LIFECYCLE_ASSISTANT_SLOT, "shutdown"),
        ("ares", "shutdown"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
        (_ACTIVE_LIFECYCLE_ASSISTANT_SLOT, "shut", "down"),
        ("ares", "shutdown"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
        ("turn", "off", _ACTIVE_LIFECYCLE_ASSISTANT_SLOT),
        ("turn", "off", "ares"),
    ),
    _ActiveLifecycleCommandTemplate(
        ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
        ("power", "off", _ACTIVE_LIFECYCLE_ASSISTANT_SLOT),
        ("power", "off", "ares"),
    ),
)


def _active_lifecycle_alias_token_forms() -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(normalize_spoken_phrase(alias).split())
        for alias in DEFAULT_ACTIVE_LIFECYCLE_ASSISTANT_ALIAS_FORMS
    )


def _expanded_active_lifecycle_action_grammar(
    classification: str,
) -> tuple[str, ...]:
    phrases: list[str] = []
    for template in _ACTIVE_LIFECYCLE_COMMAND_TEMPLATES:
        if template.classification != classification:
            continue
        slot = template.tokens.index(_ACTIVE_LIFECYCLE_ASSISTANT_SLOT)
        for alias_tokens in _active_lifecycle_alias_token_forms():
            tokens = (
                template.tokens[:slot]
                + alias_tokens
                + template.tokens[slot + 1 :]
            )
            phrase = " ".join(tokens)
            if phrase not in phrases:
                phrases.append(phrase)
    return tuple(phrases)


# Vosk receives the exact expansions required to emit every accepted assistant
# form. The complete command is still canonicalized and matched after inference;
# membership in this grammar alone never authorizes a lifecycle transition.
DEFAULT_ACTIVE_STANDBY_GRAMMAR = _expanded_active_lifecycle_action_grammar(
    ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY
)
DEFAULT_ACTIVE_SHUTDOWN_GRAMMAR = _expanded_active_lifecycle_action_grammar(
    ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN
)

# A closed grammar containing only positive commands can force acoustically
# similar ordinary speech into the nearest lifecycle phrase. These explicit
# negative competitors remain outside ``_LIFECYCLE_PHRASE_POLICY`` and can
# therefore only produce an ordinary result. Keep this list bounded to observed
# safety-critical confusions; it is not an alias list.
DEFAULT_ACTIVE_LIFECYCLE_REJECTION_GRAMMAR = (
    "ares",
    "aris",
    "aries",
    "arris",
    "rs",
    "r s",
    "artist",
    "paris",
    "harris",
    "clears throat",
    "shutdown artist",
    "shut down artist",
    "shutdown paris",
    "shutdown harris",
    "shutdown computer",
    "shut down computer",
    "goodbye artist",
    "goodbye paris",
    "goodbye everyone",
    "go to sleep",
    "turn it off",
    "where is ares",
    "i spoke to aris yesterday",
    "the artist is here",
    "remember that i like ares",
    "calculate two plus two",
    "do not shut down",
    "don't shut down",
    "don't shutdown",
    "do not go to sleep",
    "don't go to sleep",
    "don't say goodbye",
    "do not say goodbye",
    "never shut down",
    "why did you shut down",
    "explain shutdown",
    "schedule a shutdown tomorrow",
    # A compact set of named competitors keeps the total grammar within the
    # backend's 128-phrase bound after the six exact assistant forms expand.
    "do not shutdown ares",
    "do not shut down aris",
    "don't shutdown ares",
    "don't shut down aris",
    "never shutdown ares",
    "never shut down aris",
    "do not shut down ares",
    "don't say goodbye aris",
    "do not sleep aris",
    "don't standby ares",
    "do not go standby ares",
    "never go to sleep ares",
)

# Classification also recognizes the rest of the previously validated bounded
# negative matrix if a backend emits it. Those phrases need not occupy scarce
# Vosk grammar alternatives: `[unk]` remains the closed-grammar rejection slot.
_ACTIVE_LIFECYCLE_REJECTION_POLICY_EXTRAS = (
    "do not shutdown",
    "schedule a shutdown",
    "goodbye harris",
    "do not goodbye ares",
    "never goodbye ares",
    "don't go standby aris",
    "never standby aris",
)

DEFAULT_ACTIVE_CONFIRMATION_GRAMMAR = (
    "yes",
    "yes ares",
    "confirm",
    "confirm standby",
    "confirm shutdown",
    "no",
    "cancel",
    "never mind",
    "continue",
    # Bounded negative competitors keep ordinary/attention speech from being
    # forced into the short affirmative grammar.
    "ares",
    "what",
    "repeat",
    "something else",
)

_GENERIC_CONFIRMATION_PHRASES = frozenset(("yes", "yes ares", "confirm"))
_CANCELLATION_PHRASES = frozenset(("no", "cancel", "never mind", "continue"))


@dataclass(frozen=True)
class LifecycleBackendRecognition:
    """Transcript-free-of-side-effects output from one constrained backend run."""

    recognized_text: str = ""
    recognized_tokens: tuple[str, ...] = ()
    word_confidences: tuple[float, ...] = ()
    recognition_backend: str = VOSK_ACTIVE_LIFECYCLE_BACKEND
    backend_diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActiveLifecycleAssistantAliasCanonicalization:
    """Whole-command lifecycle alias evidence with no routing side effects."""

    normalized_transcript: str
    alias_canonicalized_transcript: str
    alias_detected: str = ""
    alias_position: str = "none"


def canonicalize_active_lifecycle_assistant_alias(
    value: Any,
) -> ActiveLifecycleAssistantAliasCanonicalization:
    """Canonicalize an assistant alias only in one exact lifecycle slot.

    The complete normalized token sequence must equal one supported ACTIVE
    lifecycle template. Ordinary sentences containing an alias are returned
    unchanged, and no substring, fuzzy, phonetic, or edit-distance operation is
    performed.
    """

    normalized = normalize_spoken_phrase(value)
    candidate_tokens = tuple(normalized.split())
    for template in _ACTIVE_LIFECYCLE_COMMAND_TEMPLATES:
        slot = template.tokens.index(_ACTIVE_LIFECYCLE_ASSISTANT_SLOT)
        prefix = template.tokens[:slot]
        suffix = template.tokens[slot + 1 :]
        for alias_tokens in _active_lifecycle_alias_token_forms():
            if candidate_tokens != prefix + alias_tokens + suffix:
                continue
            return ActiveLifecycleAssistantAliasCanonicalization(
                normalized_transcript=normalized,
                alias_canonicalized_transcript=" ".join(
                    template.canonicalized_tokens
                ),
                alias_detected=" ".join(alias_tokens),
                alias_position=template.alias_position,
            )
    return ActiveLifecycleAssistantAliasCanonicalization(
        normalized_transcript=normalized,
        alias_canonicalized_transcript=normalized,
    )


class ActiveLifecycleBackendCleanupError(RuntimeError):
    """A lifecycle backend child could not be confirmed stopped and reaped.

    This is deliberately distinct from an ordinary inference timeout.  The
    caller must not start Whisper while the isolated recognizer may still be
    alive, because doing so would defeat the single bounded inference/resource
    boundary.
    """


class ActiveLifecycleRecognitionBackend(Protocol):
    """Backend boundary used for deterministic tests and local Vosk inference."""

    recognition_backend: str

    def recognize_wav(
        self,
        audio_path: str | Path,
        *,
        grammar: Sequence[str],
        timeout_seconds: float,
    ) -> LifecycleBackendRecognition:
        ...


@dataclass(frozen=True)
class ActiveLifecycleAudioRecognitionResult:
    classification: str
    canonical_phrase: str = ""
    recognized_text: str = ""
    recognized_tokens: tuple[str, ...] = ()
    alias_detected: str = ""
    alias_position: str = "none"
    alias_canonicalized_transcript: str = ""
    confidence: Optional[float] = None
    confidence_available: bool = False
    recognition_backend: str = VOSK_ACTIVE_LIFECYCLE_BACKEND
    rejection_reason: str = ""
    confidence_tier: str = "missing"
    confirmation_required: bool = False
    proposed_classification: str = ""
    backend_cleanup_complete: bool = True
    backend_diagnostics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def selected_lifecycle_action(self) -> str:
        if self.classification in {
            ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
            ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
        }:
            return self.classification
        return "none"

    @property
    def whisper_fallback_required(self) -> bool:
        return self.backend_cleanup_complete and self.classification in {
            ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
            ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
        } and not self.confirmation_required


@dataclass(frozen=True)
class ActiveLifecycleConfirmationResult:
    disposition: str
    expected_classification: str
    recognized_text: str = ""
    recognized_tokens: tuple[str, ...] = ()
    confidence: Optional[float] = None
    confidence_available: bool = False
    recognition_backend: str = VOSK_ACTIVE_LIFECYCLE_BACKEND
    rejection_reason: str = ""
    backend_cleanup_complete: bool = True
    backend_diagnostics: Mapping[str, Any] = field(default_factory=dict)


class ActiveLifecycleAudioRecognizer:
    """Recognize a bounded ACTIVE lifecycle grammar from an existing WAV.

    The component has no microphone, session, runtime, CoreService, memory, or
    transition dependency. It only reads an already-finalized canonical WAV and
    reports evidence. The lifecycle authority decides what to do with it.
    """

    def __init__(
        self,
        *,
        backend: Optional[ActiveLifecycleRecognitionBackend] = None,
        model_path: str | Path = DEFAULT_WAKE_VOSK_MODEL,
        standby_high_confidence: float = DEFAULT_STANDBY_HIGH_CONFIDENCE,
        standby_medium_confidence: float = DEFAULT_STANDBY_MEDIUM_CONFIDENCE,
        shutdown_high_confidence: float = DEFAULT_SHUTDOWN_HIGH_CONFIDENCE,
        shutdown_medium_confidence: float = DEFAULT_SHUTDOWN_MEDIUM_CONFIDENCE,
        confirmation_minimum_confidence: float = (
            DEFAULT_CONFIRMATION_MINIMUM_CONFIDENCE
        ),
        cancellation_minimum_confidence: float = (
            DEFAULT_CANCELLATION_MINIMUM_CONFIDENCE
        ),
        progress_callback: Optional[
            Callable[[str, Mapping[str, Any]], None]
        ] = None,
    ) -> None:
        self.standby_high_confidence = _validated_confidence(
            standby_high_confidence,
            "standby_high_confidence",
        )
        self.standby_medium_confidence = _validated_confidence(
            standby_medium_confidence,
            "standby_medium_confidence",
        )
        self.shutdown_high_confidence = _validated_confidence(
            shutdown_high_confidence,
            "shutdown_high_confidence",
        )
        self.shutdown_medium_confidence = _validated_confidence(
            shutdown_medium_confidence,
            "shutdown_medium_confidence",
        )
        self.confirmation_minimum_confidence = _validated_confidence(
            confirmation_minimum_confidence,
            "confirmation_minimum_confidence",
        )
        self.cancellation_minimum_confidence = _validated_confidence(
            cancellation_minimum_confidence,
            "cancellation_minimum_confidence",
        )
        _require_safe_confidence_floor(
            self.standby_high_confidence,
            MINIMUM_SAFE_STANDBY_HIGH_CONFIDENCE,
            "standby_high_confidence",
        )
        _require_safe_confidence_floor(
            self.standby_medium_confidence,
            MINIMUM_SAFE_STANDBY_MEDIUM_CONFIDENCE,
            "standby_medium_confidence",
        )
        _require_safe_confidence_floor(
            self.shutdown_high_confidence,
            MINIMUM_SAFE_SHUTDOWN_HIGH_CONFIDENCE,
            "shutdown_high_confidence",
        )
        _require_safe_confidence_floor(
            self.shutdown_medium_confidence,
            MINIMUM_SAFE_SHUTDOWN_MEDIUM_CONFIDENCE,
            "shutdown_medium_confidence",
        )
        _require_safe_confidence_floor(
            self.confirmation_minimum_confidence,
            MINIMUM_SAFE_CONFIRMATION_CONFIDENCE,
            "confirmation_minimum_confidence",
        )
        if self.standby_medium_confidence >= self.standby_high_confidence:
            raise ValueError(
                "standby_medium_confidence must be less than standby_high_confidence"
            )
        if self.shutdown_medium_confidence >= self.shutdown_high_confidence:
            raise ValueError(
                "shutdown_medium_confidence must be less than shutdown_high_confidence"
            )
        self.backend: ActiveLifecycleRecognitionBackend = backend or VoskLifecycleGrammarBackend(
            model_path=model_path,
            progress_callback=progress_callback,
        )

    @property
    def grammar(self) -> tuple[str, ...]:
        return (
            DEFAULT_ACTIVE_STANDBY_GRAMMAR
            + DEFAULT_ACTIVE_SHUTDOWN_GRAMMAR
            + DEFAULT_ACTIVE_LIFECYCLE_REJECTION_GRAMMAR
        )

    def recognize_wav(
        self,
        audio_path: str | Path,
        *,
        timeout_seconds: float = DEFAULT_ACTIVE_LIFECYCLE_TIMEOUT_SECONDS,
    ) -> ActiveLifecycleAudioRecognitionResult:
        path = _validate_canonical_wav(audio_path)
        timeout = _validated_timeout(timeout_seconds)
        backend_name = str(
            getattr(self.backend, "recognition_backend", "constrained_lifecycle_backend")
            or "constrained_lifecycle_backend"
        )
        try:
            evidence = self.backend.recognize_wav(
                path,
                grammar=self.grammar,
                timeout_seconds=timeout,
            )
            evidence = _validated_backend_result(evidence, backend_name=backend_name)
        except (ImportError, OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            return ActiveLifecycleAudioRecognitionResult(
                classification=ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
                recognition_backend=backend_name,
                backend_cleanup_complete=not isinstance(
                    error,
                    ActiveLifecycleBackendCleanupError,
                ),
                rejection_reason=(
                    f"lifecycle_backend_error:{error.__class__.__name__}:"
                    f"{str(error)[:240]}"
                ),
                backend_diagnostics=self._backend_diagnostics(),
            )
        return self._classify(evidence)

    def recognize_confirmation_wav(
        self,
        audio_path: str | Path,
        *,
        expected_classification: str,
        timeout_seconds: float = DEFAULT_ACTIVE_LIFECYCLE_TIMEOUT_SECONDS,
    ) -> ActiveLifecycleConfirmationResult:
        if expected_classification not in {
            ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
            ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
        }:
            raise ValueError("expected_classification must be standby or shutdown")
        path = _validate_canonical_wav(audio_path)
        timeout = _validated_timeout(timeout_seconds)
        backend_name = str(
            getattr(self.backend, "recognition_backend", "constrained_lifecycle_backend")
            or "constrained_lifecycle_backend"
        )
        try:
            evidence = self.backend.recognize_wav(
                path,
                grammar=DEFAULT_ACTIVE_CONFIRMATION_GRAMMAR,
                timeout_seconds=timeout,
            )
            evidence = _validated_backend_result(evidence, backend_name=backend_name)
        except (ImportError, OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            return ActiveLifecycleConfirmationResult(
                disposition=ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED,
                expected_classification=expected_classification,
                recognition_backend=backend_name,
                backend_cleanup_complete=not isinstance(
                    error,
                    ActiveLifecycleBackendCleanupError,
                ),
                rejection_reason=(
                    f"confirmation_backend_error:{error.__class__.__name__}:"
                    f"{str(error)[:240]}"
                ),
                backend_diagnostics=self._backend_diagnostics(),
            )
        return self._classify_confirmation(
            evidence,
            expected_classification=expected_classification,
        )

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if callable(close):
            close()

    def release_active_resources(self, reason: str = "active_session_released") -> None:
        release = getattr(self.backend, "release_worker", None)
        if callable(release):
            release(reason=reason)

    def _backend_diagnostics(self) -> Mapping[str, Any]:
        value = getattr(self.backend, "worker_diagnostics", {})
        return dict(value) if isinstance(value, Mapping) else {}

    def _classify(
        self,
        evidence: LifecycleBackendRecognition,
    ) -> ActiveLifecycleAudioRecognitionResult:
        normalized = normalize_spoken_phrase(evidence.recognized_text)
        tokens = tuple(normalized.split())
        confidence = _minimum_aligned_confidence(evidence, tokens=tokens)
        alias = canonicalize_active_lifecycle_assistant_alias(normalized)
        common = {
            "recognized_text": evidence.recognized_text,
            # Preserve the backend's raw token evidence. Canonicalization is
            # reported separately and never rewrites confidence alignment.
            "recognized_tokens": tuple(evidence.recognized_tokens),
            "alias_detected": alias.alias_detected,
            "alias_position": alias.alias_position,
            "alias_canonicalized_transcript": (
                alias.alias_canonicalized_transcript
            ),
            "confidence": confidence,
            "confidence_available": confidence is not None,
            "recognition_backend": evidence.recognition_backend,
            "backend_diagnostics": dict(evidence.backend_diagnostics),
        }
        if not normalized:
            return ActiveLifecycleAudioRecognitionResult(
                classification=ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
                rejection_reason="empty_constrained_recognition",
                **common,
            )
        if "unk" in tokens or "[unk]" in evidence.recognized_tokens:
            return ActiveLifecycleAudioRecognitionResult(
                classification=ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
                rejection_reason="unknown_token_detected",
                **common,
            )
        if normalized in _NORMALIZED_LIFECYCLE_REJECTION_POLICY:
            return ActiveLifecycleAudioRecognitionResult(
                classification=ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
                rejection_reason="bounded_distractor_phrase",
                **common,
            )
        phrase_policy = _LIFECYCLE_PHRASE_POLICY.get(
            alias.alias_canonicalized_transcript
        )
        if phrase_policy is None:
            return ActiveLifecycleAudioRecognitionResult(
                classification=ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
                rejection_reason="exact_constrained_lifecycle_phrase_not_matched",
                **common,
            )
        proposed, canonical = phrase_policy
        if confidence is None:
            return ActiveLifecycleAudioRecognitionResult(
                classification=ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
                canonical_phrase=canonical,
                proposed_classification=proposed,
                rejection_reason="missing_or_unaligned_word_confidence",
                **common,
            )
        high, medium = self._thresholds(proposed)
        if confidence >= high:
            return ActiveLifecycleAudioRecognitionResult(
                classification=proposed,
                canonical_phrase=canonical,
                confidence_tier="high",
                **common,
            )
        if confidence >= medium:
            return ActiveLifecycleAudioRecognitionResult(
                classification=ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
                canonical_phrase=canonical,
                proposed_classification=proposed,
                confidence_tier="medium",
                confirmation_required=True,
                rejection_reason="medium_confidence_confirmation_required",
                **common,
            )
        return ActiveLifecycleAudioRecognitionResult(
            classification=ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
            canonical_phrase=canonical,
            proposed_classification=proposed,
            confidence_tier="low",
            rejection_reason="lifecycle_confidence_below_medium_threshold",
            **common,
        )

    def _classify_confirmation(
        self,
        evidence: LifecycleBackendRecognition,
        *,
        expected_classification: str,
    ) -> ActiveLifecycleConfirmationResult:
        normalized = normalize_spoken_phrase(evidence.recognized_text)
        tokens = tuple(normalized.split())
        confidence = _minimum_aligned_confidence(evidence, tokens=tokens)
        common = {
            "expected_classification": expected_classification,
            "recognized_text": evidence.recognized_text,
            "recognized_tokens": tokens,
            "confidence": confidence,
            "confidence_available": confidence is not None,
            "recognition_backend": evidence.recognition_backend,
            "backend_diagnostics": dict(evidence.backend_diagnostics),
        }
        if normalized in _CANCELLATION_PHRASES:
            if confidence is not None and confidence >= self.cancellation_minimum_confidence:
                return ActiveLifecycleConfirmationResult(
                    disposition=ACTIVE_LIFECYCLE_CONFIRMATION_CANCELLED,
                    **common,
                )
            return ActiveLifecycleConfirmationResult(
                disposition=ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED,
                rejection_reason="cancellation_confidence_below_threshold",
                **common,
            )
        expected_specific = f"confirm {expected_classification}"
        if normalized in _GENERIC_CONFIRMATION_PHRASES or normalized == expected_specific:
            if confidence is not None and confidence >= self.confirmation_minimum_confidence:
                return ActiveLifecycleConfirmationResult(
                    disposition=ACTIVE_LIFECYCLE_CONFIRMATION_CONFIRMED,
                    **common,
                )
            return ActiveLifecycleConfirmationResult(
                disposition=ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED,
                rejection_reason="confirmation_confidence_below_threshold",
                **common,
            )
        if normalized in {"confirm standby", "confirm shutdown"}:
            return ActiveLifecycleConfirmationResult(
                disposition=ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED,
                rejection_reason="confirmation_action_mismatch",
                **common,
            )
        return ActiveLifecycleConfirmationResult(
            disposition=ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED,
            rejection_reason="exact_confirmation_phrase_not_matched",
            **common,
        )

    def _thresholds(self, classification: str) -> tuple[float, float]:
        if classification == ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN:
            return self.shutdown_high_confidence, self.shutdown_medium_confidence
        return self.standby_high_confidence, self.standby_medium_confidence


class VoskLifecycleGrammarBackend:
    """Local Vosk backend that consumes only finalized canonical WAV files.

    Production inference runs in an isolated one-request process.  A result is
    not returned to the foreground runtime until that child has acknowledged a
    stop request or has been boundedly terminated and reaped. Injected
    module/factory dependencies retain the in-process seam used by deterministic
    tests.
    """

    recognition_backend = VOSK_ACTIVE_LIFECYCLE_BACKEND

    def __init__(
        self,
        *,
        model_path: str | Path = DEFAULT_WAKE_VOSK_MODEL,
        vosk_module: Any = None,
        model_factory: Any = None,
        recognizer_factory: Any = None,
        clock: Any = time.monotonic,
        process_context: Any = None,
        progress_callback: Optional[
            Callable[[str, Mapping[str, Any]], None]
        ] = None,
    ) -> None:
        self.model_path = Path(model_path).expanduser().resolve()
        self._vosk_module = vosk_module
        self._model_factory = model_factory
        self._recognizer_factory = recognizer_factory
        self._clock = clock
        self._model: Any = None
        self._lock = RLock()
        self._use_isolated_worker = not any(
            value is not None
            for value in (vosk_module, model_factory, recognizer_factory)
        )
        self._process_context = process_context
        self._progress_callback = progress_callback
        self._process_worker: Optional[_VoskLifecycleProcessWorker] = None
        self._closed = False
        self._last_worker_diagnostics: Mapping[str, Any] = (
            _empty_vosk_worker_diagnostics()
        )

    def recognize_wav(
        self,
        audio_path: str | Path,
        *,
        grammar: Sequence[str],
        timeout_seconds: float,
    ) -> LifecycleBackendRecognition:
        path = _validate_canonical_wav(audio_path)
        timeout = _validated_timeout(timeout_seconds)
        normalized_grammar = _validated_grammar(grammar)
        with self._lock:
            if self._closed:
                raise RuntimeError("vosk_lifecycle_backend_closed")
            if self._use_isolated_worker:
                worker = self._process_worker
                if worker is None:
                    worker = _VoskLifecycleProcessWorker(
                        model_path=self.model_path,
                        process_context=self._process_context,
                        progress_callback=self._progress_callback,
                    )
                    self._process_worker = worker
            else:
                worker = None
        if worker is not None:
            return worker.recognize_wav(
                path,
                grammar=normalized_grammar,
                timeout_seconds=timeout,
            )

        deadline = self._clock() + timeout
        model, module = self._loaded_model()
        return _recognize_vosk_lifecycle_wav(
            path=path,
            model=model,
            module=module,
            grammar=normalized_grammar,
            recognizer_factory=self._recognizer_factory,
            clock=self._clock,
            deadline=deadline,
        )

    def close(self) -> None:
        with self._lock:
            worker = self._process_worker
            self._closed = True
            self._model = None
        if worker is None:
            return
        try:
            worker.close()
        except ActiveLifecycleBackendCleanupError:
            # Retain the worker and its Process handle so a later cleanup pass
            # can retry.  Detaching an unreaped child would make it invisible
            # and could allow a second worker to be started beside it.
            with self._lock:
                self._last_worker_diagnostics = dict(worker.diagnostics)
            raise
        with self._lock:
            self._last_worker_diagnostics = dict(worker.diagnostics)
            if self._process_worker is worker:
                self._process_worker = None

    def release_worker(self, *, reason: str = "active_session_released") -> None:
        with self._lock:
            worker = self._process_worker
        if worker is None:
            return
        worker.release(reason=reason)
        with self._lock:
            self._last_worker_diagnostics = dict(worker.diagnostics)

    @property
    def worker_diagnostics(self) -> Mapping[str, Any]:
        with self._lock:
            worker = self._process_worker
            previous = dict(self._last_worker_diagnostics)
        return worker.diagnostics if worker is not None else previous

    def _loaded_model(self) -> tuple[Any, Any]:
        with self._lock:
            module = self._vosk_module
            if module is None:
                module = importlib.import_module("vosk")
                self._vosk_module = module
            if self._model is None:
                if not self.model_path.is_dir():
                    raise RuntimeError(f"vosk_lifecycle_model_missing:{self.model_path}")
                set_log_level = getattr(module, "SetLogLevel", None)
                if callable(set_log_level):
                    set_log_level(-1)
                factory = self._model_factory or getattr(module, "Model", None)
                if not callable(factory):
                    raise RuntimeError("vosk_lifecycle_model_factory_unavailable")
                self._model = factory(str(self.model_path))
            return self._model, module


class _BoundedLifecycleSocketTransport:
    """Deadline-bound, size-capped JSON messages over one private socket.

    ``multiprocessing.Connection.poll()`` only proves that at least part of a
    framed pickle is readable; the following ``recv()`` can still block if the
    peer stalls after a partial write.  This transport owns framing directly so
    every byte read and written is governed by the caller's monotonic deadline.
    The protocol is private and accepts JSON objects only.
    """

    def __init__(self, transport_socket: socket.socket) -> None:
        if not isinstance(transport_socket, socket.socket):
            raise TypeError("lifecycle worker transport must be a socket")
        self._socket = transport_socket
        self._socket.setblocking(False)
        self._send_lock = Lock()
        self._receive_lock = Lock()
        self._receive_buffer = bytearray()
        self._closed = False

    def send_message(
        self,
        message: Mapping[str, Any],
        *,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(message, Mapping):
            raise TypeError("lifecycle worker message must be a mapping")
        try:
            payload = json.dumps(
                dict(message),
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("lifecycle worker message is not JSON-safe") from error
        if not 0 < len(payload) <= _VOSK_LIFECYCLE_MAX_MESSAGE_BYTES:
            raise ValueError("lifecycle worker message exceeds bounded size")
        frame = struct.pack("!I", len(payload)) + payload
        remaining = max(0.0, float(deadline) - float(clock()))
        if remaining <= 0.0 or not self._send_lock.acquire(timeout=remaining):
            raise TimeoutError("vosk_lifecycle_worker_send_timeout")
        try:
            view = memoryview(frame)
            sent_total = 0
            while sent_total < len(frame):
                self._require_open()
                remaining = max(0.0, float(deadline) - float(clock()))
                if remaining <= 0.0:
                    raise TimeoutError("vosk_lifecycle_worker_send_timeout")
                try:
                    sent = self._socket.send(view[sent_total:])
                except (BlockingIOError, InterruptedError):
                    sent = -1
                if sent > 0:
                    sent_total += sent
                    continue
                if sent == 0:
                    raise BrokenPipeError("lifecycle worker socket closed during send")
                _wait_for_lifecycle_socket(
                    self._socket,
                    readable=False,
                    deadline=deadline,
                    clock=clock,
                )
        finally:
            self._send_lock.release()

    def receive_message(
        self,
        *,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> Mapping[str, Any]:
        remaining = max(0.0, float(deadline) - float(clock()))
        if remaining <= 0.0 or not self._receive_lock.acquire(timeout=remaining):
            raise TimeoutError("vosk_lifecycle_worker_receive_timeout")
        try:
            while True:
                message = self._pop_buffered_message()
                if message is not None:
                    return message
                self._require_open()
                remaining = max(0.0, float(deadline) - float(clock()))
                if remaining <= 0.0:
                    raise TimeoutError("vosk_lifecycle_worker_receive_timeout")
                try:
                    chunk = self._socket.recv(4096)
                except (ConnectionAbortedError, ConnectionResetError) as error:
                    raise EOFError(
                        "lifecycle worker socket closed unexpectedly"
                    ) from error
                except (BlockingIOError, InterruptedError):
                    chunk = None
                if chunk:
                    self._receive_buffer.extend(bytes(chunk))
                    if len(self._receive_buffer) > (
                        _VOSK_LIFECYCLE_MAX_MESSAGE_BYTES
                        + _VOSK_LIFECYCLE_MESSAGE_HEADER_BYTES
                    ):
                        raise ValueError("lifecycle worker receive buffer exceeded limit")
                    continue
                if chunk == b"":
                    reason = (
                        "lifecycle worker socket closed during partial message"
                        if self._receive_buffer
                        else "lifecycle worker socket reached EOF"
                    )
                    raise EOFError(reason)
                _wait_for_lifecycle_socket(
                    self._socket,
                    readable=True,
                    deadline=deadline,
                    clock=clock,
                )
        finally:
            self._receive_lock.release()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._socket.close()
        except OSError:
            pass

    def _pop_buffered_message(self) -> Optional[Mapping[str, Any]]:
        if len(self._receive_buffer) < _VOSK_LIFECYCLE_MESSAGE_HEADER_BYTES:
            return None
        payload_size = struct.unpack(
            "!I",
            self._receive_buffer[:_VOSK_LIFECYCLE_MESSAGE_HEADER_BYTES],
        )[0]
        if not 0 < payload_size <= _VOSK_LIFECYCLE_MAX_MESSAGE_BYTES:
            raise ValueError("lifecycle worker message length is invalid")
        frame_size = _VOSK_LIFECYCLE_MESSAGE_HEADER_BYTES + payload_size
        if len(self._receive_buffer) < frame_size:
            return None
        payload = bytes(
            self._receive_buffer[
                _VOSK_LIFECYCLE_MESSAGE_HEADER_BYTES:frame_size
            ]
        )
        del self._receive_buffer[:frame_size]
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("lifecycle worker message JSON is invalid") from error
        if not isinstance(value, Mapping):
            raise ValueError("lifecycle worker message must decode to a mapping")
        return value

    def _require_open(self) -> None:
        if self._closed or self._socket.fileno() < 0:
            raise OSError("lifecycle worker transport is closed")


def _wait_for_lifecycle_socket(
    transport_socket: socket.socket,
    *,
    readable: bool,
    deadline: float,
    clock: Callable[[], float],
) -> None:
    remaining = max(0.0, float(deadline) - float(clock()))
    if remaining <= 0.0:
        raise TimeoutError("vosk_lifecycle_worker_transport_timeout")
    try:
        ready_read, ready_write, exceptional = select.select(
            [transport_socket] if readable else [],
            [] if readable else [transport_socket],
            [transport_socket],
            remaining,
        )
    except (OSError, ValueError) as error:
        raise OSError("lifecycle worker transport wait failed") from error
    if exceptional:
        raise OSError("lifecycle worker transport reported an exceptional state")
    if not (ready_read if readable else ready_write):
        raise TimeoutError("vosk_lifecycle_worker_transport_timeout")


class _VoskLifecycleProcessWorker:
    """Spawn one bounded Vosk child for one recognition request."""

    _TERMINATION_GRACE_SECONDS = 0.25
    _KILL_GRACE_SECONDS = 0.25
    _CLOSE_LOCK_WAIT_SECONDS = 0.5
    _STOP_ACK_WAIT_SECONDS = 0.25

    def __init__(
        self,
        *,
        model_path: str | Path,
        process_context: Any = None,
        clock: Any = time.monotonic,
        progress_callback: Optional[
            Callable[[str, Mapping[str, Any]], None]
        ] = None,
    ) -> None:
        self.model_path = Path(model_path).expanduser().resolve()
        self._context = process_context or multiprocessing.get_context("spawn")
        self._clock = clock
        self._progress_callback = progress_callback
        self._request_lock = Lock()
        # Only a request owner performs full transport teardown.  A concurrent
        # close may signal the child to unblock that owner, but all
        # terminate/join/Process.close operations are serialized here.
        self._process_lock = RLock()
        self._process: Any = None
        self._connection: Any = None
        self._closed = False
        self._last_worker_pid: Optional[int] = None
        self._last_worker_exitcode: Optional[int] = None
        self._last_worker_reaped = True
        self._worker_start_count = 0
        self._worker_timeout_count = 0
        self._worker_terminate_count = 0
        self._worker_kill_count = 0
        self._last_request_id = ""
        self._request_sent_at = ""
        self._worker_request_received_at = ""
        self._result_received_at = ""
        self._stop_requested_at = ""
        self._worker_joined_at = ""
        self._stop_acknowledged = False
        self._last_cleanup_reason = ""

    @property
    def diagnostics(self) -> Mapping[str, Any]:
        with self._process_lock:
            process = self._process
            alive_state = _safe_process_alive_state(process)
            # Unknown liveness must never be reported as confirmed death.
            alive = alive_state is not False
            return {
                "worker_pid": (
                    int(process.pid)
                    if process is not None and process.pid is not None
                    else self._last_worker_pid
                ),
                "worker_alive": alive,
                "worker_liveness_known": alive_state is not None,
                "worker_exitcode": (
                    _safe_process_exitcode(process)
                    if process is not None
                    else self._last_worker_exitcode
                ),
                "worker_reaped": self._last_worker_reaped,
                "worker_start_count": self._worker_start_count,
                "worker_timeout_count": self._worker_timeout_count,
                "worker_terminate_count": self._worker_terminate_count,
                "worker_kill_count": self._worker_kill_count,
                "worker_protocol": VOSK_LIFECYCLE_WORKER_PROTOCOL,
                "worker_request_id": self._last_request_id,
                "worker_request_sent_at": self._request_sent_at,
                "worker_request_received_at": self._worker_request_received_at,
                "worker_result_received_at": self._result_received_at,
                "worker_stop_requested_at": self._stop_requested_at,
                "worker_joined_at": self._worker_joined_at,
                "worker_stop_acknowledged": self._stop_acknowledged,
                "worker_cleanup_reason": self._last_cleanup_reason,
            }

    def recognize_wav(
        self,
        audio_path: str | Path,
        *,
        grammar: Sequence[str],
        timeout_seconds: float,
    ) -> LifecycleBackendRecognition:
        timeout = _validated_timeout(timeout_seconds)
        deadline = self._clock() + timeout
        request_id = f"lifecycle-request-{uuid4()}"
        remaining = max(0.0, deadline - self._clock())
        if not self._request_lock.acquire(timeout=remaining):
            # A second caller may not fall through to Whisper while the one
            # isolated Vosk worker is still serving another request. Production
            # is serialized, but this remains a fail-closed concurrency guard.
            raise ActiveLifecycleBackendCleanupError(
                "vosk_lifecycle_worker_busy_fallback_blocked"
            )
        response: Optional[Mapping[str, Any]] = None
        pending_error: Optional[BaseException] = None
        graceful_cleanup = False
        try:
            if self._closed:
                raise RuntimeError("vosk_lifecycle_worker_closed")
            self._start_worker(deadline, request_id=request_id)
            if self._closed:
                raise RuntimeError("vosk_lifecycle_worker_closed")
            connection = self._connection_snapshot()
            if connection is None:
                raise RuntimeError("vosk_lifecycle_worker_connection_missing")
            self._last_request_id = request_id
            self._request_sent_at = _lifecycle_worker_timestamp()
            connection.send_message(
                {
                    "protocol": VOSK_LIFECYCLE_WORKER_PROTOCOL,
                    "type": "recognize_request",
                    "request_id": request_id,
                    "audio_path": str(Path(audio_path).resolve()),
                    "grammar": list(grammar),
                },
                deadline=deadline,
                clock=self._clock,
            )
            self._emit_progress(
                "lifecycle_recognizer_request_sent",
                request_id=request_id,
            )
            response = self._receive(
                deadline,
                request_id=request_id,
                allowed_types={"recognize_result", "error"},
            )
            self._worker_request_received_at = str(
                response.get("worker_request_received_at") or ""
            )[:64]
            self._result_received_at = _lifecycle_worker_timestamp()
            self._emit_progress(
                "lifecycle_result_returned",
                request_id=request_id,
                worker_request_received_at=self._worker_request_received_at,
            )
            graceful_cleanup = True
            if str(response.get("type") or "") == "error":
                pending_error = RuntimeError(
                    "vosk_lifecycle_worker_error:"
                    f"{response.get('error_class') or 'RuntimeError'}:"
                    f"{str(response.get('error_message') or '')[:240]}"
                )
        except TimeoutError as error:
            self._worker_timeout_count += 1
            pending_error = TimeoutError("vosk_lifecycle_recognition_timeout")
            pending_error.__cause__ = error
        except (BrokenPipeError, EOFError, OSError, ValueError) as error:
            pending_error = RuntimeError(
                f"vosk_lifecycle_worker_transport_error:{error.__class__.__name__}:"
                f"{str(error)[:160]}"
            )
            pending_error.__cause__ = error
        except BaseException as error:
            # KeyboardInterrupt and system-exit cancellation are cleaned up and
            # then propagated unchanged to the foreground owner.
            pending_error = error

        try:
            cleanup_complete = self._stop_and_reap_worker(
                request_id=request_id,
                reason=(
                    "recognition_completed"
                    if graceful_cleanup and pending_error is None
                    else "recognition_failed_or_cancelled"
                ),
                graceful=graceful_cleanup,
            )
        finally:
            self._request_lock.release()
        if not cleanup_complete:
            cleanup_error = (
                "vosk_lifecycle_timeout_cleanup_incomplete"
                if isinstance(pending_error, TimeoutError)
                else "vosk_lifecycle_worker_cleanup_incomplete"
            )
            raise ActiveLifecycleBackendCleanupError(
                cleanup_error
            ) from pending_error
        if pending_error is not None:
            raise pending_error
        if response is None or str(response.get("type") or "") != "recognize_result":
            raise RuntimeError("vosk_lifecycle_worker_invalid_response")
        return LifecycleBackendRecognition(
            recognized_text=str(response.get("recognized_text") or ""),
            recognized_tokens=tuple(response.get("recognized_tokens") or ()),
            word_confidences=tuple(response.get("word_confidences") or ()),
            recognition_backend=str(
                response.get("recognition_backend")
                or VOSK_ACTIVE_LIFECYCLE_BACKEND
            ),
            backend_diagnostics=dict(self.diagnostics),
        )

    def close(self) -> None:
        self._closed = True
        self.release(reason="backend_closed")

    def release(self, *, reason: str = "active_session_released") -> None:
        acquired = self._request_lock.acquire(
            timeout=self._CLOSE_LOCK_WAIT_SECONDS
        )
        if not acquired:
            self._signal_worker_stop(reason=reason)
            acquired = self._request_lock.acquire(
                timeout=self._CLOSE_LOCK_WAIT_SECONDS
            )
        if not acquired:
            raise ActiveLifecycleBackendCleanupError(
                "vosk_lifecycle_close_request_owner_unresponsive"
            )
        try:
            if not self._stop_and_reap_worker(
                request_id=self._last_request_id,
                reason=reason,
                graceful=True,
            ):
                raise ActiveLifecycleBackendCleanupError(
                    "vosk_lifecycle_close_cleanup_incomplete"
                )
        finally:
            self._request_lock.release()

    def _start_worker(self, deadline: float, *, request_id: str) -> None:
        if not self._stop_and_reap_worker(
            request_id=self._last_request_id,
            reason="before_worker_start",
            graceful=True,
        ):
            raise ActiveLifecycleBackendCleanupError(
                "vosk_lifecycle_worker_cleanup_incomplete"
            )
        if not self.model_path.is_dir():
            raise RuntimeError(f"vosk_lifecycle_model_missing:{self.model_path}")
        parent_connection: Any = None
        parent_socket: Optional[socket.socket] = None
        child_socket: Optional[socket.socket] = None
        process = None
        try:
            parent_socket, child_socket = socket.socketpair()
            parent_connection = _BoundedLifecycleSocketTransport(parent_socket)
            parent_socket = None
            process = self._context.Process(
                target=_vosk_lifecycle_worker_main,
                args=(child_socket, str(self.model_path), request_id),
                name="ares-active-lifecycle-vosk",
                daemon=False,
            )
            # multiprocessing exposes no timeout for Process.start(). The
            # spawned boundary hard-bounds model loading and native inference;
            # startup resource creation remains a short OS primitive rather
            # than an operation falsely claimed to be interruptible here.
            process.start()
            with self._process_lock:
                self._process = process
                self._connection = parent_connection
                self._last_worker_pid = (
                    int(process.pid) if process.pid is not None else None
                )
                self._last_worker_exitcode = None
                self._last_worker_reaped = False
                self._worker_start_count += 1
            parent_connection = None
            child_socket.close()
            child_socket = None
        except BaseException as error:
            _safe_close_connection(parent_connection)
            _safe_close_socket(parent_socket)
            _safe_close_socket(child_socket)
            started = process is not None and getattr(process, "pid", None) is not None
            if started:
                with self._process_lock:
                    if self._process is None:
                        self._process = process
                if not self._stop_and_reap_worker(
                    request_id=request_id,
                    reason="worker_start_failed",
                    graceful=False,
                ):
                    raise ActiveLifecycleBackendCleanupError(
                        "vosk_lifecycle_worker_start_cleanup_incomplete"
                    ) from error
            else:
                _safe_close_process(process)
            raise
        response = self._receive(
            deadline,
            request_id=request_id,
            allowed_types={"ready", "error"},
        )
        if str(response.get("type") or "") != "ready":
            if str(response.get("type") or "") == "error":
                error_class = response.get("error_class") or "RuntimeError"
                error_message = str(response.get("error_message") or "")[:240]
                if not self._stop_and_reap_worker(
                    request_id=request_id,
                    reason="worker_model_start_error",
                    graceful=False,
                ):
                    raise ActiveLifecycleBackendCleanupError(
                        "vosk_lifecycle_worker_start_error_cleanup_incomplete"
                    )
                raise RuntimeError(
                    "vosk_lifecycle_worker_start_error:"
                    f"{error_class}:{error_message}"
                )
            if not self._stop_and_reap_worker(
                request_id=request_id,
                reason="worker_invalid_ready_response",
                graceful=False,
            ):
                raise ActiveLifecycleBackendCleanupError(
                    "vosk_lifecycle_invalid_start_cleanup_incomplete"
                )
            raise RuntimeError("vosk_lifecycle_worker_invalid_start_response")

    def _receive(
        self,
        deadline: float,
        *,
        request_id: str,
        allowed_types: set[str],
    ) -> Mapping[str, Any]:
        connection = self._connection_snapshot()
        if connection is None:
            raise RuntimeError("vosk_lifecycle_worker_connection_missing")
        remaining = max(0.0, deadline - self._clock())
        if remaining <= 0.0:
            raise TimeoutError("vosk_lifecycle_worker_response_timeout")
        value = connection.receive_message(deadline=deadline, clock=self._clock)
        if not isinstance(value, Mapping):
            # ValueError is handled as a transport/protocol failure by the
            # request owner, which tears down and reaps this worker before any
            # ordinary Whisper fallback may begin.
            raise ValueError("vosk_lifecycle_worker_response_must_be_mapping")
        if str(value.get("protocol") or "") != VOSK_LIFECYCLE_WORKER_PROTOCOL:
            raise ValueError("vosk_lifecycle_worker_protocol_mismatch")
        if str(value.get("request_id") or "") != request_id:
            raise ValueError("vosk_lifecycle_worker_request_id_mismatch")
        if str(value.get("type") or "") not in allowed_types:
            raise ValueError("vosk_lifecycle_worker_unexpected_message")
        return value

    def _connection_snapshot(self) -> Any:
        with self._process_lock:
            return self._connection

    def _process_snapshot(self) -> Any:
        with self._process_lock:
            return self._process

    def _signal_worker_stop(self, *, reason: str) -> None:
        """Boundedly stop a request owner that did not release its lock."""

        with self._process_lock:
            process = self._process
            if process is None or _safe_process_alive_state(process) is False:
                return
            connection = self._connection
            if connection is not None:
                try:
                    connection.send_message(
                        {
                            "protocol": VOSK_LIFECYCLE_WORKER_PROTOCOL,
                            "type": "stop_request",
                            "request_id": self._last_request_id,
                            "reason": str(reason or "cancelled")[:80],
                        },
                        deadline=(self._clock() + self._STOP_ACK_WAIT_SECONDS),
                        clock=self._clock,
                    )
                except (BrokenPipeError, EOFError, OSError, TimeoutError, ValueError):
                    pass
            _safe_process_join(process, self._TERMINATION_GRACE_SECONDS)
            if _safe_process_alive_state(process) is False:
                return
            try:
                process.terminate()
                self._worker_terminate_count += 1
            except (OSError, ValueError):
                pass
            _safe_process_join(process, self._TERMINATION_GRACE_SECONDS)
            if _safe_process_alive_state(process) is not False:
                try:
                    process.kill()
                    self._worker_kill_count += 1
                except (OSError, ValueError):
                    pass
                _safe_process_join(process, self._KILL_GRACE_SECONDS)

    def _stop_and_reap_worker(
        self,
        *,
        request_id: str,
        reason: str,
        graceful: bool,
    ) -> bool:
        with self._process_lock:
            connection = self._connection
            process = self._process
            if process is None:
                self._connection = None
                _safe_close_connection(connection)
                self._last_worker_reaped = True
                self._last_cleanup_reason = str(reason or "not_running")[:96]
                return True
            alive_state = _safe_process_alive_state(process)
            self._stop_acknowledged = False
            if graceful and alive_state is not False and connection is not None:
                self._stop_requested_at = _lifecycle_worker_timestamp()
                self._emit_progress(
                    "lifecycle_worker_stop_requested",
                    request_id=request_id,
                    reason=reason,
                )
                try:
                    stop_deadline = self._clock() + self._STOP_ACK_WAIT_SECONDS
                    connection.send_message(
                        {
                            "protocol": VOSK_LIFECYCLE_WORKER_PROTOCOL,
                            "type": "stop_request",
                            "request_id": request_id,
                            "reason": str(reason or "completed")[:80],
                        },
                        deadline=stop_deadline,
                        clock=self._clock,
                    )
                    acknowledgement = connection.receive_message(
                        deadline=stop_deadline,
                        clock=self._clock,
                    )
                    self._stop_acknowledged = bool(
                        acknowledgement.get("protocol")
                        == VOSK_LIFECYCLE_WORKER_PROTOCOL
                        and acknowledgement.get("type") == "stopped"
                        and acknowledgement.get("request_id") == request_id
                    )
                except (BrokenPipeError, EOFError, OSError, TimeoutError, ValueError):
                    pass
            self._connection = None
            _safe_close_connection(connection)
            join_confirmed = _safe_process_join(
                process,
                self._TERMINATION_GRACE_SECONDS,
            )
            if _safe_process_alive_state(process) is not False:
                try:
                    process.terminate()
                    self._worker_terminate_count += 1
                    self._emit_progress(
                        "lifecycle_worker_terminate_sent",
                        request_id=request_id,
                        reason=reason,
                    )
                except (OSError, ValueError):
                    pass
                join_confirmed = (
                    _safe_process_join(process, self._TERMINATION_GRACE_SECONDS)
                    or join_confirmed
                )
            if _safe_process_alive_state(process) is not False:
                try:
                    process.kill()
                    self._worker_kill_count += 1
                    self._emit_progress(
                        "lifecycle_worker_kill_sent",
                        request_id=request_id,
                        reason=reason,
                    )
                except (OSError, ValueError):
                    pass
                join_confirmed = (
                    _safe_process_join(process, self._KILL_GRACE_SECONDS)
                    or join_confirmed
                )
            alive_state = _safe_process_alive_state(process)
            confirmed_dead = alive_state is False
            if confirmed_dead and not join_confirmed:
                join_confirmed = _safe_process_join(process, 0.0)
            confirmed_reaped = confirmed_dead and join_confirmed
            if confirmed_reaped:
                self._worker_joined_at = _lifecycle_worker_timestamp()
            self._last_worker_exitcode = _safe_process_exitcode(process)
            self._last_worker_reaped = confirmed_reaped
            self._last_cleanup_reason = str(reason or "cleanup")[:96]
            if confirmed_reaped:
                self._process = None
                _safe_close_process(process)
                self._emit_progress(
                    "lifecycle_worker_reaped",
                    request_id=request_id,
                    reason=reason,
                    exitcode=self._last_worker_exitcode,
                )
            else:
                # Retain the live handle.  A later call must retry cleanup and
                # may not start a second Vosk worker alongside it.
                self._process = process
            return self._last_worker_reaped

    # Backward-compatible private seam retained for focused cleanup tests.
    def _terminate_worker(self) -> bool:
        return self._stop_and_reap_worker(
            request_id=self._last_request_id,
            reason="forced_cleanup",
            graceful=False,
        )

    def _emit_progress(self, event: str, **details: Any) -> None:
        callback = self._progress_callback
        if callback is None:
            return
        payload = {
            "timestamp": _lifecycle_worker_timestamp(),
            "worker_pid": self._last_worker_pid,
            **details,
        }
        try:
            callback(str(event), payload)
        except (OSError, RuntimeError, TypeError, ValueError):
            return


def _empty_vosk_worker_diagnostics() -> Mapping[str, Any]:
    return {
        "worker_pid": None,
        "worker_alive": False,
        "worker_liveness_known": True,
        "worker_exitcode": None,
        "worker_reaped": True,
        "worker_start_count": 0,
        "worker_timeout_count": 0,
        "worker_terminate_count": 0,
        "worker_kill_count": 0,
        "worker_protocol": VOSK_LIFECYCLE_WORKER_PROTOCOL,
        "worker_request_id": "",
        "worker_request_sent_at": "",
        "worker_request_received_at": "",
        "worker_result_received_at": "",
        "worker_stop_requested_at": "",
        "worker_joined_at": "",
        "worker_stop_acknowledged": False,
        "worker_cleanup_reason": "",
    }


def _safe_process_alive_state(process: Any) -> Optional[bool]:
    if process is None:
        return False
    try:
        return bool(process.is_alive())
    except (AssertionError, OSError, ValueError):
        return None


def _safe_process_exitcode(process: Any) -> Optional[int]:
    if process is None:
        return None
    try:
        value = process.exitcode
    except (AssertionError, OSError, ValueError):
        return None
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_process_join(process: Any, timeout_seconds: float) -> bool:
    if process is None:
        return True
    try:
        process.join(max(0.0, float(timeout_seconds)))
    except (AssertionError, OSError, ValueError):
        return False
    return _safe_process_alive_state(process) is False


def _safe_close_process(process: Any) -> None:
    close = getattr(process, "close", None)
    if not callable(close):
        return
    try:
        close()
    except (AssertionError, OSError, ValueError):
        pass


def _safe_close_connection(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if not callable(close):
        return
    try:
        close()
    except (OSError, ValueError):
        pass


def _safe_close_socket(transport_socket: Optional[socket.socket]) -> None:
    if transport_socket is None:
        return
    try:
        transport_socket.close()
    except OSError:
        pass


def _vosk_lifecycle_worker_main(
    connection: socket.socket,
    model_path: str,
    request_id: str = "",
) -> None:
    """Serve one bounded recognition request and one bounded stop handshake."""

    _ignore_worker_interrupt_signal()
    sigint_ignored = _worker_interrupt_signal_is_ignored()
    expected_request_id = str(request_id or "")
    transport = _BoundedLifecycleSocketTransport(connection)
    try:
        module = importlib.import_module("vosk")
        set_log_level = getattr(module, "SetLogLevel", None)
        if callable(set_log_level):
            set_log_level(-1)
        model_factory = getattr(module, "Model", None)
        if not callable(model_factory):
            raise RuntimeError("vosk_lifecycle_model_factory_unavailable")
        model = model_factory(str(model_path))
        transport.send_message(
            {
                "protocol": VOSK_LIFECYCLE_WORKER_PROTOCOL,
                "type": "ready",
                "request_id": expected_request_id,
                "pid": os.getpid(),
                "sigint_ignored": sigint_ignored,
            },
            deadline=(time.monotonic() + _VOSK_LIFECYCLE_WORKER_READY_SEND_SECONDS),
        )
    except Exception as error:
        _send_worker_error(transport, error, request_id=expected_request_id)
        transport.close()
        return

    try:
        request = _receive_worker_message(
            transport,
            timeout_seconds=_VOSK_LIFECYCLE_WORKER_REQUEST_WAIT_SECONDS,
        )
        if request is None:
            return
        if not isinstance(request, Mapping):
            _send_worker_error(
                transport,
                TypeError("worker request must be a mapping"),
                request_id=expected_request_id,
            )
            return
        request_type = str(request.get("type") or "")
        received_request_id = str(request.get("request_id") or "")
        if request_type == "stop_request":
            if (
                request.get("protocol") == VOSK_LIFECYCLE_WORKER_PROTOCOL
                and expected_request_id
                and received_request_id == expected_request_id
            ):
                _send_worker_stopped(transport, request_id=received_request_id)
                return
            _send_worker_error(
                transport,
                ValueError("invalid lifecycle worker stop request"),
                request_id=expected_request_id or received_request_id,
            )
            return
        if (
            request.get("protocol") != VOSK_LIFECYCLE_WORKER_PROTOCOL
            or request_type != "recognize_request"
            or not expected_request_id
            or received_request_id != expected_request_id
        ):
            _send_worker_error(
                transport,
                ValueError("invalid lifecycle worker request"),
                request_id=expected_request_id or received_request_id,
            )
            return
        worker_request_received_at = _lifecycle_worker_timestamp()
        try:
            evidence = _recognize_vosk_lifecycle_wav(
                path=_validate_canonical_wav(request.get("audio_path") or ""),
                model=model,
                module=module,
                grammar=_validated_grammar(request.get("grammar") or ()),
            )
            transport.send_message(
                {
                    "protocol": VOSK_LIFECYCLE_WORKER_PROTOCOL,
                    "type": "recognize_result",
                    "request_id": expected_request_id,
                    "worker_request_received_at": worker_request_received_at,
                    "worker_result_sent_at": _lifecycle_worker_timestamp(),
                    "recognized_text": evidence.recognized_text,
                    "recognized_tokens": list(evidence.recognized_tokens),
                    "word_confidences": list(evidence.word_confidences),
                    "recognition_backend": evidence.recognition_backend,
                },
                deadline=(
                    time.monotonic()
                    + _VOSK_LIFECYCLE_WORKER_RESULT_SEND_SECONDS
                ),
            )
        except Exception as error:
            _send_worker_error(
                transport,
                error,
                request_id=expected_request_id,
                worker_request_received_at=worker_request_received_at,
            )

        stop_request = _receive_worker_message(
            transport,
            timeout_seconds=_VOSK_LIFECYCLE_WORKER_STOP_WAIT_SECONDS,
        )
        if not isinstance(stop_request, Mapping):
            return
        if (
            stop_request.get("protocol") == VOSK_LIFECYCLE_WORKER_PROTOCOL
            and stop_request.get("type") == "stop_request"
            and str(stop_request.get("request_id") or "") == expected_request_id
        ):
            _send_worker_stopped(transport, request_id=expected_request_id)
    finally:
        transport.close()


def _receive_worker_message(
    connection: _BoundedLifecycleSocketTransport,
    *,
    timeout_seconds: float,
) -> Optional[Mapping[str, Any]]:
    try:
        value = connection.receive_message(
            deadline=(time.monotonic() + max(0.0, float(timeout_seconds))),
        )
    except (EOFError, OSError, TimeoutError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _send_worker_stopped(
    connection: _BoundedLifecycleSocketTransport,
    *,
    request_id: str,
) -> None:
    try:
        connection.send_message(
            {
                "protocol": VOSK_LIFECYCLE_WORKER_PROTOCOL,
                "type": "stopped",
                "request_id": str(request_id or ""),
                "stopped_at": _lifecycle_worker_timestamp(),
            },
            deadline=(time.monotonic() + _VOSK_LIFECYCLE_WORKER_RESULT_SEND_SECONDS),
        )
    except (BrokenPipeError, EOFError, OSError, TimeoutError, ValueError):
        pass


def _send_worker_error(
    connection: _BoundedLifecycleSocketTransport,
    error: BaseException,
    *,
    request_id: str,
    worker_request_received_at: str = "",
) -> None:
    try:
        connection.send_message(
            {
                "protocol": VOSK_LIFECYCLE_WORKER_PROTOCOL,
                "type": "error",
                "request_id": str(request_id or ""),
                "worker_request_received_at": str(worker_request_received_at or ""),
                "error_class": error.__class__.__name__,
                "error_message": str(error)[:240],
            },
            deadline=(time.monotonic() + _VOSK_LIFECYCLE_WORKER_RESULT_SEND_SECONDS),
        )
    except (BrokenPipeError, EOFError, OSError, TimeoutError, ValueError):
        pass


def _ignore_worker_interrupt_signal() -> None:
    """Keep foreground Ctrl+C owned by the parent process without child tracebacks."""

    sigint = getattr(signal, "SIGINT", None)
    if sigint is None:
        return
    try:
        signal.signal(sigint, signal.SIG_IGN)
    except (OSError, RuntimeError, ValueError):
        return


def _worker_interrupt_signal_is_ignored() -> bool:
    sigint = getattr(signal, "SIGINT", None)
    if sigint is None:
        return False
    try:
        return signal.getsignal(sigint) == signal.SIG_IGN
    except (OSError, RuntimeError, ValueError):
        return False


def _lifecycle_worker_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _recognize_vosk_lifecycle_wav(
    *,
    path: Path,
    model: Any,
    module: Any,
    grammar: Sequence[str],
    recognizer_factory: Any = None,
    clock: Any = None,
    deadline: Optional[float] = None,
) -> LifecycleBackendRecognition:
    if deadline is not None and callable(clock) and clock() >= deadline:
        raise TimeoutError("vosk_lifecycle_recognition_timeout")
    factory = recognizer_factory or getattr(module, "KaldiRecognizer", None)
    if not callable(factory):
        raise RuntimeError("vosk_lifecycle_recognizer_factory_unavailable")
    vosk_grammar = list(grammar)
    if "[unk]" not in vosk_grammar:
        vosk_grammar.append("[unk]")
    recognizer = factory(model, 16000.0, json.dumps(vosk_grammar))
    set_words = getattr(recognizer, "SetWords", None)
    accept = getattr(recognizer, "AcceptWaveform", None)
    result_method = getattr(recognizer, "Result", None)
    final_method = getattr(recognizer, "FinalResult", None)
    if not all(
        callable(method)
        for method in (set_words, accept, result_method, final_method)
    ):
        raise RuntimeError("vosk_lifecycle_recognizer_methods_unavailable")
    set_words(True)
    payloads: list[Mapping[str, Any]] = []
    with wave.open(str(path), "rb") as source:
        while True:
            if deadline is not None and callable(clock) and clock() >= deadline:
                raise TimeoutError("vosk_lifecycle_recognition_timeout")
            chunk = source.readframes(4000)
            if not chunk:
                break
            if accept(chunk):
                payloads.append(_parse_vosk_payload(result_method()))
    if deadline is not None and callable(clock) and clock() >= deadline:
        raise TimeoutError("vosk_lifecycle_recognition_timeout")
    payloads.append(_parse_vosk_payload(final_method()))
    text_parts: list[str] = []
    token_parts: list[str] = []
    confidences: list[float] = []
    confidence_complete = True
    for payload in payloads:
        text = str(payload.get("text") or "").strip()
        if text:
            text_parts.append(text)
        words = payload.get("result") or []
        for word in words:
            token = str(word.get("word") or "").strip().casefold()
            if not token:
                continue
            token_parts.append(token)
            confidence = word.get("conf")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not isfinite(float(confidence))
                or not 0.0 <= float(confidence) <= 1.0
            ):
                confidence_complete = False
            else:
                confidences.append(float(confidence))
    if not confidence_complete or len(confidences) != len(token_parts):
        confidences = []
    return LifecycleBackendRecognition(
        recognized_text=" ".join(text_parts).strip(),
        recognized_tokens=tuple(token_parts),
        word_confidences=tuple(confidences),
        recognition_backend=VOSK_ACTIVE_LIFECYCLE_BACKEND,
    )


_LIFECYCLE_PHRASE_POLICY = {
    " ".join(template.canonicalized_tokens): (
        template.classification,
        (
            CANONICAL_STANDBY_PHRASE
            if template.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY
            else CANONICAL_SHUTDOWN_PHRASE
        ),
    )
    for template in _ACTIVE_LIFECYCLE_COMMAND_TEMPLATES
}

_NORMALIZED_LIFECYCLE_REJECTION_POLICY = frozenset(
    normalize_spoken_phrase(phrase)
    for phrase in (
        DEFAULT_ACTIVE_LIFECYCLE_REJECTION_GRAMMAR
        + _ACTIVE_LIFECYCLE_REJECTION_POLICY_EXTRAS
    )
)

_DEFAULT_ACTIVE_LIFECYCLE_ACTION_GRAMMAR = frozenset(
    DEFAULT_ACTIVE_STANDBY_GRAMMAR + DEFAULT_ACTIVE_SHUTDOWN_GRAMMAR
)
if _DEFAULT_ACTIVE_LIFECYCLE_ACTION_GRAMMAR & frozenset(
    DEFAULT_ACTIVE_LIFECYCLE_REJECTION_GRAMMAR
):
    raise RuntimeError("active lifecycle action and rejection grammar overlap")
if (
    len(_DEFAULT_ACTIVE_LIFECYCLE_ACTION_GRAMMAR)
    + len(DEFAULT_ACTIVE_LIFECYCLE_REJECTION_GRAMMAR)
    > 127
):
    raise RuntimeError(
        "active lifecycle grammar plus [unk] exceeds 128 bounded phrases"
    )


def _validate_canonical_wav(audio_path: str | Path) -> Path:
    path = Path(str(audio_path or "")).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"active lifecycle WAV is missing: {path}")
    with wave.open(str(path), "rb") as source:
        frame_count = source.getnframes()
        valid = (
            source.getframerate() == 16000
            and source.getnchannels() == 1
            and source.getsampwidth() == 2
            and source.getcomptype() == "NONE"
        )
    if frame_count <= 0:
        raise ValueError("active lifecycle WAV contains no PCM frames")
    if not valid:
        raise ValueError(
            "active lifecycle WAV must be 16000 Hz mono signed 16-bit little-endian PCM"
        )
    return path


def _validated_backend_result(
    value: Any,
    *,
    backend_name: str,
) -> LifecycleBackendRecognition:
    if not isinstance(value, LifecycleBackendRecognition):
        raise TypeError("lifecycle backend must return LifecycleBackendRecognition")
    name = str(value.recognition_backend or backend_name).strip() or backend_name
    return LifecycleBackendRecognition(
        recognized_text=str(value.recognized_text or ""),
        recognized_tokens=tuple(str(token or "") for token in value.recognized_tokens),
        word_confidences=tuple(value.word_confidences),
        recognition_backend=name,
        backend_diagnostics=dict(value.backend_diagnostics),
    )


def _minimum_aligned_confidence(
    evidence: LifecycleBackendRecognition,
    *,
    tokens: tuple[str, ...],
) -> Optional[float]:
    backend_tokens: list[str] = []
    expanded_confidences: list[Any] = []
    if len(evidence.word_confidences) != len(evidence.recognized_tokens):
        return None
    for token, confidence in zip(
        evidence.recognized_tokens,
        evidence.word_confidences,
    ):
        normalized_parts = tuple(normalize_spoken_phrase(token).split())
        backend_tokens.extend(normalized_parts)
        expanded_confidences.extend(confidence for _ in normalized_parts)
    if tuple(backend_tokens) != tokens or len(expanded_confidences) != len(tokens):
        return None
    values: list[float] = []
    for value in expanded_confidences:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            return None
        values.append(float(value))
    return min(values) if values else None


def _validated_grammar(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("lifecycle grammar must be a sequence")
    grammar = tuple(dict.fromkeys(_vosk_grammar_phrase(value) for value in values))
    if not grammar or any(not phrase for phrase in grammar) or len(grammar) > 128:
        raise ValueError("lifecycle grammar must contain between 1 and 128 phrases")
    return grammar


_VOSK_GRAMMAR_UNSAFE = re.compile(r"[^a-z0-9' ]+")


def _vosk_grammar_phrase(value: Any) -> str:
    """Normalize bounded grammar while preserving model vocabulary apostrophes."""

    clean = clean_spoken_phrase(value).casefold()
    return " ".join(_VOSK_GRAMMAR_UNSAFE.sub(" ", clean).split())


def _parse_vosk_payload(value: Any) -> Mapping[str, Any]:
    payload = json.loads(value) if isinstance(value, str) else value
    if not isinstance(payload, Mapping):
        raise ValueError("Vosk lifecycle result must be a JSON object")
    words = payload.get("result", [])
    if words is None:
        words = []
    if not isinstance(words, list) or any(not isinstance(word, Mapping) for word in words):
        raise ValueError("Vosk lifecycle word result must be a list of objects")
    return {"text": str(payload.get("text") or ""), "result": words}


def _validated_confidence(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    number = float(value)
    if not isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return number


def _validated_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be a finite number")
    number = float(value)
    if not isfinite(number) or not 0.1 <= number <= 30.0:
        raise ValueError("timeout_seconds must be between 0.1 and 30.0")
    return number


def _require_safe_confidence_floor(
    value: float,
    minimum: float,
    field_name: str,
) -> None:
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum:.2f}")
