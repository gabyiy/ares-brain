from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from core import (
    CONTRACT_STANDBY_LISTEN_RESULT,
    CONTRACT_WAKE_DETECTION_RESULT,
    CONTRACT_WAKE_LISTENER_REQUEST,
    CONTRACT_WAKE_LISTENER_RESULT,
    CONTRACT_WAKE_LISTENER_SNAPSHOT,
    DEFAULT_CONTRACT_REGISTRY,
    LinuxStandbyWakeListener,
    QueuedStandbyWakeListener,
    StandbyListenResultV1,
    WakeDetectionResultV1,
    WakeListenerConfig,
    WakeListenerRequestV1,
    WakeListenerResultV1,
    WakeListenerSnapshotV1,
    WakeRecognizerLocalDiagnostics,
    WakeRecognizerResultV1,
    VoiceRuntimeGate,
    build_standby_wake_listener_manifest,
    classify_wake_transcript,
    classify_constrained_recognition,
    normalize_wake_phrase,
)


def _ok(status: str = "healthy", **values):
    return SimpleNamespace(success=True, status=status, error_message="", **values)


def _failed(status: str, message: str = "failed"):
    return SimpleNamespace(success=False, status=status, error_message=message)


def _write_wav(
    path: Path,
    *,
    sample_rate: int = 16000,
    seconds: float = 0.2,
    amplitude: int = 800,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = max(1, int(sample_rate * seconds))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(
            int(amplitude).to_bytes(2, "little", signed=True) * samples
        )


class FakeMicrophone:
    def __init__(
        self,
        *,
        capture_status: str = "completed_after_silence",
        speech: bool = True,
        raw_seconds: float = 0.2,
        candidate_seconds: float = 0.2,
    ):
        self.capture_status = capture_status
        self.speech = speech
        self.started = False
        self.stopped = False
        self.cancelled = False
        self.record_calls = []
        self.raw_path = ""
        self.normalized_path = ""
        self.raw_seconds = raw_seconds
        self.candidate_seconds = candidate_seconds
        self.stream_open_count = 0
        self.stream_close_count = 0
        self.calibration_count = 0
        self.stream_handle = None
        self.candidate_reset_count = 0

    def start(self):
        self.started = True
        self.stopped = False
        return _ok("started")

    def health_check(self):
        return _ok("healthy")

    def stop(self):
        self.stopped = True
        self.started = False
        return _ok("stopped")

    def cancel_current(self):
        self.cancelled = True
        if self.stream_handle is not None:
            self.stream_handle.closed = True

    def open_persistent_stream(self, *, owner, device=None):
        if self.stream_handle is not None and not self.stream_handle.closed:
            return self.stream_handle
        self.stream_open_count += 1
        self.stream_handle = SimpleNamespace(
            owner=owner,
            device=device,
            closed=False,
            stream_id=f"fake-stream-{self.stream_open_count}",
            alsa_handle_id=f"fake-alsa-handle-{self.stream_open_count}",
        )
        return self.stream_handle

    def calibrate_persistent_stream(self, handle, request, **_kwargs):
        assert handle is self.stream_handle
        self.calibration_count += 1
        return SimpleNamespace(
            success=True,
            status="calibrated",
            thresholds=SimpleNamespace(
                speech_start_rms=request.speech_start_rms,
                speech_continue_rms=request.speech_continue_rms,
                silence_rms=request.silence_rms,
                to_dict=lambda: {
                    "speech_start_rms": request.speech_start_rms,
                    "speech_continue_rms": request.speech_continue_rms,
                    "silence_rms": request.silence_rms,
                },
            ),
            ambient_statistics=None,
            error_code="",
            error_message="",
        )

    def record_persistent_until_silence(self, handle, output_path, **kwargs):
        assert handle is self.stream_handle
        assert not handle.closed
        return self.record_until_silence(output_path, **kwargs)

    def close_persistent_stream(self, handle, *, owner):
        assert handle is self.stream_handle
        assert handle.owner == owner
        if not handle.closed:
            handle.closed = True
            self.stream_close_count += 1
        return _ok("closed")

    def reset_persistent_candidate(self, handle, **_kwargs):
        assert handle is self.stream_handle
        assert not handle.closed
        self.candidate_reset_count += 1
        return {
            "success": True,
            "status": "candidate_state_reset",
            "stale_pcm_frames_discarded": 0,
        }

    def record_until_silence(self, output_path, **kwargs):
        self.record_calls.append((str(output_path), dict(kwargs)))
        if self.capture_status in {"no_speech_timeout", "timeout"}:
            return SimpleNamespace(
                success=False,
                status=self.capture_status,
                speech_detected=False,
                stop_reason=self.capture_status,
                error_message=self.capture_status,
            )
        if self.capture_status == "cancelled":
            return SimpleNamespace(
                success=False,
                status="cancelled",
                speech_detected=False,
                stop_reason="cancelled",
                error_message="cancelled",
            )
        if self.capture_status == "device_error":
            return SimpleNamespace(
                success=False,
                status="device_error",
                speech_detected=False,
                stop_reason="device_error",
                error_message="device_error",
            )
        normalized = Path(output_path).with_name("normalized-current-turn.wav")
        assembled = Path(output_path).with_name("assembled-current-turn.wav")
        raw = Path(output_path).with_name("raw-hardware-44100.wav")
        _write_wav(raw, sample_rate=44100, seconds=self.raw_seconds)
        _write_wav(assembled, sample_rate=16000, seconds=self.candidate_seconds)
        _write_wav(normalized, sample_rate=16000, seconds=self.candidate_seconds)
        self.raw_path = str(raw)
        self.normalized_path = str(normalized)
        return SimpleNamespace(
            success=True,
            status=self.capture_status,
            speech_detected=self.speech,
            stop_reason=self.capture_status,
            error_message="",
            wav_path=str(normalized),
            normalized_wav_path=str(normalized),
            assembled_wav_path=str(assembled),
            final_whisper_input_path=str(normalized),
            raw_wav_path=str(raw),
            duration_seconds=self.candidate_seconds,
            raw_duration_seconds=self.raw_seconds,
            assembled_duration_seconds=self.candidate_seconds,
            normalized_duration_seconds=self.candidate_seconds,
            whisper_input_duration_seconds=self.candidate_seconds,
            total_frames_read=int(self.raw_seconds / 0.02),
            final_assembled_sample_count=int(self.candidate_seconds * 16000),
            normalized_sample_count=int(self.candidate_seconds * 16000),
            normalized_sample_rate_hz=16000,
            normalized_channels=1,
            normalized_sample_width_bytes=2,
            ambient_rms=40.0,
            speech_rms=1200.0,
            peak_amplitude=17000,
            derived_speech_start_rms=200.0,
            derived_speech_continue_rms=160.0,
            derived_silence_rms=120.0,
        )


class FakeWakeRecognizer:
    recognizer_name = "fake_vosk_constrained_grammar"

    def __init__(
        self,
        text: str = "Ares",
        *,
        status: str = "recognized",
        success: bool = True,
        confidence: float | None = 0.95,
    ):
        self.text = text
        self.status = status
        self.success = success
        self.confidence = confidence
        self.paths = []
        self.started = False
        self.last_diagnostics = None

    def start(self):
        self.started = True
        return _ok("started")

    def health_check(self):
        return _ok("healthy") if self.started else _failed("not_started")

    def stop(self):
        self.started = False
        return _ok("stopped")

    def cancel(self):
        return _ok("cancelled")

    def recognize_wav(self, request):
        self.paths.append((request.audio_path, request, Path(request.audio_path).exists()))
        if not self.success:
            return WakeRecognizerResultV1(
                success=False,
                status=self.status,
                recognizer_name=self.recognizer_name,
                error_code=self.status,
                error_message=self.status,
                correlation_id=request.correlation_id,
            )
        words = []
        if self.text and self.confidence is not None:
            words = [
                {"word": token, "conf": self.confidence}
                for token in normalize_wake_phrase(self.text).split()
            ]
        result = classify_constrained_recognition(
            self.text,
            words,
            wake_phrases=request.wake_phrases,
            wake_phrase_aliases=request.wake_phrase_aliases,
            standby_phrases=request.standby_phrases,
            shutdown_phrases=request.shutdown_phrases,
            canonical_wake_phrase=request.canonical_wake_phrase,
            minimum_confidence=request.minimum_confidence,
            medium_confidence=request.medium_confidence,
            allow_exact_wake_without_confidence=(
                request.allow_exact_wake_without_confidence
            ),
            recognizer_name=self.recognizer_name,
            runtime_id=request.runtime_id,
            lifecycle_state=request.lifecycle_state,
            correlation_id=request.correlation_id,
            model_path="models/vosk/test-model",
            grammar_phrase_count=len(request.wake_phrases) + 1,
            processing_time_seconds=0.125,
            audio_duration_seconds=request.audio_duration_seconds,
            maximum_duplicate_collapse_audio_seconds=(
                request.maximum_duplicate_collapse_audio_seconds
            ),
        )
        self.last_diagnostics = WakeRecognizerLocalDiagnostics(
            recognizer_name=self.recognizer_name,
            raw_recognition_result=f'{{"text": "{self.text}"}}',
            recognized_text=self.text,
            normalized_phrase=normalize_wake_phrase(self.text),
            confidence=result.confidence,
            confidence_available=result.confidence_available,
            classification="accepted" if result.wake_detected else "rejected",
            classification_reason=result.classification_reason,
            rejection_reason=result.rejection_reason,
            selected_alias=result.selected_alias,
            selected_wake_phrase=result.selected_wake_phrase,
            canonical_wake_phrase=result.canonical_wake_phrase,
            model_path="models/vosk/test-model",
            grammar_phrase_count=len(request.wake_phrases) + 1,
            processing_time_seconds=0.125,
        )
        return result


def _request(**changes) -> WakeListenerRequestV1:
    values = {
        "runtime_id": "runtime-test",
        "lifecycle_state": "STANDBY",
        "listener_timeout_seconds": 3.0,
        "wake_phrase_aliases": ["ares", "aris", "aries"],
        "wake_phrase_prefixes": ["", "hey", "hello", "wake up", "okay"],
        "standby_phrases": ["goodbye ares"],
        "shutdown_phrases": ["shutdown ares"],
        "correlation_id": "wake-test-correlation",
    }
    values.update(changes)
    return WakeListenerRequestV1(**values)


@pytest.mark.parametrize(
    ("name", "contract_type"),
    [
        (CONTRACT_WAKE_LISTENER_REQUEST, WakeListenerRequestV1),
        (CONTRACT_WAKE_LISTENER_RESULT, WakeListenerResultV1),
        (CONTRACT_WAKE_DETECTION_RESULT, WakeDetectionResultV1),
        (CONTRACT_WAKE_LISTENER_SNAPSHOT, WakeListenerSnapshotV1),
        (CONTRACT_STANDBY_LISTEN_RESULT, StandbyListenResultV1),
    ],
)
def test_wake_contracts_are_versioned_and_registered(name, contract_type):
    contract = contract_type()
    assert contract.contract_name == name
    assert contract.contract_version == "v1"
    assert DEFAULT_CONTRACT_REGISTRY.current_version(name) == "v1"


def test_wake_configuration_defaults_are_bounded_and_raspberry_pi_safe():
    config = WakeListenerConfig()
    assert config.microphone_device == "plughw:2,0"
    assert config.vosk_model_path.endswith("vosk-model-small-en-us-0.15")
    assert config.minimum_recognition_confidence == 0.55
    assert config.frame_duration_ms == 20
    assert config.speech_wait_timeout_seconds == 3.0
    assert config.maximum_utterance_seconds == 1.6
    assert config.speech_start_rms > config.speech_continue_rms >= config.silence_rms
    assert config.calibration_enabled is True
    assert config.wake_phrase_aliases == ("ares", "aris", "aries")
    assert config.wake_phrase_prefixes == ("", "hey", "hello", "wake up", "okay")
    assert config.pre_roll_seconds == 0.4
    assert config.silence_duration_seconds == 0.55
    assert config.speech_end_padding_seconds == 0.12
    assert config.medium_recognition_confidence == 0.40
    assert config.allow_exact_wake_without_confidence is True
    assert config.medium_confidence_confirmation_count == 2
    assert config.recalibration_interval_seconds == 300.0
    assert config.minimum_speech_continue_rms == 160
    assert config.minimum_silence_rms == 120
    assert config.retain_diagnostic_audio is False


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": 1},
        {"wake_phrase_aliases": []},
        {"speech_start_rms": 100, "speech_continue_rms": 160},
        {"speech_continue_rms": 100, "silence_rms": 120},
        {"speech_wait_timeout_seconds": 0},
        {"maximum_utterance_seconds": 50},
        {"minimum_recognition_confidence": True},
        {"minimum_recognition_confidence": 0.39},
        {"minimum_recognition_confidence": float("nan")},
        {"medium_recognition_confidence": 0.8},
        {"allow_exact_wake_without_confidence": 1},
        {"medium_confidence_confirmation_count": 1},
        {"recalibration_interval_seconds": True},
        {"frame_duration_ms": True},
        {"frame_duration_ms": 100},
        {"calibration_enabled": True, "calibration_duration_seconds": 0},
        {"microphone_device": "bad\x00device"},
        {"vosk_model_path": ""},
        {"retry_delay_seconds": float("nan")},
    ],
)
def test_wake_configuration_rejects_malformed_or_unsafe_values(changes):
    with pytest.raises(ValueError):
        WakeListenerConfig(**changes)


