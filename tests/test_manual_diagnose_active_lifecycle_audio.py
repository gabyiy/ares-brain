from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core import (
    AudioChunk,
    SingleTurnVoiceRequestV1,
    SingleTurnVoiceResultV1,
    VoiceRuntimeGate,
)
from scripts import manual_diagnose_active_lifecycle_audio as manual


TRANSCRIPTS = (
    "Goodbye, Ares.",
    "Shutdown Ares.",
)


def _successful_recording(path: str, *, transcript: str) -> dict:
    return {
        "success": True,
        "status": "completed_after_terminal_silence",
        "error_message": "",
        "wav_path": path,
        "normalized_wav_path": path,
        "final_whisper_input_path": path,
        "duration_seconds": 1.4,
        "raw_duration_seconds": 2.3,
        "assembled_duration_seconds": 1.4,
        "normalized_duration_seconds": 1.4,
        "whisper_input_duration_seconds": 1.4,
        "pre_roll_frames_retained": 25,
        "first_speech_frame": 41,
        "last_speech_frame": 70,
        "data": {
            "capture_start_reason": "calibration_completed_stream_ready",
            "expected_pre_roll_frames": 25,
            "pre_roll_frames_retained": 25,
            "beginning_clipped": "no",
            "valid_pcm_frames_delivered_to_vad": 70,
            "valid_microphone_bytes_delivered_to_vad": 70 * 640,
            "requested_sample_rate_hz": 16000,
            "actual_channels": 1,
            "actual_sample_width_bytes": 2,
            "process": {
                "args": ["/usr/bin/arecord", "-D", "plughw:2,0", "-"],
                "returncode": 0,
                "stderr": "",
            },
            "test_transcript": transcript,
        },
    }


def _failed_recording() -> dict:
    message = "arecord_process_exited:1"
    stderr = "arecord: audio open error: Device or resource busy"
    source = {
        "process_pid": 4321,
        "process_exit_status": 1,
        "stream_ended": True,
        "closed": True,
        "valid_pcm_frames_delivered_to_vad": 0,
        "stderr_preview": stderr,
    }
    return {
        "success": False,
        "status": "device_error",
        "error_message": f"pcm_stream_error:RuntimeError:{message}",
        "duration_seconds": 0.0,
        "data": {
            "requested_sample_rate_hz": 16000,
            "actual_channels": 1,
            "actual_sample_width_bytes": 2,
            "pcm_exception": {
                "exception_class": "RuntimeError",
                "exception_message": message,
                "traceback": (
                    "Traceback (most recent call last):\n"
                    "  File \"core/LinuxAlsaMicrophone.py\", line 1, in read_frame\n"
                    f"RuntimeError: {message}"
                ),
                "failing_adapter_class": (
                    "core.LinuxAlsaMicrophone.ContinuousPcmFrameSource"
                ),
                "failing_method": "read_frame",
                "open_called": True,
                "source_snapshot": source,
                "source_snapshot_after_close": source,
            },
            "process": {
                "args": ["/usr/bin/arecord", "-D", "plughw:2,0", "-"],
                "returncode": 1,
                "stderr": stderr,
            },
        },
    }


class FakeMicrophone:
    def __init__(self, *, preflight_failure: bool = False) -> None:
        self.preflight_failure = preflight_failure
        self.trace: list[str] = []
        self.preflight_kwargs: list[dict] = []
        self.started = False

    def start(self):
        assert not self.started
        self.started = True
        self.trace.append("start")
        return SimpleNamespace(
            success=True,
            status="started",
            data={"health": {"data": {"device_count": 1}}},
        )

    def preflight_pcm_stream(self, **kwargs):
        assert self.started
        self.preflight_kwargs.append(dict(kwargs))
        self.trace.extend(("open", "read", "close"))
        common = {
            "adapter_class": (
                "core.LinuxAlsaMicrophone.LinuxAlsaMicrophoneAdapter"
            ),
            "failing_method": "read_frame" if self.preflight_failure else "",
            "microphone_device": kwargs["device"],
            "resolved_capture_device": kwargs["device"],
            "requested_pcm_format": {
                "sample_rate_hz": 16000,
                "channels": 1,
                "sample_width_bytes": 2,
                "sample_format": "S16_LE",
                "frame_duration_ms": kwargs["frame_duration_ms"],
                "expected_frame_bytes": 640,
            },
            "open_called": True,
            "open_success": True,
            "read_called": True,
            "first_pcm_read_success": not self.preflight_failure,
            "first_frame_byte_count": 0 if self.preflight_failure else 640,
            "expected_frame_byte_count": 640,
            "first_frame_nonzero": False,
            "process_id": 1234,
            "alsa_child_process_id": 4321,
            "alsa_process_exit_status": 1 if self.preflight_failure else None,
            "exact_capture_command": [
                "/usr/bin/arecord",
                "-q",
                "-f",
                "S16_LE",
                "-c",
                "1",
                "-r",
                "16000",
                "-t",
                "raw",
                "-D",
                kwargs["device"],
                "-",
            ],
            "microphone_ownership_released": True,
            "close_called": True,
            "close_success": True,
            "cleanup_result": "completed",
        }
        if self.preflight_failure:
            message = "arecord_process_exited:1"
            stderr = "arecord: audio open error: Device or resource busy"
            common.update(
                {
                    "failure_reason": "pcm_read_error",
                    "exception_class": "RuntimeError",
                    "exception_message": message,
                    "traceback": (
                        "Traceback (most recent call last):\n"
                        "  File \"core/LinuxAlsaMicrophone.py\", line 1, in read_frame\n"
                        f"RuntimeError: {message}"
                    ),
                    "alsa_stderr": stderr,
                }
            )
            return SimpleNamespace(
                success=False,
                status="pcm_read_error",
                error_message=f"RuntimeError:{message}",
                data=common,
            )
        common["alsa_stderr"] = ""
        return SimpleNamespace(
            success=True,
            status="pcm_preflight_passed",
            error_message="",
            data=common,
        )

    def stop(self):
        self.trace.append("stop")
        self.started = False
        return SimpleNamespace(success=True, status="stopped")


