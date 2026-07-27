from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import wave

from scripts import manual_diagnose_active_lifecycle_audio as manual


TRANSCRIPTS = (
    "Goodbye, Ares.",
    "Shutdown Ares.",
    "Calculate two plus two.",
    "Remember that I like video games.",
)


def _write_canonical_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = b"".join(
        int(1800 if index % 2 == 0 else -1800).to_bytes(
            2,
            "little",
            signed=True,
        )
        for index in range(16000)
    )
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(samples)


class FakeMicrophone:
    def __init__(self, **kwargs) -> None:
        self.constructor_kwargs = dict(kwargs)
        self.calls: list[object] = []
        self.wav_path: Path | None = None

    def start(self):
        self.calls.append("start")
        return SimpleNamespace(success=True, status="started")

    def record_until_silence(self, output_path, **kwargs):
        self.calls.append(("record_until_silence", Path(output_path), dict(kwargs)))
        callback = kwargs["capture_ready_callback"]
        callback({"capture_start_reason": "calibration_completed_stream_ready"})
        self.wav_path = Path(output_path)
        _write_canonical_wav(self.wav_path)
        return SimpleNamespace(
            success=True,
            status="completed_after_terminal_silence",
            error_message="",
            wav_path=str(self.wav_path),
            raw_wav_path="",
            assembled_wav_path=str(self.wav_path),
            normalized_wav_path=str(self.wav_path),
            final_whisper_input_path=str(self.wav_path),
            raw_duration_seconds=2.3,
            assembled_duration_seconds=1.4,
            normalized_duration_seconds=1.4,
            whisper_input_duration_seconds=1.4,
            pre_roll_frames_retained=25,
            first_speech_frame=41,
            last_speech_frame=70,
            data={
                "capture_start_reason": "calibration_completed_stream_ready",
                "expected_pre_roll_frames": 25,
                "pre_roll_frames_retained": 25,
                "beginning_clipped": "no",
            },
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
    def __init__(self, *, transcript: str, **kwargs) -> None:
        self.transcript = transcript
        self.constructor_kwargs = dict(kwargs)
        self.calls: list[object] = []
        self.cancel_count = 0

    def transcribe_wav(self, wav_path, *, language, timeout_seconds):
        self.calls.append((Path(wav_path), language, timeout_seconds))
        return SimpleNamespace(
            success=True,
            status="transcribed",
            text=self.transcript,
            error_message="",
        )

    def cancel_current(self):
        self.cancel_count += 1


def _run(tmp_path: Path, *, retain_audio: bool = False):
    output: list[str] = []
    microphones: list[FakeMicrophone] = []
    runners: list[FakeRunner] = []
    stt_instances: list[FakeStt] = []
    transcript_iter = iter(TRANSCRIPTS)

    def microphone_factory(**kwargs):
        microphone = FakeMicrophone(**kwargs)
        microphones.append(microphone)
        return microphone

    def runner_factory(**kwargs):
        runner = FakeRunner(**kwargs)
        runners.append(runner)
        return runner

    def stt_factory(**kwargs):
        stt = FakeStt(transcript=next(transcript_iter), **kwargs)
        stt_instances.append(stt)
        return stt

    args = [
        "--diagnostic-active-lifecycle-audio",
        "--microphone-device",
        "plughw:2,0",
        "--speaker-device",
        "plughw:CARD=Device,DEV=0",
        "--command-whisper-command",
        "external/whisper.cpp/build/bin/whisper-cli",
        "--command-whisper-model",
        "models/whisper/ggml-base.en.bin",
        "--output-directory",
        str(tmp_path),
    ]
    if retain_audio:
        args.append("--retain-audio")

    code = manual.run_active_lifecycle_audio_diagnostic(
        args,
        output_func=output.append,
        microphone_factory=microphone_factory,
        stt_factory=stt_factory,
        runner_factory=runner_factory,
    )
    return code, output, microphones, runners, stt_instances


def test_diagnostic_uses_exact_production_active_profile_and_classifies_without_execution(
    tmp_path,
):
    code, output, microphones, runners, stt_instances = _run(tmp_path)

    assert code == 0
    assert len(microphones) == len(manual.DIAGNOSTIC_PHRASES) == 4
    assert len(runners) == len(stt_instances) == 4
    for microphone in microphones:
        assert microphone.constructor_kwargs["device"] == "plughw:2,0"
        assert microphone.constructor_kwargs["record_seconds"] == 5
        assert microphone.calls[0] == "start"
        assert microphone.calls[-2:] == ["cancel_current", "stop"]
        record_call = next(
            call for call in microphone.calls if isinstance(call, tuple)
        )
        kwargs = record_call[2]
        assert kwargs["pre_roll_seconds"] >= 0.5
        assert kwargs["silence_seconds"] == 0.9
        assert kwargs["capture_profile"] == "active_command_v1"
        assert kwargs["speech_start_rms"] == 200.0
        assert kwargs["frame_duration_ms"] == 20
        assert callable(kwargs["capture_ready_callback"])
        assert microphone.wav_path is not None
        assert microphone.wav_path.exists() is False

    for runner in runners:
        assert runner.constructor_kwargs["termination_grace_seconds"] == 1.0
        assert runner.constructor_kwargs["hard_cleanup_deadline_seconds"] == 3.0
    for stt in stt_instances:
        assert stt.constructor_kwargs["timeout_seconds"] == 15.0
        assert stt.calls[0][2] == 15.0
        assert stt.cancel_count == 1

    rendered = "\n".join(output)
    assert "Ready 1/4. Say 'goodbye Ares' now." in rendered
    assert "Ready 2/4. Say 'shutdown Ares' now." in rendered
    assert "Ready 3/4. Say 'calculate two plus two' now." in rendered
    assert "Ready 4/4. Say 'remember that I like video games' now." in rendered
    assert "Raw transcript: Goodbye, Ares." in rendered
    assert "Normalized transcript: goodbye" in rendered
    assert "Alias removal: ares" in rendered
    assert "Alias position: suffix" in rendered
    assert "Lifecycle classification: standby" in rendered
    assert "Lifecycle classification: shutdown" in rendered
    assert rendered.count("Lifecycle classification: ordinary") == 2
    assert "Beginning clipped: no" in rendered
    assert "Pre-roll duration: 0.500s retained; 0.500s configured (25/25 frames)" in rendered
    assert "Candidate duration: 1.400s" in rendered
    assert rendered.count("Lifecycle action executed: no (diagnostic-only)") == 4
    assert "No lifecycle transition, skill, or memory operation was executed." in rendered


def test_audio_is_retained_only_when_owner_explicitly_requests_it(tmp_path):
    code, output, microphones, _, _ = _run(tmp_path, retain_audio=True)

    assert code == 0
    assert all(
        microphone.wav_path is not None and microphone.wav_path.exists()
        for microphone in microphones
    )
    assert "Audio cleanup: retained" in output
    for microphone in microphones:
        record_call = next(
            call for call in microphone.calls if isinstance(call, tuple)
        )
        assert record_call[2]["diagnostic_audio"] is True


def test_production_request_helper_uses_launcher_mapping_and_active_profile(tmp_path):
    args = manual.build_parser().parse_args(
        [
            "--diagnostic-active-lifecycle-audio",
            "--microphone-device",
            "plughw:2,0",
            "--speaker-device",
            "plughw:CARD=Device,DEV=0",
        ]
    )

    request = manual.production_active_request(
        args,
        output_path=tmp_path / "active.wav",
    )

    assert request.microphone_device == "plughw:2,0"
    assert request.speaker_device == "plughw:CARD=Device,DEV=0"
    assert request.capture_mode == "auto_stop"
    assert request.pre_roll_seconds == 0.5
    assert request.silence_duration_seconds == 0.9
    assert request.transcription_timeout_seconds == 15.0
    assert request.playback_enabled is False
    assert request.metadata["capture_profile"] == "active_command_v1"
    assert request.metadata["lifecycle_execution_enabled"] is False
    assert request.metadata["memory_execution_enabled"] is False


def test_required_diagnostic_acknowledgement_is_validated_before_hardware(tmp_path):
    microphones = []

    code = manual.run_active_lifecycle_audio_diagnostic(
        ["--output-directory", str(tmp_path)],
        output_func=lambda _line: None,
        microphone_factory=lambda **kwargs: microphones.append(kwargs),
    )

    assert code == 2
    assert microphones == []


def test_script_cannot_execute_runtime_lifecycle_skills_or_owner_memory():
    source = Path(manual.__file__).read_text(encoding="utf-8").casefold()

    for forbidden in (
        "brainsessionmanager",
        "brainruntime(",
        "coreservice(",
        "skillmanager(",
        "ownerprofilestore",
        "eventhistorystore",
        ".handle_request(",
        ".activate_session(",
        ".return_to_standby(",
        ".shutdown(",
    ):
        assert forbidden not in source
    assert "normalize_active_lifecycle_command" in source
    assert "active_command_capture_request" in source
    assert "_command_pipeline_args" in source