def test_wake_configuration_rejects_unknown_mapping_fields():
    with pytest.raises(ValueError, match="Unknown standby_wake_listener"):
        WakeListenerConfig.from_mapping({"invented": True})


def test_wake_alias_configuration_is_normalized_bounded_and_collision_safe():
    config = WakeListenerConfig(
        wake_phrase_aliases=("ARES!", "Aris"),
        wake_phrase_prefixes=("", "HEY,", "Okay"),
    )
    assert config.wake_phrase_aliases == ("ares", "aris")
    assert config.wake_phrase_prefixes == ("", "hey", "okay")
    assert "hey aris" in config.wake_phrases
    assert WakeListenerConfig(
        wake_phrase_aliases=("Ares", "ares.", "Aris")
    ).wake_phrase_aliases == ("ares", "aris")
    with pytest.raises(ValueError, match="at most 8"):
        WakeListenerConfig(wake_phrase_aliases=tuple(f"alias{index}" for index in range(9)))
    with pytest.raises(ValueError, match="at most 24"):
        WakeListenerConfig(wake_phrase_aliases=("a" * 25,))
    with pytest.raises(ValueError, match="overlap"):
        classify_wake_transcript(
            "shutdown ares",
            wake_phrase_prefixes=("shutdown",),
            shutdown_phrases=("shutdown ares",),
        )
    with pytest.raises(ValueError, match="overlap"):
        classify_wake_transcript(
            "goodbye ares",
            wake_phrase_prefixes=("goodbye",),
            standby_phrases=("goodbye ares",),
        )