class FakeTurnStream:
    def __init__(self, index: int, *, fail_read: bool = False) -> None:
        self.index = index
        self.fail_read = fail_read
        self.events: list[str] = []
        self.closed = False
        self.frame_bytes = 0

    def open(self) -> None:
        assert not self.closed
        self.events.append("open")

    def read(self) -> bytes:
        assert self.events == ["open"]
        assert not self.closed
        self.events.append("read")
        if self.fail_read:
            raise RuntimeError("arecord_process_exited:1")
        frame = int(400 + self.index).to_bytes(2, "little", signed=True) * 320
        self.frame_bytes = len(frame)
        return frame

    def close(self) -> None:
        assert self.events == ["open", "read"]
        self.events.append("close")
        self.closed = True


class FakePipeline:
    def __init__(
        self,
        microphone: FakeMicrophone,
        *,
        fail_first_phrase: bool = False,
        fail_comparison_whisper_indices: tuple[int, ...] = (),
    ) -> None:
        self.microphone_adapter = microphone
        self.speech_to_text_adapter = SimpleNamespace()
        self.fail_first_phrase = fail_first_phrase
        self.fail_comparison_whisper_indices = set(
            fail_comparison_whisper_indices
        )
        self.capture_ready_observers: list = []
        self.requests: list[SingleTurnVoiceRequestV1] = []
        self.streams: list[FakeTurnStream] = []
        self.raw_hook_decisions: list = []
        self.stop_count = 0
        self.core_service_calls = 0

    def add_capture_ready_observer(self, observer):
        self.capture_ready_observers.append(observer)

        def unsubscribe():
            self.capture_ready_observers.remove(observer)

        return unsubscribe

    def run_once(
        self,
        request,
        cancellation_token=None,
        raw_transcript_hook=None,
        finalized_audio_hook=None,
    ):
        assert all(stream.closed for stream in self.streams)
        assert request.metadata["diagnostic_only"] is True
        assert request.metadata["lifecycle_execution_enabled"] is False
        assert request.metadata["memory_execution_enabled"] is False
        assert request.metadata["diagnostic_exception_traceback"] is True
        self.requests.append(request)
        index = len(self.requests)
        stream = FakeTurnStream(
            index,
            fail_read=bool(self.fail_first_phrase and index == 1),
        )
        self.streams.append(stream)
        stream.open()
        if self.fail_first_phrase and index == 1:
            with pytest.raises(RuntimeError, match="arecord_process_exited:1"):
                stream.read()
            stream.close()
            return SingleTurnVoiceResultV1(
                success=False,
                status="recording_failed",
                error_stage="recording",
                error_reason=(
                    "pcm_stream_error:RuntimeError:arecord_process_exited:1"
                ),
                data={
                    "recording": _failed_recording(),
                    "cleanup": {"status": "pipeline_cleanup_completed"},
                },
            )

        frame = stream.read()
        assert len(frame) == 640
        stream.close()

        transcript = TRANSCRIPTS[index - 1]
        for observer in list(self.capture_ready_observers):
            observer(
                {
                    "capture_start_reason": "calibration_completed_stream_ready",
                    "pre_roll_seconds": request.pre_roll_seconds,
                }
            )
        finalized_decision = finalized_audio_hook(
            AudioChunk(
                data=frame,
                metadata={
                    "wav_path": request.recording_output_path,
                    "final_whisper_input_path": request.recording_output_path,
                },
            )
        )
        assert finalized_decision.handled is False
        assert finalized_decision.continue_to_whisper is True
        recording = _successful_recording(
            request.recording_output_path,
            transcript=transcript,
        )
        if index in self.fail_comparison_whisper_indices:
            return SingleTurnVoiceResultV1(
                success=False,
                status="transcription_failed",
                error_stage="transcription",
                error_reason="comparison whisper failed exactly",
                recorded_wav_path=request.recording_output_path,
                recording_duration_seconds=1.4,
                data={
                    "recording": recording,
                    "finalized_audio_decision": {
                        "handled": finalized_decision.handled,
                        "continue_to_whisper": finalized_decision.continue_to_whisper,
                        "status": finalized_decision.status,
                        "canonical_text": finalized_decision.canonical_text,
                        "data": finalized_decision.data,
                    },
                    "cleanup": {"status": "pipeline_cleanup_completed"},
                },
            )
        decision = raw_transcript_hook(transcript)
        self.raw_hook_decisions.append(decision)
        assert decision.handled is True
        assert decision.continue_to_output is False
        return SingleTurnVoiceResultV1(
            success=True,
            status="diagnostic_transcript_captured",
            recorded_wav_path=request.recording_output_path,
            recording_duration_seconds=1.4,
            recognized_text=transcript,
            raw_transcript=transcript,
            cleaned_transcript=transcript,
            data={
                "recording": recording,
                "finalized_audio_decision": {
                    "handled": finalized_decision.handled,
                    "continue_to_whisper": finalized_decision.continue_to_whisper,
                    "status": finalized_decision.status,
                    "canonical_text": finalized_decision.canonical_text,
                    "data": finalized_decision.data,
                },
                "cleanup": {"status": "pipeline_cleanup_completed"},
            },
        )

    def stop(self, request=None):
        self.stop_count += 1
        return SimpleNamespace(success=True, status="stopped")


class FakeLockFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.enter_count = 0
        self.exit_count = 0

    def __call__(self, *args, **kwargs):
        self.calls.append((args, dict(kwargs)))
        parent = self

        class Lock:
            def __enter__(self):
                parent.enter_count += 1
                return self

            def __exit__(self, exc_type, exc, traceback):
                parent.exit_count += 1
                return False

        return Lock()


class FakeLifecycleAudioRecognizer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = 0
        self.grammar = (
            "goodbye ares",
            "goodbye rs",
            "shutdown ares",
            "shutdown rs",
            "calculate two plus two",
            "remember that i like video games",
            "[unk]",
        )

    def recognize_wav(self, wav_path):
        self.calls.append(str(wav_path))
        slug = Path(str(wav_path)).name.casefold()
        if "goodbye-ares" in slug:
            classification = "standby"
            canonical = "goodbye ares"
            recognized = "goodbye rs"
            alias = "rs"
            alias_position = "suffix"
            alias_canonicalized = "goodbye ares"
            fallback = False
        elif "shutdown-ares" in slug:
            classification = "shutdown"
            canonical = "shutdown ares"
            recognized = "shutdown rs"
            alias = "rs"
            alias_position = "suffix"
            alias_canonicalized = "shutdown ares"
            fallback = False
        else:
            classification = "ordinary"
            canonical = ""
            recognized = ""
            alias = ""
            alias_position = "none"
            alias_canonicalized = ""
            fallback = True
        return SimpleNamespace(
            classification=classification,
            canonical_phrase=canonical,
            recognized_text=recognized,
            recognized_tokens=tuple(recognized.split()),
            alias_detected=alias,
            alias_position=alias_position,
            alias_canonicalized_transcript=alias_canonicalized,
            confidence=0.91 if recognized else None,
            confidence_available=bool(recognized),
            confidence_tier="high" if recognized else "missing",
            recognition_backend="fake_constrained_vosk",
            rejection_reason="" if recognized else "unknown_token_detected",
            confirmation_required=False,
            proposed_classification="",
            selected_lifecycle_action=(
                classification if classification in {"standby", "shutdown"} else "none"
            ),
            whisper_fallback_required=fallback,
        )

    def close(self):
        self.closed += 1


class DiagnosticHarness:
    def __init__(
        self,
        *,
        fail_preflight: bool = False,
        fail_first_phrase: bool = False,
        fail_comparison_whisper_indices: tuple[int, ...] = (),
    ) -> None:
        self.microphone = FakeMicrophone(preflight_failure=fail_preflight)
        self.pipeline = FakePipeline(
            self.microphone,
            fail_first_phrase=fail_first_phrase,
            fail_comparison_whisper_indices=fail_comparison_whisper_indices,
        )
        self.gate = VoiceRuntimeGate(settle_delay_seconds=0.0)
        self.lifecycle_recognizer = FakeLifecycleAudioRecognizer()
        self.factory_calls: list = []

    def production_factory(self, args, *, output_func):
        self.factory_calls.append((args, output_func))
        request = SingleTurnVoiceRequestV1(
            microphone_device=args.microphone_device,
            speaker_device=args.speaker_device,
            recording_duration_seconds=5,
            capture_mode="auto_stop",
            pre_roll_seconds=0.5,
            silence_duration_seconds=0.9,
            frame_duration_ms=20,
            recording_timeout_seconds=30.75,
            transcription_timeout_seconds=15.0,
            playback_enabled=True,
            diagnostic_audio=bool(args.retain_diagnostic_audio),
            cleanup_policy=(
                "keep" if args.retain_diagnostic_audio else "delete_always"
            ),
            metadata={
                "source": "run_ares_standby_voice",
                "capture_profile": "active_command_v1",
            },
        )
        return SimpleNamespace(
            pipeline_args=args,
            base_request=request,
            pipeline=self.pipeline,
            voice_io_gate=self.gate,
            active_lifecycle_audio_recognizer=self.lifecycle_recognizer,
        )


