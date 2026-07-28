from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import wave

import pytest

from core.ActiveLifecycleAudioRecognizer import (
    ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY,
    ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN,
    ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY,
    ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN,
    ACTIVE_LIFECYCLE_CONFIRMATION_CANCELLED,
    ACTIVE_LIFECYCLE_CONFIRMATION_CONFIRMED,
    ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED,
    ActiveLifecycleAudioRecognizer,
    LifecycleBackendRecognition,
    VoskLifecycleGrammarBackend,
)


class FakeBackend:
    recognition_backend = "fake_constrained_backend"

    def __init__(self, *results):
        self.results = deque(results)
        self.calls = []
        self.closed = False

    def recognize_wav(self, audio_path, *, grammar, timeout_seconds):
        self.calls.append(
            {
                "audio_path": Path(audio_path),
                "grammar": tuple(grammar),
                "timeout_seconds": timeout_seconds,
            }
        )
        value = self.results.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed = True


def _evidence(text: str, confidence: float | None = 0.95):
    tokens = tuple(text.casefold().split())
    return LifecycleBackendRecognition(
        recognized_text=text,
        recognized_tokens=tokens,
        word_confidences=(
            tuple(confidence for _ in tokens) if confidence is not None else ()
        ),
        recognition_backend="fake_constrained_backend",
    )


def _wav(tmp_path: Path, *, rate=16000, channels=1, width=2) -> Path:
    path = tmp_path / f"candidate-{rate}-{channels}-{width}.wav"
    with wave.open(str(path), "wb") as target:
        target.setframerate(rate)
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.writeframes(b"\x01\x00" * 1600 * channels)
    return path


@pytest.mark.parametrize(
    "phrase",
    [
        "goodbye ares",
        "goodbye aris",
        "good bye ares",
        "good bye aris",
        "bye ares",
        "bye aris",
        "go standby ares",
        "standby ares",
        "sleep ares",
    ],
)
def test_exact_bounded_standby_grammar_selects_canonical_standby(phrase, tmp_path):
    backend = FakeBackend(_evidence(phrase))
    result = ActiveLifecycleAudioRecognizer(backend=backend).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_STANDBY
    assert result.canonical_phrase == "goodbye ares"
    assert result.selected_lifecycle_action == "standby"
    assert result.confidence == pytest.approx(0.95)
    assert result.confidence_tier == "high"
    assert not result.whisper_fallback_required


@pytest.mark.parametrize(
    "phrase",
    [
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
    ],
)
def test_exact_bounded_shutdown_grammar_selects_canonical_shutdown(phrase, tmp_path):
    backend = FakeBackend(_evidence(phrase))
    result = ActiveLifecycleAudioRecognizer(backend=backend).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN
    assert result.canonical_phrase == "shutdown ares"
    assert result.selected_lifecycle_action == "shutdown"
    assert result.confidence_tier == "high"


@pytest.mark.parametrize(
    "phrase",
    [
        "shut down artist",
        "clears throat",
        "ares",
        "aris",
        "rs",
        "shutdown artist",
        "shutdown aries",
        "shutdown rs",
        "shutdown computer",
        "shutdown paris",
        "shutdown harris",
        "goodbye everyone",
        "go to sleep",
        "turn it off",
        "paris",
        "harris",
        "aries",
    ],
)
def test_unrelated_and_name_only_results_never_select_a_lifecycle_action(
    phrase,
    tmp_path,
):
    backend = FakeBackend(_evidence(phrase, 0.99))
    result = ActiveLifecycleAudioRecognizer(backend=backend).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY
    assert result.selected_lifecycle_action == "none"
    assert result.whisper_fallback_required
    assert result.rejection_reason == "bounded_distractor_phrase"


@pytest.mark.parametrize(
    "phrase",
    [
        "do not shutdown ares",
        "do not shut down aris",
        "don't shutdown ares",
        "don't shut down aris",
        "never shutdown ares",
        "never shut down aris",
        "do not goodbye ares",
        "don't say goodbye aris",
        "never goodbye ares",
        "do not sleep aris",
        "don't standby ares",
        "never standby aris",
        "do not go standby ares",
        "don't go standby aris",
        "never go to sleep ares",
    ],
)
def test_bounded_negation_competitors_never_select_lifecycle_at_high_confidence(
    phrase,
    tmp_path,
):
    backend = FakeBackend(_evidence(phrase, 0.99))
    result = ActiveLifecycleAudioRecognizer(backend=backend).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY
    assert result.selected_lifecycle_action == "none"
    assert result.whisper_fallback_required
    assert result.rejection_reason == "bounded_distractor_phrase"