@pytest.mark.parametrize(
    "text",
    [
        "Ares",
        "Aris",
        "ARES.",
        "Aris.",
        "Aries",
        "  Hey, Ares!  ",
        "Hey Aris",
        "Hello, Aries",
        "Wake up Aris",
    ],
)
def test_exact_wake_phrase_normalization_accepts_bounded_variants(text):
    result = classify_wake_transcript(text)
    assert result.wake_detected is True
    assert result.command_category == "activation"
    assert result.classification_path == "exact"
    assert result.classification_reason == "accepted_exact_wake_phrase"
    assert result.normalized_wake_phrase == "ares"


@pytest.mark.parametrize(
    "text",
    [
        "I played God of War with Ares",
        "I read about Ares yesterday",
        "I spoke to Aris yesterday",
        "compare statistics",
        "nearest shop",
        "address the issue",
        "where is Ares located",
        "Where is Ares?",
        "Ares is a Greek god",
        "my game character is named Ares",
        "My character is called Ares",
        "Hello, are his shoes ready?",
        "Harris",
        "Paris",
        "tell me about Aries",
        "what is the Aries zodiac sign",
        "Areas",
        "Air",
        "Bye",
        "Alrighty",
        "Okay",
    ],
)
def test_wake_recognition_rejects_substrings_and_unrelated_sentences(text):
    result = classify_wake_transcript(text)
    assert result.wake_detected is False
    assert result.command_category == "non_wake"
    assert result.normalized_wake_phrase == ""
    assert result.rejection_reason == "exact_wake_phrase_not_matched"
    assert result.classification_path == "exact"


