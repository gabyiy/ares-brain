from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import wave

import pytest

from core import (
    EVENT_BRAIN_EXECUTION_COMPLETED,
    EVENT_PLAYBACK_COMPLETED,
    EVENT_RECORDING_COMPLETED,
    EVENT_RECORDING_STARTED,
    EVENT_SINGLE_TURN_COMPLETED,
    EVENT_SINGLE_TURN_FAILED,
    EVENT_SINGLE_TURN_STARTED,
    EVENT_SYNTHESIS_COMPLETED,
    EVENT_TRANSCRIPTION_COMPLETED,
    LIFECYCLE_STOPPED,
    AudioChunk,
    AdapterCandidate,
    AdapterFallbackPolicy,
    CancellationToken,
    MicrophoneResult,
    HealthPolicyConfig,
    ResourceManager,
    SingleTurnVoicePipeline,
    SingleTurnPreBrainDecision,
    SingleTurnVoiceRequestV1,
    SpeakerPlaybackResult,
    TextToSpeechResultV1,
    TranscriptionResult,
    build_single_turn_voice_pipeline_manifest,
)
from skills.base import SkillResponse


MALE_PROFILE = "en_US-hfc_male-medium"
AMY_PROFILE = "en_US-amy-low"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _write_wav(path, sample=1800, frames=1600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = int(sample).to_bytes(2, "little", signed=True) * frames
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(pcm)
    return pcm


class FakeMicrophone:
    def __init__(self, order, sample=1800, fail=False, corrupt=False, clock=None, advance=0):
        self.order = order
        self.sample = sample
        self.fail = fail
        self.corrupt = corrupt
        self.clock = clock
        self.advance = advance
        self.active = False
        self.record_count = 0
        self.cancel_count = 0

    def health_check(self):
        return MicrophoneResult(True, "healthy", "microphone healthy")

    def start(self):
        self.order.append("microphone.start")
        self.active = True
        return MicrophoneResult(True, "started", "microphone started")

    def stop(self):
        self.order.append("microphone.stop")
        self.active = False
        return MicrophoneResult(True, "stopped", "microphone stopped")

    def cancel_current(self):
        self.cancel_count += 1

    def record_wav(self, output_path, **kwargs):
        self.order.append("microphone.record")
        self.record_count += 1
        if self.clock:
            self.clock.advance(self.advance)
        if self.fail:
            return MicrophoneResult(False, "recording_failed", "failed", error_message="mic_fail")
        path = Path(output_path)
        if self.corrupt:
            path.write_bytes(b"not-a-wav")
            chunk = AudioChunk(data=b"bad", metadata={"wav_path": str(path)})
        else:
            pcm = _write_wav(path, sample=self.sample)
            chunk = AudioChunk(data=pcm, metadata={"wav_path": str(path)})
        return MicrophoneResult(
            True,
            "recorded",
            "recorded",
            chunk=chunk,
            data={"wav_path": str(path)},
        )


class FakeSpeechToText:
    def __init__(self, order, text="calculate 2 + 2", fail=False, clock=None, advance=0, on_call=None):
        self.order = order
        self.text = text
        self.fail = fail
        self.clock = clock
        self.advance = advance
        self.on_call = on_call
        self.calls = 0

    def health_check(self):
        return TranscriptionResult(True, "healthy", data={})

    def transcribe(self, audio_chunk):
        self.order.append("whisper.transcribe")
        self.calls += 1
        if self.on_call:
            self.on_call()
        if self.clock:
            self.clock.advance(self.advance)
        if self.fail:
            return TranscriptionResult(False, "transcription_failed", error_message="whisper_fail")
        return TranscriptionResult(
            True,
            "transcribed",
            text=self.text,
            confidence=0.95,
            data={"processing_time_seconds": self.advance or 0.25},
        )


class FakeTextToSpeech:
    def __init__(self, order, fail=False, clock=None, advance=0, default_profile=MALE_PROFILE):
        self.order = order
        self.fail = fail
        self.clock = clock
        self.advance = advance
        self.default_profile = default_profile
        self.requests = []
        self.cancel_count = 0

    def health_check(self, voice_profile_id=""):
        return TextToSpeechResultV1(
            success=True,
            status="healthy",
            resolved_voice_profile=voice_profile_id or self.default_profile,
        )

    def start(self):
        self.order.append("piper.start")
        return TextToSpeechResultV1(success=True, status="started")

    def stop(self):
        self.order.append("piper.stop")
        return TextToSpeechResultV1(success=True, status="stopped")

    def cancel_current(self):
        self.cancel_count += 1

    def synthesize(self, request):
        self.order.append("piper.synthesize")
        self.requests.append(request)
        if self.clock:
            self.clock.advance(self.advance)
        profile = request.voice_profile_id or self.default_profile
        if self.fail:
            return TextToSpeechResultV1(
                success=False,
                status="tts_failed",
                normalized_text=request.text,
                resolved_voice_profile=profile,
                error_message="piper_fail",
            )
        _write_wav(request.output_wav_path, sample=1200, frames=800)
        return TextToSpeechResultV1(
            success=True,
            status="synthesized",
            normalized_text=request.text,
            engine="piper",
            voice_id=profile,
            resolved_voice_profile=profile,
            generated_audio_path=request.output_wav_path,
            duration_seconds=0.05,
            processing_time_seconds=self.advance or 0.15,
            playback_status="playback_disabled",
        )


class FakeSpeaker:
    def __init__(self, order, microphone, fail=False, clock=None, advance=0):
        self.order = order
        self.microphone = microphone
        self.fail = fail
        self.clock = clock
        self.advance = advance
        self.play_count = 0
        self.playing = False
        self.cancel_count = 0

    def health_check(self):
        return SpeakerPlaybackResult(True, "healthy", "speaker healthy")

    def start(self):
        self.order.append("speaker.start")
        return SpeakerPlaybackResult(True, "started", "speaker started")

    def stop(self):
        self.order.append("speaker.stop")
        self.playing = False
        return SpeakerPlaybackResult(True, "stopped", "speaker stopped")

    def cancel_current(self):
        self.cancel_count += 1

    def play_wav(self, wav_path, device=None, timeout_seconds=None):
        assert self.microphone.active is False
        self.order.append("speaker.play")
        self.play_count += 1
        self.playing = True
        if self.clock:
            self.clock.advance(self.advance)
        self.playing = False
        if self.fail:
            return SpeakerPlaybackResult(
                False,
                "playback_failed",
                "failed",
                error_message="speaker_fail",
                wav_path=str(wav_path),
                device=device or "",
            )
        return SpeakerPlaybackResult(
            True,
            "played",
            "played",
            wav_path=str(wav_path),
            device=device or "",
            duration_seconds=0.05,
        )


def _request(tmp_path, **overrides):
    values = {
        "recording_output_path": str(tmp_path / "input.wav"),
        "recording_duration_seconds": 1,
        "minimum_rms": 10,
        "playback_enabled": True,
        "cleanup_policy": "keep",
        "correlation_id": "corr-single",
        "session_id": "session-single",
    }
    values.update(overrides)
    return SingleTurnVoiceRequestV1(**values)


def _pipeline(tmp_path, **options):
    order = []
    clock = options.get("clock") or FakeClock()
    microphone = options.get("microphone") or FakeMicrophone(order, clock=clock)
    stt = options.get("stt") or FakeSpeechToText(order, clock=clock)
    tts = options.get("tts") or FakeTextToSpeech(order, clock=clock)
    speaker = options.get("speaker") or FakeSpeaker(order, microphone, clock=clock)
    handled = []

    def default_handler(text):
        handled.append(text)
        return SkillResponse(
            text="Result: 4",
            skill="calculator",
            metadata={"detected_intent": "calculate"},
        )

    pipeline = SingleTurnVoicePipeline(
        microphone,
        stt,
        tts,
        speaker,
        options.get("handler") or default_handler,
        clock=clock,
        event_history_store=options.get("event_history_store"),
    )
    return pipeline, order, microphone, stt, tts, speaker, handled, clock


def test_complete_pipeline_runs_in_strict_order_and_returns_structured_result(tmp_path):
    pipeline, order, microphone, stt, tts, speaker, handled, _ = _pipeline(tmp_path)

    result = pipeline.run_once(_request(tmp_path))

    assert result.success is True
    assert result.status == "completed"
    assert result.recognized_text == "calculate 2 + 2"
    assert result.detected_intent == "calculate"
    assert result.routed_skill == "calculator"
    assert result.brain_text_response == "Result: 4"
    assert result.resolved_voice_profile == MALE_PROFILE
    assert result.playback_status == "played"
    assert handled == ["calculate 2 + 2"]
    execution_order = [item for item in order if item in {
        "microphone.start", "microphone.record", "microphone.stop",
        "whisper.transcribe", "piper.start", "piper.synthesize", "piper.stop",
        "speaker.start", "speaker.play", "speaker.stop",
    }]
    assert execution_order[:10] == [
        "microphone.start",
        "microphone.record",
        "microphone.stop",
        "whisper.transcribe",
        "piper.start",
        "piper.synthesize",
        "piper.stop",
        "speaker.start",
        "speaker.play",
        "speaker.stop",
    ]
    assert microphone.active is False
    assert speaker.playing is False
    assert pipeline.resource_manager.current_usage()["active_task_count"] == 0
    assert "single_turn_voice_pipeline" not in pipeline.resource_manager.current_usage()["reservation_names"]


def test_speaker_never_runs_during_recording_and_heavy_stages_do_not_overlap(tmp_path):
    pipeline, _, microphone, _, _, speaker, _, _ = _pipeline(tmp_path)

    result = pipeline.run_once(_request(tmp_path))
    trace = result.data["coordinator"]["trace"]

    assert result.success is True
    assert microphone.active is False
    assert speaker.play_count == 1
    assert trace.index({"action": "end", "stage": "microphone_capture"}) < trace.index(
        {"action": "begin", "stage": "speaker_playback"}
    )
    assert trace.index({"action": "end", "stage": "whisper"}) < trace.index(
        {"action": "begin", "stage": "piper"}
    )


def test_silent_recording_stops_before_whisper_brain_and_piper(tmp_path):
    order = []
    microphone = FakeMicrophone(order, sample=0)
    pipeline, _, _, stt, tts, speaker, handled, _ = _pipeline(
        tmp_path, microphone=microphone
    )
    speaker.microphone = microphone

    result = pipeline.run_once(_request(tmp_path, minimum_rms=10))

    assert result.status == "silent_audio"
    assert result.error_stage == "recording_validation"
    assert stt.calls == 0
    assert tts.requests == []
    assert speaker.play_count == 0
    assert handled == []


def test_corrupt_recording_fails_before_whisper(tmp_path):
    order = []
    microphone = FakeMicrophone(order, corrupt=True)
    pipeline, _, _, stt, tts, speaker, _, _ = _pipeline(tmp_path, microphone=microphone)
    speaker.microphone = microphone

    result = pipeline.run_once(_request(tmp_path))

    assert result.status == "invalid_recording"
    assert result.error_stage == "recording_validation"
    assert stt.calls == 0
    assert tts.requests == []
    assert speaker.play_count == 0


@pytest.mark.parametrize(
    ("stt", "expected_status"),
    [
        (lambda order: FakeSpeechToText(order, text=""), "blank_transcription"),
        (lambda order: FakeSpeechToText(order, fail=True), "transcription_failed"),
    ],
)
def test_transcription_failures_stop_before_brain_and_tts(tmp_path, stt, expected_status):
    order = []
    adapter = stt(order)
    pipeline, _, _, _, tts, speaker, handled, _ = _pipeline(tmp_path, stt=adapter)

    result = pipeline.run_once(_request(tmp_path))

    assert result.status == expected_status
    assert handled == []
    assert tts.requests == []
    assert speaker.play_count == 0


def test_failed_primary_stt_uses_existing_bounded_fallback_policy(tmp_path):
    order = []

    class UnhealthyStt(FakeSpeechToText):
        def health_check(self):
            return TranscriptionResult(False, "failed", error_message="primary_unhealthy")

    primary = UnhealthyStt(order)
    secondary = FakeSpeechToText(order, text="calculate 9 + 1")
    fallback = AdapterFallbackPolicy(
        config=HealthPolicyConfig(adapter_priority=["primary", "secondary"])
    )
    pipeline, _, _, _, _, _, handled, _ = _pipeline(tmp_path, stt=primary)
    pipeline.fallback_policy = fallback
    pipeline.speech_to_text_candidates = [
        AdapterCandidate("primary", primary, capabilities=["voice.transcribe"]),
        AdapterCandidate("secondary", secondary, capabilities=["voice.transcribe"]),
    ]

    result = pipeline.run_once(_request(tmp_path))

    assert result.success is True
    assert primary.calls == 0
    assert secondary.calls == 1
    assert result.recognized_text == "calculate 9 + 1"
    assert handled == ["calculate 9 + 1"]
    fallback_data = result.data["transcription"]["data"]["fallback_execution"]
    assert fallback_data["selected_adapter_name"] == "secondary"
    assert fallback_data["attempts"] == 1


def test_brain_failure_uses_local_fallback_and_can_synthesize_it(tmp_path):
    def fail_brain(text):
        raise RuntimeError("brain boom")

    pipeline, _, _, _, tts, speaker, _, _ = _pipeline(tmp_path, handler=fail_brain)

    result = pipeline.run_once(_request(tmp_path))

    assert result.status == "completed_with_brain_fallback"
    assert result.success is False
    assert result.brain_fallback_used is True
    assert result.brain_text_response == "I could not process that request."
    assert tts.requests[0].text == "I could not process that request."
    assert speaker.play_count == 1


def test_piper_failure_preserves_brain_response_and_skips_speaker(tmp_path):
    order = []
    tts = FakeTextToSpeech(order, fail=True)
    pipeline, _, _, _, _, speaker, _, _ = _pipeline(tmp_path, tts=tts)

    result = pipeline.run_once(_request(tmp_path))

    assert result.status == "tts_failed"
    assert result.brain_text_response == "Result: 4"
    assert speaker.play_count == 0


def test_speaker_failure_preserves_generated_wav(tmp_path):
    pipeline, order, microphone, _, _, _, _, clock = _pipeline(tmp_path)
    speaker = FakeSpeaker(order, microphone, fail=True, clock=clock)
    pipeline.speaker_adapter = speaker

    result = pipeline.run_once(_request(tmp_path, cleanup_policy="delete_on_success"))

    assert result.status == "playback_failed"
    assert Path(result.generated_speech_wav_path).exists()
    assert result.generated_speech_wav_path in result.data["cleanup"]["preserved"]


def test_playback_is_disabled_by_default_for_simulated_text(tmp_path):
    pipeline, _, microphone, stt, tts, speaker, handled, _ = _pipeline(tmp_path)

    result = pipeline.run_once(
        _request(tmp_path, text_input="calculate 2 + 2", playback_enabled=False)
    )

    assert result.success is True
    assert result.simulated_input is True
    assert result.recording_status == "skipped_simulated_input"
    assert result.transcription_status == "simulated_text_input"
    assert result.tts_status == "skipped_simulated_input"
    assert result.playback_status == "playback_disabled"
    assert microphone.record_count == 0
    assert stt.calls == 0
    assert tts.requests == []
    assert speaker.play_count == 0
    assert handled == ["calculate 2 + 2"]


def test_simulated_text_with_playback_uses_explicit_alternate_voice(tmp_path):
    pipeline, _, microphone, stt, tts, speaker, _, _ = _pipeline(tmp_path)

    result = pipeline.run_once(
        _request(
            tmp_path,
            text_input="calculate 2 + 2",
            playback_enabled=True,
            tts_voice_profile=AMY_PROFILE,
        )
    )

    assert result.success is True
    assert microphone.record_count == 0
    assert stt.calls == 0
    assert tts.requests[0].voice_profile_id == AMY_PROFILE
    assert result.resolved_voice_profile == AMY_PROFILE
    assert speaker.play_count == 1


def test_lifecycle_requires_health_and_stops_cleanly(tmp_path):
    pipeline, *_ = _pipeline(tmp_path)
    request = _request(tmp_path, text_input="show my notes", playback_enabled=False)

    rejected = pipeline.execute(request)
    started = pipeline.start(request)
    health = pipeline.health_check(request)
    executed = pipeline.execute(request)
    stopped = pipeline.stop(request)

    assert rejected.status == "not_ready"
    assert started.success is True
    assert health.success is True
    assert executed.success is True
    assert stopped.success is True
    assert pipeline.lifecycle_status()["state"] == LIFECYCLE_STOPPED
    assert pipeline.resource_manager.current_usage()["active_task_count"] == 0


def test_pre_cancelled_request_releases_resource_lock(tmp_path):
    pipeline, *_ = _pipeline(tmp_path)
    token = CancellationToken(task_id="cancel-before")
    token.cancel("owner_cancelled")

    result = pipeline.run_once(_request(tmp_path), cancellation_token=token)

    assert result.status == "cancelled"
    assert result.error_stage == "cancellation"
    assert pipeline.resource_manager.current_usage()["active_task_count"] == 0
    assert "single_turn_voice_pipeline" not in pipeline.resource_manager.current_usage()["reservation_names"]


def test_cooperative_cancellation_after_transcription_stops_before_brain(tmp_path):
    order = []
    token = CancellationToken(task_id="cancel-during")
    stt = FakeSpeechToText(order, on_call=lambda: token.cancel("stop_after_stt"))
    pipeline, _, _, _, tts, speaker, handled, _ = _pipeline(tmp_path, stt=stt)

    result = pipeline.run_once(_request(tmp_path), cancellation_token=token)

    assert result.status == "cancelled"
    assert result.data["cancelled_at"] == "before_brain"
    assert handled == []
    assert tts.requests == []
    assert speaker.play_count == 0


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("recording", "recording_timeout"),
        ("transcription", "transcription_timeout"),
        ("synthesis", "tts_timeout"),
        ("playback", "playback_timeout"),
    ],
)
def test_stage_timeouts_fail_safely(tmp_path, stage, expected):
    order = []
    clock = FakeClock()
    microphone = FakeMicrophone(order, clock=clock, advance=1 if stage == "recording" else 0)
    stt = FakeSpeechToText(order, clock=clock, advance=1 if stage == "transcription" else 0)
    tts = FakeTextToSpeech(order, clock=clock, advance=1 if stage == "synthesis" else 0)
    speaker = FakeSpeaker(order, microphone, clock=clock, advance=1 if stage == "playback" else 0)
    pipeline, *_ = _pipeline(
        tmp_path,
        clock=clock,
        microphone=microphone,
        stt=stt,
        tts=tts,
        speaker=speaker,
    )
    request = _request(
        tmp_path,
        recording_timeout_seconds=0.1,
        transcription_timeout_seconds=0.1,
        synthesis_timeout_seconds=0.1,
        playback_timeout_seconds=0.1,
    )

    result = pipeline.run_once(request)

    assert result.status == expected
    assert pipeline.resource_manager.current_usage()["active_task_count"] == 0