def _argv(tmp_path: Path, *, retain_audio: bool = False) -> list[str]:
    values = [
        "--diagnostic-active-lifecycle-audio",
        "--microphone-device",
        "plughw:9,7",
        "--speaker-device",
        "plughw:CARD=Diagnostic,DEV=0",
        "--command-whisper-command",
        "external/whisper.cpp/build/bin/whisper-cli",
        "--command-whisper-model",
        "models/whisper/ggml-base.en.bin",
        "--output-directory",
        str(tmp_path),
        "--runtime-lock-path",
        str(tmp_path / "runtime.lock"),
    ]
    if retain_audio:
        values.append("--retain-audio")
    return values


def _run(tmp_path: Path, harness: DiagnosticHarness):
    output: list[str] = []
    locks = FakeLockFactory()
    code = manual.run_active_lifecycle_audio_diagnostic(
        _argv(tmp_path),
        output_func=output.append,
        production_factory=harness.production_factory,
        lock_factory=locks,
    )
    return code, output, locks


def test_diagnostic_uses_default_shared_factory_and_forwards_cli_device(
    tmp_path,
    monkeypatch,
):
    harness = DiagnosticHarness()
    monkeypatch.setattr(
        manual.standby_voice,
        "build_production_active_audio_pipeline",
        harness.production_factory,
    )
    output: list[str] = []
    locks = FakeLockFactory()

    code = manual.run_active_lifecycle_audio_diagnostic(
        _argv(tmp_path),
        output_func=output.append,
        lock_factory=locks,
    )

    assert code == 0
    assert len(harness.factory_calls) == 1
    production_args, callback = harness.factory_calls[0]
    assert production_args.microphone_device == "plughw:9,7"
    assert production_args.speaker_device == "plughw:CARD=Diagnostic,DEV=0"
    assert callback is output.append or callable(callback)
    assert locks.enter_count == locks.exit_count == 1
    assert locks.calls[0][1]["owner_kind"] == (
        "active_lifecycle_audio_diagnostic"
    )


def test_diagnostic_injects_nonpersistent_event_sink_when_factory_supports_it(
    tmp_path,
):
    harness = DiagnosticHarness()
    captured = {}

    def factory(args, *, output_func, event_history_store):
        captured["store"] = event_history_store
        return harness.production_factory(args, output_func=output_func)

    output = []
    code = manual.run_active_lifecycle_audio_diagnostic(
        _argv(tmp_path),
        output_func=output.append,
        production_factory=factory,
        lock_factory=FakeLockFactory(),
    )

    assert code == 0
    assert isinstance(captured["store"], manual.DiagnosticEventHistorySink)
    assert captured["store"].dropped_event_count == 0


def test_inter_command_readiness_blocks_live_child_or_busy_voice_gate(tmp_path):
    gate = VoiceRuntimeGate(settle_delay_seconds=0.0)
    output_path = tmp_path / "next-attempt.wav"
    runner = SimpleNamespace(active_pid=4401)
    pipeline = SimpleNamespace(
        speech_to_text_adapter=SimpleNamespace(runner=runner),
        microphone_adapter=SimpleNamespace(_active_stream=None),
    )

    assert manual._inter_command_readiness(pipeline, gate, output_path) == (
        False,
        "previous_whisper_process_alive",
    )

    runner.active_pid = 0
    pipeline.microphone_adapter._active_stream = object()
    assert manual._inter_command_readiness(pipeline, gate, output_path) == (
        False,
        "previous_arecord_stream_alive",
    )

    pipeline.microphone_adapter._active_stream = None
    gate.begin_capture("previous_turn")
    assert manual._inter_command_readiness(pipeline, gate, output_path) == (
        False,
        "voice_io_gate_not_idle",
    )
    gate.end_capture("previous_turn")

    assert manual._inter_command_readiness(pipeline, gate, output_path) == (
        True,
        "ready",
    )

    assert manual._inter_command_readiness(
        pipeline,
        gate,
        output_path,
        cancellation_requested=True,
    ) == (False, "stale_cancellation_requested")

    output_path.write_bytes(b"old attempt")
    assert manual._inter_command_readiness(pipeline, gate, output_path) == (
        False,
        "diagnostic_output_path_not_unique",
    )


