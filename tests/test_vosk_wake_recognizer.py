from __future__ import annotations

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


def _classify(text: str, *, confidence: float = 0.95, words=None):
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
        medium_confirmation_repetitions=config.medium_confidence_confirmation_count,
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
        "where is ares",
        "ares is a greek god",
        "paris",
        "harris",
        "okay",
        "tell me about aries",
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


def test_low_confidence_and_missing_confidence_fail_closed():
    low = _classify("ares", confidence=0.70)
    medium = _classify("ares", confidence=0.79)
    missing = _classify("ares", words=[{"word": "ares"}])
    assert low.rejection_reason == "wake_confidence_below_medium_threshold"
    assert medium.rejection_reason == "medium_confidence_confirmation_required"
    assert medium.confirmation_required
    assert medium.confidence_tier == "medium"
    assert low.confidence_available
    assert missing.rejection_reason == "missing_word_confidence"
    assert not missing.confidence_available
    assert not low.wake_detected and not missing.wake_detected


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
        minimum_confidence=0.8,
        medium_confidence=0.72,
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


def test_medium_confidence_requires_repeated_identical_exact_wake(tmp_path):
    model = tmp_path / "vosk-model"
    model.mkdir()
    wav = tmp_path / "wake.wav"
    _write_wav(wav)
    payload = {"text": "aris", "result": _word_results("aris", 0.79)}
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


def test_medium_confidence_confirmation_expires_deterministically(tmp_path):
    model = tmp_path / "vosk-model"
    model.mkdir()
    wav = tmp_path / "wake.wav"
    _write_wav(wav)
    now = [0.0]
    payload = {"text": "ares", "result": _word_results("ares", 0.79)}
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
