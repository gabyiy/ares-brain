from __future__ import annotations

from collections import deque
import json
import multiprocessing
from pathlib import Path
import time
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
    DEFAULT_ACTIVE_LIFECYCLE_REJECTION_GRAMMAR,
    DEFAULT_ACTIVE_SHUTDOWN_GRAMMAR,
    DEFAULT_ACTIVE_STANDBY_GRAMMAR,
    ActiveLifecycleBackendCleanupError,
    ActiveLifecycleAudioRecognizer,
    LifecycleBackendRecognition,
    VOSK_LIFECYCLE_WORKER_PROTOCOL,
    VoskLifecycleGrammarBackend,
    _VoskLifecycleProcessWorker,
    _vosk_lifecycle_worker_main,
    canonicalize_active_lifecycle_assistant_alias,
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


def _wav(
    tmp_path: Path,
    *,
    rate=16000,
    channels=1,
    width=2,
    sample_value=1,
    name="",
) -> Path:
    path = tmp_path / (name or f"candidate-{rate}-{channels}-{width}.wav")
    with wave.open(str(path), "wb") as target:
        target.setframerate(rate)
        target.setnchannels(channels)
        target.setsampwidth(width)
        sample = int(sample_value).to_bytes(width, "little", signed=True)
        target.writeframes(sample * 1600 * channels)
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
    ("phrase", "classification", "canonical", "alias", "position"),
    [
        ("goodbye rs", "standby", "goodbye ares", "rs", "suffix"),
        ("goodbye aris", "standby", "goodbye ares", "aris", "suffix"),
        ("goodbye ares", "standby", "goodbye ares", "ares", "suffix"),
        ("shutdown rs", "shutdown", "shutdown ares", "rs", "suffix"),
        ("shut down aris", "shutdown", "shutdown ares", "aris", "suffix"),
        ("shutdown ares", "shutdown", "shutdown ares", "ares", "suffix"),
        ("GOODBYE, ARRIS!", "standby", "goodbye ares", "arris", "suffix"),
        ("Shut down, Aries.", "shutdown", "shutdown ares", "aries", "suffix"),
        ("Goodbye, R. S.", "standby", "goodbye ares", "r s", "suffix"),
        ("R S, shut down.", "shutdown", "ares shutdown", "r s", "prefix"),
        ("Go to standby, Ares.", "standby", "go to standby ares", "ares", "suffix"),
    ],
)
def test_lifecycle_slot_aliases_canonicalize_before_exact_classification(
    phrase,
    classification,
    canonical,
    alias,
    position,
    tmp_path,
):
    evidence = _evidence(phrase, 0.99)
    result = ActiveLifecycleAudioRecognizer(
        backend=FakeBackend(evidence)
    ).recognize_wav(_wav(tmp_path))

    assert result.classification == classification
    assert result.selected_lifecycle_action == classification
    assert result.alias_canonicalized_transcript == canonical
    assert result.alias_detected == alias
    assert result.alias_position == position
    assert result.recognized_text == phrase
    assert result.recognized_tokens == evidence.recognized_tokens
    assert result.whisper_fallback_required is False


@pytest.mark.parametrize(
    "phrase",
    [
        "where is Ares",
        "I spoke to Aris yesterday",
        "Paris",
        "Harris",
        "the artist is here",
        "remember that I like Ares",
        "calculate two plus two",
        "shut down the computer",
        "goodbye everyone",
    ],
)
def test_lifecycle_aliases_are_never_stripped_from_non_lifecycle_commands(
    phrase,
    tmp_path,
):
    evidence = _evidence(phrase, 0.99)
    result = ActiveLifecycleAudioRecognizer(
        backend=FakeBackend(evidence)
    ).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY
    assert result.selected_lifecycle_action == "none"
    assert result.whisper_fallback_required is True
    assert result.alias_detected == ""
    assert result.alias_position == "none"
    assert result.alias_canonicalized_transcript == " ".join(
        phrase.casefold().replace("!", "").split()
    ).strip(".,")
    assert result.recognized_text == phrase
    assert result.recognized_tokens == evidence.recognized_tokens


def test_authoritative_alias_canonicalizer_requires_one_complete_command_shape():
    accepted = canonicalize_active_lifecycle_assistant_alias("Shut down, R. S.")
    ordinary = canonicalize_active_lifecycle_assistant_alias(
        "remember that I like Ares"
    )

    assert accepted.normalized_transcript == "shut down r s"
    assert accepted.alias_detected == "r s"
    assert accepted.alias_position == "suffix"
    assert accepted.alias_canonicalized_transcript == "shutdown ares"
    assert ordinary.normalized_transcript == "remember that i like ares"
    assert ordinary.alias_detected == ""
    assert ordinary.alias_position == "none"
    assert ordinary.alias_canonicalized_transcript == ordinary.normalized_transcript


