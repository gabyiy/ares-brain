from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import wave

from scripts import manual_diagnose_active_transcription as manual


def _write_valid_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = b"".join(
        int(1200 if index % 2 == 0 else -1200).to_bytes(
            2,
            "little",
            signed=True,
        )
        for index in range(1600)
    )
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(samples)


class FakeMicrophone:
    def __init__(
        self,
        *,
        capture_success: bool = True,
        start_success: bool = True,
        **kwargs,
    ) -> None:
        self.capture_success = capture_success
        self.start_success = start_success
        self.constructor_kwargs = dict(kwargs)
        self.calls: list[object] = []
        self.capture_path: Path | None = None

    def start(self):
        self.calls.append("start")
        return SimpleNamespace(
            success=self.start_success,
            status="started" if self.start_success else "device_error",
            error_message="" if self.start_success else "microphone_unavailable",
        )

    def record_until_silence(self, output_path, **kwargs):
        self.calls.append(("record_until_silence", Path(output_path), dict(kwargs)))
        self.capture_path = Path(output_path).with_name("finalized-command.wav")
        _write_valid_wav(self.capture_path)
        return SimpleNamespace(
            success=self.capture_success,
            status=(
                "completed_after_silence"
                if self.capture_success
                else "no_speech_timeout"
            ),
            error_message=(
                "" if self.capture_success else "speech_not_detected_before_timeout"
            ),
            wav_path=str(self.capture_path),
            final_whisper_input_path=(
                str(self.capture_path) if self.capture_success else ""
            ),
            normalized_wav_path=(
                str(self.capture_path) if self.capture_success else ""
            ),
            raw_wav_path="",
            assembled_wav_path="",
        )

    def cancel_current(self):
        self.calls.append("cancel_current")

    def stop(self):
        self.calls.append("stop")
        return SimpleNamespace(success=True, status="stopped")


class FakeRunner:
    def __init__(self, **kwargs) -> None:
        self.constructor_kwargs = dict(kwargs)


class FakeStt:
    def __init__(
        self,
        *,
        result,
        **kwargs,
    ) -> None:
        self.result = result
        self.constructor_kwargs = dict(kwargs)
        self.calls: list[object] = []
        self.cancel_count = 0

    def transcribe_wav(self, wav_path, language, timeout_seconds):
        path = Path(wav_path)
        assert path.exists()
        self.calls.append((path, language, timeout_seconds))
        return self.result

    def cancel_current(self):
        self.cancel_count += 1


def _transcription_result(
    *,
    success: bool = True,
    status: str = "transcribed",
    text: str = "goodbye ares",
    timed_out: bool = False,
    terminated: bool = False,
    killed: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        success=success,
        status=status,
        text=text,
        error_message="" if success else "whisper_transcription_timeout",
        processing_time_seconds=0.42,
        data={
            "process": {
                "args": [
                    "/repo/external/whisper-cli",
                    "-m",
                    "/repo/models/base.en.bin",
                    "-f",
                    "/tmp/finalized-command.wav",
                ],
                "returncode": -9 if killed else 0,
                "timed_out": timed_out,
                "metadata": {
                    "pid": 1234,
                    "pgid": 1234,
                    "process_group_started": True,
                    "elapsed_seconds": 15.02 if timed_out else 0.42,
                    "terminated": terminated,
                    "killed": killed,
                    "reaped": True,
                    "handles_closed": True,
                    "cleanup_completed": True,
                },
            }
        },
    )


def _run(
    tmp_path: Path,
    *,
    result=None,
    capture_success: bool = True,
    retain_audio: bool = False,
):
    outputs: list[str] = []
    microphones: list[FakeMicrophone] = []
    runners: list[FakeRunner] = []
    stt_instances: list[FakeStt] = []

    def microphone_factory(**kwargs):
        microphone = FakeMicrophone(
            capture_success=capture_success,
            **kwargs,
        )
        microphones.append(microphone)
        return microphone

    def runner_factory(**kwargs):
        runner = FakeRunner(**kwargs)
        runners.append(runner)
        return runner

    def stt_factory(**kwargs):
        stt = FakeStt(
            result=result or _transcription_result(),
            **kwargs,
        )
        stt_instances.append(stt)
        return stt

    args = [
        "--diagnostic-active-transcription",
        "--microphone-device",
        "plughw:2,0",
        "--speaker-device",
        "plughw:CARD=Device,DEV=0",
        "--whisper-command",
        "external/whisper.cpp/build/bin/whisper-cli",
        "--whisper-model",
        "models/whisper/ggml-base.en.bin",
        "--transcription-timeout",
        "15",
        "--termination-grace-seconds",
        "1",
        "--hard-cleanup-deadline-seconds",
        "3",
        "--output",
        str(tmp_path / "command.wav"),
    ]
    if retain_audio:
        args.append("--retain-audio")

    exit_code = manual.run_active_transcription_diagnostic(
        args,
        output_func=outputs.append,
        microphone_factory=microphone_factory,
        stt_factory=stt_factory,
        runner_factory=runner_factory,
    )
    return exit_code, outputs, microphones, runners, stt_instances


