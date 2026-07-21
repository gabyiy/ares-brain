from __future__ import annotations

from dataclasses import replace
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from core import (
    CONTRACT_WAKE_RECOGNIZER_REQUEST,
    CONTRACT_WAKE_RECOGNIZER_RESULT,
    DEFAULT_CONTRACT_REGISTRY,
    WakeListenerConfig,
    WakeRecognitionAttempt,
    WakeRecognizerLocalDiagnostics,
    WakeRecognizerRequestV1,
    WakeRecognizerResultV1,
    VoskWakeRecognizer,
    classify_constrained_recognition,
)


def _word_results(text: str, confidence: float = 0.95):
    return [
        {"word": token.casefold().strip(".,!?"), "conf": confidence}
        for token in text.split()
    ]


def _classify(
    text: str,
    *,
    confidence: float = 0.95,
    words=None,
    allow_exact_wake_without_confidence: bool = True,
    audio_duration_seconds: float = 0.7,
):
    config = WakeListenerConfig()
    return classify_constrained_recognition(
        text,
        _word_results(text, confidence) if words is None else words,
        wake_phrases=config.wake_phrases,
        wake_phrase_aliases=config.wake_phrase_aliases,
        standby_phrases=("goodbye ares",),
        shutdown_phrases=("shutdown ares",),
        minimum_confidence=config.minimum_recognition_confidence,
        medium_confidence=config.medium_recognition_confidence,
        allow_exact_wake_without_confidence=allow_exact_wake_without_confidence,
        medium_confirmation_repetitions=config.medium_confidence_confirmation_count,
        audio_duration_seconds=audio_duration_seconds,
        maximum_duplicate_collapse_audio_seconds=(
            config.maximum_duplicate_collapse_audio_seconds
        ),
    )


@pytest.mark.parametrize(
    "text",
    [
        "ares",
        "aris",
        "aries",
        "hey ares",
        "hey aris",
        "hello aries",
        "wake up aris",
        "okay ares",
        "okay aries",
        "ARES.",
        "Hey, Aris!",
    ],
)
def test_exact_constrained_wake_phrases_are_accepted(text):
    result = _classify(text)
    assert result.wake_detected
    assert result.command_category == "activation"
    assert result.normalized_wake_phrase == "ares"
    assert result.classification_reason == "accepted_vosk_constrained_grammar"
    assert result.confidence == pytest.approx(0.95)


@pytest.mark.parametrize(
    "text",
    [
        "okay",
        "bye",
        "alright",
        "alrighty",
        "areas",
        "air",
        "I spoke to Ares yesterday",
        "I spoke to Aris",
        "where is ares",
        "ares calculate",
        "ares is a greek god",
        "paris",
        "harris",
        "okay",
        "tell me about aries",
        "aries horoscope",
        "what is the aries zodiac sign",
    ],
)
def test_unrelated_or_partial_phrases_are_rejected_without_substring_matching(text):
    result = _classify(text)
    assert not result.wake_detected
    assert result.command_category == "non_wake"
    assert result.rejection_reason == "exact_constrained_phrase_not_matched"


def test_unknown_token_result_is_rejected_even_when_text_looks_like_wake():
    result = _classify(
        "ares",
        words=[{"word": "[unk]", "conf": 0.99}],
    )
    assert not result.wake_detected
    assert result.unknown_token_detected
    assert result.rejection_reason == "unknown_token_result"


@pytest.mark.parametrize(
    ("confidence", "wake_detected", "confirmation_required", "reason"),
    [
        (0.399, False, False, "wake_confidence_below_medium_threshold"),
        (0.400, False, True, "medium_confidence_confirmation_required"),
        (0.549, False, True, "medium_confidence_confirmation_required"),
        (0.550, True, False, "accepted_vosk_constrained_grammar"),
        (0.616, True, False, "accepted_vosk_constrained_grammar"),
    ],
)
def test_exact_wake_confidence_policy_boundaries(
    confidence,
    wake_detected,
    confirmation_required,
    reason,
):
    result = _classify("ares", confidence=confidence)
    assert result.wake_detected is wake_detected
    assert result.confirmation_required is confirmation_required
    assert result.classification_reason == reason