@pytest.mark.parametrize(
    "text",
    [
        "ares ares",
        "aris aris",
        "okay okay ares",
    ],
)
def test_non_grammar_repetition_or_prefixes_are_rejected(text):
    result = classify_wake_transcript(text)
    assert result.wake_detected is False
    assert result.rejection_reason == "exact_wake_phrase_not_matched"


def test_aris_alias_returns_canonical_ares_activation_without_fuzzy_matching():
    result = classify_wake_transcript("Hey, Aris.")
    assert result.selected_alias == "aris"
    assert result.selected_wake_phrase == "hey aris"
    assert result.canonical_wake_phrase == "ares"
    assert result.normalized_wake_phrase == "ares"


def test_wake_classifier_recognizes_bounded_shutdown_and_standby_controls():
    shutdown = classify_wake_transcript("Shutdown, Ares", shutdown_phrases=["shutdown ares"])
    standby = classify_wake_transcript("Goodbye, Ares", standby_phrases=["goodbye ares"])
    assert (shutdown.command_category, shutdown.wake_detected) == ("shutdown", False)
    assert (standby.command_category, standby.wake_detected) == ("standby", False)


def test_phrase_normalization_is_unicode_punctuation_and_whitespace_tolerant():
    assert normalize_wake_phrase("  HEY,\tARES… ") == "hey ares"


def test_queued_listener_lifecycle_no_speech_wake_cancel_and_stop():
    listener = QueuedStandbyWakeListener([None, "Ares"])
    assert listener.start(runtime_id="runtime-test").success
    assert listener.health().success
    no_speech = listener.listen_once(_request())
    wake = listener.listen_once(_request())
    assert no_speech.status == "no_speech"
    assert wake.wake_detected
    assert listener.snapshot().wake_detection_count == 1
    assert listener.cancel().success
    assert listener.listen_once(_request()).status == "cancelled"
    assert listener.stop().success
    assert listener.snapshot().listener_state == "stopped"


def test_linux_listener_starts_health_checks_and_stops_dependencies(tmp_path):
    microphone = FakeMicrophone()
    stt = FakeWakeRecognizer()
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=stt,
        project_root=tmp_path,
    )
    assert listener.start(runtime_id="runtime-test").status == "started"
    assert listener.health().success
    assert listener.stop().success
    assert microphone.started is False
    assert microphone.stopped is True


