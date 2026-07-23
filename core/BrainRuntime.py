from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import isfinite
import re
from threading import Lock, RLock
from typing import Any, Callable, Dict, Mapping, Optional, Sequence
from uuid import uuid4

from memory.schema_migrations import MigrationError

from core.AresIdentity import (
    DEFAULT_ARES_NAME_ALIASES,
    canonicalize_ares_name_tokens,
    normalize_spoken_phrase,
    validate_ares_name_aliases,
)
from core.BrainRuntimeAdapters import (
    RUNTIME_INPUT_CANCELLED,
    RUNTIME_INPUT_END,
    RUNTIME_INPUT_FAILED,
    RUNTIME_INPUT_ITEM,
    RUNTIME_INPUT_TIMEOUT,
    RuntimeInputAdapter,
    RuntimeInputResult,
    RuntimeOutputAdapter,
    RuntimeOutputMessage,
)
from core.BrainSessionManager import (
    BRAIN_ACTIVE,
    BRAIN_ERROR,
    BRAIN_PROCESSING,
    BRAIN_RESPONDING,
    BRAIN_RETURNING_TO_STANDBY,
    BRAIN_SHUTTING_DOWN,
    BRAIN_STANDBY,
    BRAIN_STOPPED,
    BrainSessionManager,
)
from core.Contracts import (
    CONTRACT_BRAIN_RUNTIME_REQUEST,
    BrainRuntimeCommandClassificationV1,
    BrainRuntimeLoopResultV1,
    BrainRuntimeRequestV1,
    BrainRuntimeResultV1,
    BrainRuntimeSnapshotV1,
    StandbyListenResultV1,
    WakeListenerRequestV1,
    new_correlation_id,
    validate_contract,
)
from core.CoreService import CoreService
from core.EventBus import PRIORITY_CRITICAL, PRIORITY_NORMAL, Event, EventBus
from core.LifecycleControl import (
    DEFAULT_LIFECYCLE_ACTIVATION_PHRASES,
    DEFAULT_LIFECYCLE_SHUTDOWN_PHRASES,
    DEFAULT_LIFECYCLE_STANDBY_PHRASES,
    LIFECYCLE_ACTION_ACTIVATE,
    LIFECYCLE_ACTION_NONE,
    LIFECYCLE_ACTION_SHUTDOWN,
    LIFECYCLE_ACTION_STANDBY,
    normalize_lifecycle_command,
)
from core.StandbyWakeListener import (
    WAKE_CATEGORY_ACTIVATION,
    WAKE_CATEGORY_SHUTDOWN,
    WAKE_CATEGORY_STANDBY,
    WAKE_STATUS_CANCELLED,
    StandbyWakeListener,
    WakeAttemptResult,
    validate_wake_control_phrases,
)


RUNTIME_COMMAND_ACTIVATION = "activation"
RUNTIME_COMMAND_STANDBY = "standby"
RUNTIME_COMMAND_SHUTDOWN = "shutdown"
RUNTIME_COMMAND_ORDINARY = "ordinary"
RUNTIME_COMMAND_EMPTY = "empty"

EVENT_RUNTIME_STARTED = "brain_runtime_started"
EVENT_RUNTIME_INPUT_RECEIVED = "brain_runtime_input_received"
EVENT_ACTIVATION_REQUESTED = "brain_activation_requested"
EVENT_ACTIVATION_ACCEPTED = "brain_activation_accepted"
EVENT_ACTIVATION_REJECTED = "brain_activation_rejected"
EVENT_RUNTIME_COMMAND_STARTED = "brain_runtime_command_started"
EVENT_RUNTIME_COMMAND_COMPLETED = "brain_runtime_command_completed"
EVENT_RUNTIME_COMMAND_FAILED = "brain_runtime_command_failed"
EVENT_RUNTIME_INACTIVITY_EXPIRED = "brain_runtime_inactivity_expired"
EVENT_RUNTIME_STANDBY_REQUESTED = "brain_runtime_standby_requested"
EVENT_RUNTIME_SHUTDOWN_REQUESTED = "brain_runtime_shutdown_requested"
EVENT_RUNTIME_STOPPED = "brain_runtime_stopped"
EVENT_RUNTIME_STANDBY_LISTENED = "brain_runtime_standby_listened"
EVENT_WAKE_CANDIDATE_DETECTED = "brain_wake_candidate_detected"
EVENT_WAKE_DETECTED = "brain_wake_detected"
EVENT_WAKE_REJECTED = "brain_wake_rejected"
EVENT_WAKE_LISTENER_FAILED = "brain_wake_listener_failed"
EVENT_WAKE_LISTENER_STOPPED = "brain_wake_listener_stopped"

DEFAULT_ACTIVATION_PHRASES = DEFAULT_LIFECYCLE_ACTIVATION_PHRASES
DEFAULT_STANDBY_PHRASES = DEFAULT_LIFECYCLE_STANDBY_PHRASES
DEFAULT_SHUTDOWN_PHRASES = DEFAULT_LIFECYCLE_SHUTDOWN_PHRASES
DEFAULT_ACTIVE_ACKNOWLEDGEMENT = "Yes Gabi."
DEFAULT_ALREADY_ACTIVE_ACKNOWLEDGEMENT = "Yes Gabi, I am listening."
DEFAULT_UNKNOWN_RESPONSE = "I cannot handle that request yet."
DEFAULT_COMMAND_FAILURE_RESPONSE = "I could not process that request."

MIN_PHRASE_COUNT = 1
MAX_PHRASE_COUNT = 16
MAX_PHRASE_LENGTH = 64
MAX_INPUT_LENGTH = 4096
MIN_POLLING_INTERVAL_SECONDS = 0.05
MAX_POLLING_INTERVAL_SECONDS = 5.0
MIN_COMMAND_TIMEOUT_SECONDS = 0.1
MAX_COMMAND_TIMEOUT_SECONDS = 600.0

_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")


@dataclass(frozen=True)
class BrainRuntimeConfig:
    ares_name_aliases: tuple[str, ...] = DEFAULT_ARES_NAME_ALIASES
    activation_phrases: tuple[str, ...] = DEFAULT_ACTIVATION_PHRASES
    active_acknowledgement: str = DEFAULT_ACTIVE_ACKNOWLEDGEMENT
    already_active_acknowledgement: str = DEFAULT_ALREADY_ACTIVE_ACKNOWLEDGEMENT
    standby_phrases: tuple[str, ...] = DEFAULT_STANDBY_PHRASES
    shutdown_phrases: tuple[str, ...] = DEFAULT_SHUTDOWN_PHRASES
    inactivity_timeout_seconds: float = 30.0
    maximum_consecutive_failures: int = 3
    input_polling_interval_seconds: float = 0.25
    command_timeout_seconds: float = 30.0
    standby_response: str = ""
    shutdown_response: str = "ARES is shutting down."

    def __post_init__(self) -> None:
        aliases = validate_ares_name_aliases(self.ares_name_aliases)
        activation = _canonicalized_runtime_phrases(
            self.activation_phrases,
            "activation_phrases",
            aliases,
        )
        standby = _canonicalized_runtime_phrases(
            self.standby_phrases,
            "standby_phrases",
            aliases,
        )
        shutdown = _canonicalized_runtime_phrases(
            self.shutdown_phrases,
            "shutdown_phrases",
            aliases,
        )
        _validate_phrase_collisions(activation, standby, shutdown)
        # Validate the exact production normalizer's compound-word and alias
        # canonicalization too, so configuration cannot hide a cross-category
        # collision such as "shut down ares" versus "shutdown ares".
        normalize_lifecycle_command(
            "__ares_lifecycle_configuration_validation__",
            activation_phrases=activation,
            standby_phrases=standby,
            shutdown_phrases=shutdown,
            ares_name_aliases=aliases,
        )
        object.__setattr__(self, "ares_name_aliases", aliases)
        object.__setattr__(self, "activation_phrases", activation)
        object.__setattr__(self, "standby_phrases", standby)
        object.__setattr__(self, "shutdown_phrases", shutdown)
        object.__setattr__(
            self,
            "active_acknowledgement",
            _validated_response(self.active_acknowledgement, "active_acknowledgement", required=True),
        )
        object.__setattr__(
            self,
            "already_active_acknowledgement",
            _validated_response(
                self.already_active_acknowledgement,
                "already_active_acknowledgement",
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "standby_response",
            _validated_response(self.standby_response, "standby_response", required=False),
        )
        object.__setattr__(
            self,
            "shutdown_response",
            _validated_response(self.shutdown_response, "shutdown_response", required=False),
        )
        object.__setattr__(
            self,
            "inactivity_timeout_seconds",
            _validated_number(
                self.inactivity_timeout_seconds,
                "inactivity_timeout_seconds",
                1.0,
                3600.0,
            ),
        )
        object.__setattr__(
            self,
            "input_polling_interval_seconds",
            _validated_number(
                self.input_polling_interval_seconds,
                "input_polling_interval_seconds",
                MIN_POLLING_INTERVAL_SECONDS,
                MAX_POLLING_INTERVAL_SECONDS,
            ),
        )
        object.__setattr__(
            self,
            "command_timeout_seconds",
            _validated_number(
                self.command_timeout_seconds,
                "command_timeout_seconds",
                MIN_COMMAND_TIMEOUT_SECONDS,
                MAX_COMMAND_TIMEOUT_SECONDS,
            ),
        )
        failures = self.maximum_consecutive_failures
        if isinstance(failures, bool) or not isinstance(failures, int) or not 1 <= failures <= 20:
            raise ValueError("maximum_consecutive_failures must be an integer between 1 and 20")

    @classmethod
    def from_mapping(
        cls,
        value: Optional["BrainRuntimeConfig | Mapping[str, Any]"] = None,
    ) -> "BrainRuntimeConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("brain_runtime configuration must be a mapping")
        allowed = {
            "ares_name_aliases",
            "activation_phrases",
            "active_acknowledgement",
            "already_active_acknowledgement",
            "standby_phrases",
            "shutdown_phrases",
            "inactivity_timeout_seconds",
            "maximum_consecutive_failures",
            "input_polling_interval_seconds",
            "command_timeout_seconds",
            "standby_response",
            "shutdown_response",
        }
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValueError(f"Unknown brain_runtime configuration fields: {', '.join(unknown)}")
        return cls(**dict(value))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ares_name_aliases": list(self.ares_name_aliases),
            "activation_phrases": list(self.activation_phrases),
            "active_acknowledgement": self.active_acknowledgement,
            "already_active_acknowledgement": self.already_active_acknowledgement,
            "standby_phrases": list(self.standby_phrases),
            "shutdown_phrases": list(self.shutdown_phrases),
            "inactivity_timeout_seconds": self.inactivity_timeout_seconds,
            "maximum_consecutive_failures": self.maximum_consecutive_failures,
            "input_polling_interval_seconds": self.input_polling_interval_seconds,
            "command_timeout_seconds": self.command_timeout_seconds,
            "standby_response": self.standby_response,
            "shutdown_response": self.shutdown_response,
        }