def test_active_transcription_diagnostic_success_uses_production_boundaries_and_cleans_audio(
    tmp_path,
):
    exit_code, output, microphones, runners, stt_instances = _run(tmp_path)

    assert exit_code == 0
    microphone = microphones[0]
    assert microphone.constructor_kwargs == {
        "device": "plughw:2,0",
        "record_seconds": 5,
        "timeout_seconds": 30.75,
    }
    assert microphone.calls[0] == "start"
    assert microphone.calls[-2:] == ["cancel_current", "stop"]
    assert microphone.capture_path is not None
    assert microphone.capture_path.exists() is False
    capture_call = next(call for call in microphone.calls if isinstance(call, tuple))
    assert capture_call[2]["device"] == "plughw:2,0"
    assert capture_call[2]["diagnostic_audio"] is False

    runner = runners[0]
    assert runner.constructor_kwargs["termination_grace_seconds"] == 1.0
    assert runner.constructor_kwargs["hard_cleanup_deadline_seconds"] == 3.0
    assert callable(runner.constructor_kwargs["status_callback"])
    stt = stt_instances[0]
    assert stt.constructor_kwargs["runner"] is runner
    assert stt.constructor_kwargs["timeout_seconds"] == 15.0
    assert stt.calls[0][1:] == ("en", 15.0)

    assert any("Finalized WAV path:" in line for line in output)
    assert any(
        "Finalized WAV format: 16000 Hz, 1 channel(s), 16-bit PCM" in line
        for line in output
    )
    assert "Microphone gate released: yes" in output
    assert "Transcribing command" in output
    assert "Whisper process: pid=1234, pgid=1234" in output
    assert "Whisper child reaped: yes" in output
    assert "Whisper output handles closed: yes" in output
    assert "Whisper process cleanup: completed" in output
    assert "Transcript: goodbye ares" in output
    assert output[-1] == "Temporary audio cleanup: removed"


def test_active_transcription_timeout_reports_group_cleanup_and_removes_audio(tmp_path):
    result = _transcription_result(
        success=False,
        status="transcription_timeout",
        text="",
        timed_out=True,
        terminated=True,
        killed=True,
    )

    exit_code, output, microphones, _, stt_instances = _run(
        tmp_path,
        result=result,
    )

    assert exit_code == 4
    assert microphones[0].capture_path is not None
    assert microphones[0].capture_path.exists() is False
    assert stt_instances[0].calls
    assert "Whisper transcription timed out after 15 seconds" in output
    assert "Whisper termination: SIGTERM=sent, SIGKILL=sent" in output
    assert "Whisper child reaped: yes" in output
    assert "Whisper output handles closed: yes" in output
    assert "Whisper process cleanup: completed" in output
    assert "Transcript: <empty>" in output
    assert output[-1] == "Temporary audio cleanup: removed"


def test_diagnostic_does_not_infer_process_group_success_from_pid_values(tmp_path):
    result = _transcription_result()
    del result.data["process"]["metadata"]["process_group_started"]

    exit_code, output, _, _, _ = _run(tmp_path, result=result)

    assert exit_code == 4
    assert "Whisper process: pid=1234, pgid=1234" in output
    assert "Whisper process group started: no" in output
    assert "Whisper process cleanup: completed" in output


def test_capture_failure_never_constructs_whisper_and_releases_microphone(tmp_path):
    exit_code, output, microphones, runners, stt_instances = _run(
        tmp_path,
        capture_success=False,
    )

    assert exit_code == 3
    assert microphones[0].calls[-2:] == ["cancel_current", "stop"]
    assert microphones[0].capture_path is not None
    assert microphones[0].capture_path.exists() is False
    assert runners == []
    assert stt_instances == []
    assert any("Active command capture failed:" in line for line in output)
    assert "Microphone gate released: yes" in output
    assert "Microphone adapter released: yes" in output


def test_retain_audio_preserves_finalized_wav(tmp_path):
    exit_code, output, microphones, _, _ = _run(
        tmp_path,
        retain_audio=True,
    )

    assert exit_code == 0
    assert microphones[0].capture_path is not None
    assert microphones[0].capture_path.exists() is True
    assert output[-1] == "Temporary audio cleanup: retained"


def test_owner_acknowledgement_flag_is_required_before_hardware_construction():
    created = []

    exit_code = manual.run_active_transcription_diagnostic(
        [],
        output_func=lambda _line: None,
        microphone_factory=lambda **kwargs: created.append(kwargs),
    )

    assert exit_code == 2
    assert created == []


def test_microphone_start_failure_still_calls_stop_and_never_constructs_whisper():
    microphones = []
    runners = []

    def microphone_factory(**kwargs):
        microphone = FakeMicrophone(start_success=False, **kwargs)
        microphones.append(microphone)
        return microphone

    exit_code = manual.run_active_transcription_diagnostic(
        ["--diagnostic-active-transcription"],
        output_func=lambda _line: None,
        microphone_factory=microphone_factory,
        runner_factory=lambda **kwargs: runners.append(kwargs),
    )

    assert exit_code == 3
    assert microphones[0].calls == ["start", "cancel_current", "stop"]
    assert runners == []


def test_cleanup_deadline_cannot_be_shorter_than_termination_grace():
    created = []

    exit_code = manual.run_active_transcription_diagnostic(
        [
            "--diagnostic-active-transcription",
            "--termination-grace-seconds",
            "2",
            "--hard-cleanup-deadline-seconds",
            "1",
        ],
        output_func=lambda _line: None,
        microphone_factory=lambda **kwargs: created.append(kwargs),
    )

    assert exit_code == 2
    assert created == []


def test_diagnostic_source_has_no_runtime_routing_storage_or_subprocess_implementation():
    source = Path(manual.__file__).read_text(encoding="utf-8").casefold()

    for forbidden in (
        "brainruntime",
        "brainsessionmanager",
        "coreservice",
        "intentparser",
        "skillmanager",
        "eventhistorystore",
        "ownerprofilestore",
        "from memory",
        "memory.",
        "import subprocess",
        "subprocess.",
        "popen(",
        "communicate(",
        "killpg(",
    ):
        assert forbidden not in source
    assert "linuxalsamicrophoneadapter" in source
    assert "linuxwhisperspeechtotextadapter" in source
    assert "whispersubprocessrunner" in source
