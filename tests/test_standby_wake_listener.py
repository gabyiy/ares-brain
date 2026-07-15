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
    analyze_bounded_wake_repetition,
    build_standby_wake_listener_manifest,
    classify_wake_transcript,
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


class FakeSpeechToText:
    def __init__(self, text: str = "Ares", *, status: str = "transcribed", success: bool = True):
        self.text = text
        self.status = status
        self.success = success
        self.paths = []

    def health_check(self):
        return _ok("healthy")

    def transcribe_wav(self, path, **kwargs):
        self.paths.append((str(path), dict(kwargs), Path(path).exists()))
        return SimpleNamespace(
            success=self.success,
            status=self.status,
            text=self.text,
            error_message="" if self.success else self.status,
            data={
                "processing_time_seconds": 0.125,
                "model_path": "models/whisper/ggml-tiny.en.bin",
                "process": {"returncode": 0 if self.success else 1},
            },
        )


def _request(**changes) -> WakeListenerRequestV1:
    values = {
        "runtime_id": "runtime-test",
        "lifecycle_state": "STANDBY",
        "listener_timeout_seconds": 3.0,
        "wake_phrase_aliases": ["ares", "aris"],
        "wake_phrase_prefixes": ["", "hey", "hello", "wake up"],
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
    assert config.whisper_model.endswith("ggml-tiny.en.bin")
    assert config.frame_duration_ms == 20
    assert config.speech_wait_timeout_seconds == 3.0
    assert config.maximum_utterance_seconds == 2.0
    assert config.speech_start_rms > config.speech_continue_rms >= config.silence_rms
    assert config.calibration_enabled is True
    assert config.wake_phrase_aliases == ("ares", "aris")
    assert config.pre_roll_seconds == 0.25
    assert config.silence_duration_seconds == 0.7
    assert config.minimum_speech_continue_rms == 160
    assert config.minimum_silence_rms == 120
    assert config.maximum_wake_token_count == 8
    assert config.maximum_alias_repetitions == 4
    assert config.maximum_prefix_repetitions == 3
    assert config.retain_diagnostic_audio is False


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": 1},
        {"wake_phrase_aliases": ["Ares", "ares."]},
        {"wake_phrase_aliases": []},
        {"speech_start_rms": 100, "speech_continue_rms": 160},
        {"speech_continue_rms": 100, "silence_rms": 120},
        {"speech_wait_timeout_seconds": 0},
        {"maximum_utterance_seconds": 50},
        {"maximum_wake_token_count": True},
        {"maximum_wake_token_count": 17},
        {"maximum_alias_repetitions": 9},
        {"maximum_prefix_repetitions": 9},
        {"maximum_wake_token_count": 2, "maximum_alias_repetitions": 3},
        {"frame_duration_ms": True},
        {"frame_duration_ms": 100},
        {"calibration_enabled": True, "calibration_duration_seconds": 0},
        {"microphone_device": "bad\x00device"},
        {"whisper_model": ""},
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
        wake_phrase_prefixes=("", "HEY,", "Wake up"),
    )
    assert config.wake_phrase_aliases == ("ares", "aris")
    assert config.wake_phrase_prefixes == ("", "hey", "wake up")
    assert "hey aris" in config.wake_phrases
    with pytest.raises(ValueError, match="duplicates"):
        WakeListenerConfig(wake_phrase_aliases=("Ares", "ares."))
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
        "Hello Ares",
        "Hello, Aris",
        "Wake up, Ares",
        "Wake up Aris",
        "Okay, Ares",
    ],
)
def test_exact_wake_phrase_normalization_accepts_bounded_variants(text):
    result = classify_wake_transcript(text)
    assert result.wake_detected is True
    assert result.command_category == "activation"
    assert result.classification_path == "exact"
    assert result.classification_reason == "accepted_exact_wake_phrase"
    assert result.normalized_wake_phrase in {"ares", "hey ares", "hello ares", "wake up ares"}


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
    ],
)
def test_wake_recognition_rejects_substrings_and_unrelated_sentences(text):
    result = classify_wake_transcript(text)
    assert result.wake_detected is False
    assert result.command_category == "non_wake"
    assert result.normalized_wake_phrase == ""
    assert result.rejection_reason == "wake_vocabulary_contains_unknown_tokens"
    assert result.classification_path == "bounded_repetition"
    assert result.wake_vocabulary_only is False


