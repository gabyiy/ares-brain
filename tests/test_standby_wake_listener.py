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
    build_standby_wake_listener_manifest,
    classify_wake_transcript,
    classify_constrained_recognition,
    normalize_wake_phrase,
)


def _ok(status: str = "healthy", **values):
    return SimpleNamespace(success=True, status=status, error_message="", **values)


def _failed(status: str, message: str = "failed"):
    return SimpleNamespace(success=False, status=status, error_message=message)


def _write_wav(path: Path, *, sample_rate: int = 16000, seconds: float = 0.2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = max(1, int(sample_rate * seconds))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes((b"\x10\x00" * samples))


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
            recognizer_name=self.recognizer_name,
            runtime_id=request.runtime_id,
            lifecycle_state=request.lifecycle_state,
            correlation_id=request.correlation_id,
            model_path="models/vosk/test-model",
            grammar_phrase_count=len(request.wake_phrases) + 1,
            processing_time_seconds=0.125,
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
        "wake_phrase_aliases": ["ares", "aris"],
        "wake_phrase_prefixes": ["", "hey", "okay"],
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
    assert config.minimum_recognition_confidence == 0.8
    assert config.frame_duration_ms == 20
    assert config.speech_wait_timeout_seconds == 3.0
    assert config.maximum_utterance_seconds == 2.0
    assert config.speech_start_rms > config.speech_continue_rms >= config.silence_rms
    assert config.calibration_enabled is True
    assert config.wake_phrase_aliases == ("ares", "aris")
    assert config.wake_phrase_prefixes == ("", "hey", "okay")
    assert config.pre_roll_seconds == 0.25
    assert config.silence_duration_seconds == 0.7
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
        {"minimum_recognition_confidence": 0.49},
        {"minimum_recognition_confidence": float("nan")},
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
        "  Hey, Ares!  ",
        "Hey Aris",
        "Okay, Ares",
        "Okay Aris",
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
        "Aries",
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
        "hello ares",
        "wake up ares",
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
    assert stt.paths[0][0] == microphone.normalized_path
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
    assert kwargs["calibration_enabled"] is True
    assert kwargs["calibration_duration_seconds"] == 0.5
    assert kwargs["speech_start_rms"] == 240
    assert kwargs["speech_continue_rms"] == 180
    assert kwargs["silence_rms"] == 120
    assert kwargs["frame_duration_ms"] == 20
    assert kwargs["maximum_utterance_seconds"] == 2.0
    assert kwargs["pre_roll_seconds"] == 0.25
    assert kwargs["silence_seconds"] == 0.7
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


def test_linux_listener_treats_missing_confidence_as_non_wake(tmp_path):
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(),
        wake_recognizer=FakeWakeRecognizer("Ares", confidence=None),
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
        wake_recognizer=FakeWakeRecognizer("Okay, Aris."),
        config=WakeListenerConfig(diagnostic_wake=True),
        project_root=tmp_path,
        diagnostic_callback=emitted.append,
    )
    listener.start()
    result = listener.listen_once(_request(diagnostic_wake=True))
    assert result.wake_detected
    assert len(emitted) == 1
    diagnostics = emitted[0]
    assert diagnostics.raw_transcript == "Okay, Aris."
    assert diagnostics.normalized_transcript == "okay aris"
    assert diagnostics.selected_alias == "aris"
    assert diagnostics.classification == "accepted"
    assert diagnostics.classification_path == "vosk_constrained_grammar"
    assert diagnostics.classification_reason == "accepted_vosk_constrained_grammar"
    assert diagnostics.recognizer_name == "fake_vosk_constrained_grammar"
    assert diagnostics.recognition_confidence == pytest.approx(0.95)
    assert '"text": "Okay, Aris."' in diagnostics.raw_recognition_result
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
    assert result.classification_path == "vosk_constrained_grammar"
    assert result.classification_reason == "exact_constrained_phrase_not_matched"
    diagnostics = emitted[0]
    assert diagnostics.normalized_transcript == "unrelated speech"
    assert diagnostics.classification_path == "vosk_constrained_grammar"
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
        clock=iter((10.0, 25.0)).__next__,
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
        candidate_seconds=2.2,
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


def test_wake_candidate_hard_duration_limit_rejects_before_whisper(tmp_path):
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
