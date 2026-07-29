from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
from threading import RLock
import time
from typing import Any, Callable, Dict, List, Optional, Sequence
import wave

from core.LinuxAlsaMicrophone import SafeProcessResult, SafeSubprocessRunner
from core.Microphone import AudioChunk
from core.SpeechToText import SpeechToTextAdapter, TranscriptionResult
from core.WavAudio import (
    CANONICAL_CHANNELS,
    CANONICAL_SAMPLE_RATE_HZ,
    CANONICAL_SAMPLE_WIDTH_BYTES,
)


DEFAULT_WHISPER_COMMAND = "whisper-cli"
DEFAULT_WHISPER_MODEL_PATH = "models/whisper/ggml-tiny.en.bin"
DEFAULT_WHISPER_LANGUAGE = "auto"
DEFAULT_WHISPER_TIMEOUT_SECONDS = 15.0
MAX_WHISPER_TIMEOUT_SECONDS = 900.0
DEFAULT_WHISPER_TERMINATION_GRACE_SECONDS = 1.0
DEFAULT_WHISPER_HARD_CLEANUP_DEADLINE_SECONDS = 3.0
DEFAULT_WHISPER_WATCHDOG_INTERVAL_SECONDS = 0.10
MAX_WHISPER_CAPTURE_OUTPUT_BYTES = 4 * 1024 * 1024
WHISPER_TERMINATE_SIGNAL = int(getattr(signal, "SIGTERM", 15))
WHISPER_KILL_SIGNAL = int(getattr(signal, "SIGKILL", 9))

WHISPER_STATUS_BINARY_MISSING = "whisper_binary_missing"
WHISPER_STATUS_MODEL_MISSING = "whisper_model_missing"
WHISPER_STATUS_INVALID_AUDIO = "invalid_audio"
WHISPER_STATUS_TRANSCRIPTION_TIMEOUT = "transcription_timeout"
WHISPER_STATUS_TRANSCRIPTION_FAILED = "transcription_failed"
WHISPER_STATUS_NO_TRANSCRIPTION = "no_transcription"
WHISPER_STATUS_TRANSCRIBED = "transcribed"
WHISPER_STATUS_AUDIO_SILENT = "audio_silent"
WHISPER_STATUS_AUDIO_BELOW_THRESHOLD = "audio_below_threshold"
WHISPER_STATUS_NO_USABLE_SPEECH = "no_usable_speech"
NO_SPEECH_MARKERS = frozenset(
    {
        "blankaudio",
        "nospeech",
        "silence",
    }
)

Clock = Callable[[], float]
StatusCallback = Callable[[str], None]
ProcessGroupGetter = Callable[[int], int]
ProcessGroupSignaler = Callable[[int, int], None]