def test_missing_confidence_exact_wake_uses_narrow_validated_policy():
    accepted = _classify("ares", words=[{"word": "ares"}])
    accepted_without_word_details = _classify("ares", words=[])
    fail_closed = _classify(
        "ares",
        words=[{"word": "ares"}],
        allow_exact_wake_without_confidence=False,
    )
    wrong = _classify("go to", words=[])
    assert accepted.wake_detected
    assert accepted.classification_reason == "accepted_exact_wake_without_confidence"
    assert accepted.confidence_tier == "missing"
    assert not accepted.confidence_available
    assert accepted_without_word_details.wake_detected
    assert (
        accepted_without_word_details.classification_reason
        == "accepted_exact_wake_without_confidence"
    )
    assert not fail_closed.wake_detected
    assert fail_closed.rejection_reason == "missing_word_confidence"
    assert not wrong.wake_detected
    assert wrong.rejection_reason == "exact_constrained_phrase_not_matched"


def test_high_confidence_wrong_phrase_is_always_rejected():
    result = _classify("go to", confidence=0.99)
    assert not result.wake_detected
    assert result.rejection_reason == "exact_constrained_phrase_not_matched"


@pytest.mark.parametrize("text", ["ares ares", "aris aris", "aries aries"])
def test_two_token_canonical_wake_identity_collapses_only_with_audio_safeguards(text):
    result = _classify(text, confidence=0.82, audio_duration_seconds=0.9)
    assert result.wake_detected
    assert result.normalized_wake_phrase == "ares"
    assert result.duplicate_collapse_used
    assert result.collapsed_canonical_phrase == "ares"
    assert result.classification_reason == "accepted_canonical_duplicate_wake"
    assert result.minimum_word_confidence == pytest.approx(0.82)
    assert result.mean_word_confidence == pytest.approx(0.82)
    assert result.canonical_confidence == pytest.approx(0.82)
    assert result.selected_alias == text.split()[0]


def test_duplicate_wake_requires_one_confidence_entry_per_vosk_token():
    result = _classify(
        "ares ares",
        words=[{"word": "ares ares", "conf": 0.99}],
        audio_duration_seconds=0.8,
    )

    assert not result.wake_detected
    assert result.rejection_reason == "invalid_word_token_structure"
    assert not result.duplicate_collapse_used
    assert result.command_category == "non_wake"


@pytest.mark.parametrize(
    ("text", "words", "duration"),
    [
        ("[unk] aris", [{"word": "[unk]", "conf": 0.99}, {"word": "aris", "conf": 0.99}], 0.8),
        ("ares ares ares", None, 0.8),
        ("ares hello", None, 0.8),
        ("ares aris", None, 0.8),
        ("ares aris", None, 1.5),
        ("ares ares", None, 4.1),
        ("ares aris", [{"word": "ares"}, {"word": "aris"}], 0.8),
    ],
)
def test_duplicate_wake_collapse_rejects_unknown_length_duration_and_missing_confidence(
    text,
    words,
    duration,
):
    result = _classify(text, words=words, audio_duration_seconds=duration)
    assert not result.wake_detected


def test_duplicate_wake_uses_minimum_confidence_and_exposes_mean():
    result = _classify(
        "ares ares",
        words=[
            {"word": "ares", "conf": 0.90},
            {"word": "ares", "conf": 0.52},
        ],
        audio_duration_seconds=0.8,
    )
    assert not result.wake_detected
    assert result.confirmation_required
    assert result.minimum_word_confidence == pytest.approx(0.52)
    assert result.mean_word_confidence == pytest.approx(0.71)
    assert result.canonical_confidence == pytest.approx(0.52)


def test_controls_require_exact_phrase_and_usable_confidence():
    accepted = _classify("shutdown ares")
    alias = _classify("shutdown aris")
    aries = _classify("shutdown aries")
    missing = _classify("shutdown ares", words=[])
    unrelated = _classify("please shutdown ares")
    assert accepted.command_category == "shutdown"
    assert accepted.status == "control_detected"
    assert alias.command_category == "shutdown"
    assert alias.status == "control_detected"
    assert aries.command_category == "shutdown"
    assert missing.command_category == "non_wake"
    assert missing.rejection_reason == "missing_word_confidence"
    assert unrelated.command_category == "non_wake"