@pytest.mark.parametrize(
    "text",
    [
        "ares ares",
        "aris aris",
        "aris aris hello aris",
        "hey ares ares",
        "wake wake up aris",
        "hello hello ares",
    ],
)
def test_bounded_wake_only_repetition_is_accepted_without_fuzzy_matching(text):
    result = classify_wake_transcript(text)
    assert result.wake_detected is True
    assert result.normalized_wake_phrase == "ares"
    assert result.canonical_wake_phrase == "ares"
    assert result.classification_path == "bounded_repetition"
    assert result.classification_reason == "accepted_bounded_wake_repetition"
    assert result.wake_vocabulary_only is True
    assert result.collapsed_wake_representation


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("aris unknownword aris", "wake_vocabulary_contains_unknown_tokens"),
        ("please wake up ares", "wake_vocabulary_contains_unknown_tokens"),
        ("calculate two plus two ares", "wake_vocabulary_contains_unknown_tokens"),
        ("ares remember that i like games", "wake_vocabulary_contains_unknown_tokens"),
        ("hello hello", "wake_alias_missing"),
        ("ares ares ares ares ares", "wake_alias_repetition_exceeded"),
        ("hello hello hello hello ares", "wake_prefix_repetition_exceeded"),
        ("hello hello hello ares ares ares ares wake up", "wake_token_count_exceeded"),
    ],
)
def test_bounded_repetition_rejects_unknown_or_excessive_wake_tokens(text, reason):
    result = classify_wake_transcript(text)
    assert result.wake_detected is False
    assert result.rejection_reason == reason
    assert result.classification_reason == reason


def test_wake_token_analysis_collapses_only_known_vocabulary_for_safe_diagnostics():
    accepted = analyze_bounded_wake_repetition("aris aris hello aris")
    rejected = analyze_bounded_wake_repetition("aris privateword aris")
    assert accepted.accepted
    assert accepted.collapsed_representation == "ares hello ares"
    assert accepted.alias_repetition_count == 3
    assert rejected.accepted is False
    assert rejected.collapsed_representation == "ares <unknown> ares"
    assert "privateword" not in rejected.collapsed_representation


def test_bounded_repetition_limits_are_configurable_and_enforced():
    alias_limited = classify_wake_transcript(
        "ares ares",
        maximum_alias_repetitions=1,
    )
    prefix_limited = classify_wake_transcript(
        "hello hello ares",
        maximum_prefix_repetitions=1,
    )
    assert alias_limited.rejection_reason == "wake_alias_repetition_exceeded"
    assert prefix_limited.rejection_reason == "wake_prefix_repetition_exceeded"


def test_aris_alias_returns_canonical_ares_activation_without_fuzzy_matching():
    result = classify_wake_transcript("Hey, Aris.")
    assert result.selected_alias == "aris"
    assert result.selected_wake_phrase == "hey aris"
    assert result.canonical_wake_phrase == "hey ares"
    assert result.normalized_wake_phrase == "hey ares"


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
    stt = FakeSpeechToText()
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
        project_root=tmp_path,
    )
    assert listener.start(runtime_id="runtime-test").status == "started"
    assert listener.health().success
    assert listener.stop().success
    assert microphone.started is False
    assert microphone.stopped is True


def test_linux_listener_no_speech_does_not_invoke_whisper(tmp_path):
    microphone = FakeMicrophone(capture_status="no_speech_timeout", speech=False)
    stt = FakeSpeechToText()
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert result.success
    assert result.status == "no_speech"
    assert stt.paths == []
    listener.stop()


def test_linux_listener_transcribes_only_current_normalized_16khz_wav_and_cleans(tmp_path):
    microphone = FakeMicrophone()
    stt = FakeSpeechToText("Hey, Ares.")
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
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
        speech_to_text_adapter=FakeSpeechToText(),
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


@pytest.mark.parametrize("status", ["transcription_timeout", "transcription_failed"])
def test_linux_listener_reports_whisper_infrastructure_failures(status, tmp_path):
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(),
        speech_to_text_adapter=FakeSpeechToText("", status=status, success=False),
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert not result.success
    assert result.error_code == "wake_transcription_failed"
    listener.stop()


@pytest.mark.parametrize("status", ["no_transcription", "no_usable_speech", "audio_silent"])
def test_linux_listener_treats_empty_candidate_transcription_as_non_wake(status, tmp_path):
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(),
        speech_to_text_adapter=FakeSpeechToText("", status=status, success=False),
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert result.success
    assert result.status == "non_wake_speech"
    assert result.wake_detected is False
    listener.stop()