class WhisperSubprocessRunner(SafeSubprocessRunner):
    """Run whisper.cpp under one bounded, cancellable process-group boundary.

    stdout and stderr go to private temporary files rather than pipes.  A noisy
    or wedged child therefore cannot fill a pipe and block before the parent
    reaches its wall-clock deadline.
    """

    def __init__(
        self,
        *,
        process_factory: Callable[..., Any] = subprocess.Popen,
        termination_grace_seconds: float = DEFAULT_WHISPER_TERMINATION_GRACE_SECONDS,
        hard_cleanup_deadline_seconds: float = (
            DEFAULT_WHISPER_HARD_CLEANUP_DEADLINE_SECONDS
        ),
        clock: Clock = time.perf_counter,
        status_callback: Optional[StatusCallback] = None,
        diagnostic_progress: bool = False,
        process_group_getter: Optional[ProcessGroupGetter] = None,
        process_group_signaler: Optional[ProcessGroupSignaler] = None,
    ) -> None:
        grace = float(termination_grace_seconds)
        if not 0.1 <= grace <= 10.0:
            raise ValueError("termination_grace_seconds must be between 0.1 and 10")
        cleanup_deadline = float(hard_cleanup_deadline_seconds)
        if not grace <= cleanup_deadline <= 30.0:
            raise ValueError(
                "hard_cleanup_deadline_seconds must be between "
                "termination_grace_seconds and 30"
            )
        self.process_factory = process_factory
        self.termination_grace_seconds = grace
        self.hard_cleanup_deadline_seconds = cleanup_deadline
        self.clock = clock
        self.status_callback = status_callback
        self.diagnostic_progress = bool(diagnostic_progress)
        self._process_groups_enabled = bool(
            (os.name == "posix" and process_factory is subprocess.Popen)
            or (
                process_group_getter is not None
                and process_group_signaler is not None
            )
        )
        self._process_group_getter = process_group_getter or getattr(
            os,
            "getpgid",
            lambda pid: int(pid),
        )
        self._process_group_signaler = process_group_signaler or getattr(
            os,
            "killpg",
            None,
        )
        self._active_lock = RLock()
        self._cleanup_lock = RLock()
        self._run_lock = RLock()
        self._active_process: Any = None
        self._active_pid = 0
        self._active_pgid = 0
        self._active_cancelled = False
        self._active_cancel_reason = ""

    @property
    def active_pid(self) -> int:
        with self._active_lock:
            return self._active_pid

    @property
    def active_pgid(self) -> int:
        with self._active_lock:
            return self._active_pgid

    def run(self, args: Sequence[str], timeout_seconds: float) -> SafeProcessResult:
        with self._run_lock:
            return self._run_serialized(args, timeout_seconds)

    def cancel_current(self, reason: str = "cancelled") -> bool:
        """Terminate the active Whisper group without waiting without a bound."""

        with self._active_lock:
            process = self._active_process
            pgid = self._active_pgid
            if process is None:
                return False
            self._active_cancelled = True
            self._active_cancel_reason = str(reason or "cancelled")[:120]
        self._cleanup_process(
            process,
            pgid,
            cleanup_reason=self._active_cancel_reason,
        )
        return True

    def _run_serialized(
        self,
        args: Sequence[str],
        timeout_seconds: float,
    ) -> SafeProcessResult:
        safe_args = [str(arg) for arg in args]
        timeout = _bounded_timeout(timeout_seconds)
        started = self.clock()
        stdout_capture = tempfile.TemporaryFile(mode="w+b")
        stderr_capture = tempfile.TemporaryFile(mode="w+b")
        process: Any = None
        pid = 0
        pgid = 0
        process_group_started = False
        cleanup: Dict[str, Any] = self._empty_cleanup_metadata()
        timed_out = False
        process_error = ""
        process_errno: Optional[int] = None
        stdout = ""
        stderr = ""
        stdout_truncated = False
        stderr_truncated = False
        handles_closed = False
        cancelled = False
        cancel_reason = ""

        popen_kwargs: Dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": stdout_capture,
            "stderr": stderr_capture,
            "shell": False,
        }
        if self._process_groups_enabled:
            popen_kwargs["start_new_session"] = True
            process_group_started = True
        try:
            process = self.process_factory(safe_args, **popen_kwargs)
            pid = int(getattr(process, "pid", 0) or 0)
            pgid = self._resolve_pgid(pid)
            self._register_active(process, pid, pgid)
            self._emit(f"Whisper process started: pid={pid}, pgid={pgid}")
            self._emit_progress("process_started")
            self._emit(f"Whisper timeout: {timeout:g} seconds")
            try:
                self._wait_process(process, timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._emit(
                    f"Whisper transcription timed out after {timeout:g} seconds"
                )
                self._emit("Whisper timeout triggered; terminating process group")
                self._emit_progress("timeout_triggered")
                cleanup = self._cleanup_process(
                    process,
                    pgid,
                    cleanup_reason="transcription_timeout",
                )
            except OSError as error:
                process_error = f"process_io_error:{error.__class__.__name__}"
                process_errno = getattr(error, "errno", None)
                cleanup = self._cleanup_process(
                    process,
                    pgid,
                    cleanup_reason="process_io_error",
                )
            except BaseException:
                self._mark_cancelled("base_exception")
                self._cleanup_process(
                    process,
                    pgid,
                    cleanup_reason="base_exception",
                )
                raise
            else:
                active_cancelled, active_cancel_reason = self._active_cancel_state(
                    process
                )
                if active_cancelled:
                    cleanup = self._cleanup_process(
                        process,
                        pgid,
                        cleanup_reason=active_cancel_reason or "cancelled",
                    )
                elif self._group_alive(process, pgid):
                    cleanup = self._cleanup_process(
                        process,
                        pgid,
                        cleanup_reason="descendant_after_parent_exit",
                    )
                else:
                    cleanup = {
                        **self._empty_cleanup_metadata(),
                        "reaped": self._process_reaped(process),
                        "cleanup_completed": self._process_reaped(process),
                    }
        except FileNotFoundError:
            process_error = "process_not_found"
        except OSError as error:
            process_error = f"process_os_error:{error.__class__.__name__}"
            process_errno = getattr(error, "errno", None)
        finally:
            if process is not None:
                cancelled, cancel_reason = self._active_cancel_state(process)
            stdout, stdout_truncated = self._read_capture(stdout_capture)
            stderr, stderr_truncated = self._read_capture(stderr_capture)
            self._close_capture(stdout_capture)
            self._close_capture(stderr_capture)
            handles_closed = bool(
                getattr(stdout_capture, "closed", False)
                and getattr(stderr_capture, "closed", False)
            )
            if process is not None:
                self._unregister_active(process)
            if handles_closed:
                self._emit("Whisper stdout/stderr handles closed")
                self._emit_progress("output_handles_closed")

        elapsed = round(_elapsed(self.clock, started), 6)
        returncode = self._returncode(process)
        cleanup = {
            **cleanup,
            "cleanup_completed": bool(
                cleanup.get("cleanup_completed") and handles_closed
            ),
        }
        metadata: Dict[str, Any] = {
            "pid": pid,
            "pgid": pgid,
            "timeout_seconds": timeout,
            "elapsed_seconds": elapsed,
            "process_group_started": process_group_started,
            "stdout_transport": "bounded_temporary_file",
            "stderr_transport": "bounded_temporary_file",
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "output_handles_closed": handles_closed,
            "hard_cleanup_deadline_seconds": self.hard_cleanup_deadline_seconds,
            "cancelled": cancelled,
            "cancel_reason": cancel_reason,
            **cleanup,
        }
        if process_errno is not None:
            metadata["errno"] = process_errno
        if timed_out:
            metadata["typed_status"] = WHISPER_STATUS_TRANSCRIPTION_TIMEOUT
            process_error = "process_timeout"
        elif cancelled:
            process_error = "process_cancelled"
        if process is not None:
            if cleanup.get("cleanup_reason"):
                self._emit(
                    "Whisper process cleanup: "
                    + (
                        "completed"
                        if cleanup["cleanup_completed"]
                        else "incomplete"
                    )
                )
            self._emit(f"Whisper completed: exit={returncode}, elapsed={elapsed:g} seconds")
            self._emit_progress("process_completed")
        return SafeProcessResult(
            args=safe_args,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            error_message=process_error,
            metadata=metadata,
        )

    def _cleanup_process(
        self,
        process: Any,
        pgid: int,
        *,
        cleanup_reason: str,
    ) -> Dict[str, Any]:
        with self._cleanup_lock:
            cleanup_started = self.clock()
            cleanup_deadline = cleanup_started + self.hard_cleanup_deadline_seconds
            terminated = False
            killed = False
            if not self._process_alive(process) and not self._group_alive(process, pgid):
                result = {
                    "terminated": False,
                    "killed": False,
                    "reaped": self._process_reaped(process),
                    "cleanup_completed": self._process_reaped(process),
                    "cleanup_reason": cleanup_reason,
                    "cleanup_elapsed_seconds": round(
                        _elapsed(self.clock, cleanup_started),
                        6,
                    ),
                }
                return result

            self._emit("Terminating Whisper process group")
            self._emit_progress("sigterm_requested")
            terminated = self._signal_process(
                process,
                pgid,
                WHISPER_TERMINATE_SIGNAL,
            )
            self._wait_until(
                process,
                pgid,
                min(
                    self.termination_grace_seconds,
                    self._remaining(cleanup_deadline),
                ),
                require_group_exit=True,
            )
            if self._process_alive(process) or self._group_alive(process, pgid):
                self._emit("Whisper process group did not exit after SIGTERM; sending SIGKILL")
                self._emit_progress("sigkill_requested")
                killed = self._signal_process(
                    process,
                    pgid,
                    WHISPER_KILL_SIGNAL,
                )
            self._wait_until(
                process,
                pgid,
                self._remaining(cleanup_deadline),
                require_group_exit=True,
            )
            reaped = self._process_reaped(process)
            if reaped:
                self._emit("Whisper process reaped")
                self._emit_progress("process_reaped")
            cleanup_completed = bool(
                reaped and not self._process_alive(process)
                and not self._group_alive(process, pgid)
            )
            result = {
                "terminated": terminated,
                "killed": killed,
                "reaped": reaped,
                "cleanup_completed": cleanup_completed,
                "cleanup_reason": cleanup_reason,
                "cleanup_elapsed_seconds": round(
                    _elapsed(self.clock, cleanup_started),
                    6,
                ),
            }
            return result

    def _wait_until(
        self,
        process: Any,
        pgid: int,
        timeout_seconds: float,
        *,
        require_group_exit: bool,
    ) -> None:
        timeout = max(0.0, float(timeout_seconds))
        deadline = self.clock() + timeout
        if self._process_alive(process) and timeout > 0:
            try:
                self._wait_process(process, timeout)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not require_group_exit or not self._process_groups_enabled:
            return
        while self._group_alive(process, pgid) and self._remaining(deadline) > 0:
            time.sleep(min(0.01, self._remaining(deadline)))

    def _wait_process(self, process: Any, timeout_seconds: float) -> int:
        poll = getattr(process, "poll", None)
        if not callable(poll):
            raise OSError("process_poll_unavailable")
        timeout = max(0.0, float(timeout_seconds))
        deadline = self.clock() + timeout
        while True:
            value = poll()
            if value is not None:
                return int(value)
            remaining = self._remaining(deadline)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(
                    getattr(process, "args", "whisper-cli"),
                    timeout,
                )
            time.sleep(
                min(
                    DEFAULT_WHISPER_WATCHDOG_INTERVAL_SECONDS,
                    remaining,
                )
            )

    def _resolve_pgid(self, pid: int) -> int:
        if pid <= 0:
            return 0
        if not self._process_groups_enabled:
            return pid
        try:
            return int(self._process_group_getter(pid))
        except (OSError, TypeError, ValueError):
            return pid

    def _signal_process(self, process: Any, pgid: int, signal_number: int) -> bool:
        if (
            self._process_groups_enabled
            and pgid > 0
            and callable(self._process_group_signaler)
        ):
            try:
                self._process_group_signaler(pgid, signal_number)
                self._emit(
                    "Whisper process-group signal sent: "
                    f"signal={signal_number}, pid={int(getattr(process, 'pid', 0) or 0)}, "
                    f"pgid={pgid}"
                )
                return True
            except ProcessLookupError:
                return False
            except OSError as error:
                if getattr(error, "errno", None) == errno.ESRCH:
                    return False
        method_name = (
            "terminate"
            if signal_number == WHISPER_TERMINATE_SIGNAL
            else "kill"
        )
        method = getattr(process, method_name, None)
        if not callable(method):
            return False
        try:
            method()
            return True
        except OSError:
            return False

    def _group_alive(self, process: Any, pgid: int) -> bool:
        if (
            not self._process_groups_enabled
            or pgid <= 0
            or not callable(self._process_group_signaler)
        ):
            return self._process_alive(process)
        try:
            self._process_group_signaler(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as error:
            return getattr(error, "errno", None) != errno.ESRCH

    @staticmethod
    def _process_alive(process: Any) -> bool:
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return True
        try:
            return poll() is None
        except OSError:
            return False

    @staticmethod
    def _process_reaped(process: Any) -> bool:
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return False
        try:
            return poll() is not None
        except OSError:
            return False

    @staticmethod
    def _returncode(process: Any) -> int:
        if process is None:
            return -1
        value = getattr(process, "returncode", None)
        if value is None:
            poll = getattr(process, "poll", None)
            if callable(poll):
                try:
                    value = poll()
                except OSError:
                    value = None
        return int(value) if value is not None else -1

    @staticmethod
    def _read_capture(handle: Any) -> tuple[str, bool]:
        try:
            handle.flush()
            handle.seek(0)
            payload = handle.read(MAX_WHISPER_CAPTURE_OUTPUT_BYTES + 1)
        except (OSError, ValueError):
            return "", False
        truncated = len(payload) > MAX_WHISPER_CAPTURE_OUTPUT_BYTES
        if truncated:
            payload = payload[:MAX_WHISPER_CAPTURE_OUTPUT_BYTES]
        return _process_text(payload), truncated

    @staticmethod
    def _close_capture(handle: Any) -> None:
        try:
            handle.close()
        except OSError:
            pass

    def _register_active(self, process: Any, pid: int, pgid: int) -> None:
        with self._active_lock:
            self._active_process = process
            self._active_pid = pid
            self._active_pgid = pgid
            self._active_cancelled = False
            self._active_cancel_reason = ""

    def _unregister_active(self, process: Any) -> None:
        with self._active_lock:
            if self._active_process is process:
                self._active_process = None
                self._active_pid = 0
                self._active_pgid = 0
                self._active_cancelled = False
                self._active_cancel_reason = ""

    def _active_cancel_state(self, process: Any) -> tuple[bool, str]:
        with self._active_lock:
            if self._active_process is not process:
                return False, ""
            return self._active_cancelled, self._active_cancel_reason

    def _mark_cancelled(self, reason: str) -> None:
        with self._active_lock:
            if self._active_process is not None:
                self._active_cancelled = True
                self._active_cancel_reason = str(reason or "cancelled")[:120]

    def _remaining(self, deadline: float) -> float:
        return max(0.0, float(deadline) - float(self.clock()))

    def _emit(self, message: str) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(str(message))
        except (OSError, RuntimeError, TypeError, ValueError):
            return

    def _emit_progress(self, event: str) -> None:
        if not self.diagnostic_progress:
            return
        self._emit(
            "Whisper progress timestamp: "
            f"{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}; "
            f"event={str(event or 'unknown')[:80]}"
        )

    @staticmethod
    def _empty_cleanup_metadata() -> Dict[str, Any]:
        return {
            "terminated": False,
            "killed": False,
            "reaped": False,
            "cleanup_completed": False,
            "cleanup_reason": "",
            "cleanup_elapsed_seconds": 0.0,
        }


@dataclass(frozen=True)
class WhisperTranscriptionMetadata:
    processing_time_seconds: float
    language: str = ""
    model_path: str = ""
    whisper_command: str = ""
    audio_path: str = ""
    engine: str = "whisper.cpp"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "processing_time_seconds": round(max(0.0, self.processing_time_seconds), 6),
            "language": self.language,
            "model_path": self.model_path,
            "whisper_command": self.whisper_command,
            "audio_path": self.audio_path,
            "engine": self.engine,
            "metadata": dict(self.metadata),
        }


class LinuxWhisperSpeechToTextAdapter(SpeechToTextAdapter):
    """Offline Whisper STT adapter for Linux/Raspberry Pi.

    The adapter runs a local Whisper executable against local WAV files. It does
    not call internet services, start wake-word detection, run TTS, or create
    conversation loops.
    """

    def __init__(
        self,
        model_path: Optional[str | Path] = None,
        whisper_command: str = DEFAULT_WHISPER_COMMAND,
        language: str = DEFAULT_WHISPER_LANGUAGE,
        timeout_seconds: float = DEFAULT_WHISPER_TIMEOUT_SECONDS,
        minimum_rms: float = 0.0,
        runner: Optional[SafeSubprocessRunner] = None,
        clock: Clock = time.perf_counter,
        source: str = "linux_whisper_speech_to_text_adapter",
        status_callback: Optional[StatusCallback] = None,
    ):
        self.model_path = Path(
            model_path
            or os.environ.get("ARES_WHISPER_MODEL_PATH")
            or DEFAULT_WHISPER_MODEL_PATH
        ).expanduser()
        self.whisper_command = str(
            whisper_command
            or os.environ.get("ARES_WHISPER_COMMAND")
            or DEFAULT_WHISPER_COMMAND
        ).strip()
        self.language = str(language or DEFAULT_WHISPER_LANGUAGE).strip()
        self.timeout_seconds = _bounded_timeout(timeout_seconds)
        self.minimum_rms = _non_negative_float(minimum_rms, "minimum_rms")
        self.runner = runner or WhisperSubprocessRunner(
            status_callback=status_callback,
        )
        self.clock = clock
        self.source = source
        self.transcription_count = 0
        self.speech_engine_accessed = False
        self.audio_hardware_accessed = False

    def cancel_current(self, reason: str = "cancelled") -> bool:
        cancel = getattr(self.runner, "cancel_current", None)
        if not callable(cancel):
            return False
        try:
            return bool(cancel(reason))
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def transcribe(self, audio_chunk: AudioChunk) -> TranscriptionResult:
        if audio_chunk.byte_count == 0:
            self.transcription_count += 1
            return self._success(
                status="empty_audio",
                text="",
                confidence=0.0,
                audio_path="",
                processing_time_seconds=0.0,
                extra_data={
                    "audio_chunk": audio_chunk.to_dict(),
                    "message": "Offline Whisper received empty audio.",
                },
            )

        wav_path = _audio_chunk_wav_path(audio_chunk)
        request_timeout = _audio_chunk_transcription_timeout(audio_chunk)
        if wav_path:
            return self.transcribe_wav(
                wav_path,
                audio_chunk=audio_chunk,
                timeout_seconds=request_timeout,
            )

        with tempfile.TemporaryDirectory(prefix="ares_whisper_audio_") as temp_dir:
            temp_wav = Path(temp_dir) / "audio_chunk.wav"
            try:
                _write_audio_chunk_wav(audio_chunk, temp_wav)
            except (OSError, wave.Error, ValueError) as error:
                return self._failure(
                    status=WHISPER_STATUS_INVALID_AUDIO,
                    error_message=f"invalid_audio:{error.__class__.__name__}",
                    audio_path=str(temp_wav),
                    processing_time_seconds=0.0,
                    extra_data={"audio_chunk": audio_chunk.to_dict()},
                )
            return self.transcribe_wav(
                temp_wav,
                audio_chunk=audio_chunk,
                timeout_seconds=request_timeout,
            )

    def transcribe_wav(
        self,
        wav_path: str | Path,
        audio_chunk: Optional[AudioChunk] = None,
        language: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> TranscriptionResult:
        self.transcription_count += 1
        start_time = self.clock()
        transcription_started_at = _utc_timestamp()
        audio_path = Path(wav_path).expanduser()
        binary_path = self._find_whisper_binary()
        if not binary_path:
            return self._failure(
                status=WHISPER_STATUS_BINARY_MISSING,
                error_message="whisper_binary_missing",
                audio_path=str(audio_path),
                processing_time_seconds=_elapsed(self.clock, start_time),
            )
        if not self.model_path.exists():
            return self._failure(
                status=WHISPER_STATUS_MODEL_MISSING,
                error_message="whisper_model_missing",
                audio_path=str(audio_path),
                processing_time_seconds=_elapsed(self.clock, start_time),
            )

        audio_validation_started_at = _utc_timestamp()
        audio_validation = _validate_wav_audio(audio_path)
        audio_validation_completed_at = _utc_timestamp()
        if not audio_validation["success"]:
            return self._failure(
                status=WHISPER_STATUS_INVALID_AUDIO,
                error_message=str(audio_validation["error_message"]),
                audio_path=str(audio_path),
                processing_time_seconds=_elapsed(self.clock, start_time),
                extra_data={"audio_validation": audio_validation},
            )
        canonical_error = _canonical_wav_error(audio_validation)
        if canonical_error:
            return self._failure(
                status=WHISPER_STATUS_INVALID_AUDIO,
                error_message=canonical_error,
                audio_path=str(audio_path),
                processing_time_seconds=_elapsed(self.clock, start_time),
                extra_data={"audio_validation": audio_validation},
            )
        if int(audio_validation.get("peak_amplitude", 0)) <= 0:
            return self._failure(
                status=WHISPER_STATUS_AUDIO_SILENT,
                error_message="audio_silent",
                audio_path=str(audio_path),
                processing_time_seconds=_elapsed(self.clock, start_time),
                extra_data={"audio_validation": audio_validation},
            )
        if self.minimum_rms > 0 and float(audio_validation.get("rms_amplitude", 0.0)) < self.minimum_rms:
            return self._failure(
                status=WHISPER_STATUS_AUDIO_BELOW_THRESHOLD,
                error_message="audio_below_threshold",
                audio_path=str(audio_path),
                processing_time_seconds=_elapsed(self.clock, start_time),
                extra_data={
                    "audio_validation": audio_validation,
                    "minimum_rms": self.minimum_rms,
                },
            )

        timeout = _bounded_timeout(
            timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        )
        requested_language = str(language or self.language or DEFAULT_WHISPER_LANGUAGE).strip()
        effective_language = _resolve_whisper_language(requested_language, self.model_path)
        model_english_only = _is_english_only_whisper_model(self.model_path)
        with tempfile.TemporaryDirectory(prefix="ares_whisper_output_") as temp_dir:
            output_base = Path(temp_dir) / "transcript"
            command = self._transcribe_command(
                binary_path=binary_path,
                audio_path=audio_path,
                output_base=output_base,
                language=effective_language,
            )
            process_started_at = _utc_timestamp()
            result = self.runner.run(command, timeout_seconds=timeout)
            process_completed_at = _utc_timestamp()
            self.speech_engine_accessed = True
            elapsed = _elapsed(self.clock, start_time)
            timing_data = {
                "transcription_backend": "whisper.cpp",
                "transcription_started_at": transcription_started_at,
                "audio_validation_started_at": audio_validation_started_at,
                "audio_validation_completed_at": audio_validation_completed_at,
                "whisper_process_started_at": process_started_at,
                "whisper_process_completed_at": process_completed_at,
                "transcription_timeout_seconds": timeout,
                "wav_closed_before_inference": True,
            }

            if result.timed_out:
                return self._failure(
                    status=WHISPER_STATUS_TRANSCRIPTION_TIMEOUT,
                    error_message="whisper_transcription_timeout",
                    audio_path=str(audio_path),
                    processing_time_seconds=elapsed,
                    extra_data={
                        "process": _safe_process_data(result),
                        "audio_validation": audio_validation,
                        **timing_data,
                        "transcription_completed_at": process_completed_at,
                        "transcript_parsing_status": "not_started",
                    },
                )
            if result.returncode != 0:
                return self._failure(
                    status=WHISPER_STATUS_TRANSCRIPTION_FAILED,
                    error_message=f"whisper_exit_{result.returncode}",
                    audio_path=str(audio_path),
                    processing_time_seconds=elapsed,
                    extra_data={
                        "process": _safe_process_data(result),
                        "audio_validation": audio_validation,
                        **timing_data,
                        "transcription_completed_at": process_completed_at,
                        "transcript_parsing_status": "not_started",
                    },
                )

            parsing_started_at = _utc_timestamp()
            transcript = _normalize_transcript_text(_read_transcript_text(output_base, result))
            parsing_completed_at = _utc_timestamp()
            detected_language = (
                _detect_language(result.stdout, result.stderr)
                if effective_language == "auto"
                else effective_language
            )
            if not transcript:
                return self._failure(
                    status=WHISPER_STATUS_NO_USABLE_SPEECH,
                    error_message="no_usable_speech",
                    audio_path=str(audio_path),
                    processing_time_seconds=elapsed,
                    extra_data={
                        "audio_chunk": audio_chunk.to_dict() if audio_chunk else None,
                        "audio_validation": audio_validation,
                        "process": _safe_process_data(result),
                        "language_requested": requested_language,
                        "language_effective": effective_language,
                        "language": detected_language,
                        "model_english_only": model_english_only,
                        **timing_data,
                        "transcript_parsing_started_at": parsing_started_at,
                        "transcript_parsing_completed_at": parsing_completed_at,
                        "transcript_parsing_status": "empty",
                        "transcription_completed_at": parsing_completed_at,
                    },
                )

            return self._success(
                status=WHISPER_STATUS_TRANSCRIBED,
                text=transcript,
                confidence=1.0,
                audio_path=str(audio_path),
                processing_time_seconds=elapsed,
                extra_data={
                    "audio_chunk": audio_chunk.to_dict() if audio_chunk else None,
                    "audio_validation": audio_validation,
                    "process": _safe_process_data(result),
                    "language_requested": requested_language,
                    "language_effective": effective_language,
                    "language": detected_language,
                    "model_english_only": model_english_only,
                    **timing_data,
                    "transcript_parsing_started_at": parsing_started_at,
                    "transcript_parsing_completed_at": parsing_completed_at,
                    "transcript_parsing_status": "completed",
                    "transcription_completed_at": parsing_completed_at,
                },
            )

    def get_status(self) -> TranscriptionResult:
        binary_path = self._find_whisper_binary()
        model_exists = self.model_path.exists()
        status = "ready" if binary_path and model_exists else "unavailable"
        return TranscriptionResult(
            success=True,
            status=status,
            text="",
            confidence=1.0 if status == "ready" else 0.0,
            data={
                **self._base_data(),
                "whisper_binary_available": bool(binary_path),
                "whisper_binary_path": binary_path or "",
                "model_available": model_exists,
                "model_path": str(self.model_path),
                "language": self.language,
                "language_effective": _resolve_whisper_language(self.language, self.model_path),
                "timeout_seconds": self.timeout_seconds,
                "minimum_rms": self.minimum_rms,
            },
            metadata=self._metadata(),
        )

    def get_capabilities(self) -> TranscriptionResult:
        return TranscriptionResult(
            success=True,
            status="capabilities",
            text="",
            confidence=1.0,
            data={
                **self._base_data(),
                "supported_input": "WAV file or AudioChunk",
                "supported_modes": ["offline_whisper_wav_transcription"],
                "recommended_model": "ggml-tiny.en.bin",
                "confidence": "not_reported_by_whisper_cli",
                "language": "auto_or_configured",
                "language_resolution": "English-only GGML models resolve auto to en.",
                "minimum_rms": self.minimum_rms,
                "internet": "disabled",
                "wake_word": "disabled",
                "background_listening": "disabled",
                "tts": "disabled",
            },
            metadata=self._metadata(),
        )

    def health_check(self) -> TranscriptionResult:
        binary_path = self._find_whisper_binary()
        if not binary_path:
            return self._failure(
                status=WHISPER_STATUS_BINARY_MISSING,
                error_message="whisper_binary_missing",
                audio_path="",
                processing_time_seconds=0.0,
            )
        if not self.model_path.exists():
            return self._failure(
                status=WHISPER_STATUS_MODEL_MISSING,
                error_message="whisper_model_missing",
                audio_path="",
                processing_time_seconds=0.0,
                extra_data={"whisper_binary_path": binary_path},
            )
        return TranscriptionResult(
            success=True,
            status="healthy",
            text="",
            confidence=1.0,
            data={
                **self._base_data(),
                "whisper_binary_available": True,
                "whisper_binary_path": binary_path,
                "model_available": True,
                "model_path": str(self.model_path),
                "language": self.language,
                "language_effective": _resolve_whisper_language(self.language, self.model_path),
                "minimum_rms": self.minimum_rms,
            },
            metadata=self._metadata(),
        )

    def _find_whisper_binary(self) -> str:
        found = self.runner.which(self.whisper_command)
        return str(found or "")

    def _transcribe_command(
        self,
        binary_path: str,
        audio_path: Path,
        output_base: Path,
        language: str,
    ) -> List[str]:
        command = [
            binary_path,
            "-m",
            str(self.model_path),
            "-f",
            str(audio_path),
            "-otxt",
            "-of",
            str(output_base),
        ]
        if language:
            command.extend(["-l", language])
        return command

    def _success(
        self,
        status: str,
        text: str,
        confidence: float,
        audio_path: str,
        processing_time_seconds: float,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> TranscriptionResult:
        language = str(dict(extra_data or {}).get("language") or "")
        timing = WhisperTranscriptionMetadata(
            processing_time_seconds=processing_time_seconds,
            language=language,
            model_path=str(self.model_path),
            whisper_command=self.whisper_command,
            audio_path=audio_path,
        )
        return TranscriptionResult(
            success=True,
            status=status,
            text=text,
            confidence=confidence,
            data={
                **self._base_data(),
                **timing.to_dict(),
                **dict(extra_data or {}),
            },
            metadata=self._metadata(),
        )

    def _failure(
        self,
        status: str,
        error_message: str,
        audio_path: str,
        processing_time_seconds: float,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> TranscriptionResult:
        timing = WhisperTranscriptionMetadata(
            processing_time_seconds=processing_time_seconds,
            model_path=str(self.model_path),
            whisper_command=self.whisper_command,
            audio_path=audio_path,
        )
        return TranscriptionResult(
            success=False,
            status=status,
            text="",
            confidence=0.0,
            error_message=error_message,
            data={
                **self._base_data(),
                **timing.to_dict(),
                **dict(extra_data or {}),
            },
            metadata=self._metadata(),
        )

    def _base_data(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "stt": "offline_whisper",
            "speech_engine": "whisper.cpp",
            "speech_engine_access": "offline_local_process",
            "internet": "disabled",
            "wake_word": "disabled",
            "background_listening": "disabled",
            "tts": "disabled",
        }

    def _metadata(self) -> Dict[str, Any]:
        return {
            "safe": True,
            "source": self.source,
            "mock": False,
            "offline": True,
            "speech_engine_accessed": self.speech_engine_accessed,
            "audio_hardware_accessed": self.audio_hardware_accessed,
            "subprocess_shell": False,
        }


def _audio_chunk_wav_path(audio_chunk: AudioChunk) -> Optional[Path]:
    wav_path = dict(audio_chunk.metadata or {}).get("wav_path")
    if not wav_path:
        return None
    return Path(str(wav_path)).expanduser()


def _audio_chunk_transcription_timeout(audio_chunk: AudioChunk) -> Optional[float]:
    value = dict(audio_chunk.metadata or {}).get("transcription_timeout_seconds")
    if value is None:
        return None
    return _bounded_timeout(value)


def _write_audio_chunk_wav(audio_chunk: AudioChunk, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(audio_chunk.channels)
        wav_file.setsampwidth(audio_chunk.sample_width_bytes)
        wav_file.setframerate(audio_chunk.sample_rate_hz)
        wav_file.writeframes(audio_chunk.data)


def _validate_wav_audio(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "success": False,
            "error_message": "audio_file_missing",
            "path": str(path),
        }
    size = path.stat().st_size
    if size == 0:
        return {
            "success": False,
            "error_message": "audio_file_empty",
            "path": str(path),
            "byte_count": size,
        }
    try:
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            frame_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_data = wav_file.readframes(frames)
    except (wave.Error, EOFError, OSError) as error:
        return {
            "success": False,
            "error_message": f"invalid_wav:{error.__class__.__name__}",
            "path": str(path),
        }
    if frames <= 0:
        return {
            "success": False,
            "error_message": "audio_has_no_frames",
            "path": str(path),
        }
    try:
        signal = _pcm_signal_stats(frame_data, sample_width)
    except ValueError as error:
        return {
            "success": False,
            "error_message": str(error),
            "path": str(path),
            "byte_count": size,
        }
    return {
        "success": True,
        "path": str(path),
        "byte_count": size,
        "frames": frames,
        "sample_rate_hz": frame_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "pcm_encoding": (
            "signed_16_bit_little_endian"
            if sample_width == 2
            else f"pcm_{sample_width * 8}_bit"
        ),
        "file_size_greater_than_wav_header": size > 44,
        "duration_seconds": frames / frame_rate if frame_rate else 0.0,
        **signal,
    }


def _canonical_wav_error(audio: Dict[str, Any]) -> str:
    if int(audio.get("sample_rate_hz", 0)) != CANONICAL_SAMPLE_RATE_HZ:
        return "audio_sample_rate_must_be_16000_hz"
    if int(audio.get("channels", 0)) != CANONICAL_CHANNELS:
        return "audio_must_be_mono"
    if int(audio.get("sample_width_bytes", 0)) != CANONICAL_SAMPLE_WIDTH_BYTES:
        return "audio_sample_width_must_be_16_bit"
    if not bool(audio.get("file_size_greater_than_wav_header")):
        return "audio_has_no_pcm_payload"
    return ""


def analyze_wav_audio(path: str | Path) -> Dict[str, Any]:
    return _validate_wav_audio(Path(path).expanduser())


def _read_transcript_text(output_base: Path, process_result: SafeProcessResult) -> str:
    text_path = output_base.with_suffix(".txt")
    if text_path.exists():
        try:
            return text_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""
    return _extract_transcript_from_stdout(process_result.stdout)


def _extract_transcript_from_stdout(stdout: str) -> str:
    lines: List[str] = []
    for raw_line in str(stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith(
            (
                "whisper_",
                "system_info",
                "main:",
                "ggml_",
                "whisper_print",
                "detected language",
            )
        ):
            continue
        line = re.sub(r"^\[[^\]]+\]\s*", "", line).strip()
        if line:
            lines.append(line)
    return " ".join(lines).strip()


def _normalize_transcript_text(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    clean = re.sub(
        r"<\|\s*(?:blank[_\s-]*audio|no[_\s-]*speech|nospeech|silence)\s*\|>",
        " ",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"\[\s*(?:blank[_\s-]*audio|no[_\s-]*speech|nospeech|silence)\s*\]",
        " ",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"\(\s*(?:blank[_\s-]*audio|no[_\s-]*speech|nospeech|silence)\s*\)",
        " ",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"\s+", " ", clean).strip()
    if _is_no_speech_marker(clean):
        return ""
    return clean


def _detect_language(stdout: str, stderr: str) -> str:
    combined = f"{stdout}\n{stderr}"
    match = re.search(r"detected language:\s*([A-Za-z_-]+)", combined, flags=re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _resolve_whisper_language(language: str, model_path: Path) -> str:
    requested = str(language or DEFAULT_WHISPER_LANGUAGE).strip() or DEFAULT_WHISPER_LANGUAGE
    if requested.lower() == "auto" and _is_english_only_whisper_model(model_path):
        return "en"
    return requested


def _is_english_only_whisper_model(model_path: Path) -> bool:
    name = Path(model_path).name.lower()
    stem = name[:-4] if name.endswith(".bin") else name
    return bool(re.search(r"\.en(?:[._-]|$)", stem))


def _is_no_speech_marker(text: str) -> bool:
    marker = re.sub(r"[\s_\-]+", "", str(text or "").strip().lower())
    marker = marker.strip("[]()<>|")
    return marker in NO_SPEECH_MARKERS


def _safe_process_data(result: SafeProcessResult) -> Dict[str, Any]:
    return {
        "args": list(result.args),
        "command": " ".join(str(arg) for arg in result.args),
        "returncode": result.returncode,
        "stdout_preview": _bounded_text(result.stdout, limit=4000),
        "stderr_preview": _bounded_text(result.stderr, limit=4000),
        "timed_out": result.timed_out,
        "error_message": result.error_message,
        "metadata": dict(result.metadata or {}),
    }


def _bounded_text(text: str, limit: int = 500) -> str:
    return str(text or "")[:limit]


def _process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("timeout_seconds must be numeric")
    timeout = float(value)
    if not math.isfinite(timeout):
        raise ValueError("timeout_seconds must be finite")
    if timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    if timeout > MAX_WHISPER_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be <= {MAX_WHISPER_TIMEOUT_SECONDS}")
    return timeout


def _non_negative_float(value: Any, name: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _pcm_signal_stats(frame_data: bytes, sample_width: int) -> Dict[str, Any]:
    samples = list(_iter_pcm_samples(frame_data, sample_width))
    if not samples:
        return {
            "sample_count": 0,
            "peak_amplitude": 0,
            "rms_amplitude": 0.0,
        }
    peak = max(abs(sample) for sample in samples)
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return {
        "sample_count": len(samples),
        "peak_amplitude": int(peak),
        "rms_amplitude": round(math.sqrt(mean_square), 6),
    }


def _iter_pcm_samples(frame_data: bytes, sample_width: int):
    if sample_width not in (1, 2, 3, 4):
        raise ValueError(f"unsupported_sample_width:{sample_width}")
    for offset in range(0, len(frame_data) - sample_width + 1, sample_width):
        raw = frame_data[offset : offset + sample_width]
        if sample_width == 1:
            yield int(raw[0]) - 128
            continue
        yield int.from_bytes(raw, byteorder="little", signed=True)


def _elapsed(clock: Clock, start_time: float) -> float:
    return max(0.0, float(clock()) - float(start_time))