def test_preflight_and_two_phrase_cycles_have_strict_stream_lifecycles(tmp_path):
    harness = DiagnosticHarness()

    code, output, _ = _run(tmp_path, harness)

    assert code == 0
    assert harness.microphone.trace == ["start", "open", "read", "close", "stop"]
    assert len(harness.microphone.preflight_kwargs) == 1
    preflight = harness.microphone.preflight_kwargs[0]
    assert preflight["device"] == "plughw:9,7"
    assert preflight["frame_duration_ms"] == 20
    assert preflight["diagnostic_traceback"] is True
    assert preflight["owner"] == "diagnostic_active_capture"
    assert len(harness.pipeline.requests) == len(manual.DIAGNOSTIC_PHRASES) == 2
    assert harness.lifecycle_recognizer.calls == [
        request.recording_output_path for request in harness.pipeline.requests
    ]
    assert harness.lifecycle_recognizer.closed == 1
    rendered = "\n".join(output)
    assert "Constrained lifecycle classification: standby" in rendered
    assert "Canonical lifecycle phrase: goodbye ares" in rendered
    assert "Constrained lifecycle classification: shutdown" in rendered
    assert "Canonical lifecycle phrase: shutdown ares" in rendered
    assert "Whisper fallback would run in production: no" in rendered
    assert "Loaded constrained lifecycle grammar (7 phrases; diagnostic-only):" in rendered
    assert '"goodbye rs"' in rendered
    assert '"shutdown rs"' in rendered
    assert "Raw constrained transcript: goodbye rs" in rendered
    assert 'Raw constrained tokens: ["goodbye", "rs"]' in rendered
    assert "Constrained alias detected: rs" in rendered
    assert "Constrained alias position: suffix" in rendered
    assert "Alias-canonicalized constrained transcript: goodbye ares" in rendered
    assert "Alias-canonicalized constrained transcript: shutdown ares" in rendered
    assert "Constrained lifecycle rejection reason: <none>" in rendered


def test_rejected_constrained_lifecycle_phrase_uses_valid_whisper_fallback(tmp_path):
    harness = DiagnosticHarness()
    original = harness.lifecycle_recognizer.recognize_wav

    def miss_goodbye(wav_path):
        if "goodbye-ares" not in Path(str(wav_path)).name.casefold():
            return original(wav_path)
        harness.lifecycle_recognizer.calls.append(str(wav_path))
        return SimpleNamespace(
            classification="ordinary",
            canonical_phrase="",
            recognized_text="clears throat",
            recognized_tokens=("clears", "throat"),
            confidence=0.95,
            confidence_available=True,
            confidence_tier="rejected",
            recognition_backend="fake_constrained_vosk",
            rejection_reason="bounded_distractor_phrase",
            confirmation_required=False,
            proposed_classification="",
            selected_lifecycle_action="none",
            whisper_fallback_required=True,
        )

    harness.lifecycle_recognizer.recognize_wav = miss_goodbye

    code, output, _ = _run(tmp_path, harness)

    assert code == 0
    rendered = "\n".join(output)
    assert "Whisper fallback ran: yes" in rendered
    assert "Final lifecycle decision: standby" in rendered
    assert "both lifecycle phrases passed" in rendered
    assert len({id(stream) for stream in harness.pipeline.streams}) == 2
    assert all(stream.events == ["open", "read", "close"] for stream in harness.pipeline.streams)
    assert all(stream.closed and stream.frame_bytes == 640 for stream in harness.pipeline.streams)
    assert all(request.microphone_device == "plughw:9,7" for request in harness.pipeline.requests)
    assert all(request.pre_roll_seconds == 0.5 for request in harness.pipeline.requests)
    assert all(request.silence_duration_seconds == 0.9 for request in harness.pipeline.requests)
    assert len(harness.pipeline.raw_hook_decisions) == 2
    assert harness.pipeline.core_service_calls == 0
    assert harness.pipeline.stop_count == 1
    assert harness.gate.snapshot()["capture_active"] is False

    rendered = "\n".join(output)
    assert "Microphone preflight:" in rendered
    assert "first PCM read: success" in rendered
    assert "frame bytes: 640" in rendered
    assert "expected frame bytes: 640" in rendered
    assert "stream health: healthy" in rendered
    assert "ownership: diagnostic_active_capture -> released" in rendered
    assert rendered.count("Ready ") == 2
    assert rendered.count("Pre-roll retained: 0.500s / 25 of 25 frames") == 2
    assert rendered.count("Candidate duration: 1.400s") == 2
    assert "Raw transcript: Goodbye, Ares." in rendered
    assert "Lifecycle classification: standby" in rendered
    assert "Lifecycle classification: shutdown" in rendered
    assert rendered.count("Lifecycle action executed: no (diagnostic-only)") == 2