def test_recognizer_contracts_are_versioned_registered_and_transcript_free():
    request = WakeRecognizerRequestV1()
    result = WakeRecognizerResultV1()
    assert request.contract_name == CONTRACT_WAKE_RECOGNIZER_REQUEST
    assert result.contract_name == CONTRACT_WAKE_RECOGNIZER_RESULT
    assert request.contract_version == result.contract_version == "v1"
    assert DEFAULT_CONTRACT_REGISTRY.current_version(CONTRACT_WAKE_RECOGNIZER_REQUEST) == "v1"
    assert DEFAULT_CONTRACT_REGISTRY.current_version(CONTRACT_WAKE_RECOGNIZER_RESULT) == "v1"
    assert "transcript" not in result.to_dict()


class FakeVoskRecognizer:
    def __init__(self, payload, grammar_log):
        self.payload = payload
        self.grammar_log = grammar_log
        self.words_enabled = False

    def SetWords(self, enabled):
        self.words_enabled = enabled

    def AcceptWaveform(self, chunk):
        return False

    def Result(self):
        return json.dumps({"text": "", "result": []})

    def FinalResult(self):
        return json.dumps(self.payload)


class FakeVoskModule:
    def __init__(self, payload):
        self.payload = payload
        self.model_paths = []
        self.grammars = []
        self.log_levels = []

    def SetLogLevel(self, level):
        self.log_levels.append(level)

    def Model(self, path):
        self.model_paths.append(path)
        return SimpleNamespace(path=path)

    def KaldiRecognizer(self, model, sample_rate, grammar):
        self.grammars.append((model, sample_rate, json.loads(grammar)))
        return FakeVoskRecognizer(self.payload, self.grammars)


def _write_wav(path: Path, *, sample_rate=16000, channels=1, width=2):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(sample_rate)
        output.writeframes(b"\x20\x03" * 3200)


def _recognizer_request(path: Path):
    config = WakeListenerConfig()
    return WakeRecognizerRequestV1(
        runtime_id="runtime-vosk-test",
        audio_path=str(path),
        wake_phrases=list(config.wake_phrases),
        wake_phrase_aliases=list(config.wake_phrase_aliases),
        standby_phrases=["goodbye ares"],
        shutdown_phrases=["shutdown ares"],
        minimum_confidence=0.55,
        medium_confidence=0.40,
        validated_speech_candidate=True,
        correlation_id="recognizer-test",
    )


def test_vosk_adapter_loads_model_once_uses_constrained_grammar_and_unk(tmp_path):
    model = tmp_path / "vosk-model"
    model.mkdir()
    wav = tmp_path / "wake.wav"
    _write_wav(wav)
    module = FakeVoskModule(
        {"text": "hello aries", "result": _word_results("hello aries")}
    )
    adapter = VoskWakeRecognizer(model_path=model, vosk_module=module)
    assert adapter.start().success
    assert adapter.start().status == "already_started"
    result = adapter.recognize_wav(_recognizer_request(wav))
    assert result.wake_detected
    assert result.selected_alias == "aries"
    assert result.normalized_wake_phrase == "ares"
    assert module.model_paths == [str(model.resolve())]
    grammar = module.grammars[0][2]
    assert grammar == [
        "ares",
        "aris",
        "aries",
        "hey ares",
        "hey aris",
        "hey aries",
        "hello ares",
        "hello aris",
        "hello aries",
        "wake up ares",
        "wake up aris",
        "wake up aries",
        "okay ares",
        "okay aris",
        "okay aries",
        "goodbye ares",
        "goodbye aris",
        "goodbye aries",
        "shutdown ares",
        "shutdown aris",
        "shutdown aries",
        "[unk]",
    ]
    assert adapter.last_diagnostics.raw_recognition_result
    assert adapter.stop().success


def test_vosk_model_health_probe_is_deterministic_and_requires_no_microphone(tmp_path):
    model = tmp_path / "vosk-model"
    model.mkdir()
    module = FakeVoskModule({"text": "", "result": []})
    adapter = VoskWakeRecognizer(model_path=model, vosk_module=module)
    assert adapter.start().success
    health = adapter.health_check()
    assert health.success
    assert health.status == "healthy"
    assert module.grammars[-1][2] == ["ares", "[unk]"]