@pytest.mark.parametrize(
    "phrase",
    [
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
    ],
)
def test_no_alias_negative_and_descriptive_distractors_are_never_actions(
    phrase,
    tmp_path,
):
    backend = FakeBackend(_evidence(phrase, 0.99))
    result = ActiveLifecycleAudioRecognizer(backend=backend).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY
    assert result.selected_lifecycle_action == "none"
    assert result.rejection_reason == "bounded_distractor_phrase"


def test_shutdown_uses_minimum_word_confidence_and_medium_requires_confirmation(tmp_path):
    evidence = LifecycleBackendRecognition(
        recognized_text="shutdown ares",
        recognized_tokens=("shutdown", "ares"),
        word_confidences=(0.93, 0.69),
        recognition_backend="fake_constrained_backend",
    )
    result = ActiveLifecycleAudioRecognizer(backend=FakeBackend(evidence)).recognize_wav(
        _wav(tmp_path)
    )

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN
    assert result.proposed_classification == ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN
    assert result.canonical_phrase == "shutdown ares"
    assert result.confidence == pytest.approx(0.69)
    assert result.confidence_tier == "medium"
    assert result.confirmation_required
    assert not result.whisper_fallback_required
    assert result.selected_lifecycle_action == "none"


def test_low_confidence_lifecycle_guess_falls_back_to_ordinary_whisper(tmp_path):
    result = ActiveLifecycleAudioRecognizer(
        backend=FakeBackend(_evidence("shutdown ares", 0.59))
    ).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY
    assert result.confidence_tier == "low"
    assert not result.confirmation_required
    assert result.whisper_fallback_required
    assert result.selected_lifecycle_action == "none"


@pytest.mark.parametrize(
    ("phrase", "confidence", "classification", "confirmation_required"),
    [
        ("goodbye ares", 0.499, "ordinary", False),
        ("goodbye ares", 0.500, "uncertain", True),
        ("goodbye ares", 0.699, "uncertain", True),
        ("goodbye ares", 0.700, "standby", False),
        ("shutdown ares", 0.599, "ordinary", False),
        ("shutdown ares", 0.600, "uncertain", True),
        ("shutdown ares", 0.779, "uncertain", True),
        ("shutdown ares", 0.780, "shutdown", False),
    ],
)
def test_action_specific_confidence_boundaries_are_exact(
    phrase,
    confidence,
    classification,
    confirmation_required,
    tmp_path,
):
    result = ActiveLifecycleAudioRecognizer(
        backend=FakeBackend(_evidence(phrase, confidence))
    ).recognize_wav(_wav(tmp_path))

    assert result.classification == classification
    assert result.confirmation_required is confirmation_required


def test_missing_or_unaligned_confidence_never_executes_a_lifecycle_action(tmp_path):
    result = ActiveLifecycleAudioRecognizer(
        backend=FakeBackend(_evidence("shutdown ares", None))
    ).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY
    assert not result.confidence_available
    assert result.rejection_reason == "missing_or_unaligned_word_confidence"
    assert result.selected_lifecycle_action == "none"


@pytest.mark.parametrize("phrase", ["yes", "yes ares", "confirm", "confirm shutdown"])
def test_explicit_confirmation_confirms_only_the_pending_action(phrase, tmp_path):
    backend = FakeBackend(_evidence(phrase, 0.88))
    result = ActiveLifecycleAudioRecognizer(backend=backend).recognize_confirmation_wav(
        _wav(tmp_path),
        expected_classification="shutdown",
    )

    assert result.disposition == ACTIVE_LIFECYCLE_CONFIRMATION_CONFIRMED
    assert result.expected_classification == "shutdown"