def test_high_constrained_lifecycle_result_does_not_depend_on_comparison_whisper(
    tmp_path,
):
    harness = DiagnosticHarness(fail_comparison_whisper_indices=(1, 2))

    code, output, _ = _run(tmp_path, harness)

    assert code == 0
    rendered = "\n".join(output)
    assert rendered.count("Raw Whisper transcript: <empty>") == 2
    assert rendered.count("Comparison Whisper status: transcription_failed") == 2
    assert rendered.count(
        "Comparison Whisper reason: comparison whisper failed exactly"
    ) == 2
    assert "Constrained lifecycle classification: standby" in rendered
    assert "Constrained lifecycle classification: shutdown" in rendered
    assert rendered.count("Attempt status: captured_and_classified") == 2
    assert "Diagnostic result: both lifecycle phrases passed" in rendered


def test_rejected_constrained_phrase_requires_successful_whisper_fallback(tmp_path):
    harness = DiagnosticHarness(fail_comparison_whisper_indices=(1,))
    original = harness.lifecycle_recognizer.recognize_wav

    def reject_goodbye(wav_path):
        if "goodbye-ares" not in Path(str(wav_path)).name.casefold():
            return original(wav_path)
        return SimpleNamespace(
            classification="ordinary",
            canonical_phrase="",
            recognized_text="[unk]",
            recognized_tokens=("[unk]",),
            confidence=0.99,
            confidence_available=True,
            confidence_tier="rejected",
            recognition_backend="fake_constrained_vosk",
            rejection_reason="unknown_token_detected",
            confirmation_required=False,
            proposed_classification="",
            selected_lifecycle_action="none",
            whisper_fallback_required=True,
        )

    harness.lifecycle_recognizer.recognize_wav = reject_goodbye

    code, output, _ = _run(tmp_path, harness)

    assert code == 3
    rendered = "\n".join(output)
    assert "Comparison Whisper status: transcription_failed" in rendered
    assert "Comparison Whisper reason: comparison whisper failed exactly" in rendered
    assert "Failure category: whisper_error" in rendered
    assert "Diagnostic result: 1 phrase(s) failed" in rendered


def test_preflight_preserves_exact_runtime_error_traceback_and_alsa_stderr(tmp_path):
    harness = DiagnosticHarness(fail_preflight=True)

    code, output, _ = _run(tmp_path, harness)

    assert code == 3
    assert harness.pipeline.requests == []
    assert harness.microphone.trace == ["start", "open", "read", "close", "stop"]
    assert harness.pipeline.stop_count == 1
    assert harness.gate.snapshot()["capture_active"] is False
    rendered = "\n".join(output)
    assert "failure category: pcm_read_error" in rendered
    assert "exception class: RuntimeError" in rendered
    assert "exception message: arecord_process_exited:1" in rendered
    assert "RuntimeError: arecord_process_exited:1" in rendered
    assert "ALSA stderr: arecord: audio open error: Device or resource busy" in rendered
    assert "failing method: read_frame" in rendered
    assert "cleanup: completed" in rendered


def test_preflight_attributes_microphone_start_exception_exactly():
    class StartFailureMicrophone(FakeMicrophone):
        def start(self):
            raise RuntimeError("microphone start exploded exactly")

    microphone = StartFailureMicrophone()
    gate = VoiceRuntimeGate(settle_delay_seconds=0.0)
    output = []

    success = manual._run_microphone_preflight(
        microphone,
        gate=gate,
        device="plughw:2,0",
        frame_duration_ms=20,
        output_func=output.append,
    )

    assert success is False
    rendered = "\n".join(output)
    assert "exception message: microphone start exploded exactly" in rendered
    assert "failing method: start" in rendered
    assert "StartFailureMicrophone" in rendered
    assert gate.snapshot()["capture_active"] is False


def test_preflight_attributes_gate_ownership_exception_without_opening_microphone():
    class GateFailure:
        def begin_capture(self, owner):
            raise RuntimeError(f"gate already owned:{owner}")

        def end_capture(self, owner):
            raise AssertionError("unacquired gate must not be released")

        def snapshot(self):
            return {"capture_active": False}

    microphone = FakeMicrophone()
    output = []

    success = manual._run_microphone_preflight(
        microphone,
        gate=GateFailure(),
        device="plughw:2,0",
        frame_duration_ms=20,
        output_func=output.append,
    )

    assert success is False
    rendered = "\n".join(output)
    assert "exception message: gate already owned:diagnostic_active_capture" in rendered
    assert "failing method: begin_capture" in rendered
    assert "GateFailure" in rendered
    assert microphone.preflight_kwargs == []