def test_generated_vosk_grammar_covers_alias_forms_without_policy_overlap():
    action = set(DEFAULT_ACTIVE_STANDBY_GRAMMAR + DEFAULT_ACTIVE_SHUTDOWN_GRAMMAR)
    rejection = set(DEFAULT_ACTIVE_LIFECYCLE_REJECTION_GRAMMAR)

    for alias in ("ares", "aris", "aries", "arris", "rs", "r s"):
        assert f"goodbye {alias}" in action
        assert f"shutdown {alias}" in action
        assert f"shut down {alias}" in action
    assert "r s shutdown" in action
    assert "go to standby rs" in action
    assert action.isdisjoint(rejection)
    assert len(action) == 78
    assert len(rejection) == 49
    assert len(action | rejection) + 1 <= 128  # plus Vosk's explicit [unk]


@pytest.mark.parametrize(
    "phrase",
    [
        "shut down artist",
        "clears throat",
        "ares",
        "aris",
        "rs",
        "shutdown artist",
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
        "please shutdown ares",
        "shutdown ares tomorrow",
        "goodbye ares yesterday",
        "goodbye ares everyone",
    ],
)
def test_extra_words_cannot_turn_a_bounded_phrase_into_a_lifecycle_action(
    phrase,
    tmp_path,
):
    result = ActiveLifecycleAudioRecognizer(
        backend=FakeBackend(_evidence(phrase, 0.99))
    ).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_ORDINARY
    assert result.selected_lifecycle_action == "none"
    assert result.whisper_fallback_required
    assert result.rejection_reason == "exact_constrained_lifecycle_phrase_not_matched"


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


def test_explicit_standby_confirmation_confirms_a_pending_standby_action(tmp_path):
    result = ActiveLifecycleAudioRecognizer(
        backend=FakeBackend(_evidence("confirm standby", 0.88))
    ).recognize_confirmation_wav(
        _wav(tmp_path),
        expected_classification="standby",
    )

    assert result.disposition == ACTIVE_LIFECYCLE_CONFIRMATION_CONFIRMED
    assert result.expected_classification == "standby"


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