def test_vosk_recognize_attempt_consumes_candidate_diagnostics_and_uses_fresh_recognizer(tmp_path):
    model = tmp_path / "vosk-model"
    model.mkdir()
    wav = tmp_path / "wake.wav"
    _write_wav(wav)
    module = FakeVoskModule(
        {"text": "ares", "result": _word_results("ares")}
    )
    adapter = VoskWakeRecognizer(model_path=model, vosk_module=module)
    assert adapter.start().success
    request = replace(
        _recognizer_request(wav),
        attempt_id="wake-attempt-one",
        stream_generation=3,
        candidate_number=7,
    )
    first = adapter.recognize_attempt(request)
    second = adapter.recognize_attempt(
        replace(request, attempt_id="wake-attempt-two", candidate_number=8)
    )
    assert first.result.wake_detected
    assert first.diagnostics.attempt_id == "wake-attempt-one"
    assert first.diagnostics.stream_generation == 3
    assert first.diagnostics.candidate_number == 7
    assert second.diagnostics.attempt_id == "wake-attempt-two"
    assert adapter.last_diagnostics is None
    assert len(module.grammars) == 2


def test_recognition_attempt_rejects_mixed_candidate_metadata():
    result = WakeRecognizerResultV1(
        attempt_id="attempt-one",
        stream_generation=4,
        candidate_number=9,
    )
    diagnostics = WakeRecognizerLocalDiagnostics(
        attempt_id="attempt-two",
        stream_generation=4,
        candidate_number=9,
    )

    with pytest.raises(ValueError, match="attempt IDs"):
        WakeRecognitionAttempt(result=result, diagnostics=diagnostics)


def test_vosk_failure_resets_candidate_local_diagnostics(tmp_path):
    model = tmp_path / "vosk-model"
    model.mkdir()
    missing = tmp_path / "missing.wav"
    adapter = VoskWakeRecognizer(
        model_path=model,
        vosk_module=FakeVoskModule({"text": "ares", "result": _word_results("ares")}),
    )
    assert adapter.start().success
    adapter.last_diagnostics = SimpleNamespace(recognized_text="stale ares")
    attempt = adapter.recognize_attempt(_recognizer_request(missing))
    assert not attempt.result.success
    assert attempt.diagnostics.recognized_text == ""
    assert adapter.last_diagnostics is None


def test_medium_confidence_requires_repeated_identical_exact_wake(tmp_path):
    model = tmp_path / "vosk-model"
    model.mkdir()
    wav = tmp_path / "wake.wav"
    _write_wav(wav)
    payload = {"text": "aris", "result": _word_results("aris", 0.50)}
    adapter = VoskWakeRecognizer(model_path=model, vosk_module=FakeVoskModule(payload))
    assert adapter.start().success
    first = adapter.recognize_wav(_recognizer_request(wav))
    second = adapter.recognize_wav(_recognizer_request(wav))
    assert not first.wake_detected
    assert first.classification_reason == "medium_confidence_confirmation_required"
    assert first.confirmation_count == 1
    assert second.wake_detected
    assert second.classification_reason == "accepted_medium_confidence_repetition"
    assert second.confirmation_count == 2


def test_medium_confidence_wrong_phrase_never_enters_confirmation(tmp_path):
    model = tmp_path / "vosk-model"
    model.mkdir()
    wav = tmp_path / "wake.wav"
    _write_wav(wav)
    payload = {"text": "go to", "result": _word_results("go to", 0.99)}
    adapter = VoskWakeRecognizer(model_path=model, vosk_module=FakeVoskModule(payload))
    assert adapter.start().success
    for _ in range(3):
        result = adapter.recognize_wav(_recognizer_request(wav))
        assert not result.wake_detected
        assert not result.confirmation_required
        assert result.rejection_reason == "exact_constrained_phrase_not_matched"


def test_missing_confidence_requires_listener_validated_speech_candidate(tmp_path):
    model = tmp_path / "vosk-model"
    model.mkdir()
    wav = tmp_path / "wake.wav"
    _write_wav(wav)
    payload = {"text": "ares", "result": []}
    adapter = VoskWakeRecognizer(
        model_path=model,
        vosk_module=FakeVoskModule(payload),
    )
    assert adapter.start().success
    validated = adapter.recognize_wav(_recognizer_request(wav))
    unvalidated = adapter.recognize_wav(
        replace(_recognizer_request(wav), validated_speech_candidate=False)
    )
    assert validated.wake_detected
    assert validated.classification_reason == "accepted_exact_wake_without_confidence"
    assert not unvalidated.wake_detected
    assert unvalidated.rejection_reason == "missing_word_confidence"