def test_linux_listener_reuses_one_stream_and_one_calibration_for_rejections(tmp_path):
    microphone = FakeMicrophone()
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=FakeWakeRecognizer("unrelated speech"),
        project_root=tmp_path,
    )
    assert listener.start().success
    results = [listener.listen_once(_request()) for _ in range(10)]
    snapshot = listener.snapshot()
    assert all(not result.wake_detected for result in results)
    assert microphone.stream_open_count == 1
    assert microphone.calibration_count == 1
    assert microphone.stream_close_count == 0
    assert snapshot.stream_open_count == 1
    assert snapshot.calibration_count == 1
    assert snapshot.candidate_count == 10
    assert snapshot.stream_active
    assert snapshot.stream_instance_id == "fake-stream-1"
    assert snapshot.alsa_handle_id == "fake-alsa-handle-1"
    assert snapshot.stream_open_reasons == ["listener_start"]
    assert snapshot.calibration_reasons == [
        "listener_start:initial_calibration"
    ]
    listener.stop()


def test_linux_listener_recalibrates_in_place_only_after_interval(tmp_path):
    now = [100.0]
    microphone = FakeMicrophone(capture_status="no_speech_timeout", speech=False)
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=FakeWakeRecognizer(),
        config=WakeListenerConfig(recalibration_interval_seconds=300.0),
        project_root=tmp_path,
        clock=lambda: now[0],
    )
    listener.start()
    listener.listen_once(_request())
    now[0] = 399.999
    listener.listen_once(_request())
    assert microphone.stream_open_count == 1
    assert microphone.calibration_count == 1

    now[0] = 400.0
    listener.listen_once(_request())
    assert microphone.stream_open_count == 1
    assert microphone.stream_close_count == 0
    assert microphone.calibration_count == 2
    listener.stop()


def test_linux_listener_manual_recalibration_reuses_current_stream(tmp_path):
    microphone = FakeMicrophone(capture_status="no_speech_timeout", speech=False)
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=FakeWakeRecognizer(),
        config=WakeListenerConfig(recalibration_interval_seconds=0.0),
        project_root=tmp_path,
    )
    listener.start()
    listener.listen_once(_request())
    assert listener.request_recalibration().status == "recalibration_requested"
    listener.listen_once(_request())
    assert microphone.stream_open_count == 1
    assert microphone.stream_close_count == 0
    assert microphone.calibration_count == 2
    listener.stop()


def test_linux_listener_closes_on_leave_and_reopens_for_new_standby(tmp_path):
    microphone = FakeMicrophone(capture_status="no_speech_timeout", speech=False)
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=FakeWakeRecognizer(),
        project_root=tmp_path,
    )
    listener.start(runtime_id="runtime-one")
    assert listener.leave_standby(
        "activation",
        handoff_destination="acknowledgement_playback",
    ).success
    assert microphone.stream_close_count == 1
    assert listener.enter_standby(
        runtime_id="runtime-one",
        reason="runtime_return_to_standby",
        handoff_source="active_command",
    ).success
    snapshot = listener.snapshot()
    assert snapshot.stream_open_count == 2
    assert snapshot.calibration_count == 2
    assert snapshot.stream_active
    assert snapshot.stream_open_reasons == [
        "listener_start",
        "runtime_return_to_standby",
    ]
    assert snapshot.stream_close_reasons == ["activation"]
    assert snapshot.ownership_handoffs == [
        "standby_wake_listener->acknowledgement_playback:activation",
        "active_command->standby_wake_listener:runtime_return_to_standby",
    ]
    listener.stop()


def test_persistent_listener_holds_exclusive_gate_ownership_only_in_standby(tmp_path):
    gate = VoiceRuntimeGate(settle_delay_seconds=0)
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(),
        wake_recognizer=FakeWakeRecognizer(),
        project_root=tmp_path,
        voice_io_gate=gate,
    )
    listener.start()
    assert gate.snapshot()["capture_owner"] == "standby_wake"
    with pytest.raises(RuntimeError, match="microphone_capture_already_active"):
        gate.begin_capture("active_command")
    listener.leave_standby("activation")
    assert gate.snapshot()["capture_active"] is False
    gate.begin_capture("active_command")
    gate.end_capture("active_command")
    listener.enter_standby()
    assert gate.snapshot()["capture_owner"] == "standby_wake"
    listener.stop()
    assert gate.snapshot()["capture_active"] is False


def test_linux_listener_reopens_after_device_failure_on_next_poll(tmp_path):
    microphone = FakeMicrophone(capture_status="device_error", speech=False)
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=FakeWakeRecognizer(),
        config=WakeListenerConfig(retry_delay_seconds=0),
        project_root=tmp_path,
    )
    listener.start()
    failed = listener.listen_once(_request())
    assert not failed.success
    assert not listener.snapshot().stream_active
    microphone.capture_status = "no_speech_timeout"
    recovered = listener.listen_once(_request())
    assert recovered.success
    assert listener.snapshot().stream_open_count == 2
    assert listener.snapshot().calibration_count == 2
    assert listener.snapshot().stream_open_reasons[-1] == "device_recovery"
    assert listener.snapshot().calibration_reasons[-1] == (
        "device_recovery:initial_calibration"
    )
    listener.stop()