def test_total_timeout_is_enforced_and_resources_are_released(tmp_path):
    order = []
    clock = FakeClock()
    microphone = FakeMicrophone(order, clock=clock, advance=1)
    pipeline, *_ = _pipeline(tmp_path, clock=clock, microphone=microphone)

    result = pipeline.run_once(_request(tmp_path, timeout_seconds=0.5))

    assert result.status == "recording_timeout"
    assert pipeline.resource_manager.current_usage()["active_task_count"] == 0
    assert "single_turn_voice_pipeline" not in pipeline.resource_manager.current_usage()["reservation_names"]


def test_brain_timeout_uses_safe_fallback(tmp_path):
    clock = FakeClock()

    def slow_brain(text):
        clock.advance(1)
        return SkillResponse("late", "test")

    pipeline, *_ = _pipeline(tmp_path, clock=clock, handler=slow_brain)

    result = pipeline.run_once(_request(tmp_path, brain_timeout_seconds=0.1))

    assert result.brain_fallback_used is True
    assert result.brain_execution_status == "brain_timeout_fallback"
    assert result.brain_text_response == "I could not process that request."


def test_stop_requests_child_cleanup_hooks(tmp_path):
    pipeline, _, microphone, _, tts, speaker, _, _ = _pipeline(tmp_path)

    result = pipeline.run_once(_request(tmp_path))

    assert result.success is True
    assert microphone.cancel_count >= 1
    assert tts.cancel_count >= 1
    assert speaker.cancel_count >= 1