def test_medium_confidence_confirmation_expires_deterministically(tmp_path):
    model = tmp_path / "vosk-model"
    model.mkdir()
    wav = tmp_path / "wake.wav"
    _write_wav(wav)
    now = [0.0]
    payload = {"text": "ares", "result": _word_results("ares", 0.50)}
    adapter = VoskWakeRecognizer(
        model_path=model,
        vosk_module=FakeVoskModule(payload),
        clock=lambda: now[0],
    )
    assert adapter.start().success
    first = adapter.recognize_wav(_recognizer_request(wav))
    now[0] = 9.0
    second = adapter.recognize_wav(_recognizer_request(wav))
    assert first.confirmation_count == 1
    assert second.confirmation_count == 1
    assert not second.wake_detected


def test_missing_vosk_model_returns_actionable_error_without_loading_module(tmp_path):
    module = FakeVoskModule({})
    expected = tmp_path / "models" / "vosk" / "vosk-model-small-en-us-0.15"
    adapter = VoskWakeRecognizer(model_path=expected, vosk_module=module)
    result = adapter.start()
    assert not result.success
    assert result.error_code == "vosk_model_missing"
    assert str(expected.resolve()) in result.error_message
    assert "vosk-model-small-en-us-0.15" in result.error_message
    assert "curl -fL" in result.error_message
    assert module.model_paths == []


def test_missing_vosk_dependency_returns_actionable_error_before_recognition(tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()

    def missing_module(name):
        assert name == "vosk"
        raise ImportError("injected missing dependency")

    module = importlib.import_module("core.VoskWakeRecognizer")
    monkeypatch.setattr(module.importlib, "import_module", missing_module)
    adapter = VoskWakeRecognizer(model_path=model)
    result = adapter.start()
    assert not result.success
    assert result.error_code == "vosk_dependency_missing"
    assert "python -m pip install -r requirements.txt" in result.error_message


def test_vosk_recognition_timeout_is_bounded_between_pcm_chunks(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    wav = tmp_path / "wake.wav"
    _write_wav(wav)
    module = FakeVoskModule(
        {"text": "ares", "result": _word_results("ares")}
    )
    clock_values = iter((0.0, 0.0, 4.0))
    adapter = VoskWakeRecognizer(
        model_path=model,
        vosk_module=module,
        clock=lambda: next(clock_values),
    )
    assert adapter.start().success
    result = adapter.recognize_wav(_recognizer_request(wav))
    assert not result.success
    assert result.error_code == "vosk_recognition_timeout"


@pytest.mark.parametrize(
    ("sample_rate", "channels", "width"),
    [(44100, 1, 2), (16000, 2, 2), (16000, 1, 1)],
)
def test_vosk_adapter_rejects_noncanonical_wav(sample_rate, channels, width, tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    wav = tmp_path / "bad.wav"
    _write_wav(wav, sample_rate=sample_rate, channels=channels, width=width)
    adapter = VoskWakeRecognizer(model_path=model, vosk_module=FakeVoskModule({}))
    adapter.start()
    result = adapter.recognize_wav(_recognizer_request(wav))
    assert not result.success
    assert result.error_code == "vosk_recognition_failed"
    assert "canonical 16 khz" in result.error_message.casefold()


def test_vosk_adapter_result_contract_excludes_raw_text_but_local_diagnostics_keep_it(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    wav = tmp_path / "wake.wav"
    _write_wav(wav)
    private_text = "unrelated private sentence"
    module = FakeVoskModule(
        {"text": private_text, "result": _word_results(private_text)}
    )
    adapter = VoskWakeRecognizer(model_path=model, vosk_module=module)
    adapter.start()
    result = adapter.recognize_wav(_recognizer_request(wav))
    assert private_text not in json.dumps(result.to_dict()).casefold()
    assert private_text in adapter.last_diagnostics.raw_recognition_result


def test_standby_vosk_dependency_is_explicit_and_active_whisper_path_remains():
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    launcher = Path("scripts/run_ares_standby_voice.py").read_text(encoding="utf-8")
    active = Path("core/SingleTurnVoicePipeline.py").read_text(encoding="utf-8")
    listener = Path("core/LinuxStandbyWakeListener.py").read_text(encoding="utf-8")
    assert "vosk==0.3.45" in requirements
    assert "VoskWakeRecognizer" in launcher
    assert "LinuxWhisperSpeechToTextAdapter" not in launcher
    assert "transcribe_wav" not in listener
    assert "speech_to_text_adapter" in active