class BrainRuntime:
    """Serialized Capital/Core foreground runtime; it owns no hardware or skill logic."""

    def __init__(
        self,
        *,
        core_service: CoreService,
        command_handler: Callable[[str], Any],
        input_adapter: RuntimeInputAdapter,
        output_adapter: RuntimeOutputAdapter,
        config: Optional[BrainRuntimeConfig | Mapping[str, Any]] = None,
        event_bus: Optional[EventBus] = None,
        event_history_store: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
        runtime_id_factory: Optional[Callable[[], str]] = None,
        standby_wake_listener: Optional[StandbyWakeListener] = None,
    ) -> None:
        if not isinstance(core_service, CoreService):
            raise ValueError("core_service must be a CoreService")
        if not callable(command_handler):
            raise ValueError("command_handler must be callable")
        if not callable(getattr(input_adapter, "wait_for_input", None)):
            raise ValueError("input_adapter must implement wait_for_input(timeout_seconds)")
        if not callable(getattr(output_adapter, "write", None)):
            raise ValueError("output_adapter must implement write(message)")
        self.core_service = core_service
        self.session_manager: BrainSessionManager = core_service.brain_session_manager
        self.config = BrainRuntimeConfig.from_mapping(config)
        if self.config.inactivity_timeout_seconds != self.session_manager.config.inactivity_timeout_seconds:
            raise ValueError("brain_runtime inactivity timeout must match BrainSessionManager")
        if self.config.maximum_consecutive_failures != self.session_manager.config.maximum_consecutive_failures:
            raise ValueError("brain_runtime failure limit must match BrainSessionManager")
        self.command_handler = command_handler
        self.input_adapter = input_adapter
        self.output_adapter = output_adapter
        if standby_wake_listener is not None:
            required_wake_methods = (
                "start",
                "enter_standby",
                "leave_standby",
                "listen_once",
                "cancel",
                "stop",
                "snapshot",
                "health",
            )
            missing_wake_methods = [
                name for name in required_wake_methods if not callable(getattr(standby_wake_listener, name, None))
            ]
            if missing_wake_methods:
                raise ValueError(
                    "standby_wake_listener is missing methods: " + ", ".join(missing_wake_methods)
                )
            wake_config = getattr(standby_wake_listener, "config", None)
            if wake_config is None:
                raise ValueError("standby_wake_listener must expose validated config")
            validate_wake_control_phrases(
                wake_config.wake_phrases,
                self.config.standby_phrases,
                self.config.shutdown_phrases,
                wake_config.wake_phrase_aliases,
            )
            if tuple(wake_config.wake_phrase_aliases) != self.config.ares_name_aliases:
                raise ValueError(
                    "standby wake aliases must match BrainRuntime ares_name_aliases"
                )
        self.standby_wake_listener = standby_wake_listener
        self._event_bus = event_bus or EventBus(max_history=300)
        self._event_history_store = event_history_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        identifier_factory = runtime_id_factory or (lambda: f"brain-runtime-{uuid4()}")
        self.runtime_id = str(identifier_factory() or "").strip()
        if not _SAFE_IDENTIFIER_PATTERN.fullmatch(self.runtime_id):
            raise ValueError("runtime_id_factory returned an invalid identifier")
        self._command_lock = RLock()
        self._poll_lock = Lock()
        self._last_clock_at: Optional[datetime] = None
        self._resources_closed = False
        self._command_count = 0
        self._activation_count = 0
        self._standby_return_count = 0
        self._failure_count = 0
        self._output_count = 0
        self._last_stop_reason = ""
        self._wake_listener_failure_count = 0
        self._event_history_failures: list[Dict[str, str]] = []

    def start(self, *, correlation_id: str = "") -> BrainRuntimeResultV1:
        correlation = correlation_id or new_correlation_id("runtime-start")
        with self._command_lock:
            state = self.session_manager.state
            if state != BRAIN_STOPPED:
                return self._result(
                    success=state in {BRAIN_STANDBY, BRAIN_ACTIVE},
                    status="already_started",
                    correlation_id=correlation,
                    error_code="" if state in {BRAIN_STANDBY, BRAIN_ACTIVE} else "runtime_not_startable",
                    error_message="" if state in {BRAIN_STANDBY, BRAIN_ACTIVE} else f"cannot start from {state}",
                )
            self._resources_closed = False
            transitions = (
                (self.session_manager.begin_boot, "runtime_boot_requested"),
                (self.session_manager.begin_initialization, "runtime_initialization_requested"),
                (self.session_manager.enter_standby, "runtime_standby_ready"),
            )
            for transition, reason in transitions:
                result = transition(correlation_id=correlation, reason=reason)
                if not result.success:
                    return self._lifecycle_failure(result, correlation, "runtime_start_failed")
            if self.standby_wake_listener is not None:
                wake_started = self.standby_wake_listener.start(runtime_id=self.runtime_id)
                if not bool(getattr(wake_started, "success", False)):
                    self.shutdown(correlation_id=correlation, reason="wake_listener_start_failed")
                    return self._result(
                        False,
                        "wake_listener_start_failed",
                        correlation_id=correlation,
                        stop_reason="wake_listener_start_failed",
                        error_code=str(
                            getattr(wake_started, "error_code", "")
                            or "wake_listener_start_failed"
                        ),
                        error_message=str(
                            getattr(wake_started, "error_message", "")
                            or getattr(wake_started, "status", "")
                            or "standby wake listener failed to start"
                        )[:160],
                    )
            self._publish(
                EVENT_RUNTIME_STARTED,
                correlation,
                {"state": BRAIN_STANDBY, "runtime_id": self.runtime_id},
            )
            return self._result(True, "started", correlation_id=correlation)

    def snapshot(self) -> BrainRuntimeSnapshotV1:
        with self._command_lock:
            lifecycle = self.session_manager.snapshot()
            return BrainRuntimeSnapshotV1(
                success=True,
                status="current",
                runtime_id=self.runtime_id,
                current_lifecycle_state=lifecycle.current_state,
                previous_lifecycle_state=lifecycle.previous_state,
                active=lifecycle.current_state == BRAIN_ACTIVE,
                session_id=lifecycle.session_id,
                correlation_id=lifecycle.correlation_id,
                command_count=self._command_count,
                activation_count=self._activation_count,
                standby_return_count=self._standby_return_count,
                failure_count=self._failure_count,
                inactivity_timeout_seconds=self.config.inactivity_timeout_seconds,
                maximum_consecutive_failures=self.config.maximum_consecutive_failures,
                last_stop_reason=self._last_stop_reason,
                metadata={"safe": True, "source": "brain_runtime"},
            )

    def classify_command(
        self,
        text: str,
        *,
        correlation_id: str = "",
    ) -> BrainRuntimeCommandClassificationV1:
        control = normalize_lifecycle_command(
            text,
            activation_phrases=self.config.activation_phrases,
            standby_phrases=self.config.standby_phrases,
            shutdown_phrases=self.config.shutdown_phrases,
            ares_name_aliases=self.config.ares_name_aliases,
        )
        normalized = (
            control.canonicalized_transcript
            if control.matched
            else control.normalized_transcript
        )
        state = self.session_manager.state
        category = RUNTIME_COMMAND_ORDINARY
        matched = ""
        if not normalized:
            category = RUNTIME_COMMAND_EMPTY
        elif control.action == LIFECYCLE_ACTION_SHUTDOWN:
            category = RUNTIME_COMMAND_SHUTDOWN
            matched = control.matched_phrase
        elif control.action == LIFECYCLE_ACTION_STANDBY:
            category = RUNTIME_COMMAND_STANDBY
            matched = control.matched_phrase
        elif control.action == LIFECYCLE_ACTION_ACTIVATE:
            category = RUNTIME_COMMAND_ACTIVATION
            matched = control.matched_phrase
        return BrainRuntimeCommandClassificationV1(
            success=True,
            status="classified",
            runtime_id=self.runtime_id,
            current_lifecycle_state=state,
            command_category=category,
            normalized_input=normalized,
            matched_phrase=matched,
            correlation_id=correlation_id or new_correlation_id("runtime-classify"),
            session_id=self.session_manager.session_id,
            metadata={
                "safe": True,
                "source": "brain_runtime",
                "lifecycle_action": control.action,
                "routing_reason": control.routing_reason,
                "lifecycle_cleaned_transcript": control.cleaned_transcript,
                "lifecycle_normalized_transcript": control.normalized_transcript,
                "lifecycle_canonicalized_transcript": (
                    control.canonicalized_transcript
                ),
                "lifecycle_canonical_name": control.canonical_name,
                "lifecycle_matched_alias": control.matched_alias,
                "lifecycle_alias_type": control.alias_type,
                "lifecycle_matched_phrase": control.matched_phrase,
                "lifecycle_negation_detected": control.negation_detected,
                "lifecycle_rejection_reason": control.rejection_reason,
                "core_service_bypassed": category
                in {
                    RUNTIME_COMMAND_ACTIVATION,
                    RUNTIME_COMMAND_STANDBY,
                    RUNTIME_COMMAND_SHUTDOWN,
                },
            },
        )

    def handle_text(
        self,
        text: str,
        *,
        correlation_id: str = "",
    ) -> BrainRuntimeResultV1:
        return self.handle_request(
            BrainRuntimeRequestV1(
                runtime_id=self.runtime_id,
                input_text=text,
                timeout_seconds=self.config.command_timeout_seconds,
                correlation_id=correlation_id or new_correlation_id("runtime-input"),
                session_id=self.session_manager.session_id,
                metadata={"safe": True, "source": "runtime_input_adapter"},
            )
        )

    def handle_request(
        self,
        request: BrainRuntimeRequestV1 | Dict[str, Any],
    ) -> BrainRuntimeResultV1:
        with self._command_lock:
            try:
                normalized_request = (
                    request
                    if isinstance(request, BrainRuntimeRequestV1)
                    else BrainRuntimeRequestV1.from_dict(request)
                )
            except (TypeError, ValueError) as error:
                return self._result(
                    False,
                    "invalid_request",
                    correlation_id=new_correlation_id("runtime-invalid"),
                    error_code="invalid_runtime_request",
                    error_message=str(error)[:160],
                )
            compatibility = validate_contract(
                normalized_request,
                expected_contract_name=CONTRACT_BRAIN_RUNTIME_REQUEST,
            )
            if not compatibility.success:
                return self._result(
                    False,
                    "invalid_request",
                    correlation_id=normalized_request.correlation_id,
                    error_code="incompatible_runtime_request",
                    error_message=compatibility.error_message[:160],
                )
            if normalized_request.runtime_id and normalized_request.runtime_id != self.runtime_id:
                return self._result(
                    False,
                    "wrong_runtime",
                    correlation_id=normalized_request.correlation_id,
                    error_code="runtime_id_mismatch",
                    error_message="request targets a different runtime",
                )
            text = str(normalized_request.input_text or "")
            if len(text) > MAX_INPUT_LENGTH:
                return self._result(
                    False,
                    "input_rejected",
                    correlation_id=normalized_request.correlation_id,
                    error_code="input_too_long",
                    error_message=f"input exceeds {MAX_INPUT_LENGTH} characters",
                )
            classification = self.classify_command(
                text,
                correlation_id=normalized_request.correlation_id,
            )
            self._publish(
                EVENT_RUNTIME_INPUT_RECEIVED,
                normalized_request.correlation_id,
                {
                    "command_category": classification.command_category,
                    "input_length": len(text),
                    "state": self.session_manager.state,
                },
            )
            if classification.command_category == RUNTIME_COMMAND_SHUTDOWN:
                return self._with_lifecycle_command(
                    self.shutdown(
                        correlation_id=normalized_request.correlation_id,
                        reason="explicit_shutdown_command",
                        command_category=RUNTIME_COMMAND_SHUTDOWN,
                        normalized_input=classification.normalized_input,
                    ),
                    classification,
                )
            if classification.command_category == RUNTIME_COMMAND_STANDBY:
                return self._with_lifecycle_command(
                    self._handle_standby(classification),
                    classification,
                )
            if classification.command_category == RUNTIME_COMMAND_ACTIVATION:
                return self._with_lifecycle_command(
                    self._handle_activation(classification),
                    classification,
                )
            if classification.command_category == RUNTIME_COMMAND_EMPTY:
                return self._result(
                    False,
                    "input_rejected",
                    correlation_id=normalized_request.correlation_id,
                    command_category=RUNTIME_COMMAND_EMPTY,
                    normalized_input="",
                    error_code="empty_input",
                    error_message="input is empty",
                )
            if self.session_manager.state == BRAIN_STANDBY:
                return self._result(
                    True,
                    "ignored_in_standby",
                    correlation_id=normalized_request.correlation_id,
                    command_category=RUNTIME_COMMAND_ORDINARY,
                    normalized_input=classification.normalized_input,
                    stop_reason="standby_requires_activation",
                )
            if self.session_manager.state != BRAIN_ACTIVE:
                return self._result(
                    False,
                    "command_rejected",
                    correlation_id=normalized_request.correlation_id,
                    command_category=RUNTIME_COMMAND_ORDINARY,
                    normalized_input=classification.normalized_input,
                    error_code="runtime_not_active",
                    error_message=f"commands cannot run while state is {self.session_manager.state}",
                )
            return self._process_active_command(normalized_request, classification)

    def poll_once(self) -> BrainRuntimeResultV1:
        if not self._poll_lock.acquire(blocking=False):
            return self._result(
                False,
                "poll_rejected",
                correlation_id=new_correlation_id("runtime-poll"),
                error_code="concurrent_poll_rejected",
                error_message="runtime input polling is serialized",
            )
        try:
            if self.session_manager.state == BRAIN_STOPPED:
                started = self.start()
                if not started.success:
                    return started
            if (
                self.session_manager.state == BRAIN_STANDBY
                and self.standby_wake_listener is not None
            ):
                return self._poll_standby_wake()
            try:
                input_result = self.input_adapter.wait_for_input(
                    self.config.input_polling_interval_seconds
                )
            except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
                return self._handle_input_failure(
                    "input_adapter_exception",
                    str(error)[:160],
                )
            if not isinstance(input_result, RuntimeInputResult):
                return self._handle_input_failure(
                    "malformed_input_adapter_result",
                    "input adapter did not return RuntimeInputResult",
                )
            if input_result.status == RUNTIME_INPUT_ITEM:
                if self.session_manager.state == BRAIN_STOPPED:
                    return self._result(
                        False,
                        "runtime_stopped",
                        correlation_id=new_correlation_id("runtime-stopped"),
                        error_code="runtime_stopped",
                        error_message="runtime stopped while waiting for input",
                    )
                before = self.session_manager.snapshot()
                handled = self.handle_text(input_result.text)
                self._record_local_input_diagnostics(
                    handled,
                    lifecycle_state_before=before.current_state,
                    session_id_before=before.session_id,
                )
                return handled
            if input_result.status == RUNTIME_INPUT_TIMEOUT:
                return self._handle_input_timeout()
            if input_result.status == RUNTIME_INPUT_CANCELLED:
                self.shutdown(reason="owner_cancellation")
                return self._result(
                    False,
                    "cancelled",
                    correlation_id=new_correlation_id("runtime-cancel"),
                    stop_reason="owner_cancellation",
                    error_code="input_cancelled",
                    error_message="runtime input was cancelled",
                )
            if input_result.status == RUNTIME_INPUT_END:
                if not bool(input_result.metadata.get("runtime_terminal", True)):
                    # A bounded active-command source may finish one capture without
                    # ending the persistent owner runtime. Treat that source-local
                    # EOF like an empty/no-speech turn and retain normal inactivity
                    # handling; only terminal-capable foreground adapters may end.
                    return self._handle_input_timeout()
                self.shutdown(reason="end_of_input")
                return self._result(
                    False,
                    "end_of_input",
                    correlation_id=new_correlation_id("runtime-eof"),
                    stop_reason="end_of_input",
                    error_code="end_of_input",
                    error_message="runtime input ended",
                )
            if input_result.status == RUNTIME_INPUT_FAILED:
                return self._handle_input_failure(
                    input_result.error_code or "input_adapter_failed",
                    input_result.error_message or "input adapter failed",
                )
            return self._handle_input_failure(
                "unsupported_input_status",
                f"unsupported input status: {input_result.status}",
            )
        finally:
            self._poll_lock.release()

    def run(self, *, maximum_iterations: Optional[int] = None) -> BrainRuntimeLoopResultV1:
        if maximum_iterations is not None and (
            isinstance(maximum_iterations, bool)
            or not isinstance(maximum_iterations, int)
            or maximum_iterations < 1
        ):
            raise ValueError("maximum_iterations must be a positive integer")
        start_result = self.start()
        if not start_result.success:
            return self._loop_result(False, "start_failed", 0, start_result)
        iterations = 0
        terminal_result: Optional[BrainRuntimeResultV1] = None
        while self.session_manager.state != BRAIN_STOPPED:
            if maximum_iterations is not None and iterations >= maximum_iterations:
                terminal_result = self.shutdown(reason="maximum_iterations_reached")
                break
            terminal_result = self.poll_once()
            iterations += 1
            if terminal_result.status in {"cancelled", "end_of_input"}:
                break
        if terminal_result is None:
            terminal_result = self._result(
                False,
                "stopped",
                correlation_id=new_correlation_id("runtime-loop"),
                stop_reason=self._last_stop_reason,
            )
        terminal_reason = _canonical_runtime_terminal_reason(terminal_result)
        terminal_result = replace(terminal_result, stop_reason=terminal_reason)
        loop_success = (
            terminal_result.status == "stopped"
            and terminal_reason == "explicit_shutdown_command"
        )
        loop_status = (
            "stopped"
            if loop_success
            else terminal_reason
            if terminal_result.status == "stopped"
            else terminal_result.status
        )
        return self._loop_result(
            loop_success,
            loop_status,
            iterations,
            terminal_result,
        )

    def shutdown(
        self,
        *,
        correlation_id: str = "",
        reason: str = "runtime_shutdown_requested",
        command_category: str = RUNTIME_COMMAND_SHUTDOWN,
        normalized_input: str = "",
    ) -> BrainRuntimeResultV1:
        correlation = correlation_id or new_correlation_id("runtime-shutdown")
        with self._command_lock:
            state_before = self.session_manager.state
            session_before = self.session_manager.session_id
            if self.session_manager.state == BRAIN_STOPPED:
                self._close_resources()
                return self._result(
                    True,
                    "already_stopped",
                    correlation_id=correlation,
                    command_category=command_category,
                    normalized_input=normalized_input,
                    stop_reason=self._last_stop_reason or reason,
                    data={
                        "core_service_bypassed": True,
                        "lifecycle_action": "shutdown",
                        "lifecycle_state_before": state_before,
                        "session_id_before": session_before,
                    },
                )
            self._publish(
                EVENT_RUNTIME_SHUTDOWN_REQUESTED,
                correlation,
                {"state": self.session_manager.state, "reason": reason},
            )
            shutting_down = self.session_manager.begin_shutdown(
                correlation_id=correlation,
                reason=reason,
            )
            if not shutting_down.success:
                return self._lifecycle_failure(shutting_down, correlation, reason)
            if self.config.shutdown_response:
                self._write_output(
                    "shutdown",
                    self.config.shutdown_response,
                    correlation,
                    count_failure=False,
                )
            self._close_resources()
            stopped = self.session_manager.mark_stopped(
                correlation_id=correlation,
                reason="runtime_shutdown_completed",
            )
            if not stopped.success:
                return self._lifecycle_failure(stopped, correlation, reason)
            self._last_stop_reason = reason
            self._publish(
                EVENT_RUNTIME_STOPPED,
                correlation,
                {"state": BRAIN_STOPPED, "reason": reason},
            )
            return self._result(
                True,
                "stopped",
                correlation_id=correlation,
                command_category=command_category,
                normalized_input=normalized_input,
                stop_reason=reason,
                data={
                    "core_service_bypassed": True,
                    "lifecycle_action": "shutdown",
                    "lifecycle_state_before": state_before,
                    "session_id_before": session_before,
                },
            )

    def events(self, event_type: Optional[str] = None) -> list[Event]:
        return self._event_bus.history(event_type=event_type)

    def event_history_failures(self) -> list[Dict[str, str]]:
        return [dict(item) for item in self._event_history_failures]

    def _handle_activation(
        self,
        classification: BrainRuntimeCommandClassificationV1,
    ) -> BrainRuntimeResultV1:
        correlation = classification.correlation_id
        state = self.session_manager.state
        self._publish(EVENT_ACTIVATION_REQUESTED, correlation, {"state": state})
        if state == BRAIN_STANDBY:
            if self.standby_wake_listener is not None:
                released = self.standby_wake_listener.leave_standby(
                    "runtime_activation_requested",
                    handoff_destination="acknowledgement_playback",
                )
                if not bool(getattr(released, "success", False)):
                    self._publish(
                        EVENT_ACTIVATION_REJECTED,
                        correlation,
                        {"state": state, "reason": "standby_stream_release_failed"},
                    )
                    return self._result(
                        False,
                        "activation_rejected",
                        correlation_id=correlation,
                        command_category=RUNTIME_COMMAND_ACTIVATION,
                        normalized_input=classification.normalized_input,
                        error_code="standby_stream_release_failed",
                        error_message=str(
                            getattr(released, "error_message", "")
                            or getattr(released, "status", "")
                            or "standby capture could not be released"
                        )[:160],
                    )
            activated = self.session_manager.activate_session(
                correlation_id=correlation,
                reason="runtime_activation_phrase",
            )
            if not activated.success:
                if self.standby_wake_listener is not None:
                    self.standby_wake_listener.enter_standby(
                        runtime_id=self.runtime_id,
                        reason="activation_rollback",
                        handoff_source="activation_rollback",
                    )
                self._publish(
                    EVENT_ACTIVATION_REJECTED,
                    correlation,
                    {"state": state, "reason": activated.error_code},
                )
                return self._lifecycle_failure(activated, correlation, "activation_failed")
            output = self._write_output(
                "acknowledgement",
                self.config.active_acknowledgement,
                correlation,
            )
            if not output.success:
                return self._recover_output_failure(output, correlation, "activation_output_failed")
            self._activation_count += 1
            self._publish(
                EVENT_ACTIVATION_ACCEPTED,
                correlation,
                {"state": BRAIN_ACTIVE, "activation_count": self._activation_count},
            )
            return self._result(
                True,
                "activated",
                correlation_id=correlation,
                command_category=RUNTIME_COMMAND_ACTIVATION,
                normalized_input=classification.normalized_input,
                response_text=self.config.active_acknowledgement,
                data={
                    "core_service_bypassed": True,
                    "lifecycle_action": LIFECYCLE_ACTION_ACTIVATE,
                },
            )
        if state == BRAIN_ACTIVE:
            activity = self.session_manager.record_activity(
                correlation_id=correlation,
                reason="repeated_activation_phrase",
            )
            if not activity.success:
                return self._lifecycle_failure(activity, correlation, "activity_record_failed")
            self._publish(
                EVENT_ACTIVATION_REJECTED,
                correlation,
                {"state": state, "reason": "already_active"},
            )
            output = self._write_output(
                "acknowledgement",
                self.config.already_active_acknowledgement,
                correlation,
            )
            if not output.success:
                return self._recover_output_failure(output, correlation, "active_output_failed")
            return self._result(
                True,
                "already_active",
                correlation_id=correlation,
                command_category=RUNTIME_COMMAND_ACTIVATION,
                normalized_input=classification.normalized_input,
                response_text=self.config.already_active_acknowledgement,
                data={
                    "core_service_bypassed": True,
                    "lifecycle_action": LIFECYCLE_ACTION_ACTIVATE,
                },
            )
        self._publish(
            EVENT_ACTIVATION_REJECTED,
            correlation,
            {"state": state, "reason": "activation_not_allowed"},
        )
        return self._result(
            False,
            "activation_rejected",
            correlation_id=correlation,
            command_category=RUNTIME_COMMAND_ACTIVATION,
            normalized_input=classification.normalized_input,
            error_code="activation_not_allowed",
            error_message=f"activation is not allowed while state is {state}",
        )

    def _handle_standby(
        self,
        classification: BrainRuntimeCommandClassificationV1,
    ) -> BrainRuntimeResultV1:
        correlation = classification.correlation_id
        state = self.session_manager.state
        session_before = self.session_manager.session_id
        self._publish(
            EVENT_RUNTIME_STANDBY_REQUESTED,
            correlation,
            {"state": state, "reason": "owner_standby_phrase"},
        )
        if state == BRAIN_STANDBY:
            return self._result(
                True,
                "already_in_standby",
                correlation_id=correlation,
                command_category=RUNTIME_COMMAND_STANDBY,
                normalized_input=classification.normalized_input,
                stop_reason="owner_standby_phrase",
                data={
                    "core_service_bypassed": True,
                    "lifecycle_action": "standby",
                    "lifecycle_state_before": state,
                    "session_id_before": session_before,
                },
            )
        if state != BRAIN_ACTIVE:
            return self._result(
                False,
                "standby_rejected",
                correlation_id=correlation,
                command_category=RUNTIME_COMMAND_STANDBY,
                normalized_input=classification.normalized_input,
                error_code="standby_not_allowed",
                error_message=f"standby phrase cannot be handled while state is {state}",
            )
        return self._return_to_standby(
            correlation,
            "owner_standby_phrase",
            normalized_input=classification.normalized_input,
            response_text=self.config.standby_response,
            lifecycle_state_before=state,
            session_id_before=session_before,
        )

    def _process_active_command(
        self,
        request: BrainRuntimeRequestV1,
        classification: BrainRuntimeCommandClassificationV1,
    ) -> BrainRuntimeResultV1:
        correlation = request.correlation_id
        processing = self.session_manager.begin_processing(
            correlation_id=correlation,
            reason="runtime_command_started",
        )
        if not processing.success:
            return self._lifecycle_failure(processing, correlation, "processing_transition_failed")
        self._publish(
            EVENT_RUNTIME_COMMAND_STARTED,
            correlation,
            {"state": BRAIN_PROCESSING, "input_length": len(request.input_text)},
        )
        started_at = self._now()
        try:
            response = self.command_handler(request.input_text)
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            return self._recover_command_failure(
                correlation,
                "core_service_exception",
                str(error)[:160],
            )
        elapsed = max(0.0, (self._now() - started_at).total_seconds())
        timeout = request.timeout_seconds or self.config.command_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            return self._recover_command_failure(
                correlation,
                "invalid_command_timeout",
                "command timeout must be a positive finite number",
            )
        if elapsed > min(float(timeout), self.config.command_timeout_seconds):
            return self._recover_command_failure(
                correlation,
                "command_processing_timeout",
                "command processing exceeded its synchronous timeout",
            )
        response_text, selected_skill, response_success, response_error = _normalize_brain_response(response)
        if not response_success:
            return self._recover_command_failure(
                correlation,
                "core_service_failure",
                response_error or "central command route returned failure",
            )
        if not response_text:
            response_text = DEFAULT_UNKNOWN_RESPONSE
            selected_skill = selected_skill or "unknown"
        responding = self.session_manager.begin_responding(
            correlation_id=correlation,
            reason="runtime_response_started",
        )
        if not responding.success:
            return self._lifecycle_failure(responding, correlation, "responding_transition_failed")
        output = self._write_output("brain_response", response_text, correlation)
        if not output.success:
            return self._recover_output_failure(output, correlation, "response_output_failed")
        completed = self.session_manager.finish_response(
            correlation_id=correlation,
            reason="runtime_response_completed",
        )
        if not completed.success:
            return self._lifecycle_failure(completed, correlation, "response_completion_failed")
        self._command_count += 1
        self._publish(
            EVENT_RUNTIME_COMMAND_COMPLETED,
            correlation,
            {
                "state": BRAIN_ACTIVE,
                "status": "completed",
                "selected_skill": selected_skill,
                "response_length": len(response_text),
                "processing_time_ms": round(elapsed * 1000.0, 3),
            },
        )
        return self._result(
            True,
            "command_completed",
            correlation_id=correlation,
            command_category=RUNTIME_COMMAND_ORDINARY,
            normalized_input=classification.normalized_input,
            response_text=response_text,
            data={
                "selected_skill": selected_skill,
                "processing_time_seconds": elapsed,
                "core_service_bypassed": False,
                "lifecycle_action": "none",
                "lifecycle_command": _lifecycle_command_data(classification),
            },
        )

    def _handle_input_timeout(self) -> BrainRuntimeResultV1:
        with self._command_lock:
            correlation = new_correlation_id("runtime-timeout")
            if self.session_manager.state == BRAIN_ACTIVE and self.session_manager.inactivity_expired(
                at_time=self._now()
            ):
                self._publish(
                    EVENT_RUNTIME_INACTIVITY_EXPIRED,
                    correlation,
                    {"state": BRAIN_ACTIVE, "timeout_seconds": self.config.inactivity_timeout_seconds},
                )
                return self._return_to_standby(correlation, "inactivity_expired")
            return self._result(
                True,
                "input_timeout",
                correlation_id=correlation,
                stop_reason="poll_timeout",
            )

    def _return_to_standby(
        self,
        correlation: str,
        reason: str,
        *,
        normalized_input: str = "",
        response_text: str = "",
        lifecycle_state_before: str = "",
        session_id_before: str = "",
    ) -> BrainRuntimeResultV1:
        returning = self.session_manager.request_return_to_standby(
            correlation_id=correlation,
            reason=reason,
        )
        if not returning.success:
            return self._lifecycle_failure(returning, correlation, reason)
        standby = self.session_manager.complete_return_to_standby(
            correlation_id=correlation,
            reason="runtime_standby_restored",
        )
        if not standby.success:
            return self._lifecycle_failure(standby, correlation, reason)
        self._standby_return_count += 1
        self._last_stop_reason = reason
        self._release_active_transport_resources()
        if response_text:
            output = self._write_output("standby", response_text, correlation)
            if not output.success:
                return self._result(
                    False,
                    "output_failed",
                    correlation_id=correlation,
                    command_category=RUNTIME_COMMAND_STANDBY,
                    normalized_input=normalized_input,
                    stop_reason=reason,
                    error_code=output.error_code,
                    error_message=output.error_message,
                    data={
                        "core_service_bypassed": bool(normalized_input),
                        "lifecycle_action": "standby" if normalized_input else "inactivity",
                        "lifecycle_state_before": lifecycle_state_before,
                        "session_id_before": session_id_before,
                    },
                )
        if self.standby_wake_listener is not None:
            entered = self.standby_wake_listener.enter_standby(
                runtime_id=self.runtime_id,
                reason="runtime_return_to_standby",
                handoff_source="active_command",
            )
            if not bool(getattr(entered, "success", False)):
                return self._result(
                    False,
                    "standby_listener_failed",
                    correlation_id=correlation,
                    command_category=RUNTIME_COMMAND_STANDBY,
                    normalized_input=normalized_input,
                    stop_reason=reason,
                    error_code=str(
                        getattr(entered, "error_code", "")
                        or "standby_listener_start_failed"
                    ),
                    error_message=str(
                        getattr(entered, "error_message", "")
                        or getattr(entered, "status", "")
                        or "standby listener failed to resume"
                    )[:160],
                    data={
                        "core_service_bypassed": bool(normalized_input),
                        "lifecycle_action": (
                            "standby" if normalized_input else "inactivity"
                        ),
                        "lifecycle_state_before": lifecycle_state_before,
                        "session_id_before": session_id_before,
                    },
                )
        return self._result(
            True,
            "standby_entered",
            correlation_id=correlation,
            command_category=RUNTIME_COMMAND_STANDBY,
            normalized_input=normalized_input,
            response_text=response_text,
            stop_reason=reason,
            data={
                "core_service_bypassed": bool(normalized_input),
                "lifecycle_action": "standby" if normalized_input else "inactivity",
                "lifecycle_state_before": lifecycle_state_before,
                "session_id_before": session_id_before,
            },
        )

    def _recover_command_failure(
        self,
        correlation: str,
        error_code: str,
        error_message: str,
    ) -> BrainRuntimeResultV1:
        self._failure_count += 1
        failure = self.session_manager.report_failure(
            correlation_id=correlation,
            reason=error_code,
            unrecoverable=False,
        )
        self._publish(
            EVENT_RUNTIME_COMMAND_FAILED,
            correlation,
            {
                "state": self.session_manager.state,
                "error_code": error_code,
                "failure_count": self._failure_count,
            },
            priority=PRIORITY_CRITICAL,
        )
        if self.session_manager.state == BRAIN_ERROR:
            self.shutdown(correlation_id=correlation, reason="maximum_failures_reached")
            return self._result(
                False,
                "maximum_failures_reached",
                correlation_id=correlation,
                error_code=failure.error_code,
                error_message=error_message,
                stop_reason="maximum_failures_reached",
            )
        if self.session_manager.state == BRAIN_PROCESSING:
            responding = self.session_manager.begin_responding(
                correlation_id=correlation,
                reason="safe_failure_response",
            )
            if responding.success:
                self._write_output(
                    "safe_error",
                    DEFAULT_COMMAND_FAILURE_RESPONSE,
                    correlation,
                    count_failure=False,
                )
        if self.session_manager.state in {BRAIN_PROCESSING, BRAIN_RESPONDING, BRAIN_ACTIVE}:
            self._return_to_standby(correlation, "command_failure_recovery")
        return self._result(
            False,
            "command_failed",
            correlation_id=correlation,
            command_category=RUNTIME_COMMAND_ORDINARY,
            response_text=DEFAULT_COMMAND_FAILURE_RESPONSE,
            stop_reason="command_failure_recovery",
            error_code=error_code,
            error_message=error_message,
        )

    def _handle_input_failure(self, error_code: str, error_message: str) -> BrainRuntimeResultV1:
        with self._command_lock:
            correlation = new_correlation_id("runtime-input-failure")
            self._failure_count += 1
            failure = self.session_manager.report_failure(
                correlation_id=correlation,
                reason="runtime_input_failure",
                unrecoverable=False,
            )
            self._publish(
                EVENT_RUNTIME_COMMAND_FAILED,
                correlation,
                {
                    "state": self.session_manager.state,
                    "error_code": error_code,
                    "failure_count": self._failure_count,
                },
                priority=PRIORITY_CRITICAL,
            )
            self._write_output(
                "safe_error",
                "ARES could not read input safely.",
                correlation,
                count_failure=False,
            )
            if self.session_manager.state == BRAIN_ERROR:
                self.shutdown(correlation_id=correlation, reason="maximum_failures_reached")
                return self._result(
                    False,
                    "maximum_failures_reached",
                    correlation_id=correlation,
                    stop_reason="maximum_failures_reached",
                    error_code=failure.error_code or error_code,
                    error_message=error_message,
                )
            return self._result(
                False,
                "input_failed",
                correlation_id=correlation,
                error_code=error_code,
                error_message=error_message,
            )

    def _recover_output_failure(
        self,
        output: Any,
        correlation: str,
        reason: str,
    ) -> BrainRuntimeResultV1:
        self._failure_count += 1
        self.session_manager.report_failure(
            correlation_id=correlation,
            reason=reason,
            unrecoverable=False,
        )
        self._publish(
            EVENT_RUNTIME_COMMAND_FAILED,
            correlation,
            {
                "state": self.session_manager.state,
                "error_code": output.error_code or "output_failed",
                "failure_count": self._failure_count,
            },
            priority=PRIORITY_CRITICAL,
        )
        if self.session_manager.state == BRAIN_ERROR:
            self.shutdown(correlation_id=correlation, reason="maximum_failures_reached")
        elif self.session_manager.state in {BRAIN_ACTIVE, BRAIN_PROCESSING, BRAIN_RESPONDING}:
            self._return_to_standby(correlation, "output_failure_recovery")
        return self._result(
            False,
            "output_failed",
            correlation_id=correlation,
            stop_reason="output_failure_recovery",
            error_code=output.error_code or "output_failed",
            error_message=output.error_message or "runtime output failed",
        )

    def _lifecycle_failure(
        self,
        snapshot: Any,
        correlation: str,
        reason: str,
    ) -> BrainRuntimeResultV1:
        self._failure_count += 1
        self._publish(
            EVENT_RUNTIME_COMMAND_FAILED,
            correlation,
            {
                "state": self.session_manager.state,
                "error_code": snapshot.error_code or "lifecycle_failure",
                "failure_count": self._failure_count,
            },
            priority=PRIORITY_CRITICAL,
        )
        if self.session_manager.state not in {BRAIN_STOPPED, BRAIN_SHUTTING_DOWN}:
            reported = self.session_manager.report_failure(
                correlation_id=correlation,
                reason="runtime_lifecycle_failure",
                unrecoverable=True,
            )
            if reported.current_state == BRAIN_ERROR:
                self.shutdown(correlation_id=correlation, reason="unsafe_lifecycle_state")
        return self._result(
            False,
            "lifecycle_failure",
            correlation_id=correlation,
            stop_reason=reason,
            error_code=snapshot.error_code or "lifecycle_failure",
            error_message=snapshot.error_message or "brain lifecycle transition failed",
        )

    def _write_output(
        self,
        category: str,
        text: str,
        correlation: str,
        *,
        count_failure: bool = True,
    ) -> Any:
        del count_failure
        try:
            result = self.output_adapter.write(
                RuntimeOutputMessage(
                    category=category,
                    text=text,
                    correlation_id=correlation,
                    session_id=self.session_manager.session_id,
                    metadata={"safe": True, "source": "brain_runtime"},
                )
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            from core.BrainRuntimeAdapters import RuntimeOutputResult

            result = RuntimeOutputResult(False, "output_failed", "output_adapter_exception", str(error)[:160])
        required_fields = ("success", "status", "error_code", "error_message")
        if any(not hasattr(result, field_name) for field_name in required_fields):
            from core.BrainRuntimeAdapters import RuntimeOutputResult

            result = RuntimeOutputResult(
                False,
                "malformed_output",
                "malformed_output_adapter_result",
                "output adapter returned an invalid result",
            )
        if bool(result.success):
            self._output_count += 1
        return result

    def _close_resources(self) -> None:
        if self._resources_closed:
            return
        resources = [self.input_adapter, self.output_adapter]
        if self.standby_wake_listener is not None:
            try:
                self.standby_wake_listener.cancel("runtime_shutdown")
                stopped = self.standby_wake_listener.stop("runtime_shutdown")
                self._publish(
                    EVENT_WAKE_LISTENER_STOPPED,
                    new_correlation_id("wake-stop"),
                    {
                        "status": str(getattr(stopped, "status", "stopped")),
                        "cleanup_status": str(
                            getattr(stopped, "cleanup_status", "unknown")
                        ),
                    },
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                self._failure_count += 1
        for adapter in resources:
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except (OSError, RuntimeError, TypeError, ValueError):
                    self._failure_count += 1
        self._resources_closed = True

    def build_standby_wake_request(
        self,
        *,
        correlation_id: str = "",
    ) -> WakeListenerRequestV1:
        """Build the privacy-safe request shared by runtime and bounded probes."""

        if self.standby_wake_listener is None:
            raise RuntimeError("standby_wake_listener_not_configured")
        correlation = correlation_id or new_correlation_id("standby-wake")
        wake_config = self.standby_wake_listener.config
        return WakeListenerRequestV1(
            runtime_id=self.runtime_id,
            lifecycle_state=BRAIN_STANDBY,
            listener_timeout_seconds=wake_config.speech_wait_timeout_seconds,
            language=wake_config.language,
            wake_phrases=list(wake_config.wake_phrases),
            wake_phrase_aliases=list(wake_config.wake_phrase_aliases),
            wake_phrase_prefixes=list(wake_config.wake_phrase_prefixes),
            standby_phrases=list(self.config.standby_phrases),
            shutdown_phrases=list(self.config.shutdown_phrases),
            diagnostic_wake=wake_config.diagnostic_wake,
            retain_diagnostic_audio=wake_config.retain_diagnostic_audio,
            correlation_id=correlation,
            metadata={"safe": True, "contains_transcript": False, "contains_audio": False},
        )

    def _poll_standby_wake(self) -> BrainRuntimeResultV1:
        assert self.standby_wake_listener is not None
        request = self.build_standby_wake_request()
        correlation = request.correlation_id
        attempt: Optional[WakeAttemptResult] = None

        def completed(runtime_result: BrainRuntimeResultV1) -> BrainRuntimeResultV1:
            if attempt is None:
                return runtime_result
            complete_lifecycle = getattr(
                self.standby_wake_listener,
                "complete_attempt_lifecycle",
                None,
            )
            if callable(complete_lifecycle):
                finalized = complete_lifecycle(
                    attempt.attempt_id,
                    runtime_result.current_lifecycle_state,
                )
            else:
                finalized = attempt
            if finalized is None:
                finalized = attempt
            return replace(
                runtime_result,
                data={
                    **dict(runtime_result.data or {}),
                    "wake_attempt_id": finalized.attempt_id,
                    "wake_candidate_id": finalized.candidate_id,
                    "wake_stream_generation": finalized.stream_generation,
                    "wake_capture_valid": finalized.capture_valid,
                    "wake_recognizer_invoked": finalized.recognizer_invoked,
                    "wake_infrastructure_failure": finalized.infrastructure_failure,
                },
            )

        try:
            listen_attempt = getattr(self.standby_wake_listener, "listen_attempt", None)
            if callable(listen_attempt):
                attempt = listen_attempt(request)
                if not isinstance(attempt, WakeAttemptResult):
                    return self._handle_wake_listener_failure(
                        correlation,
                        "malformed_wake_attempt_result",
                        "wake listener did not return WakeAttemptResult",
                    )
                listened = attempt.result
            else:
                listened = self.standby_wake_listener.listen_once(request)
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            return self._handle_wake_listener_failure(
                correlation,
                "wake_listener_exception",
                f"{error.__class__.__name__}:{str(error)[:120]}",
            )
        if not isinstance(listened, StandbyListenResultV1):
            return self._handle_wake_listener_failure(
                correlation,
                "malformed_wake_listener_result",
                "wake listener did not return StandbyListenResultV1",
            )
        if listened.speech_detected or not listened.success:
            self._publish(
                EVENT_RUNTIME_STANDBY_LISTENED,
                correlation,
                {
                    "status": listened.status,
                    "speech_detected": listened.speech_detected,
                    "wake_detected": listened.wake_detected,
                    "command_category": listened.command_category,
                    "classification_path": listened.classification_path,
                    "classification_reason": listened.classification_reason,
                    "capture_stop_reason": listened.capture_stop_reason,
                    "duration_ms": round(listened.duration_seconds * 1000.0, 3),
                    "processing_time_ms": round(
                        listened.processing_time_seconds * 1000.0, 3
                    ),
                    "attempt_id": listened.attempt_id,
                    "stream_generation": listened.stream_generation,
                    "candidate_number": listened.candidate_number,
                },
            )
        if listened.status == WAKE_STATUS_CANCELLED:
            self.shutdown(correlation_id=correlation, reason="wake_listener_cancelled")
            return completed(self._result(
                False,
                "cancelled",
                correlation_id=correlation,
                stop_reason="wake_listener_cancelled",
                error_code="wake_listener_cancelled",
                error_message="standby wake listening was cancelled",
            ))
        if not listened.success:
            return completed(self._handle_wake_listener_failure(
                correlation,
                listened.error_code or "wake_listener_failed",
                listened.error_message or listened.status,
            ))
        self._wake_listener_failure_count = 0
        if listened.speech_detected:
            self._publish(
                EVENT_WAKE_CANDIDATE_DETECTED,
                correlation,
                {
                    "status": listened.status,
                    "command_category": listened.command_category,
                    "wake_detected": listened.wake_detected,
                    "classification_path": listened.classification_path,
                    "sample_rate_hz": listened.sample_rate_hz,
                    "duration_ms": round(listened.duration_seconds * 1000.0, 3),
                },
            )
        if listened.command_category == WAKE_CATEGORY_ACTIVATION and listened.wake_detected:
            phrase = listened.normalized_wake_phrase or listened.matched_phrase
            self._publish(
                EVENT_WAKE_DETECTED,
                correlation,
                {"status": "verified", "matched_phrase": phrase},
            )
            return completed(self.handle_text(phrase, correlation_id=correlation))
        if listened.command_category in {WAKE_CATEGORY_SHUTDOWN, WAKE_CATEGORY_STANDBY}:
            return completed(
                self.handle_text(listened.matched_phrase, correlation_id=correlation)
            )
        if listened.speech_detected:
            self._publish(
                EVENT_WAKE_REJECTED,
                correlation,
                {
                    "status": "non_wake_speech",
                    "reason": listened.rejection_reason or "wake_phrase_not_matched",
                    "classification_path": listened.classification_path,
                },
            )
        return completed(self._result(
            True,
            "standby_listening",
            correlation_id=correlation,
            stop_reason=listened.stop_reason or "standby_poll_complete",
            data={
                "speech_detected": listened.speech_detected,
                "wake_detected": False,
                "capture_stop_reason": listened.capture_stop_reason,
                "sample_rate_hz": listened.sample_rate_hz,
                "channels": listened.channels,
                "sample_width_bytes": listened.sample_width_bytes,
                "duration_seconds": listened.duration_seconds,
                "processing_time_seconds": listened.processing_time_seconds,
                "raw_capture_duration_seconds": listened.raw_capture_duration_seconds,
                "assembled_duration_seconds": listened.assembled_duration_seconds,
                "normalized_duration_seconds": listened.normalized_duration_seconds,
            },
        ))

    def _handle_wake_listener_failure(
        self,
        correlation: str,
        error_code: str,
        error_message: str,
    ) -> BrainRuntimeResultV1:
        self._failure_count += 1
        self._wake_listener_failure_count += 1
        failure_limit = min(
            self.config.maximum_consecutive_failures,
            int(self.standby_wake_listener.config.consecutive_failure_limit)
            if self.standby_wake_listener is not None
            else self.config.maximum_consecutive_failures,
        )
        self._publish(
            EVENT_WAKE_LISTENER_FAILED,
            correlation,
            {
                "state": self.session_manager.state,
                "error_code": error_code,
                "consecutive_failure_count": self._wake_listener_failure_count,
                "failure_limit": failure_limit,
            },
            priority=PRIORITY_CRITICAL,
        )
        if self._wake_listener_failure_count >= failure_limit:
            self.session_manager.report_failure(
                correlation_id=correlation,
                reason="wake_listener_failure_limit",
                unrecoverable=True,
            )
            self.shutdown(correlation_id=correlation, reason="wake_listener_failure_limit")
            return self._result(
                False,
                "maximum_failures_reached",
                correlation_id=correlation,
                stop_reason="wake_listener_failure_limit",
                error_code=error_code,
                error_message=error_message,
            )
        return self._result(
            False,
            "wake_listener_failed",
            correlation_id=correlation,
            stop_reason="wake_listener_retry_allowed",
            error_code=error_code,
            error_message=error_message,
        )

    def _release_active_transport_resources(self) -> None:
        for adapter in (self.input_adapter, self.output_adapter):
            release = getattr(adapter, "release_active_resources", None)
            if callable(release):
                try:
                    release()
                except (OSError, RuntimeError, TypeError, ValueError):
                    self._failure_count += 1

    def _record_local_input_diagnostics(
        self,
        runtime_result: BrainRuntimeResultV1,
        *,
        lifecycle_state_before: str,
        session_id_before: str,
    ) -> None:
        recorder = getattr(self.input_adapter, "record_runtime_result", None)
        if not callable(recorder):
            return
        try:
            recorder(
                runtime_result=runtime_result,
                lifecycle_state_before=lifecycle_state_before,
                session_id_before=session_id_before,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            # Owner-terminal diagnostics are non-authoritative and cannot alter lifecycle.
            return

    def _publish(
        self,
        event_type: str,
        correlation_id: str,
        payload: Dict[str, Any],
        *,
        priority: str = PRIORITY_NORMAL,
    ) -> None:
        safe_payload = _safe_event_payload(payload)
        event = self._event_bus.publish(
            source="brain_runtime",
            type=event_type,
            payload=safe_payload,
            priority=priority,
            correlation_id=correlation_id,
            session_id=self.session_manager.session_id,
            metadata={"safe": True, "contains_private_content": False},
        )
        if self._event_history_store is None:
            return
        try:
            self._event_history_store.add(
                event,
                {
                    "success": True,
                    "decision": "recorded",
                    "status": "brain_runtime_event_recorded",
                    "metadata": {"safe": True},
                },
            )
        except (MigrationError, OSError) as error:
            self._event_history_failures.append(
                {
                    "event_type": event_type,
                    "error_type": type(error).__name__,
                    "error_message": str(error)[:160],
                }
            )
            self._event_history_failures = self._event_history_failures[-20:]

    def _result(
        self,
        success: bool,
        status: str,
        *,
        correlation_id: str,
        command_category: str = "",
        normalized_input: str = "",
        response_text: str = "",
        stop_reason: str = "",
        error_code: str = "",
        error_message: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> BrainRuntimeResultV1:
        with self._command_lock:
            lifecycle = self.session_manager.snapshot()
            return BrainRuntimeResultV1(
                success=success,
                status=status,
                runtime_id=self.runtime_id,
                current_lifecycle_state=lifecycle.current_state,
                command_category=command_category,
                normalized_input=normalized_input,
                response_text=response_text,
                stop_reason=stop_reason,
                error_code=error_code,
                error_message=error_message,
                data=dict(data or {}),
                correlation_id=correlation_id,
                session_id=lifecycle.session_id,
                metadata={"safe": True, "source": "brain_runtime"},
            )

    def _with_lifecycle_command(
        self,
        result: BrainRuntimeResultV1,
        classification: BrainRuntimeCommandClassificationV1,
    ) -> BrainRuntimeResultV1:
        return replace(
            result,
            data={
                **dict(result.data or {}),
                "lifecycle_command": _lifecycle_command_data(classification),
            },
        )

    def _loop_result(
        self,
        success: bool,
        status: str,
        iterations: int,
        terminal: BrainRuntimeResultV1,
    ) -> BrainRuntimeLoopResultV1:
        with self._command_lock:
            return BrainRuntimeLoopResultV1(
                success=success,
                status=status,
                runtime_id=self.runtime_id,
                current_lifecycle_state=self.session_manager.state,
                iteration_count=iterations,
                command_count=self._command_count,
                activation_count=self._activation_count,
                standby_return_count=self._standby_return_count,
                failure_count=self._failure_count,
                output_count=self._output_count,
                stop_reason=terminal.stop_reason or self._last_stop_reason,
                error_code=terminal.error_code,
                error_message=terminal.error_message,
                correlation_id=terminal.correlation_id,
                session_id=self.session_manager.session_id,
                metadata={"safe": True, "source": "brain_runtime"},
            )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise ValueError("brain runtime clock must return datetime")
        current = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        if self._last_clock_at is not None and current < self._last_clock_at:
            return self._last_clock_at
        self._last_clock_at = current
        return current


def normalize_runtime_phrase(value: Any) -> str:
    return normalize_spoken_phrase(value)


def _lifecycle_command_data(
    classification: BrainRuntimeCommandClassificationV1,
) -> Dict[str, Any]:
    metadata = dict(classification.metadata or {})
    return {
        "cleaned_transcript": str(
            metadata.get("lifecycle_cleaned_transcript") or ""
        ),
        "normalized_transcript": str(
            metadata.get("lifecycle_normalized_transcript") or ""
        ),
        "canonicalized_transcript": str(
            metadata.get("lifecycle_canonicalized_transcript")
            or classification.normalized_input
            or ""
        ),
        "canonical_name": str(
            metadata.get("lifecycle_canonical_name") or ""
        ),
        "matched_alias": str(
            metadata.get("lifecycle_matched_alias") or ""
        ),
        "alias_type": str(metadata.get("lifecycle_alias_type") or ""),
        "action": str(metadata.get("lifecycle_action") or LIFECYCLE_ACTION_NONE),
        "matched_phrase": str(
            metadata.get("lifecycle_matched_phrase")
            or classification.matched_phrase
            or ""
        ),
        "negation_detected": bool(
            metadata.get("lifecycle_negation_detected", False)
        ),
        "rejection_reason": str(
            metadata.get("lifecycle_rejection_reason") or ""
        ),
    }


def _canonical_runtime_terminal_reason(result: BrainRuntimeResultV1) -> str:
    """Return the explicit outer-runtime terminal reason for one loop result."""

    reason = str(getattr(result, "stop_reason", "") or "").strip()
    if reason in {"explicit_shutdown_command", "owner_shutdown_phrase"}:
        return "explicit_shutdown_command"
    if reason in {"input_cancelled", "owner_cancellation"} or str(
        getattr(result, "status", "") or ""
    ) == "cancelled":
        return "owner_cancellation"
    return reason or "unrecoverable_failure"


def _canonicalized_runtime_phrases(
    value: Any,
    label: str,
    aliases: Sequence[str],
) -> tuple[str, ...]:
    normalized = _validated_phrases(value, label)
    canonicalized = tuple(
        canonicalize_ares_name_tokens(phrase, aliases) for phrase in normalized
    )
    if len(set(canonicalized)) != len(canonicalized):
        raise ValueError(f"{label} contains duplicate phrases after alias canonicalization")
    return canonicalized


def _validated_phrases(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a sequence of phrases")
    if not MIN_PHRASE_COUNT <= len(value) <= MAX_PHRASE_COUNT:
        raise ValueError(f"{label} must contain between {MIN_PHRASE_COUNT} and {MAX_PHRASE_COUNT} phrases")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{label} must contain strings")
        phrase = normalize_runtime_phrase(item)
        if not phrase or len(phrase) > MAX_PHRASE_LENGTH:
            raise ValueError(f"{label} contains an empty or oversized phrase")
        if phrase in normalized:
            raise ValueError(f"{label} contains a duplicate normalized phrase: {phrase}")
        normalized.append(phrase)
    return tuple(normalized)


def _validate_phrase_collisions(
    activation: tuple[str, ...],
    standby: tuple[str, ...],
    shutdown: tuple[str, ...],
) -> None:
    groups = {
        "activation": set(activation),
        "standby": set(standby),
        "shutdown": set(shutdown),
    }
    pairs = (("activation", "standby"), ("activation", "shutdown"), ("standby", "shutdown"))
    for left, right in pairs:
        overlap = sorted(groups[left] & groups[right])
        if overlap:
            raise ValueError(f"brain_runtime phrase collision between {left} and {right}: {overlap[0]}")


def _validated_response(value: Any, label: str, *, required: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    clean = value.strip()
    if required and not clean:
        raise ValueError(f"{label} must not be empty")
    if len(clean) > 256 or any(ord(character) < 32 and character not in "\t" for character in clean):
        raise ValueError(f"{label} must be a bounded printable string")
    return clean


def _validated_number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}")
    return number


def _normalize_brain_response(response: Any) -> tuple[str, str, bool, str]:
    if response is None:
        return DEFAULT_UNKNOWN_RESPONSE, "unknown", True, ""
    if isinstance(response, str):
        return response.strip(), "", True, ""
    text = str(getattr(response, "text", "") or "").strip()
    skill = str(getattr(response, "skill", "") or "").strip()
    success = bool(getattr(response, "success", True))
    error = str(getattr(response, "error_message", "") or "").strip()[:160]
    return text, skill, success, error


def _safe_event_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "runtime_id",
        "state",
        "reason",
        "command_category",
        "input_length",
        "activation_count",
        "status",
        "selected_skill",
        "response_length",
        "processing_time_ms",
        "timeout_seconds",
        "error_code",
        "failure_count",
    }
    result: Dict[str, Any] = {"safe": True}
    for key, value in payload.items():
        if key in allowed and isinstance(value, (str, int, float, bool)) and len(str(value)) <= 96:
            result[key] = value
    return result
