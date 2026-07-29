import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Thread
import time
import wave

import pytest

from core import (
    AudioChunk,
    LinuxAlsaMicrophoneAdapter,
    LinuxWhisperSpeechToTextAdapter,
    SafeProcessResult,
    WHISPER_STATUS_AUDIO_BELOW_THRESHOLD,
    WHISPER_STATUS_AUDIO_SILENT,
    WHISPER_STATUS_BINARY_MISSING,
    WHISPER_STATUS_INVALID_AUDIO,
    WHISPER_STATUS_MODEL_MISSING,
    WHISPER_STATUS_NO_TRANSCRIPTION,
    WHISPER_STATUS_NO_USABLE_SPEECH,
    WHISPER_STATUS_TRANSCRIBED,
    WHISPER_STATUS_TRANSCRIPTION_FAILED,
    WHISPER_STATUS_TRANSCRIPTION_TIMEOUT,
    WhisperSubprocessRunner,
)
from scripts import manual_verify_linux_whisper_stt as manual_whisper


ARECORD_DEVICES = """**** List of CAPTURE Hardware Devices ****
card 1: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
"""


class FakeWhisperRunner:
    def __init__(
        self,
        available=True,
        returncode=0,
        stdout="detected language: English\n[00:00:00.000 --> 00:00:01.000] hello ares",
        stderr="",
        timed_out=False,
        transcript_text="hello ares",
        write_transcript=True,
    ):
        self.available = available
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.transcript_text = transcript_text
        self.write_transcript = write_transcript
        self.calls = []

    def which(self, executable):
        return "/usr/local/bin/whisper-cli" if self.available else None

    def run(self, args, timeout_seconds):
        safe_args = list(args)
        self.calls.append({"args": safe_args, "timeout_seconds": timeout_seconds})
        if self.write_transcript and "-of" in safe_args and self.returncode == 0 and not self.timed_out:
            output_base = safe_args[safe_args.index("-of") + 1]
            with open(f"{output_base}.txt", "w", encoding="utf-8") as handle:
                handle.write(self.transcript_text)
        return SafeProcessResult(
            args=safe_args,
            returncode=-1 if self.timed_out else self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
            error_message="process_timeout" if self.timed_out else "",
        )


class FakeAlsaRunner:
    def __init__(self):
        self.calls = []

    def which(self, executable):
        return "/usr/bin/arecord"

    def run(self, args, timeout_seconds):
        safe_args = list(args)
        self.calls.append({"args": safe_args, "timeout_seconds": timeout_seconds})
        if safe_args[-1] == "-l":
            return SafeProcessResult(args=safe_args, returncode=0, stdout=ARECORD_DEVICES)
        write_valid_wav(safe_args[-1])
        return SafeProcessResult(args=safe_args, returncode=0)


def write_valid_wav(path, frames=b"\x00\x01" * 160):
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(frames)


def pcm16_frames(amplitude, count=160):
    value = int(amplitude)
    return b"".join(value.to_bytes(2, byteorder="little", signed=True) for _ in range(count))


def create_model(tmp_path):
    model = tmp_path / "ggml-tiny.en.bin"
    model.write_bytes(b"fake model")
    return model


def create_adapter(tmp_path, runner=None, clock_values=None, minimum_rms=0.0):
    model = create_model(tmp_path)
    values = list(clock_values or [10.0, 10.25])

    def clock():
        return values.pop(0) if values else 10.25

    return LinuxWhisperSpeechToTextAdapter(
        model_path=model,
        runner=runner or FakeWhisperRunner(),
        clock=clock,
        minimum_rms=minimum_rms,
    )


def test_linux_whisper_health_check_passes_with_binary_and_model(tmp_path):
    adapter = create_adapter(tmp_path)

    result = adapter.health_check()

    assert result.success is True
    assert result.status == "healthy"
    assert result.data["model_available"] is True
    assert result.data["whisper_binary_available"] is True
    assert result.data["internet"] == "disabled"


def test_linux_whisper_missing_binary_fails_safely(tmp_path):
    adapter = create_adapter(tmp_path, runner=FakeWhisperRunner(available=False))

    result = adapter.health_check()

    assert result.success is False
    assert result.status == WHISPER_STATUS_BINARY_MISSING
    assert result.error_message == "whisper_binary_missing"