def test_linux_listener_does_not_start_microphone_when_recognizer_start_fails(tmp_path):
    class FailedRecognizer(FakeWakeRecognizer):
        def start(self):
            return WakeRecognizerResultV1(
                success=False,
                status="model_missing",
                error_code="vosk_model_missing",
                error_message="expected model path is missing",
            )

    microphone = FakeMicrophone()
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=FailedRecognizer(),
        project_root=tmp_path,
    )
    result = listener.start(runtime_id="runtime-test")
    assert not result.success
    assert result.error_code == "wake_recognizer_start_failed"
    assert "expected model path" in result.error_message
    assert not microphone.started


def test_linux_listener_no_speech_does_not_invoke_wake_recognizer(tmp_path):
    microphone = FakeMicrophone(capture_status="no_speech_timeout", speech=False)
    stt = FakeWakeRecognizer()
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=stt,
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert result.success
    assert result.status == "no_speech"
    assert stt.paths == []
    listener.stop()


def test_linux_listener_recognizes_only_current_normalized_16khz_wav_and_cleans(tmp_path):
    microphone = FakeMicrophone()
    stt = FakeWakeRecognizer("Hey, Ares.")
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=stt,
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert result.wake_detected
    assert result.cleanup_status == "removed"
    assert result.sample_rate_hz == 16000
    assert result.channels == 1
    assert result.sample_width_bytes == 2
    assert stt.paths[0][0].endswith("wake_recognizer_input.wav")
    assert stt.paths[0][0] != microphone.normalized_path
    assert stt.paths[0][0] != microphone.raw_path
    assert stt.paths[0][2] is True
    assert not Path(microphone.normalized_path).exists()
    assert not Path(microphone.raw_path).exists()
    listener.stop()


def test_linux_listener_forwards_calibrated_vad_bounds_and_safe_capture_settings(tmp_path):
    microphone = FakeMicrophone(capture_status="no_speech_timeout", speech=False)
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=FakeWakeRecognizer(),
        config=WakeListenerConfig(
            speech_start_rms=240,
            speech_continue_rms=180,
            silence_rms=120,
            calibration_duration_seconds=0.5,
        ),
        project_root=tmp_path,
    )
    listener.start()
    listener.listen_once(_request())
    kwargs = microphone.record_calls[0][1]
    assert microphone.calibration_count == 1
    assert kwargs["calibration_enabled"] is False
    assert kwargs["calibration_duration_seconds"] == 0.0
    assert kwargs["speech_start_rms"] == 240
    assert kwargs["speech_continue_rms"] == 180
    assert kwargs["silence_rms"] == 120
    assert kwargs["frame_duration_ms"] == 20
    assert kwargs["maximum_utterance_seconds"] == 1.6
    assert kwargs["capture_profile"] == "standby_wake_short_v1"
    assert kwargs["pre_roll_seconds"] == 0.4
    assert kwargs["speech_end_padding_seconds"] == 0.12
    assert kwargs["silence_seconds"] == 0.55
    listener.stop()


@pytest.mark.parametrize("status", ["recognition_timeout", "recognition_failed"])
def test_linux_listener_reports_recognizer_infrastructure_failures(status, tmp_path):
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(),
        wake_recognizer=FakeWakeRecognizer("", status=status, success=False),
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert not result.success
    assert result.error_code == status
    listener.stop()


def test_linux_listener_accepts_exact_wake_without_confidence_after_audio_validation(tmp_path):
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(),
        wake_recognizer=FakeWakeRecognizer("Ares", confidence=None),
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert result.success
    assert result.status == "wake_detected"
    assert result.wake_detected is True
    assert result.classification_reason == "accepted_exact_wake_without_confidence"
    assert result.recognition_confidence_available is False


def test_linux_listener_accepts_guarded_two_alias_vosk_duplication(tmp_path):
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(candidate_seconds=0.8),
        wake_recognizer=FakeWakeRecognizer("Ares Aris", confidence=0.82),
        project_root=tmp_path,
    )
    assert listener.start().success
    result = listener.listen_once(_request())
    assert result.wake_detected
    assert result.canonical_wake_phrase == "ares"
    assert result.duplicate_collapse_used
    assert result.classification_reason == "accepted_canonical_duplicate_wake"
    assert result.classification_path == (
        "vosk_constrained_grammar_duplicate_collapse"
    )
    assert result.minimum_word_confidence == pytest.approx(0.82)
    assert result.mean_word_confidence == pytest.approx(0.82)
    listener.stop()


def test_linux_listener_rejects_unknown_plus_alias_without_duplicate_collapse(tmp_path):
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(candidate_seconds=0.8),
        wake_recognizer=FakeWakeRecognizer("[unk] Aris", confidence=0.99),
        project_root=tmp_path,
    )
    assert listener.start().success
    result = listener.listen_once(_request())
    assert not result.wake_detected
    assert result.rejection_reason == "unknown_token_result"
    assert not result.duplicate_collapse_used
    listener.stop()
    listener.stop()