def test_keyboard_interrupt_during_adapter_call_cleans_children_and_resource_slots(tmp_path):
    order = []

    class InterruptingMicrophone(FakeMicrophone):
        def record_wav(self, output_path, **kwargs):
            self.order.append("microphone.record")
            raise KeyboardInterrupt

    microphone = InterruptingMicrophone(order)
    pipeline, _, _, _, tts, speaker, _, _ = _pipeline(tmp_path, microphone=microphone)
    speaker.microphone = microphone

    result = pipeline.run_once(_request(tmp_path))

    assert result.status == "cancelled"
    assert result.error_stage == "cancellation"
    assert microphone.active is False
    assert microphone.cancel_count >= 1
    assert tts.cancel_count >= 1
    assert speaker.cancel_count >= 1
    assert pipeline.resource_manager.current_usage()["active_task_count"] == 0


def test_existing_heavy_reservation_blocks_pipeline_before_activation(tmp_path):
    resource_manager = ResourceManager()
    pipeline, *_ = _pipeline(tmp_path)
    pipeline_manifest = build_single_turn_voice_pipeline_manifest()
    other_manifest = replace(
        pipeline_manifest,
        module_name="other_heavy_voice_module",
        resources=replace(pipeline_manifest.resources, estimated_ram_mb=80),
    )
    assert resource_manager.reserve(other_manifest).success is True
    pipeline.resource_manager = resource_manager

    result = pipeline.run_once(_request(tmp_path))

    assert result.status == "start_failed"
    assert result.error_reason == "heavy_module_limit_exceeded"
    assert pipeline.lifecycle_status()["state"] == LIFECYCLE_STOPPED