@pytest.mark.parametrize("phrase", ["no", "cancel", "never mind", "continue"])
def test_negative_confirmation_cancels_without_executing(phrase, tmp_path):
    backend = FakeBackend(_evidence(phrase, 0.80))
    result = ActiveLifecycleAudioRecognizer(backend=backend).recognize_confirmation_wav(
        _wav(tmp_path),
        expected_classification="shutdown",
    )

    assert result.disposition == ACTIVE_LIFECYCLE_CONFIRMATION_CANCELLED


def test_confirmation_for_wrong_action_is_unmatched(tmp_path):
    backend = FakeBackend(_evidence("confirm standby", 0.99))
    result = ActiveLifecycleAudioRecognizer(backend=backend).recognize_confirmation_wav(
        _wav(tmp_path),
        expected_classification="shutdown",
    )

    assert result.disposition == ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED
    assert result.rejection_reason == "confirmation_action_mismatch"


@pytest.mark.parametrize("phrase", ["ares", "what", "repeat", "something else"])
def test_confirmation_distractors_are_unmatched_even_at_high_confidence(
    phrase,
    tmp_path,
):
    backend = FakeBackend(_evidence(phrase, 0.99))
    result = ActiveLifecycleAudioRecognizer(backend=backend).recognize_confirmation_wav(
        _wav(tmp_path),
        expected_classification="shutdown",
    )

    assert result.disposition == ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED
    assert result.rejection_reason == "exact_confirmation_phrase_not_matched"


@pytest.mark.parametrize("confidence", [0.69, 0.79])
def test_weak_affirmative_never_confirms_shutdown(confidence, tmp_path):
    backend = FakeBackend(_evidence("yes", confidence))
    result = ActiveLifecycleAudioRecognizer(backend=backend).recognize_confirmation_wav(
        _wav(tmp_path),
        expected_classification="shutdown",
    )

    assert result.disposition == ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED
    assert result.rejection_reason == "confirmation_confidence_below_threshold"


def test_affirmative_and_cancellation_confidence_boundaries_are_exact(tmp_path):
    path = _wav(tmp_path)
    backend = FakeBackend(
        _evidence("yes", 0.80),
        _evidence("no", 0.499),
        _evidence("no", 0.50),
    )
    recognizer = ActiveLifecycleAudioRecognizer(backend=backend)

    confirmed = recognizer.recognize_confirmation_wav(
        path,
        expected_classification="shutdown",
    )
    weak_cancel = recognizer.recognize_confirmation_wav(
        path,
        expected_classification="shutdown",
    )
    cancelled = recognizer.recognize_confirmation_wav(
        path,
        expected_classification="shutdown",
    )

    assert confirmed.disposition == ACTIVE_LIFECYCLE_CONFIRMATION_CONFIRMED
    assert weak_cancel.disposition == ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED
    assert cancelled.disposition == ACTIVE_LIFECYCLE_CONFIRMATION_CANCELLED


def test_backend_failure_is_typed_uncertain_and_never_an_action(tmp_path):
    backend = FakeBackend(RuntimeError("model exploded exactly"))
    result = ActiveLifecycleAudioRecognizer(backend=backend).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN
    assert result.selected_lifecycle_action == "none"
    assert result.whisper_fallback_required
    assert "RuntimeError:model exploded exactly" in result.rejection_reason


@pytest.mark.parametrize(
    ("rate", "channels", "width"),
    [(44100, 1, 2), (16000, 2, 2), (16000, 1, 1)],
)
def test_recognizer_rejects_noncanonical_wav_before_backend(
    rate,
    channels,
    width,
    tmp_path,
):
    backend = FakeBackend(_evidence("goodbye ares"))
    recognizer = ActiveLifecycleAudioRecognizer(backend=backend)

    with pytest.raises(ValueError, match="16000 Hz mono signed 16-bit"):
        recognizer.recognize_wav(
            _wav(tmp_path, rate=rate, channels=channels, width=width)
        )

    assert backend.calls == []


def test_recognizer_reuses_the_exact_finalized_wav_and_has_no_microphone_or_transition_api(
    tmp_path,
):
    path = _wav(tmp_path)
    backend = FakeBackend(_evidence("goodbye ares"))
    recognizer = ActiveLifecycleAudioRecognizer(backend=backend)

    recognizer.recognize_wav(path)

    assert backend.calls[0]["audio_path"] == path.resolve()
    assert not hasattr(recognizer, "open_microphone")
    assert not hasattr(recognizer, "capture")
    assert not hasattr(recognizer, "transition")
    assert not hasattr(recognizer, "core_service")
    recognizer.close()
    assert backend.closed