def test_linux_listener_can_fail_closed_on_missing_confidence_by_configuration(tmp_path):
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(),
        wake_recognizer=FakeWakeRecognizer("Ares", confidence=None),
        config=WakeListenerConfig(allow_exact_wake_without_confidence=False),
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert result.success
    assert result.status == "non_wake_speech"
    assert result.wake_detected is False
    assert result.rejection_reason == "missing_word_confidence"
    listener.stop()


def test_linux_listener_cancellation_stops_active_capture_safely(tmp_path):
    microphone = FakeMicrophone(capture_status="cancelled", speech=False)
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=FakeWakeRecognizer(),
        project_root=tmp_path,
    )
    listener.start()
    listener.cancel("test_cancel")
    result = listener.listen_once(_request())
    assert result.status == "cancelled"
    assert microphone.cancelled
    listener.stop()


def test_diagnostic_audio_retention_is_explicit_and_opt_in(tmp_path):
    microphone = FakeMicrophone()
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=FakeWakeRecognizer("Ares"),
        config=WakeListenerConfig(
            diagnostic_wake=True,
            retain_diagnostic_audio=True,
            diagnostic_output_directory="diagnostics/wake",
        ),
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request(retain_diagnostic_audio=True))
    assert result.wake_detected
    assert result.cleanup_status == "retained_by_explicit_request"
    retained = listener.retained_directories
    assert len(retained) == 1
    assert Path(retained[0]).is_dir()
    assert Path(microphone.normalized_path).is_file()
    listener.stop()


def test_linux_listener_refuses_retention_without_diagnostic_authorization(tmp_path):
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(),
        wake_recognizer=FakeWakeRecognizer("Ares"),
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request(retain_diagnostic_audio=True))
    assert not result.success
    assert result.error_code == "wake_diagnostic_retention_not_authorized"
    assert listener.retained_directories == ()
    listener.stop()


def test_local_wake_diagnostics_are_explicit_and_not_returned_in_contract(tmp_path):
    emitted = []
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(raw_seconds=1.4, candidate_seconds=0.8),
        wake_recognizer=FakeWakeRecognizer("Hello, Aries."),
        config=WakeListenerConfig(diagnostic_wake=True),
        project_root=tmp_path,
        diagnostic_callback=emitted.append,
    )
    listener.start()
    result = listener.listen_once(_request(diagnostic_wake=True))
    assert result.wake_detected
    assert len(emitted) == 1
    diagnostics = emitted[0]
    assert diagnostics.raw_transcript == "Hello, Aries."
    assert diagnostics.normalized_transcript == "hello aries"
    assert diagnostics.selected_alias == "aries"
    assert diagnostics.classification == "accepted"
    assert diagnostics.classification_path == "vosk_constrained_grammar_exact"
    assert diagnostics.classification_reason == "accepted_vosk_constrained_grammar"
    assert diagnostics.recognizer_name == "fake_vosk_constrained_grammar"
    assert diagnostics.recognition_confidence == pytest.approx(0.95)
    assert '"text": "Hello, Aries."' in diagnostics.raw_recognition_result
    assert diagnostics.raw_capture_duration_seconds == pytest.approx(1.4, abs=0.001)
    assert diagnostics.whisper_input_duration_seconds == pytest.approx(0.8, abs=0.001)
    assert "Okay, Aris" not in str(result.to_dict())
    listener.stop()


def test_local_diagnostics_report_strict_rejection_without_event_payload_text(tmp_path):
    emitted = []
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(raw_seconds=2.8, candidate_seconds=1.6),
        wake_recognizer=FakeWakeRecognizer("unrelated speech"),
        config=WakeListenerConfig(diagnostic_wake=True),
        project_root=tmp_path,
        diagnostic_callback=emitted.append,
    )
    listener.start()
    result = listener.listen_once(_request(diagnostic_wake=True))
    assert not result.wake_detected
    assert result.classification_path == "vosk_constrained_grammar_exact"
    assert result.classification_reason == "exact_constrained_phrase_not_matched"
    diagnostics = emitted[0]
    assert diagnostics.normalized_transcript == "unrelated speech"
    assert diagnostics.classification_path == "vosk_constrained_grammar_exact"
    assert "unrelated speech" not in str(result.to_dict()).casefold()
    listener.stop()


def test_local_wake_transcript_diagnostics_are_disabled_by_default(tmp_path):
    emitted = []
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(),
        wake_recognizer=FakeWakeRecognizer("Aris"),
        project_root=tmp_path,
        diagnostic_callback=emitted.append,
    )
    listener.start()
    assert listener.listen_once(_request()).wake_detected
    assert emitted == []
    assert listener.last_diagnostics is None
    listener.stop()