def test_event_log_is_complete_and_does_not_store_transcript_text(tmp_path):
    pipeline, *_ = _pipeline(tmp_path)

    result = pipeline.run_once(_request(tmp_path))
    event_types = [event["type"] for event in result.events]
    serialized_payloads = repr([event["payload"] for event in result.events])

    assert event_types == [
        EVENT_SINGLE_TURN_STARTED,
        EVENT_RECORDING_STARTED,
        EVENT_RECORDING_COMPLETED,
        EVENT_TRANSCRIPTION_COMPLETED,
        EVENT_BRAIN_EXECUTION_COMPLETED,
        EVENT_SYNTHESIS_COMPLETED,
        EVENT_PLAYBACK_COMPLETED,
        EVENT_SINGLE_TURN_COMPLETED,
    ]
    assert "calculate 2 + 2" not in serialized_payloads
    assert "Result: 4" not in serialized_payloads


def test_stage_callback_reports_all_six_stages(tmp_path):
    pipeline, *_ = _pipeline(tmp_path)
    stages = []
    pipeline.stage_callback = lambda index, total, label, status: stages.append(
        (index, total, label, status)
    )

    result = pipeline.run_once(_request(tmp_path))

    assert result.success is True
    assert {index for index, _, _, _ in stages} == {1, 2, 3, 4, 5, 6}
    assert all(total == 6 for _, total, _, _ in stages)