def test_shutdown_confirmation_cannot_confirm_a_pending_standby_action(tmp_path):
    result = ActiveLifecycleAudioRecognizer(
        backend=FakeBackend(_evidence("confirm shutdown", 0.99))
    ).recognize_confirmation_wav(
        _wav(tmp_path),
        expected_classification="standby",
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


def test_unreaped_backend_failure_blocks_whisper_and_every_lifecycle_action(tmp_path):
    result = ActiveLifecycleAudioRecognizer(
        backend=FakeBackend(
            ActiveLifecycleBackendCleanupError(
                "vosk_lifecycle_timeout_cleanup_incomplete"
            )
        )
    ).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN
    assert result.selected_lifecycle_action == "none"
    assert result.backend_cleanup_complete is False
    assert result.whisper_fallback_required is False
    assert "ActiveLifecycleBackendCleanupError" in result.rejection_reason


def test_unreaped_confirmation_backend_failure_blocks_fallback(tmp_path):
    result = ActiveLifecycleAudioRecognizer(
        backend=FakeBackend(
            ActiveLifecycleBackendCleanupError(
                "vosk_lifecycle_close_cleanup_incomplete"
            )
        )
    ).recognize_confirmation_wav(
        _wav(tmp_path),
        expected_classification="shutdown",
    )

    assert result.disposition == ACTIVE_LIFECYCLE_CONFIRMATION_UNMATCHED
    assert result.backend_cleanup_complete is False
    assert "ActiveLifecycleBackendCleanupError" in result.rejection_reason


@pytest.mark.parametrize(
    "backend_result,expected_error",
    [
        (TimeoutError("backend exceeded its deadline"), "TimeoutError"),
        ({"classification": "shutdown"}, "TypeError"),
    ],
)
def test_timeout_and_malformed_backend_results_fail_closed(
    backend_result,
    expected_error,
    tmp_path,
):
    result = ActiveLifecycleAudioRecognizer(
        backend=FakeBackend(backend_result)
    ).recognize_wav(_wav(tmp_path))

    assert result.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN
    assert result.selected_lifecycle_action == "none"
    assert result.whisper_fallback_required
    assert expected_error in result.rejection_reason


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
    assert "goodbye rs" in grammar
    assert "shutdown r s" in grammar
    # Alias-shaped complete lifecycle phrases are action candidates. Explicit
    # unrelated acoustic competitors remain rejection phrases and prevent Vosk
    # from being forced to the nearest positive grammar phrase.
    assert "shutdown artist" in grammar
    assert "shutdown aries" in grammar
    assert "ares" in grammar
    assert "do not shut down ares" in grammar
    assert "don't shut down aris" in grammar
    assert "[unk]" in grammar
    assert instance.set_words is True
    assert b"".join(instance.accepted) == b"\x01\x00" * 1600


def test_production_vosk_worker_timeout_is_killed_reaped_and_retryable(
    tmp_path,
    monkeypatch,
    request,
):
    fake_module_directory = tmp_path / "fake-vosk-module"
    fake_module_directory.mkdir()
    (fake_module_directory / "vosk.py").write_text(
        """
import json
import time
import os

def SetLogLevel(_level):
    return None

class Model:
    def __init__(self, path):
        self.path = path

class KaldiRecognizer:
    def __init__(self, _model, _sample_rate, _grammar):
        self.hang = False

    def SetWords(self, _enabled):
        return None

    def AcceptWaveform(self, value):
        if bytes(value).startswith(b'\\x02\\x00'):
            time.sleep(60)
        if bytes(value).startswith(b'\\x03\\x00'):
            raise RuntimeError('backend exploded exactly')
        if bytes(value).startswith(b'\\x04\\x00'):
            os._exit(17)
        return False

    def Result(self):
        return json.dumps({'text': '', 'result': []})

    def FinalResult(self):
        return json.dumps({
            'text': 'shutdown ares',
            'result': [
                {'word': 'shutdown', 'conf': 0.91},
                {'word': 'ares', 'conf': 0.88},
            ],
        })
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(fake_module_directory))
    model = tmp_path / "model"
    model.mkdir()
    normal_path = _wav(tmp_path, name="normal.wav", sample_value=1)
    hanging_path = _wav(tmp_path, name="hanging.wav", sample_value=2)
    error_path = _wav(tmp_path, name="error.wav", sample_value=3)
    crash_path = _wav(tmp_path, name="crash.wav", sample_value=4)
    progress = []
    backend = VoskLifecycleGrammarBackend(
        model_path=model,
        progress_callback=lambda event, payload: progress.append(
            (event, dict(payload))
        ),
    )
    recognizer = ActiveLifecycleAudioRecognizer(backend=backend)
    request.addfinalizer(recognizer.close)

    first = recognizer.recognize_wav(normal_path, timeout_seconds=5.0)
    first_diagnostics = dict(backend.worker_diagnostics)
    first_progress = list(progress)
    started = time.monotonic()
    timed_out = recognizer.recognize_wav(hanging_path, timeout_seconds=0.2)
    elapsed = time.monotonic() - started
    timeout_diagnostics = dict(backend.worker_diagnostics)
    retried = recognizer.recognize_wav(normal_path, timeout_seconds=5.0)
    retry_diagnostics = dict(backend.worker_diagnostics)
    backend_error = recognizer.recognize_wav(error_path, timeout_seconds=5.0)
    error_diagnostics = dict(backend.worker_diagnostics)
    crashed = recognizer.recognize_wav(crash_path, timeout_seconds=5.0)
    crash_diagnostics = dict(backend.worker_diagnostics)
    recovered = recognizer.recognize_wav(normal_path, timeout_seconds=5.0)
    recovered_diagnostics = dict(backend.worker_diagnostics)

    assert first.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN
    assert first_diagnostics["worker_alive"] is False
    assert first_diagnostics["worker_reaped"] is True
    assert first_diagnostics["worker_stop_acknowledged"] is True
    assert first_diagnostics["worker_start_count"] == 1
    assert first_diagnostics["worker_protocol"] == VOSK_LIFECYCLE_WORKER_PROTOCOL
    assert first_diagnostics["worker_request_id"].startswith("lifecycle-request-")
    assert first_diagnostics["worker_request_sent_at"].endswith("Z")
    assert first_diagnostics["worker_request_received_at"].endswith("Z")
    assert first_diagnostics["worker_result_received_at"].endswith("Z")
    assert first_diagnostics["worker_stop_requested_at"].endswith("Z")
    assert first_diagnostics["worker_joined_at"].endswith("Z")
    assert [event for event, _payload in first_progress] == [
        "lifecycle_recognizer_request_sent",
        "lifecycle_result_returned",
        "lifecycle_worker_stop_requested",
        "lifecycle_worker_reaped",
    ]
    assert timed_out.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN
    assert timed_out.selected_lifecycle_action == "none"
    assert "TimeoutError:vosk_lifecycle_recognition_timeout" in (
        timed_out.rejection_reason
    )
    assert elapsed < 2.0
    assert timeout_diagnostics["worker_alive"] is False
    assert timeout_diagnostics["worker_reaped"] is True
    assert timeout_diagnostics["worker_timeout_count"] == 1
    assert retried.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN
    assert retry_diagnostics["worker_alive"] is False
    assert retry_diagnostics["worker_reaped"] is True
    assert retry_diagnostics["worker_start_count"] == 3
    assert backend_error.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN
    assert backend_error.backend_cleanup_complete is True
    assert backend_error.whisper_fallback_required is True
    assert "RuntimeError:backend exploded exactly" in backend_error.rejection_reason
    assert error_diagnostics["worker_alive"] is False
    assert error_diagnostics["worker_reaped"] is True
    assert crashed.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_UNCERTAIN
    assert "worker_transport_error:EOFError" in crashed.rejection_reason
    assert crash_diagnostics["worker_alive"] is False
    assert crash_diagnostics["worker_reaped"] is True
    assert crash_diagnostics["worker_exitcode"] == 17
    assert recovered.classification == ACTIVE_LIFECYCLE_CLASSIFICATION_SHUTDOWN
    assert recovered_diagnostics["worker_alive"] is False
    assert recovered_diagnostics["worker_reaped"] is True
    assert recovered_diagnostics["worker_start_count"] == 6

    recognizer.close()
    assert backend.worker_diagnostics["worker_alive"] is False


def _install_minimal_spawn_vosk_module(tmp_path, monkeypatch):
    module_directory = tmp_path / "spawn-vosk-module"
    module_directory.mkdir()
    (module_directory / "vosk.py").write_text(
        """
def SetLogLevel(_level):
    return None

class Model:
    def __init__(self, path):
        self.path = path
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(module_directory))


def test_worker_explicit_stop_before_request_exits_and_is_reaped(tmp_path, monkeypatch):
    _install_minimal_spawn_vosk_module(tmp_path, monkeypatch)
    model = tmp_path / "model"
    model.mkdir()
    request_id = "request-stop-before-recognition"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = context.Process(
        target=_vosk_lifecycle_worker_main,
        args=(child, str(model), request_id),
        daemon=False,
    )
    process.start()
    child.close()
    try:
        assert parent.poll(5.0)
        ready = parent.recv()
        assert ready["type"] == "ready"
        assert ready["request_id"] == request_id
        parent.send(
            {
                "protocol": VOSK_LIFECYCLE_WORKER_PROTOCOL,
                "type": "stop_request",
                "request_id": request_id,
                "reason": "test_stop",
            }
        )
        assert parent.poll(2.0)
        stopped = parent.recv()
        assert stopped["type"] == "stopped"
        assert stopped["request_id"] == request_id
    finally:
        parent.close()
        process.join(3.0)
        if process.is_alive():
            process.terminate()
            process.join(1.0)
    assert process.is_alive() is False
    process.close()


def test_worker_exits_when_parent_closes_pipe_before_request(tmp_path, monkeypatch):
    _install_minimal_spawn_vosk_module(tmp_path, monkeypatch)
    model = tmp_path / "model"
    model.mkdir()
    request_id = "request-parent-closed"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = context.Process(
        target=_vosk_lifecycle_worker_main,
        args=(child, str(model), request_id),
        daemon=False,
    )
    process.start()
    child.close()
    assert parent.poll(5.0)
    assert parent.recv()["type"] == "ready"
    parent.close()
    process.join(3.0)
    if process.is_alive():
        process.terminate()
        process.join(1.0)
    assert process.is_alive() is False
    process.close()


def test_parent_cancellation_during_result_wait_reaps_worker_and_propagates(
    tmp_path,
    monkeypatch,
):
    class SendOnlyConnection:
        def __init__(self):
            self.messages = []

        def send(self, value):
            self.messages.append(value)

    model = tmp_path / "model"
    model.mkdir()
    worker = _VoskLifecycleProcessWorker(model_path=model)
    connection = SendOnlyConnection()
    cleanup_calls = []
    monkeypatch.setattr(worker, "_start_worker", lambda _deadline, **_kwargs: None)
    monkeypatch.setattr(worker, "_connection_snapshot", lambda: connection)
    monkeypatch.setattr(
        worker,
        "_receive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        worker,
        "_stop_and_reap_worker",
        lambda **kwargs: cleanup_calls.append(kwargs) is None or True,
    )

    with pytest.raises(KeyboardInterrupt):
        worker.recognize_wav(
            _wav(tmp_path),
            grammar=("shutdown ares",),
            timeout_seconds=1.0,
        )

    assert connection.messages[0]["type"] == "recognize_request"
    assert cleanup_calls[0]["graceful"] is False
    assert worker._request_lock.acquire(timeout=0.1)
    worker._request_lock.release()


def test_timeout_with_unreaped_worker_is_not_downgraded_to_normal_fallback(
    tmp_path,
    monkeypatch,
):
    model = tmp_path / "model"
    model.mkdir()
    worker = _VoskLifecycleProcessWorker(model_path=model)

    def time_out(_deadline, *, request_id):
        del request_id
        raise TimeoutError("worker response deadline")

    monkeypatch.setattr(worker, "_start_worker", time_out)
    monkeypatch.setattr(worker, "_stop_and_reap_worker", lambda **_kwargs: False)

    with pytest.raises(
        ActiveLifecycleBackendCleanupError,
        match="timeout_cleanup_incomplete",
    ):
        worker.recognize_wav(
            _wav(tmp_path),
            grammar=("shutdown ares",),
            timeout_seconds=1.0,
        )


def test_busy_worker_cannot_start_competing_whisper_fallback(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    worker = _VoskLifecycleProcessWorker(model_path=model)
    assert worker._request_lock.acquire(timeout=0.1)
    try:
        with pytest.raises(
            ActiveLifecycleBackendCleanupError,
            match="worker_busy_fallback_blocked",
        ):
            worker.recognize_wav(
                _wav(tmp_path),
                grammar=("shutdown ares",),
                timeout_seconds=0.1,
            )
    finally:
        worker._request_lock.release()


def test_unknown_worker_liveness_is_not_mistaken_for_confirmed_reap(tmp_path):
    class UnknownLivenessProcess:
        pid = 4321
        exitcode = None

        def __init__(self):
            self.terminate_count = 0
            self.kill_count = 0
            self.join_count = 0
            self.close_count = 0

        def is_alive(self):
            raise OSError("process liveness unavailable")

        def terminate(self):
            self.terminate_count += 1

        def kill(self):
            self.kill_count += 1

        def join(self, _timeout):
            self.join_count += 1

        def close(self):
            self.close_count += 1

    model = tmp_path / "model"
    model.mkdir()
    worker = _VoskLifecycleProcessWorker(model_path=model)
    process = UnknownLivenessProcess()
    worker._process = process
    worker._last_worker_reaped = False

    cleanup_complete = worker._terminate_worker()
    diagnostics = dict(worker.diagnostics)

    assert cleanup_complete is False
    assert worker._process is process
    assert process.terminate_count == 1
    assert process.kill_count == 1
    assert process.close_count == 0
    assert diagnostics["worker_alive"] is True
    assert diagnostics["worker_liveness_known"] is False
    assert diagnostics["worker_reaped"] is False


def test_backend_close_retains_unreaped_worker_handle_and_is_terminal(tmp_path):
    class UnreapedWorker:
        def __init__(self):
            self.close_count = 0
            self.allow_cleanup = False

        @property
        def diagnostics(self):
            return {
                "worker_pid": 4321,
                "worker_alive": not self.allow_cleanup,
                "worker_exitcode": None if not self.allow_cleanup else -9,
                "worker_reaped": self.allow_cleanup,
                "worker_start_count": 1,
                "worker_timeout_count": 1,
            }

        def close(self):
            self.close_count += 1
            if not self.allow_cleanup:
                raise ActiveLifecycleBackendCleanupError(
                    "vosk_lifecycle_close_cleanup_incomplete"
                )

    model = tmp_path / "model"
    model.mkdir()
    backend = VoskLifecycleGrammarBackend(model_path=model)
    worker = UnreapedWorker()
    backend._process_worker = worker

    with pytest.raises(ActiveLifecycleBackendCleanupError):
        backend.close()

    assert backend._process_worker is worker
    assert backend.worker_diagnostics["worker_alive"] is True
    with pytest.raises(RuntimeError, match="backend_closed"):
        backend.recognize_wav(
            _wav(tmp_path),
            grammar=("shutdown ares",),
            timeout_seconds=1.0,
        )

    worker.allow_cleanup = True
    backend.close()
    assert backend._process_worker is None
    assert backend.worker_diagnostics["worker_reaped"] is True
    assert worker.close_count == 2


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
