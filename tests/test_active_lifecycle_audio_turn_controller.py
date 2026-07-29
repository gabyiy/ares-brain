from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
import wave

from core import (
    ActiveLifecycleAudioTurnController,
    AudioChunk,
    SingleTurnPipelineRuntimeInputAdapter,
    SingleTurnVoiceRequestV1,
)
from core.ActiveLifecycleAudioRecognizer import (
    ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
    ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
    ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
    ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
    ACTIVE_LIFECYCLE_CONFIRMATION_CANCELLED,
    ACTIVE_LIFECYCLE_CONFIRMATION_CONFIRMED,
    ActiveLifecycleAudioRecognitionResult,
    ActiveLifecycleConfirmationResult,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class QueuedLifecycleRecognizer:
    def __init__(self, recognitions=(), confirmations=()) -> None:
        self.recognitions = list(recognitions)
        self.confirmations = list(confirmations)
        self.recognition_paths: list[str] = []
        self.confirmation_paths: list[tuple[str, str]] = []
        self.closed = False

    def recognize_wav(self, path):
        self.recognition_paths.append(str(path))
        value = self.recognitions.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def recognize_confirmation_wav(self, path, *, expected_classification):
        self.confirmation_paths.append((str(path), expected_classification))
        value = self.confirmations.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class BlockingHighLifecycleRecognizer:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.closed = False

    def recognize_wav(self, _path):
        self.entered.set()
        if not self.release.wait(5.0):
            raise TimeoutError("test lifecycle recognizer was not released")
        return _recognition(
            ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
            canonical_phrase="shutdown ares",
        )

    def recognize_confirmation_wav(self, _path, *, expected_classification):
        raise AssertionError(
            f"unexpected confirmation for {expected_classification}"
        )

    def close(self):
        self.closed = True
        self.release.set()


def _write_wav(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = int(1800).to_bytes(2, "little", signed=True) * 1600
    with wave.open(str(path), "wb") as output:
        output.setframerate(16000)
        output.setnchannels(1)
        output.setsampwidth(2)
        output.writeframes(pcm)
    return pcm


def _chunk(tmp_path: Path, name: str = "turn.wav") -> AudioChunk:
    path = tmp_path / name
    pcm = _write_wav(path)
    return AudioChunk(
        data=pcm,
        metadata={
            "wav_path": str(path),
            "final_whisper_input_path": str(path),
        },
    )


def _recognition(
    classification: str,
    *,
    canonical_phrase: str = "",
    confidence: float | None = 0.9,
    confirmation_required: bool = False,
    proposed_classification: str = "",
    rejection_reason: str = "",
    backend_cleanup_complete: bool = True,
) -> ActiveLifecycleAudioRecognitionResult:
    text = canonical_phrase or "calculate two plus two"
    return ActiveLifecycleAudioRecognitionResult(
        classification=classification,
        canonical_phrase=canonical_phrase,
        recognized_text=text,
        recognized_tokens=tuple(text.split()),
        confidence=confidence,
        confidence_available=confidence is not None,
        recognition_backend="fake_constrained_vosk",
        rejection_reason=rejection_reason,
        confidence_tier=("medium" if confirmation_required else "high"),
        confirmation_required=confirmation_required,
        proposed_classification=proposed_classification,
        backend_cleanup_complete=backend_cleanup_complete,
    )


def _controller(recognizer, state, clock=None):
    return ActiveLifecycleAudioTurnController(
        recognizer=recognizer,
        session_id_provider=lambda: state["session"],
        lifecycle_state_provider=lambda: state["lifecycle"],
        clock=clock or FakeClock(),
        confirmation_timeout_seconds=10.0,
    )


def _payload(decision):
    return decision.data["active_lifecycle_audio"]


def test_high_confidence_lifecycle_audio_authorizes_without_executing_transition(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    recognizer = QueuedLifecycleRecognizer(
        [_recognition(ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY, canonical_phrase="goodbye ares")]
    )
    controller = _controller(recognizer, state)

    decision = controller(_chunk(tmp_path))

    assert decision.handled is True
    assert decision.continue_to_whisper is False
    assert decision.canonical_text == "goodbye ares"
    assert _payload(decision)["lifecycle_authorized"] is True
    assert _payload(decision)["selected_lifecycle_action"] == "standby"
    assert state == {"lifecycle": "ACTIVE", "session": "session-1"}
    assert recognizer.recognition_paths == [str(tmp_path / "turn.wav")]


def test_malformed_high_confidence_results_never_authorize_lifecycle(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    valid = _recognition(
        ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
        canonical_phrase="goodbye ares",
    )
    malformed = [
        replace(valid, confidence=None, confidence_available=False),
        replace(valid, confidence=float("nan")),
        replace(valid, confidence=0.10),
        replace(valid, confidence_tier="medium"),
        replace(valid, canonical_phrase="standby ares"),
        SimpleNamespace(
            **{
                **valid.__dict__,
                "selected_lifecycle_action": "shutdown",
            }
        ),
    ]
    recognizer = QueuedLifecycleRecognizer(malformed)
    controller = _controller(recognizer, state)

    decisions = [
        controller(_chunk(tmp_path, f"malformed-{index}.wav"))
        for index in range(len(malformed))
    ]

    assert all(decision.handled is False for decision in decisions)
    assert all(decision.continue_to_whisper is True for decision in decisions)
    assert all(
        _payload(decision)["lifecycle_authorized"] is False
        for decision in decisions
    )
    assert all(
        _payload(decision)["rejection_reason"]
        == "controller_high_confidence_authorization_invariant_failed"
        for decision in decisions
    )
    assert state == {"lifecycle": "ACTIVE", "session": "session-1"}


def test_ordinary_and_backend_error_continue_same_wav_to_whisper(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    recognizer = QueuedLifecycleRecognizer(
        [
            _recognition(
                ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
                rejection_reason="exact_lifecycle_phrase_not_matched",
            ),
            RuntimeError("vosk backend unavailable"),
        ]
    )
    controller = _controller(recognizer, state)

    ordinary = controller(_chunk(tmp_path, "ordinary.wav"))
    backend_error = controller(_chunk(tmp_path, "error.wav"))

    assert ordinary.handled is False and ordinary.continue_to_whisper is True
    assert backend_error.handled is False and backend_error.continue_to_whisper is True
    assert _payload(ordinary)["whisper_fallback_required"] is True
    assert "RuntimeError" in _payload(backend_error)["rejection_reason"]
    assert recognizer.recognition_paths == [
        str(tmp_path / "ordinary.wav"),
        str(tmp_path / "error.wav"),
    ]


def test_unreaped_backend_blocks_lifecycle_and_whisper_fallback(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    recognizer = QueuedLifecycleRecognizer(
        [
            _recognition(
                ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
                rejection_reason="lifecycle_backend_cleanup_incomplete",
                backend_cleanup_complete=False,
            )
        ]
    )
    controller = _controller(recognizer, state)

    decision = controller(_chunk(tmp_path, "unreaped.wav"))

    assert decision.handled is True
    assert decision.continue_to_whisper is False
    assert decision.canonical_text == ""
    assert decision.status == "active_lifecycle_audio_cleanup_incomplete"
    assert _payload(decision)["backend_cleanup_complete"] is False
    assert _payload(decision)["routing_blocked"] is True
    assert _payload(decision)["lifecycle_authorized"] is False
    assert _payload(decision)["selected_lifecycle_action"] == "none"
    assert _payload(decision)["whisper_fallback_required"] is False
    assert state == {"lifecycle": "ACTIVE", "session": "session-1"}


def test_medium_shutdown_requires_whisper_fallback_in_same_turn(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    clock = FakeClock()
    recognizer = QueuedLifecycleRecognizer(
        [
            _recognition(
                ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
                canonical_phrase="shutdown ares",
                confidence=0.66,
                confirmation_required=True,
                proposed_classification=ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
            )
        ],
    )
    controller = _controller(recognizer, state, clock)

    proposed = controller(_chunk(tmp_path, "proposal.wav"))
    assert proposed.handled is False
    assert proposed.continue_to_whisper is True
    assert proposed.canonical_text == ""
    assert _payload(proposed)["confirmation_required"] is True
    assert _payload(proposed)["lifecycle_authorized"] is False
    assert _payload(proposed)["whisper_fallback_required"] is True
    assert _payload(proposed)["selected_lifecycle_action"] == "none"
    assert controller.pending_confirmation() is None
    assert recognizer.confirmation_paths == []


def test_medium_standby_does_not_create_pending_confirmation(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    recognizer = QueuedLifecycleRecognizer(
        [
            _recognition(
                ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
                canonical_phrase="goodbye ares",
                confidence=0.56,
                confirmation_required=True,
                proposed_classification=ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
            )
        ],
    )
    controller = _controller(recognizer, state)

    decision = controller(_chunk(tmp_path, "proposal.wav"))

    assert decision.handled is False
    assert decision.continue_to_whisper is True
    assert _payload(decision)["whisper_fallback_required"] is True
    assert controller.pending_confirmation() is None


def test_medium_result_never_invokes_confirmation_backend(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    recognizer = QueuedLifecycleRecognizer(
        [
            _recognition(
                ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
                canonical_phrase="shutdown ares",
                confidence=0.66,
                confirmation_required=True,
                proposed_classification=ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
            )
        ],
    )
    controller = _controller(recognizer, state)

    decision = controller(_chunk(tmp_path, "proposal.wav"))

    assert decision.handled is False
    assert decision.continue_to_whisper is True
    assert _payload(decision)["lifecycle_authorized"] is False
    assert _payload(decision)["whisper_fallback_required"] is True
    assert recognizer.confirmation_paths == []
    assert controller.pending_confirmation() is None
    assert state == {"lifecycle": "ACTIVE", "session": "session-1"}


def test_repeated_medium_results_always_fall_back_without_authorization(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    medium = _recognition(
        ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
        canonical_phrase="shutdown ares",
        confidence=0.66,
        confirmation_required=True,
        proposed_classification=ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
    )
    recognizer = QueuedLifecycleRecognizer(
        [medium, medium],
    )
    controller = _controller(recognizer, state)

    weak = controller(_chunk(tmp_path, "weak-proposal.wav"))
    repeated = controller(_chunk(tmp_path, "second-proposal.wav"))

    for decision in (weak, repeated):
        assert decision.handled is False
        assert decision.continue_to_whisper is True
        assert _payload(decision)["lifecycle_authorized"] is False
        assert _payload(decision)["whisper_fallback_required"] is True
    assert recognizer.confirmation_paths == []
    assert controller.pending_confirmation() is None
    assert state == {"lifecycle": "ACTIVE", "session": "session-1"}


def test_medium_fallback_is_stateless_across_time_and_session_changes(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    clock = FakeClock()
    medium = _recognition(
        ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
        canonical_phrase="shutdown ares",
        confidence=0.64,
        confirmation_required=True,
        proposed_classification=ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
    )
    recognizer = QueuedLifecycleRecognizer([medium, medium], [])
    controller = _controller(recognizer, state, clock)

    controller(_chunk(tmp_path, "first.wav"))
    clock.advance(10.0)
    expired = controller(_chunk(tmp_path, "expired.wav"))
    assert expired.continue_to_whisper is True
    assert _payload(expired)["whisper_fallback_required"] is True
    assert recognizer.confirmation_paths == []
    state["session"] = "session-2"
    recognizer.recognitions.append(medium)
    changed = controller(_chunk(tmp_path, "changed.wav"))
    assert changed.continue_to_whisper is True
    assert _payload(changed)["whisper_fallback_required"] is True
    assert controller.pending_confirmation() is None


class FinalizedAudioPipeline:
    def __init__(self, fallback_text="calculate 2 plus 2") -> None:
        self.fallback_text = fallback_text
        self.capture_count = 0
        self.whisper_count = 0
        self.stop_count = 0

    def run_once(self, request, **kwargs):
        self.capture_count += 1
        path = Path(request.recording_output_path)
        chunk = AudioChunk(
            data=_write_wav(path),
            metadata={
                "wav_path": str(path),
                "final_whisper_input_path": str(path),
            },
        )
        decision = kwargs["finalized_audio_hook"](chunk)
        if decision.continue_to_whisper:
            self.whisper_count += 1
            text = self.fallback_text
            status = "runtime_transport_captured"
            raw_hook = kwargs.get("raw_transcript_hook")
            if raw_hook is not None:
                raw_hook(text)
        else:
            text = decision.canonical_text
            status = decision.status
        return SimpleNamespace(
            success=True,
            status=status,
            recognized_text=text,
            raw_transcript=text,
            cleaned_transcript=text,
            error_stage="",
            error_reason="",
            recording_status="completed_after_silence",
            recording_duration_seconds=0.1,
            transcription_processing_time_seconds=(0.2 if decision.continue_to_whisper else 0.0),
            recorded_wav_path=str(path),
            data={
                "finalized_audio_decision": {
                    "handled": decision.handled,
                    "continue_to_whisper": decision.continue_to_whisper,
                    "status": decision.status,
                    "canonical_text": decision.canonical_text,
                    "data": dict(decision.data),
                },
                "recording": {
                    "stop_reason": "completed_after_silence",
                    "raw_duration_seconds": 0.1,
                    "assembled_duration_seconds": 0.1,
                },
                "audio_finalization": {
                    "wav_path": str(path),
                    "sample_rate_hz": 16000,
                    "channels": 1,
                    "sample_width_bytes": 2,
                },
            },
        )

    def stop(self, request=None):
        self.stop_count += 1
        return SimpleNamespace(success=True, status="stopped")


def _request(tmp_path):
    return SingleTurnVoiceRequestV1(
        recording_output_path=str(tmp_path / "base.wav"),
        capture_mode="auto_stop",
        cleanup_policy="keep",
        correlation_id="corr-controller",
    )


def test_runtime_adapter_high_lifecycle_skips_whisper_and_exports_authorization(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    recognizer = QueuedLifecycleRecognizer(
        [_recognition(ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN, canonical_phrase="shutdown ares")]
    )
    controller = _controller(recognizer, state)
    pipeline = FinalizedAudioPipeline()
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: state["session"],
        lifecycle_state_provider=lambda: state["lifecycle"],
        active_lifecycle_audio_controller=controller,
    )

    result = adapter.wait_for_input(1.0)

    assert result.status == "input"
    assert result.text == "shutdown ares"
    assert result.metadata["active_lifecycle_audio_checked"] is True
    assert result.metadata["active_lifecycle_audio_authorized"] is True
    assert result.metadata["active_lifecycle_audio_authorized_action"] == "shutdown"
    assert result.metadata["active_lifecycle_classification"] == "shutdown"
    assert result.metadata["active_lifecycle_recognized_tokens"] == "shutdown ares"
    assert result.metadata["active_lifecycle_whisper_fallback"] is False
    assert result.metadata["active_lifecycle_whisper_fallback_completed"] is False
    assert pipeline.capture_count == 1
    assert pipeline.whisper_count == 0
    assert recognizer.recognition_paths == [adapter.last_result.recorded_wav_path]


def test_runtime_adapter_ordinary_reuses_one_capture_for_whisper(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    recognizer = QueuedLifecycleRecognizer(
        [_recognition(ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY)]
    )
    controller = _controller(recognizer, state)
    pipeline = FinalizedAudioPipeline()
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: state["session"],
        lifecycle_state_provider=lambda: state["lifecycle"],
        active_lifecycle_audio_controller=controller,
    )

    result = adapter.wait_for_input(1.0)

    assert result.status == "input"
    assert result.text == "calculate 2 plus 2"
    assert result.metadata["active_lifecycle_audio_authorized"] is False
    assert result.metadata["active_lifecycle_whisper_fallback"] is True
    assert result.metadata["active_lifecycle_whisper_fallback_completed"] is True
    assert pipeline.capture_count == 1
    assert pipeline.whisper_count == 1
    assert recognizer.recognition_paths == [adapter.last_result.recorded_wav_path]


def test_runtime_adapter_never_starts_whisper_with_unreaped_lifecycle_worker(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    recognizer = QueuedLifecycleRecognizer(
        [
            _recognition(
                ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
                rejection_reason="lifecycle_backend_cleanup_incomplete",
                backend_cleanup_complete=False,
            )
        ]
    )
    controller = _controller(recognizer, state)
    pipeline = FinalizedAudioPipeline()
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: state["session"],
        lifecycle_state_provider=lambda: state["lifecycle"],
        active_lifecycle_audio_controller=controller,
    )

    result = adapter.wait_for_input(1.0)
    finalized = adapter.last_result.data["finalized_audio_decision"]
    payload = finalized["data"]["active_lifecycle_audio"]

    assert result.status == "timeout"
    assert result.text == ""
    assert result.metadata["runtime_terminal"] is False
    assert result.metadata["capture_status"] == (
        "active_lifecycle_audio_cleanup_incomplete"
    )
    assert pipeline.capture_count == 1
    assert pipeline.whisper_count == 0
    assert finalized["handled"] is True
    assert finalized["continue_to_whisper"] is False
    assert payload["routing_blocked"] is True
    assert payload["lifecycle_authorized"] is False
    assert payload["whisper_fallback_required"] is False
    assert state == {"lifecycle": "ACTIVE", "session": "session-1"}


def test_runtime_adapter_runs_whisper_fallback_for_medium_lifecycle_evidence(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    recognizer = QueuedLifecycleRecognizer(
        [
            _recognition(
                ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
                canonical_phrase="goodbye ares",
                confidence=0.55,
                confirmation_required=True,
                proposed_classification=ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
            )
        ],
    )
    controller = _controller(recognizer, state)
    pipeline = FinalizedAudioPipeline()
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: state["session"],
        lifecycle_state_provider=lambda: state["lifecycle"],
        active_lifecycle_audio_controller=controller,
    )

    proposed = adapter.wait_for_input(1.0)

    assert proposed.status == "input"
    assert proposed.text == "calculate 2 plus 2"
    assert proposed.metadata["active_lifecycle_whisper_fallback"] is True
    assert proposed.metadata["active_lifecycle_whisper_fallback_completed"] is True
    assert pipeline.capture_count == 1
    assert pipeline.whisper_count == 1
    assert recognizer.confirmation_paths == []


def test_runtime_adapter_resource_release_resets_and_close_releases_recognizer(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    recognizer = QueuedLifecycleRecognizer(
        [
            _recognition(
                ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
                canonical_phrase="shutdown ares",
                confirmation_required=True,
                proposed_classification=ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
            )
        ]
    )
    controller = _controller(recognizer, state)
    pipeline = FinalizedAudioPipeline()
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: state["session"],
        lifecycle_state_provider=lambda: state["lifecycle"],
        active_lifecycle_audio_controller=controller,
    )

    adapter.wait_for_input(1.0)
    assert controller.pending_confirmation() is None
    adapter.release_active_resources()
    assert controller.pending_confirmation() is None
    adapter.close()
    assert recognizer.closed is True


def test_adapter_close_during_lifecycle_audio_cannot_export_shutdown_input(tmp_path):
    state = {"lifecycle": "ACTIVE", "session": "session-1"}
    recognizer = BlockingHighLifecycleRecognizer()
    controller = _controller(recognizer, state)
    pipeline = FinalizedAudioPipeline()
    adapter = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=_request(tmp_path),
        session_id_provider=lambda: state["session"],
        lifecycle_state_provider=lambda: state["lifecycle"],
        active_lifecycle_audio_controller=controller,
    )
    result_holder = {}
    worker = Thread(
        target=lambda: result_holder.setdefault(
            "result",
            adapter.wait_for_input(5.0),
        )
    )
    worker.start()
    assert recognizer.entered.wait(5.0)

    adapter.close()
    worker.join(5.0)

    assert worker.is_alive() is False
    result = result_holder["result"]
    assert result.status == "cancelled"
    assert result.text == ""
    assert result.metadata.get("active_lifecycle_audio_authorized") is not True
    assert pipeline.capture_count == 1
    assert pipeline.whisper_count == 0
    assert pipeline.stop_count >= 1
    assert recognizer.closed is True
    assert state == {"lifecycle": "ACTIVE", "session": "session-1"}