def test_event_history_failure_does_not_break_pipeline(tmp_path):
    class BrokenHistory:
        def add(self, event, result):
            raise OSError("disk unavailable")

    pipeline, *_ = _pipeline(tmp_path, event_history_store=BrokenHistory())

    result = pipeline.run_once(_request(tmp_path))

    assert result.success is True
    assert result.data["event_history_failures"]


def test_manifest_declares_contract_lifecycle_permissions_and_heavy_budget():
    manifest = build_single_turn_voice_pipeline_manifest()

    assert manifest.capabilities == ["voice.single_turn"]
    assert manifest.resources.heavy_module is True
    assert manifest.resources.maximum_concurrent_tasks == 1
    assert "microphone.read" in manifest.permissions
    assert "speaker.write" in manifest.permissions
    assert set(manifest.lifecycle_support) == {"start", "health_check", "execute", "stop"}


def test_request_contract_round_trip_and_result_correlation(tmp_path):
    request = _request(tmp_path)
    restored = SingleTurnVoiceRequestV1.from_dict(request.to_dict())
    pipeline, *_ = _pipeline(tmp_path)

    result = pipeline.run_once(restored)

    assert restored == request
    assert result.correlation_id == request.correlation_id
    assert result.session_id == request.session_id
    assert result.to_dict()["contract_name"] == "voice.single_turn.result"