def test_retained_wake_candidates_are_bounded_to_latest_directory(tmp_path):
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(),
        wake_recognizer=FakeWakeRecognizer("Ares"),
        config=WakeListenerConfig(
            diagnostic_wake=True,
            retain_diagnostic_audio=True,
            maximum_retained_candidates=1,
            diagnostic_output_directory="diagnostics/wake",
        ),
        project_root=tmp_path,
    )
    listener.start()
    listener.listen_once(_request(diagnostic_wake=True, retain_diagnostic_audio=True))
    first = Path(listener.retained_directories[0])
    listener.listen_once(_request(diagnostic_wake=True, retain_diagnostic_audio=True))
    retained = listener.retained_directories
    assert len(retained) == 1
    assert Path(retained[0]).is_dir()
    assert not first.exists()
    listener.stop()


def test_wake_duration_metadata_uses_audio_headers_not_processing_wall_time(tmp_path):
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(raw_seconds=4.0, candidate_seconds=2.4),
        wake_recognizer=FakeWakeRecognizer("Ares"),
        config=WakeListenerConfig(maximum_utterance_seconds=3.0),
        project_root=tmp_path,
        clock=iter((0.0, 10.0, 10.0, 25.0)).__next__,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert result.duration_seconds == pytest.approx(2.4, abs=0.001)
    assert result.raw_capture_duration_seconds == pytest.approx(4.0, abs=0.001)
    assert result.normalized_duration_seconds == pytest.approx(2.4, abs=0.001)
    assert result.whisper_input_duration_seconds == pytest.approx(2.4, abs=0.001)
    assert result.processing_time_seconds == 15.0
    listener.stop()


def test_maximum_duration_wake_candidate_still_reaches_strict_classifier(tmp_path):
    microphone = FakeMicrophone(
        capture_status="maximum_duration_reached",
        raw_seconds=3.2,
        candidate_seconds=2.0,
    )
    stt = FakeWakeRecognizer("Ares")
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=stt,
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert result.wake_detected is True
    assert result.capture_stop_reason == "maximum_duration_reached"
    assert result.classification_reason == "accepted_vosk_constrained_grammar"
    assert len(stt.paths) == 1
    listener.stop()


def test_wake_candidate_hard_duration_limit_rejects_before_recognizer(tmp_path):
    microphone = FakeMicrophone(raw_seconds=4.0, candidate_seconds=3.5)
    stt = FakeWakeRecognizer("Ares")
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=stt,
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert not result.success
    assert result.error_code == "wake_audio_duration_exceeded"
    assert "wake_candidate_duration_exceeded" in result.error_message
    assert stt.paths == []
    listener.stop()


def test_wake_raw_capture_hard_limit_includes_only_bounded_capture_phases(tmp_path):
    stt = FakeWakeRecognizer("Ares")
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(raw_seconds=7.0, candidate_seconds=0.8),
        wake_recognizer=stt,
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert not result.success
    assert result.error_code == "wake_audio_duration_exceeded"
    assert "wake_raw_duration_exceeded" in result.error_message
    assert stt.paths == []
    listener.stop()


def test_wake_raw_capture_limit_excludes_separate_stream_calibration(tmp_path):
    stt = FakeWakeRecognizer("Ares")
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(raw_seconds=5.2, candidate_seconds=0.8),
        wake_recognizer=stt,
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert not result.success
    assert result.error_code == "wake_audio_duration_exceeded"
    assert "wake_raw_duration_exceeded" in result.error_message
    assert stt.paths == []
    listener.stop()


def test_wake_listener_contracts_and_results_never_contain_transcript_or_audio_bytes(tmp_path):
    microphone = FakeMicrophone()
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        wake_recognizer=FakeWakeRecognizer("private wake transcript Ares"),
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    serialized = str(result.to_dict()).casefold()
    assert "private wake transcript" not in serialized
    assert "audio_bytes" not in serialized
    listener.stop()


def test_wake_listener_manifest_is_bounded_offline_and_core_owned():
    manifest = build_standby_wake_listener_manifest()
    assert manifest.module_name == "linux_standby_wake_listener"
    assert manifest.capabilities == ["voice.standby_wake"]
    assert manifest.resources.persistent_module is True
    assert manifest.resources.heavy_module is False
    assert manifest.resources.estimated_ram_mb == 320
    assert manifest.resources.startup_cost == "medium"
    assert manifest.resources.maximum_concurrent_tasks == 1
    assert manifest.metadata["owner"] == "capital_core_brain_runtime"
    assert manifest.metadata["continuous_whisper"] is False
    assert manifest.metadata["standby_whisper"] is False
    assert manifest.metadata["recognizer_model_loaded_once"] is True
    assert "network.outbound" not in manifest.permissions


def test_linux_listener_contains_no_shell_or_background_thread_implementation():
    source = Path("core/LinuxStandbyWakeListener.py").read_text(encoding="utf-8").casefold()
    assert "shell=true" not in source
    assert "thread(" not in source
    assert "daemon" not in source