def test_failed_first_phrase_does_not_poison_later_fresh_captures(tmp_path):
    harness = DiagnosticHarness(fail_first_phrase=True)

    code, output, _ = _run(tmp_path, harness)

    assert code == 3
    assert len(harness.pipeline.requests) == 2
    assert len({id(stream) for stream in harness.pipeline.streams}) == 2
    assert all(stream.events == ["open", "read", "close"] for stream in harness.pipeline.streams)
    assert all(stream.closed for stream in harness.pipeline.streams)
    assert harness.pipeline.streams[0].frame_bytes == 0
    assert all(stream.frame_bytes == 640 for stream in harness.pipeline.streams[1:])
    assert harness.gate.snapshot()["capture_active"] is False
    rendered = "\n".join(output)
    assert "Failure category: microphone_open_error" in rendered
    assert "Exception class: RuntimeError" in rendered
    assert "Exception message: arecord_process_exited:1" in rendered
    assert "Diagnostic traceback:" in rendered
    assert "RuntimeError: arecord_process_exited:1" in rendered
    assert "ALSA stderr: arecord: audio open error: Device or resource busy" in rendered
    assert "Raw transcript: Shutdown Ares." in rendered
    assert "Diagnostic result: 1 phrase(s) failed" in rendered


@pytest.mark.parametrize(
    ("result", "recording", "expected"),
    [
        (
            SimpleNamespace(success=False, status="no_speech_timeout", error_stage="recording", error_reason=""),
            {},
            "no_speech_timeout",
        ),
        (
            SimpleNamespace(success=False, status="transcription_failed", error_stage="transcription", error_reason="whisper failed"),
            {},
            "whisper_error",
        ),
        (
            SimpleNamespace(success=False, status="blank_transcription", error_stage="transcription_validation", error_reason="empty_transcript"),
            {},
            "empty_transcript",
        ),
        (
            SimpleNamespace(success=False, status="recording_failed", error_stage="recording_start", error_reason="microphone_start_failed"),
            {},
            "microphone_open_error",
        ),
        (
            SimpleNamespace(success=False, status="invalid_recording", error_stage="recording", error_reason="invalid_pcm incomplete frame"),
            {},
            "invalid_frame_error",
        ),
        (
            SimpleNamespace(success=False, status="recording_failed", error_stage="recording", error_reason="wav write failed"),
            {},
            "wav_write_error",
        ),
        (
            SimpleNamespace(success=False, status="recording_failed", error_stage="recording", error_reason="VAD detector failed"),
            {},
            "VAD_error",
        ),
        (
            SimpleNamespace(success=False, status="recording_failed", error_stage="recording", error_reason="pcm failure"),
            {},
            "pcm_read_error",
        ),
    ],
)
def test_typed_failure_categories(result, recording, expected):
    pcm_exception = {}
    if expected == "pcm_read_error":
        pcm_exception = {
            "exception_message": "pcm_transport_glitch",
            "open_called": True,
            "source_snapshot": {"valid_pcm_frames_delivered_to_vad": 1},
        }

    actual = manual._pipeline_failure_kind(
        result,
        recording=recording,
        pcm_exception=pcm_exception,
        candidate_duration=0.0,
        raw_transcript="",
    )

    assert actual == expected


def test_lifecycle_parse_failure_is_typed_without_executing_action(tmp_path, monkeypatch):
    request = SingleTurnVoiceRequestV1(
        recording_output_path=str(tmp_path / "candidate.wav"),
        capture_mode="auto_stop",
        pre_roll_seconds=0.5,
        metadata={"capture_profile": "active_command_v1"},
    )
    result = SingleTurnVoiceResultV1(
        success=True,
        status="diagnostic_transcript_captured",
        raw_transcript="Goodbye, Ares.",
        recording_duration_seconds=1.0,
        data={
            "recording": _successful_recording(
                request.recording_output_path,
                transcript="Goodbye, Ares.",
            )
        },
    )
    monkeypatch.setattr(
        manual,
        "normalize_active_lifecycle_command",
        lambda _text: (_ for _ in ()).throw(ValueError("parse exploded exactly")),
    )

    attempt = manual._attempt_from_pipeline_result(
        "goodbye Ares",
        result,
        request=request,
    )

    assert attempt.success is False
    assert attempt.failure_kind == "lifecycle_parse_error"
    assert attempt.error_message == "ValueError:parse exploded exactly"
    assert attempt.exception_class == "ValueError"
    assert attempt.exception_message == "parse exploded exactly"
    assert "ValueError: parse exploded exactly" in attempt.exception_traceback
    assert attempt.failing_method == "normalize_active_lifecycle_command"

    output = []
    manual._print_attempt(attempt, request=request, output_func=output.append)
    assert "Lifecycle classification: lifecycle_parse_error" in output
    assert "Exception message: parse exploded exactly" in output