def test_invalid_request_fails_before_lifecycle_or_hardware(tmp_path):
    pipeline, _, microphone, stt, tts, speaker, _, _ = _pipeline(tmp_path)
    request = _request(tmp_path).to_dict()
    request["contract_version"] = "v2"

    result = pipeline.run_once(request)

    assert result.status == "contract_rejected"
    assert microphone.record_count == 0
    assert stt.calls == 0
    assert tts.requests == []
    assert speaker.play_count == 0


def test_failure_event_is_emitted_for_recording_failure(tmp_path):
    order = []
    microphone = FakeMicrophone(order, fail=True)
    pipeline, _, _, _, _, speaker, _, _ = _pipeline(tmp_path, microphone=microphone)
    speaker.microphone = microphone

    result = pipeline.run_once(_request(tmp_path))

    assert result.events[-1]["type"] == EVENT_SINGLE_TURN_FAILED
    assert result.error_stage == "recording"


def test_integration_recognized_text_uses_real_skill_manager_then_tts_and_speaker(tmp_path):
    from core import CoreService
    from events import EventBus as SkillEventBus
    from skills import SkillManager
    from skills.builtin.calculator import CalculatorSkill

    order = []
    microphone = FakeMicrophone(order)
    stt = FakeSpeechToText(order)
    tts = FakeTextToSpeech(order)
    speaker = FakeSpeaker(order, microphone)
    core_service = CoreService()
    manager = SkillManager(event_bus=SkillEventBus(), core_service=core_service)
    manager.register(CalculatorSkill())

    def real_text_path(text):
        intent = manager.parse_intent(text)
        response = manager.handle(text, run_before_intents=True)
        assert response is not None
        return SkillResponse(
            response.text,
            response.skill,
            {**dict(response.metadata), "detected_intent": intent.intent_name},
        )

    pipeline = SingleTurnVoicePipeline(
        microphone,
        stt,
        tts,
        speaker,
        real_text_path,
        core_service=core_service,
    )

    result = pipeline.run_once(
        _request(
            tmp_path,
            text_input="calculate 15 * 8",
            playback_enabled=True,
        )
    )

    assert result.success is True
    assert result.detected_intent == "calculate"
    assert result.routed_skill == "calculator"
    assert result.brain_text_response == "Result: 120"
    assert tts.requests[0].text == "Result: 120"
    assert speaker.play_count == 1
    assert core_service.resource_manager.current_usage()["active_task_count"] == 0


