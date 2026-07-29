from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
from math import isfinite
import multiprocessing
import os
from pathlib import Path
import re
from threading import Lock, RLock
import time
from typing import Any, Mapping, Optional, Protocol, Sequence
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

# This grammar is deliberately independent of the broader transcript alias
# policy. Vosk may only select one of these complete, owner-validated phrases
# (or [unk]); individual words such as ``artist`` and ``aries`` are not aliases.
DEFAULT_ACTIVE_STANDBY_GRAMMAR = (
    "goodbye ares",
    "goodbye aris",
    "good bye ares",
    "good bye aris",
    "bye ares",
    "bye aris",
    "go standby ares",
    "standby ares",
    "sleep ares",
)

DEFAULT_ACTIVE_SHUTDOWN_GRAMMAR = (
    "shutdown ares",
    "shutdown aris",
    "shut down ares",
    "shut down aris",
    "ares shutdown",
    "aris shutdown",
    "ares shut down",
    "aris shut down",
    "turn off ares",
    "turn off aris",
    "power off ares",
    "power off aris",
)

# A closed grammar containing only positive commands can force acoustically
# similar ordinary speech into the nearest lifecycle phrase. These explicit
# negative competitors remain outside ``_LIFECYCLE_PHRASE_POLICY`` and can
# therefore only produce an ordinary result. Keep this list bounded to observed
# safety-critical confusions; it is not an alias list.
DEFAULT_ACTIVE_LIFECYCLE_REJECTION_GRAMMAR = (
    "ares",
    "aris",
    "rs",
    "aries",
    "artist",
    "paris",
    "harris",
    "clears throat",
    "shutdown artist",
    "shut down artist",
    "shutdown aries",
    "shut down aries",
    "shutdown rs",
    "shutdown paris",
    "shutdown harris",
    "shutdown computer",
    "goodbye artist",
    "goodbye aries",
    "goodbye paris",
    "goodbye harris",
    "goodbye everyone",
    "go to sleep",
    "turn it off",
    "do not shut down",
    "do not shutdown",
    "don't shut down",
    "don't shutdown",
    "do not go to sleep",
    "don't go to sleep",
    "don't say goodbye",
    "do not say goodbye",
    "never shut down",
    "why did you shut down",
    "explain shutdown",
    "schedule a shutdown",
    "schedule a shutdown tomorrow",
) + tuple(
    f"{negation} {command} {alias}"
    for negation in ("do not", "don't", "never")
    for command in (
        "shutdown",
        "shut down",
        "goodbye",
        "say goodbye",
        "sleep",
        "standby",
        "go standby",
        "go to sleep",
    )
    for alias in ("ares", "aris")
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
    confidence: Optional[float] = None
    confidence_available: bool = False
    recognition_backend: str = VOSK_ACTIVE_LIFECYCLE_BACKEND
    rejection_reason: str = ""
    confidence_tier: str = "missing"
    confirmation_required: bool = False
    proposed_classification: str = ""
    backend_cleanup_complete: bool = True

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
            model_path=model_path
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
            )
        return self._classify_confirmation(
            evidence,
            expected_classification=expected_classification,
        )

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if callable(close):
            close()

    def _classify(
        self,
        evidence: LifecycleBackendRecognition,
    ) -> ActiveLifecycleAudioRecognitionResult:
        normalized = normalize_spoken_phrase(evidence.recognized_text)
        tokens = tuple(normalized.split())
        confidence = _minimum_aligned_confidence(evidence, tokens=tokens)
        common = {
            "recognized_text": evidence.recognized_text,
            "recognized_tokens": tokens,
            "confidence": confidence,
            "confidence_available": confidence is not None,
            "recognition_backend": evidence.recognition_backend,
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
        phrase_policy = _LIFECYCLE_PHRASE_POLICY.get(normalized)
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

    Production inference runs in one reusable spawned worker process.  The
    worker loads the model once, but every request remains outside the
    foreground runtime and can be terminated and reaped when its wall-clock
    deadline expires.  Injected module/factory dependencies retain the small
    in-process seam used by deterministic unit tests.
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


class _VoskLifecycleProcessWorker:
    """Reusable spawned Vosk worker with bounded request and cleanup waits."""

    _TERMINATION_GRACE_SECONDS = 0.25
    _KILL_GRACE_SECONDS = 0.25
    _CLOSE_LOCK_WAIT_SECONDS = 0.5

    def __init__(
        self,
        *,
        model_path: str | Path,
        process_context: Any = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.model_path = Path(model_path).expanduser().resolve()
        self._context = process_context or multiprocessing.get_context("spawn")
        self._clock = clock
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
        remaining = max(0.0, deadline - self._clock())
        if not self._request_lock.acquire(timeout=remaining):
            # A second caller may not fall through to Whisper while the one
            # isolated Vosk worker is still serving another request. Production
            # is serialized, but this remains a fail-closed concurrency guard.
            raise ActiveLifecycleBackendCleanupError(
                "vosk_lifecycle_worker_busy_fallback_blocked"
            )
        try:
            if self._closed:
                raise RuntimeError("vosk_lifecycle_worker_closed")
            try:
                self._ensure_started(deadline)
                if self._closed:
                    cleanup_complete = self._terminate_worker()
                    if not cleanup_complete:
                        raise ActiveLifecycleBackendCleanupError(
                            "vosk_lifecycle_worker_closed_cleanup_incomplete"
                        )
                    raise RuntimeError("vosk_lifecycle_worker_closed")
                connection = self._connection_snapshot()
                if connection is None:
                    raise RuntimeError("vosk_lifecycle_worker_connection_missing")
                # The request is a small, bounded grammar/path message. Python's
                # Connection.send itself has no timeout API, so it is kept out
                # of documentation claims of a hard OS-call deadline; a blocked
                # send is interruptible by concurrent close terminating the
                # isolated child.
                connection.send(
                    {
                        "operation": "recognize",
                        "audio_path": str(Path(audio_path).resolve()),
                        "grammar": list(grammar),
                    }
                )
                response = self._receive(deadline)
            except TimeoutError as error:
                self._worker_timeout_count += 1
                if not self._terminate_worker():
                    raise ActiveLifecycleBackendCleanupError(
                        "vosk_lifecycle_timeout_cleanup_incomplete"
                    ) from error
                raise TimeoutError("vosk_lifecycle_recognition_timeout") from error
            except (BrokenPipeError, EOFError, OSError, ValueError) as error:
                if not self._terminate_worker():
                    raise ActiveLifecycleBackendCleanupError(
                        "vosk_lifecycle_transport_cleanup_incomplete"
                    ) from error
                raise RuntimeError(
                    f"vosk_lifecycle_worker_transport_error:{error.__class__.__name__}:"
                    f"{str(error)[:160]}"
                ) from error
            response_type = str(response.get("type") or "")
            if response_type == "error":
                error_class = response.get("error_class") or "RuntimeError"
                error_message = str(response.get("error_message") or "")[:240]
                if not self._terminate_worker():
                    raise ActiveLifecycleBackendCleanupError(
                        "vosk_lifecycle_backend_error_cleanup_incomplete"
                    )
                raise RuntimeError(
                    "vosk_lifecycle_worker_error:"
                    f"{error_class}:{error_message}"
                )
            if response_type != "result":
                if not self._terminate_worker():
                    raise ActiveLifecycleBackendCleanupError(
                        "vosk_lifecycle_invalid_response_cleanup_incomplete"
                    )
                raise RuntimeError("vosk_lifecycle_worker_invalid_response")
            return LifecycleBackendRecognition(
                recognized_text=str(response.get("recognized_text") or ""),
                recognized_tokens=tuple(response.get("recognized_tokens") or ()),
                word_confidences=tuple(response.get("word_confidences") or ()),
                recognition_backend=str(
                    response.get("recognition_backend")
                    or VOSK_ACTIVE_LIFECYCLE_BACKEND
                ),
            )
        finally:
            self._request_lock.release()

    def close(self) -> None:
        self._closed = True
        acquired = self._request_lock.acquire(
            timeout=self._CLOSE_LOCK_WAIT_SECONDS
        )
        if not acquired:
            # Do not close a Connection out from under send/poll/recv. Signal
            # only; the request owner will observe EOF and perform serialized
            # teardown. Then make one bounded attempt to take ownership.
            self._signal_worker_stop()
            acquired = self._request_lock.acquire(
                timeout=self._CLOSE_LOCK_WAIT_SECONDS
            )
        if not acquired:
            raise ActiveLifecycleBackendCleanupError(
                "vosk_lifecycle_close_request_owner_unresponsive"
            )
        try:
            connection = self._connection_snapshot()
            process = self._process_snapshot()
            if connection is not None and process is not None:
                try:
                    if _safe_process_alive_state(process) is not False:
                        connection.send({"operation": "close"})
                        if connection.poll(self._TERMINATION_GRACE_SECONDS):
                            connection.recv()
                except (BrokenPipeError, EOFError, OSError, ValueError):
                    pass
            if not self._terminate_worker():
                raise ActiveLifecycleBackendCleanupError(
                    "vosk_lifecycle_close_cleanup_incomplete"
                )
        finally:
            self._request_lock.release()

    def _ensure_started(self, deadline: float) -> None:
        process = self._process_snapshot()
        connection = self._connection_snapshot()
        if (
            process is not None
            and _safe_process_alive_state(process) is True
            and connection is not None
        ):
            return
        if not self._terminate_worker():
            raise ActiveLifecycleBackendCleanupError(
                "vosk_lifecycle_worker_cleanup_incomplete"
            )
        if not self.model_path.is_dir():
            raise RuntimeError(f"vosk_lifecycle_model_missing:{self.model_path}")
        parent_connection: Any = None
        child_connection: Any = None
        process = None
        try:
            parent_connection, child_connection = self._context.Pipe(duplex=True)
            process = self._context.Process(
                target=_vosk_lifecycle_worker_main,
                args=(child_connection, str(self.model_path)),
                name="ares-active-lifecycle-vosk",
                daemon=True,
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
            child_connection.close()
            child_connection = None
        except BaseException as error:
            _safe_close_connection(parent_connection)
            _safe_close_connection(child_connection)
            started = process is not None and getattr(process, "pid", None) is not None
            if started:
                with self._process_lock:
                    if self._process is None:
                        self._process = process
                if not self._terminate_worker():
                    raise ActiveLifecycleBackendCleanupError(
                        "vosk_lifecycle_worker_start_cleanup_incomplete"
                    ) from error
            else:
                _safe_close_process(process)
            raise
        response = self._receive(deadline)
        if str(response.get("type") or "") != "ready":
            if str(response.get("type") or "") == "error":
                error_class = response.get("error_class") or "RuntimeError"
                error_message = str(response.get("error_message") or "")[:240]
                if not self._terminate_worker():
                    raise ActiveLifecycleBackendCleanupError(
                        "vosk_lifecycle_worker_start_error_cleanup_incomplete"
                    )
                raise RuntimeError(
                    "vosk_lifecycle_worker_start_error:"
                    f"{error_class}:{error_message}"
                )
            if not self._terminate_worker():
                raise ActiveLifecycleBackendCleanupError(
                    "vosk_lifecycle_invalid_start_cleanup_incomplete"
                )
            raise RuntimeError("vosk_lifecycle_worker_invalid_start_response")

    def _receive(self, deadline: float) -> Mapping[str, Any]:
        connection = self._connection_snapshot()
        if connection is None:
            raise RuntimeError("vosk_lifecycle_worker_connection_missing")
        remaining = max(0.0, deadline - self._clock())
        if remaining <= 0.0 or not connection.poll(remaining):
            raise TimeoutError("vosk_lifecycle_worker_response_timeout")
        value = connection.recv()
        if not isinstance(value, Mapping):
            # ValueError is handled as a transport/protocol failure by the
            # request owner, which tears down and reaps this worker before any
            # ordinary Whisper fallback may begin.
            raise ValueError("vosk_lifecycle_worker_response_must_be_mapping")
        return value

    def _connection_snapshot(self) -> Any:
        with self._process_lock:
            return self._connection

    def _process_snapshot(self) -> Any:
        with self._process_lock:
            return self._process

    def _signal_worker_stop(self) -> None:
        """Boundedly signal a busy request without mutating its transport."""

        with self._process_lock:
            process = self._process
            if process is None or _safe_process_alive_state(process) is False:
                return
            try:
                process.terminate()
            except (OSError, ValueError):
                pass
            _safe_process_join(process, self._TERMINATION_GRACE_SECONDS)
            if _safe_process_alive_state(process) is not False:
                try:
                    process.kill()
                except (OSError, ValueError):
                    pass
                _safe_process_join(process, self._KILL_GRACE_SECONDS)

    def _terminate_worker(self) -> bool:
        with self._process_lock:
            connection, self._connection = self._connection, None
            process = self._process
            _safe_close_connection(connection)
            if process is None:
                self._last_worker_reaped = True
                return True
            if _safe_process_alive_state(process) is not False:
                try:
                    process.terminate()
                except (OSError, ValueError):
                    pass
                _safe_process_join(process, self._TERMINATION_GRACE_SECONDS)
            if _safe_process_alive_state(process) is not False:
                try:
                    process.kill()
                except (OSError, ValueError):
                    pass
                _safe_process_join(process, self._KILL_GRACE_SECONDS)
            alive_state = _safe_process_alive_state(process)
            confirmed_dead = alive_state is False
            if confirmed_dead:
                _safe_process_join(process, 0.0)
            self._last_worker_exitcode = _safe_process_exitcode(process)
            self._last_worker_reaped = confirmed_dead
            if confirmed_dead:
                self._process = None
                _safe_close_process(process)
            else:
                # Retain the live handle.  A later call must retry cleanup and
                # may not start a second Vosk worker alongside it.
                self._process = process
            return self._last_worker_reaped


def _empty_vosk_worker_diagnostics() -> Mapping[str, Any]:
    return {
        "worker_pid": None,
        "worker_alive": False,
        "worker_liveness_known": True,
        "worker_exitcode": None,
        "worker_reaped": True,
        "worker_start_count": 0,
        "worker_timeout_count": 0,
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


def _safe_process_join(process: Any, timeout_seconds: float) -> None:
    if process is None:
        return
    try:
        process.join(max(0.0, float(timeout_seconds)))
    except (AssertionError, OSError, ValueError):
        pass


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


def _vosk_lifecycle_worker_main(connection: Any, model_path: str) -> None:
    """Load Vosk once and serve bounded parent-controlled recognition calls."""

    try:
        module = importlib.import_module("vosk")
        set_log_level = getattr(module, "SetLogLevel", None)
        if callable(set_log_level):
            set_log_level(-1)
        model_factory = getattr(module, "Model", None)
        if not callable(model_factory):
            raise RuntimeError("vosk_lifecycle_model_factory_unavailable")
        model = model_factory(str(model_path))
        connection.send({"type": "ready", "pid": os.getpid()})
    except BaseException as error:
        _send_worker_error(connection, error)
        try:
            connection.close()
        except OSError:
            pass
        return

    try:
        while True:
            try:
                request = connection.recv()
            except EOFError:
                return
            if not isinstance(request, Mapping):
                connection.send(
                    {
                        "type": "error",
                        "error_class": "TypeError",
                        "error_message": "worker request must be a mapping",
                    }
                )
                continue
            operation = str(request.get("operation") or "")
            if operation == "close":
                connection.send({"type": "closed"})
                return
            if operation != "recognize":
                connection.send(
                    {
                        "type": "error",
                        "error_class": "ValueError",
                        "error_message": "unsupported worker operation",
                    }
                )
                continue
            try:
                evidence = _recognize_vosk_lifecycle_wav(
                    path=_validate_canonical_wav(request.get("audio_path") or ""),
                    model=model,
                    module=module,
                    grammar=_validated_grammar(request.get("grammar") or ()),
                )
                connection.send(
                    {
                        "type": "result",
                        "recognized_text": evidence.recognized_text,
                        "recognized_tokens": list(evidence.recognized_tokens),
                        "word_confidences": list(evidence.word_confidences),
                        "recognition_backend": evidence.recognition_backend,
                    }
                )
            except Exception as error:
                _send_worker_error(connection, error)
    finally:
        try:
            connection.close()
        except OSError:
            pass


def _send_worker_error(connection: Any, error: BaseException) -> None:
    try:
        connection.send(
            {
                "type": "error",
                "error_class": error.__class__.__name__,
                "error_message": str(error)[:240],
            }
        )
    except (BrokenPipeError, EOFError, OSError, ValueError):
        pass


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
    **{
        phrase: (
            ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
            CANONICAL_STANDBY_PHRASE,
        )
        for phrase in DEFAULT_ACTIVE_STANDBY_GRAMMAR
    },
    **{
        phrase: (
            ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
            CANONICAL_SHUTDOWN_PHRASE,
        )
        for phrase in DEFAULT_ACTIVE_SHUTDOWN_GRAMMAR
    },
}

_NORMALIZED_LIFECYCLE_REJECTION_POLICY = frozenset(
    normalize_spoken_phrase(phrase)
    for phrase in DEFAULT_ACTIVE_LIFECYCLE_REJECTION_GRAMMAR
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