def test_pcm_cleanup_failure_preserves_exact_close_exception_and_status(tmp_path):
    request = SingleTurnVoiceRequestV1(
        recording_output_path=str(tmp_path / "candidate.wav"),
        capture_mode="auto_stop",
        pre_roll_seconds=0.5,
        metadata={"capture_profile": "active_command_v1"},
    )
    recording = _successful_recording(
        request.recording_output_path,
        transcript="calculate two plus two",
    )
    recording["data"]["pcm_stream_cleanup"] = {
        "status": "incomplete",
        "exception_class": "RuntimeError",
        "exception_message": "arecord_pcm_stream_cleanup_failed:wait_timeout",
        "traceback": "RuntimeError: arecord_pcm_stream_cleanup_failed:wait_timeout",
        "source_snapshot_after_close": {
            "closed": False,
            "stream_ended": True,
            "read_sequence": 70,
        },
    }
    result = SingleTurnVoiceResultV1(
        success=False,
        status="recording_failed",
        error_stage="recording",
        error_reason="pcm_stream_cleanup_error",
        raw_transcript="calculate two plus two",
        recording_duration_seconds=1.4,
        data={
            "recording": recording,
            "cleanup": {"status": "pipeline_cleanup_completed"},
        },
    )

    attempt = manual._attempt_from_pipeline_result(
        "calculate two plus two",
        result,
        request=request,
    )

    assert attempt.success is False
    assert attempt.failure_kind == "pcm_read_error"
    assert attempt.exception_class == "RuntimeError"
    assert attempt.exception_message == (
        "arecord_pcm_stream_cleanup_failed:wait_timeout"
    )
    assert attempt.failing_method == "close"
    assert attempt.cleanup_result == "incomplete"


def test_factory_exception_prints_exact_message_and_diagnostic_traceback(tmp_path):
    def broken_factory(_args, *, output_func):
        raise RuntimeError("production factory exploded exactly")

    output: list[str] = []
    locks = FakeLockFactory()
    code = manual.run_active_lifecycle_audio_diagnostic(
        _argv(tmp_path),
        output_func=output.append,
        production_factory=broken_factory,
        lock_factory=locks,
    )

    assert code == 3
    rendered = "\n".join(output)
    assert "Failure type: RuntimeError" in rendered
    assert "Failure message: production factory exploded exactly" in rendered
    assert "Diagnostic traceback:" in rendered
    assert "RuntimeError: production factory exploded exactly" in rendered
    assert "Microphone ownership: released" in rendered
    assert locks.enter_count == locks.exit_count == 1


def test_required_acknowledgement_is_checked_before_factory_or_lock(tmp_path):
    factories = []
    locks = FakeLockFactory()
    output: list[str] = []

    code = manual.run_active_lifecycle_audio_diagnostic(
        ["--output-directory", str(tmp_path)],
        output_func=output.append,
        production_factory=lambda *args, **kwargs: factories.append((args, kwargs)),
        lock_factory=locks,
    )

    assert code == 2
    assert factories == []
    assert locks.calls == []
    assert "Loaded constrained lifecycle grammar" not in "\n".join(output)


def test_constrained_payload_preserves_raw_evidence_and_alias_rewrite():
    recognition = SimpleNamespace(
        classification="standby",
        canonical_phrase="goodbye ares",
        recognized_text="goodbye r s",
        recognized_tokens=("goodbye", "r", "s"),
        alias_detected="r s",
        alias_position="suffix",
        alias_canonicalized_transcript="goodbye ares",
        confidence=0.91,
        confidence_available=True,
        confidence_tier="high",
        recognition_backend="fake_constrained_vosk",
        rejection_reason="",
        confirmation_required=False,
        proposed_classification="",
        selected_lifecycle_action="standby",
        whisper_fallback_required=False,
    )

    payload = manual._constrained_recognition_payload(recognition)

    assert payload["recognized_text"] == "goodbye r s"
    assert payload["recognized_tokens"] == ["goodbye", "r", "s"]
    assert payload["alias_detected"] == "r s"
    assert payload["alias_position"] == "suffix"
    assert payload["alias_canonicalized_transcript"] == "goodbye ares"
    assert payload["canonical_phrase"] == "goodbye ares"
    assert payload["selected_lifecycle_action"] == "standby"
    assert payload["whisper_fallback_required"] is False


def test_diagnostic_grammar_includes_backend_added_unknown_alternative():
    recognizer = SimpleNamespace(grammar=("goodbye rs", "shutdown rs"))

    grammar = manual._diagnostic_lifecycle_grammar(recognizer)

    assert grammar == ("goodbye rs", "shutdown rs", "[unk]")


def test_script_has_no_independent_audio_or_lifecycle_composition():
    source = Path(manual.__file__).read_text(encoding="utf-8")
    folded = source.casefold()

    for forbidden in (
        "linuxalsamicrophoneadapter(",
        "linuxwhisperspeechtotextadapter(",
        "whispersubprocessrunner(",
        "brainsessionmanager(",
        "brainruntime(",
        "coreservice(",
        "skillmanager(",
        "ownerprofilestore",
        "eventhistorystore",
        ".activate_session(",
        ".return_to_standby(",
        ".shutdown(",
    ):
        assert forbidden not in folded
    assert "build_production_active_audio_pipeline" in source
    assert "raw_transcript_hook" in source
    assert "Lifecycle action executed: no (diagnostic-only)" in source