def test_linux_whisper_missing_model_fails_safely(tmp_path):
    missing_model = tmp_path / "missing.bin"
    adapter = LinuxWhisperSpeechToTextAdapter(
        model_path=missing_model,
        runner=FakeWhisperRunner(),
    )

    result = adapter.health_check()

    assert result.success is False
    assert result.status == WHISPER_STATUS_MODEL_MISSING
    assert result.error_message == "whisper_model_missing"


def test_linux_whisper_transcribes_wav_file_with_metadata(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeWhisperRunner(transcript_text="hello from raspberry pi")
    adapter = create_adapter(tmp_path, runner=runner, clock_values=[1.0, 1.42])

    result = adapter.transcribe_wav(wav_path, language="en", timeout_seconds=33)

    assert result.success is True
    assert result.status == WHISPER_STATUS_TRANSCRIBED
    assert result.text == "hello from raspberry pi"
    assert result.data["processing_time_seconds"] == 0.42
    assert result.data["language"] == "en"
    assert result.data["audio_validation"]["success"] is True
    assert result.data["audio_validation"]["peak_amplitude"] > 0
    assert result.data["audio_validation"]["rms_amplitude"] > 0
    assert result.metadata["subprocess_shell"] is False
    assert result.metadata["speech_engine_accessed"] is True
    assert adapter.transcription_count == 1
    command = runner.calls[0]["args"]
    assert isinstance(command, list)
    assert "-m" in command
    assert "-f" in command
    assert "-otxt" in command
    assert "-of" in command
    assert runner.calls[0]["timeout_seconds"] == 33
    assert "whisper-cli" in result.data["process"]["command"]
    assert result.data["wav_closed_before_inference"] is True
    assert result.data["transcription_timeout_seconds"] == 33
    assert result.data["transcription_started_at"]
    assert result.data["whisper_process_started_at"]
    assert result.data["whisper_process_completed_at"]
    assert result.data["transcript_parsing_status"] == "completed"


def test_linux_whisper_default_timeout_is_bounded_for_raspberry_pi(tmp_path):
    adapter = create_adapter(tmp_path)

    assert adapter.timeout_seconds == 15.0


def test_audio_chunk_request_timeout_overrides_long_adapter_timeout(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeWhisperRunner(transcript_text="bounded command")
    adapter = LinuxWhisperSpeechToTextAdapter(
        model_path=create_model(tmp_path),
        runner=runner,
        timeout_seconds=300,
    )
    chunk = AudioChunk(
        data=b"\x00\x01" * 160,
        sample_rate_hz=16000,
        channels=1,
        sample_width_bytes=2,
        source="active_command",
        metadata={
            "wav_path": str(wav_path),
            "transcription_timeout_seconds": 30.0,
        },
    )

    result = adapter.transcribe(chunk)

    assert result.success is True
    assert runner.calls[0]["timeout_seconds"] == 30.0
    assert result.data["transcription_timeout_seconds"] == 30.0


def test_linux_whisper_auto_language_resolves_to_en_for_english_only_model(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeWhisperRunner(transcript_text="english model text")
    adapter = create_adapter(tmp_path, runner=runner)

    result = adapter.transcribe_wav(wav_path)

    assert result.success is True
    assert result.text == "english model text"
    assert result.data["language_requested"] == "auto"
    assert result.data["language_effective"] == "en"
    assert result.data["language"] == "en"
    assert result.data["model_english_only"] is True
    command = runner.calls[0]["args"]
    assert command[command.index("-l") + 1] == "en"


def test_linux_whisper_parses_stdout_when_text_file_is_missing(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeWhisperRunner(
        write_transcript=False,
        stdout="detected language: English\n[00:00:00.000 --> 00:00:01.000] stdout text",
    )
    adapter = create_adapter(tmp_path, runner=runner)

    result = adapter.transcribe_wav(wav_path)

    assert result.success is True
    assert result.status == WHISPER_STATUS_TRANSCRIBED
    assert result.text == "stdout text"
    assert result.data["language"] == "en"


def test_linux_whisper_no_transcription_is_safe(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeWhisperRunner(write_transcript=True, transcript_text="", stdout="")
    adapter = create_adapter(tmp_path, runner=runner)

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_NO_USABLE_SPEECH
    assert result.text == ""
    assert result.confidence == 0.0


def test_linux_whisper_blank_audio_marker_is_not_success(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeWhisperRunner(write_transcript=True, transcript_text="[BLANK_AUDIO]")
    adapter = create_adapter(tmp_path, runner=runner)

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_NO_USABLE_SPEECH
    assert result.error_message == "no_usable_speech"
    assert result.text == ""


def test_linux_whisper_common_no_speech_markers_are_not_success(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    markers = [
        "[BLANK_AUDIO]",
        "[ blank audio ]",
        "<|nospeech|>",
        "<|no_speech|>",
        "(no speech)",
        "SILENCE",
    ]

    for index, marker in enumerate(markers):
        runner = FakeWhisperRunner(write_transcript=True, transcript_text=marker)
        adapter = create_adapter(tmp_path, runner=runner)

        result = adapter.transcribe_wav(wav_path)

        assert result.success is False
        assert result.status == WHISPER_STATUS_NO_USABLE_SPEECH
        assert result.text == ""


def test_linux_whisper_transcribe_missing_binary_fails_before_running(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeWhisperRunner(available=False)
    adapter = create_adapter(tmp_path, runner=runner)

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_BINARY_MISSING
    assert result.error_message == "whisper_binary_missing"
    assert runner.calls == []


def test_linux_whisper_transcribe_missing_model_fails_before_running(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    missing_model = tmp_path / "missing" / "ggml-tiny.en.bin"
    runner = FakeWhisperRunner()
    adapter = LinuxWhisperSpeechToTextAdapter(
        model_path=missing_model,
        runner=runner,
    )

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_MODEL_MISSING
    assert result.error_message == "whisper_model_missing"
    assert runner.calls == []


def test_linux_whisper_silent_wav_fails_before_whisper_runs(tmp_path):
    wav_path = tmp_path / "silent.wav"
    write_valid_wav(wav_path, frames=pcm16_frames(0))
    runner = FakeWhisperRunner(transcript_text="should not run")
    adapter = create_adapter(tmp_path, runner=runner)

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_AUDIO_SILENT
    assert result.data["audio_validation"]["peak_amplitude"] == 0
    assert runner.calls == []


def test_linux_whisper_near_silent_wav_fails_with_configured_threshold(tmp_path):
    wav_path = tmp_path / "near_silent.wav"
    write_valid_wav(wav_path, frames=pcm16_frames(1))
    runner = FakeWhisperRunner(transcript_text="should not run")
    adapter = create_adapter(tmp_path, runner=runner, minimum_rms=50.0)

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_AUDIO_BELOW_THRESHOLD
    assert result.data["audio_validation"]["rms_amplitude"] < 50.0
    assert result.data["minimum_rms"] == 50.0
    assert runner.calls == []


def test_linux_whisper_invalid_audio_fails_safely(tmp_path):
    wav_path = tmp_path / "bad.wav"
    wav_path.write_bytes(b"not wav")
    adapter = create_adapter(tmp_path)

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_INVALID_AUDIO
    assert result.error_message.startswith("invalid_wav")
    assert result.metadata["speech_engine_accessed"] is False


def test_linux_whisper_header_only_wav_fails_before_inference(tmp_path):
    wav_path = tmp_path / "header-only.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
    runner = FakeWhisperRunner()
    adapter = create_adapter(tmp_path, runner=runner)

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_INVALID_AUDIO
    assert result.error_message == "audio_has_no_frames"
    assert runner.calls == []


def test_linux_whisper_rejects_noncanonical_wav_before_inference(tmp_path):
    wav_path = tmp_path / "noncanonical.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(44100)
        wav_file.writeframes(pcm16_frames(1000, count=4410))
    runner = FakeWhisperRunner()
    adapter = create_adapter(tmp_path, runner=runner)

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_INVALID_AUDIO
    assert result.error_message == "audio_sample_rate_must_be_16000_hz"
    assert result.data["audio_validation"]["sample_rate_hz"] == 44100
    assert runner.calls == []


def test_linux_whisper_missing_audio_file_fails_safely(tmp_path):
    adapter = create_adapter(tmp_path)

    result = adapter.transcribe_wav(tmp_path / "missing.wav")

    assert result.success is False
    assert result.status == WHISPER_STATUS_INVALID_AUDIO
    assert result.error_message == "audio_file_missing"


def test_linux_whisper_timeout_fails_safely(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    adapter = create_adapter(tmp_path, runner=FakeWhisperRunner(timed_out=True))

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_TRANSCRIPTION_TIMEOUT
    assert result.error_message == "whisper_transcription_timeout"
    assert result.data["transcript_parsing_status"] == "not_started"


def test_whisper_process_timeout_terminates_and_reaps_child():
    statuses = []
    runner = WhisperSubprocessRunner(
        termination_grace_seconds=0.2,
        hard_cleanup_deadline_seconds=0.4,
        status_callback=statuses.append,
    )

    result = runner.run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        timeout_seconds=0.1,
    )

    assert result.timed_out is True
    assert result.error_message == "process_timeout"
    assert result.metadata["pid"] > 0
    assert result.metadata["pgid"] > 0
    assert result.metadata["terminated"] or result.metadata["killed"]
    assert result.metadata["reaped"] is True
    assert result.metadata["cleanup_completed"] is True
    assert result.metadata["output_handles_closed"] is True
    assert result.metadata["typed_status"] == WHISPER_STATUS_TRANSCRIPTION_TIMEOUT
    assert runner.active_pid == 0
    assert any(line.startswith("Whisper process started: pid=") for line in statuses)
    assert "Whisper transcription timed out after 0.1 seconds" in statuses
    assert "Terminating Whisper process group" in statuses
    assert "Whisper process cleanup: completed" in statuses


def test_whisper_process_is_killed_and_reaped_on_keyboard_interrupt():
    class InterruptedProcess:
        pid = 12345

        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.killed = False
            self.calls = 0

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.killed = True
            self.returncode = -9

        def poll(self):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            return self.returncode

    process = InterruptedProcess()
    handles = {}

    def process_factory(*args, **kwargs):
        handles["stdout"] = kwargs["stdout"]
        handles["stderr"] = kwargs["stderr"]
        return process

    runner = WhisperSubprocessRunner(
        process_factory=process_factory,
        termination_grace_seconds=0.1,
        hard_cleanup_deadline_seconds=0.2,
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run(["whisper-cli"], timeout_seconds=1.0)

    assert process.terminated or process.killed
    assert process.poll() is not None
    assert handles["stdout"].closed is True
    assert handles["stderr"].closed is True
    assert runner.active_pid == 0


def test_whisper_runner_normal_completion_captures_output_and_closes_handles():
    statuses = []
    runner = WhisperSubprocessRunner(status_callback=statuses.append)

    result = runner.run(
        [
            sys.executable,
            "-c",
            "import sys; print('normal stdout'); print('normal stderr', file=sys.stderr)",
        ],
        timeout_seconds=2.0,
    )

    assert result.returncode == 0
    assert result.timed_out is False
    assert "normal stdout" in result.stdout
    assert "normal stderr" in result.stderr
    assert result.metadata["reaped"] is True
    assert result.metadata["cleanup_completed"] is True
    assert result.metadata["output_handles_closed"] is True
    assert runner.active_pid == 0
    assert any(line.startswith("Whisper completed: exit=0") for line in statuses)


def test_whisper_runner_nonzero_exit_is_bounded_and_reaped():
    runner = WhisperSubprocessRunner()

    result = runner.run(
        [
            sys.executable,
            "-c",
            "import sys; print('decode failed', file=sys.stderr); raise SystemExit(7)",
        ],
        timeout_seconds=2.0,
    )

    assert result.returncode == 7
    assert result.timed_out is False
    assert "decode failed" in result.stderr
    assert result.metadata["reaped"] is True
    assert result.metadata["output_handles_closed"] is True
    assert runner.active_pid == 0


def test_whisper_runner_stdout_and_stderr_saturation_cannot_deadlock_parent():
    runner = WhisperSubprocessRunner()

    result = runner.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "os.write(1, b'o' * 524288); "
                "os.write(2, b'e' * 524288)"
            ),
        ],
        timeout_seconds=2.0,
    )

    assert result.returncode == 0
    assert len(result.stdout) == 524288
    assert len(result.stderr) == 524288
    assert result.metadata["stdout_transport"] == "bounded_temporary_file"
    assert result.metadata["stderr_transport"] == "bounded_temporary_file"
    assert result.metadata["stdout_truncated"] is False
    assert result.metadata["stderr_truncated"] is False
    assert result.metadata["reaped"] is True
    assert runner.active_pid == 0


def test_whisper_runner_ignoring_sigterm_is_group_killed_and_reaped():
    terminate_signal = int(getattr(signal, "SIGTERM", 15))
    kill_signal = int(getattr(signal, "SIGKILL", 9))

    class IgnoringProcess:
        pid = 42001

        def __init__(self):
            self.returncode = None
            self.wait_calls = []

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fake-whisper", timeout)
            return self.returncode

        def poll(self):
            return self.returncode

    process = IgnoringProcess()
    group = {"alive": True, "signals": []}
    launch = {}

    def group_signaler(pgid, signal_number):
        if signal_number == 0:
            if group["alive"]:
                return
            raise ProcessLookupError
        group["signals"].append((pgid, signal_number))
        if signal_number == terminate_signal:
            return
        if signal_number == kill_signal:
            process.returncode = -kill_signal
            group["alive"] = False

    def process_factory(*args, **kwargs):
        launch.update(kwargs)
        return process

    runner = WhisperSubprocessRunner(
        process_factory=process_factory,
        termination_grace_seconds=0.1,
        hard_cleanup_deadline_seconds=0.2,
        process_group_getter=lambda _pid: 42001,
        process_group_signaler=group_signaler,
    )

    result = runner.run(["fake-whisper"], timeout_seconds=0.01)

    assert launch["start_new_session"] is True
    assert result.timed_out is True
    assert result.metadata["process_group_started"] is True
    assert result.metadata["pgid"] == 42001
    assert result.metadata["terminated"] is True
    assert result.metadata["killed"] is True
    assert result.metadata["reaped"] is True
    assert result.metadata["cleanup_completed"] is True
    assert group["signals"] == [
        (42001, terminate_signal),
        (42001, kill_signal),
    ]
    assert runner.active_pid == 0


def test_whisper_runner_hard_cleanup_deadline_returns_if_kill_cannot_reap():
    class UnreapableProcess:
        pid = 43001
        returncode = None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("fake-unreapable-whisper", timeout)

        def poll(self):
            return None

    class AdvancingClock:
        def __init__(self):
            self.value = 100.0

        def __call__(self):
            self.value += 0.025
            return self.value

    signals = []

    def group_signaler(_pgid, signal_number):
        if signal_number != 0:
            signals.append(signal_number)

    statuses = []
    runner = WhisperSubprocessRunner(
        process_factory=lambda *args, **kwargs: UnreapableProcess(),
        termination_grace_seconds=0.1,
        hard_cleanup_deadline_seconds=0.2,
        clock=AdvancingClock(),
        status_callback=statuses.append,
        process_group_getter=lambda _pid: 43001,
        process_group_signaler=group_signaler,
    )

    result = runner.run(["fake-unreapable-whisper"], timeout_seconds=0.01)

    assert result.timed_out is True
    assert result.metadata["terminated"] is True
    assert result.metadata["killed"] is True
    assert result.metadata["reaped"] is False
    assert result.metadata["cleanup_completed"] is False
    assert result.metadata["output_handles_closed"] is True
    assert runner.active_pid == 0
    assert int(getattr(signal, "SIGTERM", 15)) in signals
    assert int(getattr(signal, "SIGKILL", 9)) in signals
    assert "Whisper process cleanup: incomplete" in statuses


def test_whisper_cleanup_completion_is_reported_only_after_output_handles_close():
    class TimeoutProcess:
        pid = 44001

        def __init__(self):
            self.returncode = None

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fake-whisper", timeout)
            return self.returncode

        def poll(self):
            return self.returncode

    process = TimeoutProcess()
    group_alive = {"value": True}
    handles = {}
    cleanup_handle_states = []

    def process_factory(*args, **kwargs):
        handles["stdout"] = kwargs["stdout"]
        handles["stderr"] = kwargs["stderr"]
        return process

    def group_signaler(_pgid, signal_number):
        if signal_number == 0:
            if group_alive["value"]:
                return
            raise ProcessLookupError
        process.returncode = -int(signal_number)
        group_alive["value"] = False

    def status(message):
        if message == "Whisper process cleanup: completed":
            cleanup_handle_states.append(
                (handles["stdout"].closed, handles["stderr"].closed)
            )

    runner = WhisperSubprocessRunner(
        process_factory=process_factory,
        termination_grace_seconds=0.1,
        hard_cleanup_deadline_seconds=0.2,
        status_callback=status,
        process_group_getter=lambda _pid: 44001,
        process_group_signaler=group_signaler,
    )

    result = runner.run(["fake-whisper"], timeout_seconds=0.01)

    assert result.timed_out is True
    assert result.metadata["cleanup_completed"] is True
    assert cleanup_handle_states == [(True, True)]


def test_whisper_runner_active_cancellation_is_bounded_and_retryable():
    runner = WhisperSubprocessRunner(
        termination_grace_seconds=0.1,
        hard_cleanup_deadline_seconds=0.3,
    )
    completed = {}

    def execute():
        completed["result"] = runner.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=10.0,
        )

    worker = Thread(target=execute)
    worker.start()
    deadline = time.monotonic() + 2.0
    while runner.active_pid == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    active_pid = runner.active_pid

    assert active_pid > 0
    assert runner.cancel_current("owner_cancellation") is True
    worker.join(timeout=2.0)
    assert worker.is_alive() is False
    result = completed["result"]
    assert result.error_message == "process_cancelled"
    assert result.metadata["cancelled"] is True
    assert result.metadata["cancel_reason"] == "owner_cancellation"
    assert result.metadata["reaped"] is True
    assert result.metadata["cleanup_completed"] is True
    assert result.metadata["output_handles_closed"] is True
    assert runner.active_pid == 0
    assert runner.cancel_current() is False

    retry = runner.run(
        [sys.executable, "-c", "print('retry works')"],
        timeout_seconds=2.0,
    )
    assert retry.returncode == 0
    assert "retry works" in retry.stdout
    assert retry.metadata["reaped"] is True
    assert runner.active_pid == 0


def test_linux_whisper_adapter_delegates_active_process_cancellation(tmp_path):
    class CancellableRunner(FakeWhisperRunner):
        def __init__(self):
            super().__init__()
            self.cancel_reasons = []

        def cancel_current(self, reason):
            self.cancel_reasons.append(reason)
            return True

    runner = CancellableRunner()
    adapter = create_adapter(tmp_path, runner=runner)

    assert adapter.cancel_current("runtime_shutdown") is True
    assert runner.cancel_reasons == ["runtime_shutdown"]


def test_linux_whisper_input_wav_is_closed_before_process_launch(tmp_path):
    wav_path = tmp_path / "closed-before-inference.wav"
    write_valid_wav(wav_path)

    class ClosedFileCheckingRunner(FakeWhisperRunner):
        def run(self, args, timeout_seconds):
            source = Path(args[args.index("-f") + 1])
            moved = source.with_suffix(".moved")
            source.replace(moved)
            moved.replace(source)
            return super().run(args, timeout_seconds)

    runner = ClosedFileCheckingRunner(transcript_text="goodbye ares")
    adapter = create_adapter(tmp_path, runner=runner)

    result = adapter.transcribe_wav(wav_path)

    assert result.success is True
    assert result.text == "goodbye ares"
    assert result.data["wav_closed_before_inference"] is True


def test_linux_whisper_malformed_transcript_file_is_rejected(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)

    class MalformedTranscriptRunner(FakeWhisperRunner):
        def run(self, args, timeout_seconds):
            safe_args = list(args)
            self.calls.append({"args": safe_args, "timeout_seconds": timeout_seconds})
            output_base = safe_args[safe_args.index("-of") + 1]
            Path(f"{output_base}.txt").write_bytes(b"\xff\xfe\x00")
            return SafeProcessResult(args=safe_args, returncode=0)

    adapter = create_adapter(tmp_path, runner=MalformedTranscriptRunner())

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_NO_USABLE_SPEECH
    assert result.data["transcript_parsing_status"] == "empty"


def test_linux_whisper_nonzero_process_exit_fails_safely(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    adapter = create_adapter(
        tmp_path,
        runner=FakeWhisperRunner(returncode=2, stderr="model decode failed"),
    )

    result = adapter.transcribe_wav(wav_path)

    assert result.success is False
    assert result.status == WHISPER_STATUS_TRANSCRIPTION_FAILED
    assert result.error_message == "whisper_exit_2"
    assert "model decode failed" in result.data["process"]["stderr_preview"]


def test_linux_whisper_transcribes_audio_chunk_by_writing_temporary_wav(tmp_path):
    adapter = create_adapter(tmp_path, runner=FakeWhisperRunner(transcript_text="chunk text"))
    chunk = AudioChunk(
        data=b"\x00\x01" * 160,
        sample_rate_hz=16000,
        channels=1,
        sample_width_bytes=2,
        source="test_chunk",
    )

    result = adapter.transcribe(chunk)

    assert result.success is True
    assert result.status == WHISPER_STATUS_TRANSCRIBED
    assert result.text == "chunk text"
    assert result.data["audio_chunk"]["source"] == "test_chunk"


def test_linux_whisper_transcribes_audio_chunk_with_existing_wav_path(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    adapter = create_adapter(tmp_path, runner=FakeWhisperRunner(transcript_text="metadata path"))
    chunk = AudioChunk(
        data=b"\x00\x01" * 160,
        source="alsa",
        metadata={"wav_path": str(wav_path)},
    )

    result = adapter.transcribe(chunk)

    assert result.success is True
    assert result.text == "metadata path"
    assert result.data["audio_path"] == str(wav_path)


def test_linux_whisper_empty_audio_chunk_returns_safe_empty_result(tmp_path):
    adapter = create_adapter(tmp_path)

    result = adapter.transcribe(AudioChunk(data=b"", source="empty"))

    assert result.success is True
    assert result.status == "empty_audio"
    assert result.text == ""
    assert result.data["audio_chunk"]["byte_count"] == 0


def test_linux_whisper_status_and_capabilities_are_structured(tmp_path):
    adapter = create_adapter(tmp_path)

    status = adapter.get_status()
    capabilities = adapter.get_capabilities()

    assert status.success is True
    assert status.status == "ready"
    assert status.data["model_available"] is True
    assert status.data["whisper_binary_available"] is True
    assert capabilities.success is True
    assert capabilities.data["supported_input"] == "WAV file or AudioChunk"
    assert capabilities.data["recommended_model"] == "ggml-tiny.en.bin"
    assert capabilities.data["wake_word"] == "disabled"
    assert capabilities.data["tts"] == "disabled"


def test_raspberry_pi_integration_records_wav_then_transcribes_with_mocks(tmp_path):
    alsa_runner = FakeAlsaRunner()
    whisper_runner = FakeWhisperRunner(transcript_text="raspberry pi test")
    model = create_model(tmp_path)
    wav_path = tmp_path / "pi_sample.wav"
    microphone = LinuxAlsaMicrophoneAdapter(runner=alsa_runner, record_seconds=1)
    stt = LinuxWhisperSpeechToTextAdapter(model_path=model, runner=whisper_runner)

    record = microphone.record_wav(wav_path, seconds=1)
    transcription = stt.transcribe_wav(wav_path)

    assert record.success is True
    assert wav_path.exists()
    assert transcription.success is True
    assert transcription.text == "raspberry pi test"
    capture_targets = [
        call["args"][-1]
        for call in alsa_runner.calls
        if call["args"][-1] != "-l"
    ]
    assert len(capture_targets) == 1
    assert ".raw." in capture_targets[0]
    assert record.data["final_whisper_input_path"] == str(wav_path)
    assert whisper_runner.calls[0]["args"][whisper_runner.calls[0]["args"].index("-f") + 1] == str(wav_path)


def test_manual_linux_whisper_script_records_and_transcribes_with_mocks(tmp_path):
    outputs = []
    alsa_runner = FakeAlsaRunner()
    whisper_runner = FakeWhisperRunner(transcript_text="manual recognized text")
    model = create_model(tmp_path)
    wav_path = tmp_path / "manual.wav"

    def microphone_factory(**kwargs):
        return LinuxAlsaMicrophoneAdapter(runner=alsa_runner, **kwargs)

    def stt_factory(**kwargs):
        return LinuxWhisperSpeechToTextAdapter(runner=whisper_runner, **kwargs)

    playback_runner = FakePlaybackRunner()

    exit_code = manual_whisper.run_manual_verification(
        argv=[
            "--record",
            "--model",
            str(model),
            "--seconds",
            "1",
            "--output",
            str(wav_path),
        ],
        output_func=outputs.append,
        microphone_factory=microphone_factory,
        stt_factory=stt_factory,
        runner=playback_runner,
    )

    assert exit_code == 0
    assert wav_path.exists()
    assert any("Recognized text: manual recognized text" in line for line in outputs)
    assert any("Processing time:" in line for line in outputs)
    assert any("RMS amplitude:" in line for line in outputs)
    assert playback_runner.calls == []


def test_manual_linux_whisper_script_does_not_record_without_flag(tmp_path):
    outputs = []
    alsa_runner = FakeAlsaRunner()
    whisper_runner = FakeWhisperRunner(transcript_text="ignored")
    model = create_model(tmp_path)

    def microphone_factory(**kwargs):
        return LinuxAlsaMicrophoneAdapter(runner=alsa_runner, **kwargs)

    def stt_factory(**kwargs):
        return LinuxWhisperSpeechToTextAdapter(runner=whisper_runner, **kwargs)

    exit_code = manual_whisper.run_manual_verification(
        argv=["--model", str(model)],
        output_func=outputs.append,
        microphone_factory=microphone_factory,
        stt_factory=stt_factory,
    )

    assert exit_code == 0
    assert any("No recording requested" in line for line in outputs)
    assert all(call["args"][-1] == "-l" for call in alsa_runner.calls)
    assert whisper_runner.calls == []


class FakePlaybackRunner:
    def __init__(self, available=True, returncode=0):
        self.available = available
        self.returncode = returncode
        self.calls = []

    def which(self, executable):
        return "/usr/bin/aplay" if self.available else None

    def run(self, args, timeout_seconds):
        safe_args = list(args)
        self.calls.append({"args": safe_args, "timeout_seconds": timeout_seconds})
        return SafeProcessResult(args=safe_args, returncode=self.returncode)


def test_manual_linux_whisper_script_playback_requires_explicit_flag(tmp_path):
    outputs = []
    alsa_runner = FakeAlsaRunner()
    whisper_runner = FakeWhisperRunner(transcript_text="manual recognized text")
    playback_runner = FakePlaybackRunner()
    model = create_model(tmp_path)
    wav_path = tmp_path / "manual.wav"

    def microphone_factory(**kwargs):
        return LinuxAlsaMicrophoneAdapter(runner=alsa_runner, **kwargs)

    def stt_factory(**kwargs):
        return LinuxWhisperSpeechToTextAdapter(runner=whisper_runner, **kwargs)

    exit_code = manual_whisper.run_manual_verification(
        argv=[
            "--record",
            "--playback",
            "--model",
            str(model),
            "--seconds",
            "1",
            "--output",
            str(wav_path),
        ],
        output_func=outputs.append,
        microphone_factory=microphone_factory,
        stt_factory=stt_factory,
        runner=playback_runner,
    )

    assert exit_code == 0
    assert playback_runner.calls
    assert playback_runner.calls[0]["args"] == ["/usr/bin/aplay", str(wav_path)]
    assert any("Playback command:" in line for line in outputs)