def test_pre_brain_hook_can_intercept_stop_phrase_before_handler(tmp_path):
    pipeline, order, _, stt, tts, speaker, handled, _ = _pipeline(tmp_path)
    stt.text = "goodbye Ares"

    result = pipeline.run_once(
        _request(tmp_path),
        pre_brain_hook=lambda text: SingleTurnPreBrainDecision(
            handled=True,
            status="owner_stop_phrase",
            data={"matched_stop_phrase": "goodbye Ares"},
        ),
    )

    assert result.success is True
    assert result.status == "owner_stop_phrase"
    assert result.recognized_text == "goodbye Ares"
    assert result.brain_execution_status == "owner_stop_phrase"
    assert handled == []
    assert tts.requests == []
    assert speaker.play_count == 0
    assert "piper.synthesize" not in order


def test_local_output_uses_pipeline_tts_and_speaker_without_brain(tmp_path):
    pipeline, order, _, _, tts, speaker, handled, _ = _pipeline(tmp_path)

    result = pipeline.run_local_output(
        _request(tmp_path, playback_enabled=True),
        "Goodbye Gabriel.",
    )

    assert result.success is True
    assert result.status == "completed_local_output"
    assert result.brain_execution_status == "local_output"
    assert result.brain_text_response == "Goodbye Gabriel."
    assert handled == []
    assert tts.requests[0].text == "Goodbye Gabriel."
    assert speaker.play_count == 1
    assert order.index("piper.synthesize") < order.index("speaker.play")


def test_stage_observer_can_be_added_and_removed_without_replacing_primary_callback(tmp_path):
    primary = []
    observed = []
    pipeline, _, _, _, _, _, _, _ = _pipeline(tmp_path)
    pipeline.stage_callback = lambda index, total, label, status: primary.append(index)
    unsubscribe = pipeline.add_stage_observer(
        lambda index, total, label, status: observed.append(index)
    )

    first = pipeline.run_once(
        _request(tmp_path, text_input="calculate 2 + 2", playback_enabled=False)
    )
    unsubscribe()
    second = pipeline.run_once(
        _request(tmp_path, text_input="calculate 3 + 3", playback_enabled=False)
    )

    assert first.success is True
    assert second.success is True
    assert observed
    assert len(primary) > len(observed)
