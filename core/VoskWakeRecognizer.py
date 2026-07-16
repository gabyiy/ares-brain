from __future__ import annotations

from dataclasses import replace
import importlib
import json
from math import isfinite
from pathlib import Path
from threading import RLock
import time
from typing import Any, Callable, Mapping, Optional
import wave

from core.Contracts import (
    WakeRecognizerRequestV1,
    WakeRecognizerResultV1,
    new_correlation_id,
)
from core.WakeRecognizer import (
    WAKE_RECOGNIZER_ERROR,
    WAKE_RECOGNIZER_READY,
    WAKE_RECOGNIZER_RECOGNIZING,
    WAKE_RECOGNIZER_STOPPED,
    WakeRecognizerLocalDiagnostics,
    classify_constrained_recognition,
)
from core.StandbyWakeListener import (
    DEFAULT_WAKE_VOSK_MODEL,
    expand_control_phrase_aliases,
)


VOSK_RECOGNIZER_NAME = "vosk_constrained_grammar"
DEFAULT_VOSK_WAKE_MODEL = DEFAULT_WAKE_VOSK_MODEL
RECOMMENDED_RASPBERRY_PI_VOSK_MODEL = "vosk-model-small-en-us-0.15"
VOSK_MODEL_DOWNLOAD_URL = (
    "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
)
VOSK_MODEL_INSTALL_COMMAND = (
    "mkdir -p models/vosk && "
    "curl -fL https://alphacephei.com/vosk/models/"
    "vosk-model-small-en-us-0.15.zip -o /tmp/vosk-model-small-en-us-0.15.zip && "
    "unzip -q /tmp/vosk-model-small-en-us-0.15.zip -d models/vosk"
)
DEFAULT_VOSK_WAKE_MINIMUM_CONFIDENCE = 0.8