class FakeKaldiRecognizer:
    def __init__(self, payload):
        self.payload = payload
        self.set_words = None
        self.accepted = []

    def SetWords(self, enabled):
        self.set_words = enabled

    def AcceptWaveform(self, value):
        self.accepted.append(bytes(value))
        return False

    def Result(self):
        return json.dumps({"text": "", "result": []})

    def FinalResult(self):
        return json.dumps(self.payload)


class FakeVoskModule:
    def __init__(self, payload):
        self.payload = payload
        self.model_calls = []
        self.recognizer_calls = []
        self.log_levels = []

    def SetLogLevel(self, level):
        self.log_levels.append(level)

    def Model(self, path):
        self.model_calls.append(path)
        return object()

    def KaldiRecognizer(self, model, sample_rate, grammar):
        instance = FakeKaldiRecognizer(self.payload)
        self.recognizer_calls.append((model, sample_rate, json.loads(grammar), instance))
        return instance


def test_default_vosk_backend_loads_model_once_and_uses_only_bounded_grammar(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    module = FakeVoskModule(
        {
            "text": "shutdown ares",
            "result": [
                {"word": "shutdown", "conf": 0.91},
                {"word": "ares", "conf": 0.84},
            ],
        }
    )
    backend = VoskLifecycleGrammarBackend(model_path=model, vosk_module=module)
    recognizer = ActiveLifecycleAudioRecognizer(backend=backend)
    path = _wav(tmp_path)

    first = recognizer.recognize_wav(path)
    second = recognizer.recognize_wav(path)

    assert first.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN
    assert second.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN
    assert len(module.model_calls) == 1
    assert len(module.recognizer_calls) == 2
    _model, sample_rate, grammar, instance = module.recognizer_calls[0]
    assert sample_rate == 16000.0
    assert "shutdown ares" in grammar
    assert "goodbye aris" in grammar
    # Explicit acoustic competitors are constrained rejection phrases, never
    # aliases or lifecycle actions. They prevent Vosk from being forced to the
    # nearest positive grammar phrase.
    assert "shutdown artist" in grammar
    assert "shutdown aries" in grammar
    assert "ares" in grammar
    assert "do not shut down ares" in grammar
    assert "don't shut down aris" in grammar
    assert "[unk]" in grammar
    assert instance.set_words is True
    assert b"".join(instance.accepted) == b"\x01\x00" * 1600


def test_unknown_vosk_token_never_selects_lifecycle_action(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    module = FakeVoskModule(
        {
            "text": "[unk]",
            "result": [{"word": "[unk]", "conf": 0.99}],
        }
    )
    recognizer = ActiveLifecycleAudioRecognizer(
        backend=VoskLifecycleGrammarBackend(model_path=model, vosk_module=module)
    )

    result = recognizer.recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY
    assert result.selected_lifecycle_action == "none"
    assert result.rejection_reason == "unknown_token_detected"


def test_threshold_configuration_is_ordered_and_bounded():
    with pytest.raises(ValueError, match="standby_medium_confidence"):
        ActiveLifecycleAudioRecognizer(
            backend=FakeBackend(),
            standby_high_confidence=0.60,
            standby_medium_confidence=0.60,
        )
    with pytest.raises(ValueError, match="shutdown_high_confidence"):
        ActiveLifecycleAudioRecognizer(
            backend=FakeBackend(),
            shutdown_high_confidence=1.1,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("standby_high_confidence", 0.59),
        ("standby_medium_confidence", 0.39),
        ("shutdown_high_confidence", 0.69),
        ("shutdown_medium_confidence", 0.49),
        ("confirmation_minimum_confidence", 0.74),
    ],
)
def test_safety_critical_confidence_configuration_cannot_be_weakened_below_floor(
    field,
    value,
):
    with pytest.raises(ValueError, match=field):
        ActiveLifecycleAudioRecognizer(
            backend=FakeBackend(),
            **{field: value},
        )