def test_linux_listener_cancellation_stops_active_capture_safely(tmp_path):
    microphone = FakeMicrophone(capture_status="cancelled", speech=False)
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        speech_to_text_adapter=FakeSpeechToText(),
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
        speech_to_text_adapter=FakeSpeechToText("Ares"),
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
        speech_to_text_adapter=FakeSpeechToText("Ares"),
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
        speech_to_text_adapter=FakeSpeechToText("Hello, Aris."),
        config=WakeListenerConfig(diagnostic_wake=True),
        project_root=tmp_path,
        diagnostic_callback=emitted.append,
    )
    listener.start()
    result = listener.listen_once(_request(diagnostic_wake=True))
    assert result.wake_detected
    assert len(emitted) == 1
    diagnostics = emitted[0]
    assert diagnostics.raw_transcript == "Hello, Aris."
    assert diagnostics.normalized_transcript == "hello aris"
    assert diagnostics.selected_alias == "aris"
    assert diagnostics.classification == "accepted"
    assert diagnostics.classification_path == "exact"
    assert diagnostics.classification_reason == "accepted_exact_wake_phrase"
    assert diagnostics.collapsed_wake_representation == "hello ares"
    assert diagnostics.wake_vocabulary_only is True
    assert diagnostics.raw_capture_duration_seconds == pytest.approx(1.4, abs=0.001)
    assert diagnostics.whisper_input_duration_seconds == pytest.approx(0.8, abs=0.001)
    assert "Hello, Aris" not in str(result.to_dict())
    listener.stop()


def test_local_diagnostics_report_bounded_repetition_path_without_event_payload_text(tmp_path):
    emitted = []
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(raw_seconds=2.8, candidate_seconds=1.6),
        speech_to_text_adapter=FakeSpeechToText("Aris, Aris, hello, Aris."),
        config=WakeListenerConfig(diagnostic_wake=True),
        project_root=tmp_path,
        diagnostic_callback=emitted.append,
    )
    listener.start()
    result = listener.listen_once(_request(diagnostic_wake=True))
    assert result.wake_detected
    assert result.classification_path == "bounded_repetition"
    assert result.classification_reason == "accepted_bounded_wake_repetition"
    assert result.collapsed_wake_representation == "ares hello ares"
    diagnostics = emitted[0]
    assert diagnostics.normalized_transcript == "aris aris hello aris"
    assert diagnostics.collapsed_wake_representation == "ares hello ares"
    assert diagnostics.classification_path == "bounded_repetition"
    assert "aris aris hello aris" not in str(result.to_dict()).casefold()
    listener.stop()


def test_local_wake_transcript_diagnostics_are_disabled_by_default(tmp_path):
    emitted = []
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(),
        speech_to_text_adapter=FakeSpeechToText("Aris"),
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
        speech_to_text_adapter=FakeSpeechToText("Ares"),
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
        speech_to_text_adapter=FakeSpeechToText("Ares"),
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
    stt = FakeSpeechToText("Aris, Aris, hello, Aris.")
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
        project_root=tmp_path,
    )
    listener.start()
    result = listener.listen_once(_request())
    assert result.wake_detected is True
    assert result.capture_stop_reason == "maximum_duration_reached"
    assert result.classification_reason == "accepted_bounded_wake_repetition"
    assert len(stt.paths) == 1
    listener.stop()


def test_wake_candidate_hard_duration_limit_rejects_before_whisper(tmp_path):
    microphone = FakeMicrophone(raw_seconds=4.0, candidate_seconds=3.5)
    stt = FakeSpeechToText("Ares")
    listener = LinuxStandbyWakeListener(
        microphone_adapter=microphone,
        speech_to_text_adapter=stt,
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
    stt = FakeSpeechToText("Ares")
    listener = LinuxStandbyWakeListener(
        microphone_adapter=FakeMicrophone(raw_seconds=7.0, candidate_seconds=0.8),
        speech_to_text_adapter=stt,
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
        speech_to_text_adapter=FakeSpeechToText("private wake transcript Ares"),
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
    assert manifest.resources.maximum_concurrent_tasks == 1
    assert manifest.metadata["owner"] == "capital_core_brain_runtime"
    assert manifest.metadata["continuous_whisper"] is False
    assert "network.outbound" not in manifest.permissions


def test_linux_listener_contains_no_shell_or_background_thread_implementation():
    source = Path("core/LinuxStandbyWakeListener.py").read_text(encoding="utf-8").casefold()
    assert "shell=true" not in source
    assert "thread(" not in source
    assert "daemon" not in source