class VoskWakeRecognizer:
    """Offline constrained-grammar recognizer for bounded standby candidates."""

    recognizer_name = VOSK_RECOGNIZER_NAME

    def __init__(
        self,
        *,
        model_path: str | Path = DEFAULT_VOSK_WAKE_MODEL,
        minimum_confidence: float = DEFAULT_VOSK_WAKE_MINIMUM_CONFIDENCE,
        vosk_module: Any = None,
        model_factory: Optional[Callable[[str], Any]] = None,
        recognizer_factory: Optional[Callable[[Any, float, str], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model_path = Path(model_path).expanduser().resolve()
        self.minimum_confidence = _validated_confidence(minimum_confidence)
        self._vosk_module = vosk_module
        self._model_factory = model_factory
        self._recognizer_factory = recognizer_factory
        self.clock = clock
        self._lock = RLock()
        self._state = WAKE_RECOGNIZER_STOPPED
        self._model: Any = None
        self._cancelled = False
        self._pending_medium_phrase = ""
        self._pending_medium_count = 0
        self._pending_medium_at = 0.0
        self.last_diagnostics: Optional[WakeRecognizerLocalDiagnostics] = None

    def start(self) -> WakeRecognizerResultV1:
        with self._lock:
            if self._state == WAKE_RECOGNIZER_READY and self._model is not None:
                return self._lifecycle_result(True, "already_started")
            self._cancelled = False
            self._clear_medium_confirmation_locked()
        if not self.model_path.is_dir():
            with self._lock:
                self._state = WAKE_RECOGNIZER_ERROR
            return self._lifecycle_result(
                False,
                "model_missing",
                error_code="vosk_model_missing",
                error_message=_missing_model_message(self.model_path),
            )
        try:
            module = self._load_module()
            set_log_level = getattr(module, "SetLogLevel", None)
            if callable(set_log_level):
                set_log_level(-1)
            factory = self._model_factory or getattr(module, "Model", None)
            if not callable(factory):
                raise RuntimeError("vosk_model_factory_unavailable")
            model = factory(str(self.model_path))
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
            with self._lock:
                self._state = WAKE_RECOGNIZER_ERROR
            code = "vosk_dependency_missing" if isinstance(error, ImportError) else "vosk_model_load_failed"
            return self._lifecycle_result(
                False,
                "start_failed",
                error_code=code,
                error_message=(
                    "Vosk is unavailable. Install project dependencies with "
                    "python -m pip install -r requirements.txt."
                    if isinstance(error, ImportError)
                    else f"Vosk model could not be loaded from {self.model_path}: {error}"
                ),
            )
        with self._lock:
            self._model = model
            self._state = WAKE_RECOGNIZER_READY
        return self._lifecycle_result(True, "started")

    def health_check(self) -> WakeRecognizerResultV1:
        with self._lock:
            healthy = self._state == WAKE_RECOGNIZER_READY and self._model is not None
        return self._lifecycle_result(
            healthy,
            "healthy" if healthy else "unhealthy",
            error_code="" if healthy else "vosk_recognizer_not_ready",
            error_message="" if healthy else "Vosk wake recognizer is not started.",
        )

    def recognize_wav(self, request: WakeRecognizerRequestV1) -> WakeRecognizerResultV1:
        if not isinstance(request, WakeRecognizerRequestV1):
            raise TypeError("request must be WakeRecognizerRequestV1")
        with self._lock:
            if self._state != WAKE_RECOGNIZER_READY or self._model is None:
                return self._failure(request, "vosk_recognizer_not_ready", "Vosk wake recognizer is not ready.")
            if self._cancelled:
                return self._failure(request, "vosk_recognition_cancelled", "Vosk wake recognition was cancelled.")
            self._state = WAKE_RECOGNIZER_RECOGNIZING
            model = self._model
        started_at = self.clock()
        self.last_diagnostics = None
        try:
            timeout_seconds = _validated_timeout(request.timeout_seconds)
            deadline = started_at + timeout_seconds
            path = Path(str(request.audio_path or "")).expanduser().resolve()
            metadata = _validate_canonical_wav(path)
            if (
                request.sample_rate_hz != metadata["sample_rate_hz"]
                or request.channels != metadata["channels"]
                or request.sample_width_bytes != metadata["sample_width_bytes"]
            ):
                return self._failure(
                    request,
                    "vosk_audio_contract_mismatch",
                    "Wake recognizer request metadata does not match the WAV header.",
                )
            grammar = _build_grammar(request)
            module = self._load_module()
            factory = self._recognizer_factory or getattr(module, "KaldiRecognizer", None)
            if not callable(factory):
                return self._failure(
                    request,
                    "vosk_recognizer_factory_unavailable",
                    "Vosk KaldiRecognizer is unavailable.",
                )
            recognizer = factory(model, float(metadata["sample_rate_hz"]), json.dumps(grammar))
            set_words = getattr(recognizer, "SetWords", None)
            if not callable(set_words):
                return self._failure(
                    request,
                    "vosk_word_confidence_unavailable",
                    "Vosk recognizer cannot enable word confidence output.",
                )
            set_words(True)
            payloads = _decode_complete_wav(
                recognizer,
                path,
                self._is_cancelled,
                clock=self.clock,
                deadline=deadline,
            )
            combined = _combine_payloads(payloads)
            elapsed = max(0.0, self.clock() - started_at)
            result = classify_constrained_recognition(
                combined["text"],
                combined["result"],
                wake_phrases=request.wake_phrases,
                wake_phrase_aliases=request.wake_phrase_aliases,
                standby_phrases=request.standby_phrases,
                shutdown_phrases=request.shutdown_phrases,
                canonical_wake_phrase=request.canonical_wake_phrase,
                minimum_confidence=request.minimum_confidence,
                medium_confidence=request.medium_confidence,
                medium_confirmation_repetitions=(
                    request.medium_confirmation_repetitions
                ),
                recognizer_name=self.recognizer_name,
                runtime_id=request.runtime_id,
                lifecycle_state=request.lifecycle_state,
                correlation_id=request.correlation_id,
                model_path=str(self.model_path),
                grammar_phrase_count=len(grammar),
                processing_time_seconds=elapsed,
            )
            result = self._apply_medium_confidence_confirmation(
                result,
                at_time=started_at + elapsed,
                required_count=request.medium_confirmation_repetitions,
                window_seconds=request.medium_confirmation_window_seconds,
            )
            self.last_diagnostics = WakeRecognizerLocalDiagnostics(
                recognizer_name=self.recognizer_name,
                raw_recognition_result=json.dumps(payloads, sort_keys=True),
                recognized_text=combined["text"],
                normalized_phrase=_normalize_for_diagnostics(combined["text"]),
                confidence=result.confidence,
                confidence_available=result.confidence_available,
                confidence_tier=result.confidence_tier,
                confirmation_count=result.confirmation_count,
                confirmation_required_count=result.confirmation_required_count,
                classification=(
                    "accepted"
                    if result.command_category != "non_wake"
                    else "rejected"
                ),
                classification_reason=result.classification_reason,
                rejection_reason=result.rejection_reason,
                selected_alias=result.selected_alias,
                selected_wake_phrase=result.selected_wake_phrase,
                canonical_wake_phrase=result.canonical_wake_phrase,
                model_path=str(self.model_path),
                grammar_phrase_count=len(grammar),
                processing_time_seconds=elapsed,
            )
            return result
        except TimeoutError as error:
            return self._failure(
                request,
                "vosk_recognition_timeout",
                str(error),
            )
        except (EOFError, json.JSONDecodeError, OSError, RuntimeError, TypeError, ValueError, wave.Error) as error:
            return self._failure(
                request,
                "vosk_recognition_failed",
                f"{error.__class__.__name__}:{str(error)[:140]}",
            )
        finally:
            with self._lock:
                if self._state != WAKE_RECOGNIZER_STOPPED:
                    self._state = WAKE_RECOGNIZER_READY if self._model is not None else WAKE_RECOGNIZER_STOPPED

    def cancel(self) -> WakeRecognizerResultV1:
        with self._lock:
            self._cancelled = True
        return self._lifecycle_result(True, "cancelled")

    def stop(self) -> WakeRecognizerResultV1:
        with self._lock:
            self._cancelled = True
            self._model = None
            self._clear_medium_confirmation_locked()
            self._state = WAKE_RECOGNIZER_STOPPED
        return self._lifecycle_result(True, "stopped")

    close = stop

    def _load_module(self) -> Any:
        if self._vosk_module is None:
            self._vosk_module = importlib.import_module("vosk")
        return self._vosk_module

    def _is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def _apply_medium_confidence_confirmation(
        self,
        result: WakeRecognizerResultV1,
        *,
        at_time: float,
        required_count: int,
        window_seconds: float,
    ) -> WakeRecognizerResultV1:
        if not result.confirmation_required or not result.matched_phrase:
            with self._lock:
                self._clear_medium_confirmation_locked()
            return result
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, (int, float))
            or not isfinite(float(window_seconds))
            or not 1.0 <= float(window_seconds) <= 30.0
        ):
            raise ValueError(
                "medium_confirmation_window_seconds must be between 1 and 30"
            )
        if (
            isinstance(required_count, bool)
            or not isinstance(required_count, int)
            or not 2 <= required_count <= 3
        ):
            raise ValueError(
                "medium_confirmation_repetitions must be between 2 and 3"
            )
        with self._lock:
            elapsed = float(at_time) - self._pending_medium_at
            same_phrase = self._pending_medium_phrase == result.matched_phrase
            within_window = 0.0 <= elapsed <= float(window_seconds)
            if same_phrase and within_window:
                self._pending_medium_count += 1
            else:
                self._pending_medium_phrase = result.matched_phrase
                self._pending_medium_count = 1
            self._pending_medium_at = float(at_time)
            count = self._pending_medium_count
            if count >= required_count:
                self._clear_medium_confirmation_locked()
        if count < required_count:
            return replace(
                result,
                confirmation_count=count,
                confirmation_required_count=required_count,
            )
        return replace(
            result,
            status="wake_detected",
            wake_detected=True,
            command_category="activation",
            normalized_wake_phrase=result.canonical_wake_phrase or "ares",
            selected_wake_phrase=result.matched_phrase,
            classification_reason="accepted_medium_confidence_repetition",
            rejection_reason="",
            confirmation_required=False,
            confirmation_count=count,
            confirmation_required_count=required_count,
        )

    def _clear_medium_confirmation_locked(self) -> None:
        self._pending_medium_phrase = ""
        self._pending_medium_count = 0
        self._pending_medium_at = 0.0

    def _failure(
        self,
        request: WakeRecognizerRequestV1,
        code: str,
        message: str,
    ) -> WakeRecognizerResultV1:
        return WakeRecognizerResultV1(
            success=False,
            status="recognition_failed",
            runtime_id=request.runtime_id,
            lifecycle_state=request.lifecycle_state,
            recognizer_name=self.recognizer_name,
            minimum_confidence=request.minimum_confidence,
            medium_confidence=request.medium_confidence,
            model_path=str(self.model_path),
            error_code=code,
            error_message=str(message or code)[:1024],
            correlation_id=request.correlation_id,
            metadata={"safe": True, "contains_transcript": False, "contains_audio": False},
        )

    def _lifecycle_result(
        self,
        success: bool,
        status: str,
        *,
        error_code: str = "",
        error_message: str = "",
    ) -> WakeRecognizerResultV1:
        return WakeRecognizerResultV1(
            success=success,
            status=status,
            recognizer_name=self.recognizer_name,
            minimum_confidence=self.minimum_confidence,
            model_path=str(self.model_path),
            error_code=error_code,
            error_message=str(error_message or "")[:1024],
            correlation_id=new_correlation_id("vosk-wake"),
            metadata={"safe": True, "contains_transcript": False, "contains_audio": False},
        )


def _build_grammar(request: WakeRecognizerRequestV1) -> list[str]:
    standby = expand_control_phrase_aliases(
        request.standby_phrases,
        request.wake_phrase_aliases,
    )
    shutdown = expand_control_phrase_aliases(
        request.shutdown_phrases,
        request.wake_phrase_aliases,
    )
    phrases = [
        str(value or "").strip().casefold()
        for value in (
            *request.wake_phrases,
            *standby,
            *shutdown,
        )
        if str(value or "").strip()
    ]
    unique = list(dict.fromkeys(phrases))
    if "[unk]" not in unique:
        unique.append("[unk]")
    if len(unique) < 2 or len(unique) > 65:
        raise ValueError("Vosk wake grammar must contain between 1 and 64 phrases plus [unk]")
    return unique


def _validate_canonical_wav(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise ValueError(f"wake WAV is missing: {path}")
    with wave.open(str(path), "rb") as source:
        metadata = {
            "sample_rate_hz": source.getframerate(),
            "channels": source.getnchannels(),
            "sample_width_bytes": source.getsampwidth(),
            "frame_count": source.getnframes(),
            "compression_type": 1 if source.getcomptype() == "NONE" else 0,
        }
    if metadata["frame_count"] <= 0:
        raise ValueError("wake WAV contains no PCM frames")
    if (
        metadata["sample_rate_hz"] != 16000
        or metadata["channels"] != 1
        or metadata["sample_width_bytes"] != 2
        or metadata["compression_type"] != 1
    ):
        raise ValueError("wake WAV must be canonical 16 kHz mono signed 16-bit PCM")
    return metadata


def _decode_complete_wav(
    recognizer: Any,
    path: Path,
    cancelled: Callable[[], bool],
    *,
    clock: Callable[[], float],
    deadline: float,
) -> list[Mapping[str, Any]]:
    accept = getattr(recognizer, "AcceptWaveform", None)
    result_method = getattr(recognizer, "Result", None)
    final_method = getattr(recognizer, "FinalResult", None)
    if not callable(accept) or not callable(result_method) or not callable(final_method):
        raise RuntimeError("vosk_recognizer_methods_unavailable")
    payloads: list[Mapping[str, Any]] = []
    with wave.open(str(path), "rb") as source:
        while True:
            if cancelled():
                raise RuntimeError("vosk_recognition_cancelled")
            if clock() >= deadline:
                raise TimeoutError("vosk_recognition_timeout")
            chunk = source.readframes(4000)
            if not chunk:
                break
            if accept(chunk):
                payloads.append(_parse_payload(result_method()))
            if clock() >= deadline:
                raise TimeoutError("vosk_recognition_timeout")
    if clock() >= deadline:
        raise TimeoutError("vosk_recognition_timeout")
    payloads.append(_parse_payload(final_method()))
    return payloads


def _parse_payload(value: Any) -> Mapping[str, Any]:
    payload = json.loads(value) if isinstance(value, str) else value
    if not isinstance(payload, Mapping):
        raise ValueError("Vosk result must be a JSON object")
    result = payload.get("result", [])
    if result is None:
        result = []
    if not isinstance(result, list):
        raise ValueError("Vosk word result must be a list")
    return {"text": str(payload.get("text") or ""), "result": result}


def _combine_payloads(payloads: list[Mapping[str, Any]]) -> dict[str, Any]:
    text_parts: list[str] = []
    words: list[Mapping[str, Any]] = []
    for payload in payloads:
        text = str(payload.get("text") or "").strip()
        if text:
            text_parts.append(text)
        values = payload.get("result") or []
        if isinstance(values, list):
            words.extend(value for value in values if isinstance(value, Mapping))
    return {"text": " ".join(text_parts).strip(), "result": words}


def _normalize_for_diagnostics(text: str) -> str:
    from core.StandbyWakeListener import normalize_wake_phrase

    return normalize_wake_phrase(text)


def _missing_model_message(path: Path) -> str:
    return (
        f"Vosk wake model is missing at {path}. Recommended Raspberry Pi model family: "
        f"{RECOMMENDED_RASPBERRY_PI_VOSK_MODEL}. Install it with: "
        f"{VOSK_MODEL_INSTALL_COMMAND}"
    )


def _validated_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("minimum_confidence must be a finite number")
    number = float(value)
    if not isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("minimum_confidence must be between 0 and 1")
    return number


def _validated_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be a finite number")
    number = float(value)
    if not isfinite(number) or not 0.1 <= number <= 30.0:
        raise ValueError("timeout_seconds must be between 0.1 and 30.0")
    return number
