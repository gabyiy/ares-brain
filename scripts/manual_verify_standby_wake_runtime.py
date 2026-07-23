from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import deque
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Callable
import wave


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    BRAIN_ACTIVE,
    BRAIN_STANDBY,
    BRAIN_STOPPED,
    BrainRuntime,
    BrainRuntimeConfig,
    BrainSessionConfig,
    BrainSessionManager,
    CollectingRuntimeOutputAdapter,
    ConversationContextManager,
    CoreService,
    QueuedRuntimeInputAdapter,
    QueuedStandbyWakeListener,
    RuntimeInputResult,
    RuntimeOutputMessage,
    SingleTurnPipelineRuntimeInputAdapter,
    SingleTurnPipelineRuntimeOutputAdapter,
    SingleTurnVoicePipeline,
    SingleTurnVoiceRequestV1,
    TextToSpeechResultV1,
    VoiceActivityCaptureResultV1,
    VoiceRuntimeGate,
    WakeListenerConfig,
)
from core.LinuxAlsaSpeaker import SpeakerPlaybackResult  # noqa: E402
from core.SpeechToText import TranscriptionResult  # noqa: E402
from events import EventBus as SkillEventBus, EventHistoryStore  # noqa: E402
from memory import (  # noqa: E402
    GoalsStore,
    MemoryStore,
    NotesStore,
    OwnerMemoryService,
    TasksStore,
    UserProfileStore,
)
from scripts import manual_verify_single_turn_voice as single_turn  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class TrackedInputAdapter(QueuedRuntimeInputAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TrackedOutputAdapter(CollectingRuntimeOutputAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class DeterministicMicrophone:
    def __init__(self) -> None:
        self.outcomes = deque()
        self.started = False
        self.capture_count = 0
        self.cancelled = False

    def push_speech(self) -> None:
        self.outcomes.append("speech")

    def push_no_speech(self) -> None:
        self.outcomes.append("no_speech")

    def start(self):
        self.started = True
        return {"success": True, "status": "started"}

    def stop(self):
        self.started = False
        return {"success": True, "status": "stopped"}

    def health_check(self):
        return {"success": True, "status": "healthy"}

    def cancel_current(self):
        self.cancelled = True

    def record_until_silence(self, output_path, **kwargs):
        self.capture_count += 1
        outcome = self.outcomes.popleft() if self.outcomes else "speech"
        if outcome == "no_speech":
            return VoiceActivityCaptureResultV1(
                success=False,
                status="no_speech_timeout",
                speech_detected=False,
                stop_reason="no_speech_timeout",
                error_message="no_speech_timeout",
            )
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x20\x03" * 3200)
        return VoiceActivityCaptureResultV1(
            success=True,
            status="completed_after_silence",
            wav_path=str(path),
            final_whisper_input_path=str(path),
            normalized_wav_path=str(path),
            speech_detected=True,
            duration_seconds=0.2,
            speech_duration_seconds=0.2,
            peak_amplitude=800,
            rms_amplitude=800.0,
            speech_rms=800.0,
            sample_rate_hz=16000,
            channels=1,
            sample_width_bytes=2,
            normalized_sample_rate_hz=16000,
            normalized_channels=1,
            normalized_sample_width_bytes=2,
            stop_reason="completed_after_silence",
            correlation_id=kwargs.get("correlation_id", ""),
            session_id=kwargs.get("session_id", ""),
        )


class DeterministicWhisper:
    def __init__(self) -> None:
        self.transcripts = deque()
        self.transcription_count = 0

    def push(self, text: str) -> None:
        self.transcripts.append(text)

    def health_check(self):
        return TranscriptionResult(True, "healthy")

    def transcribe(self, audio_chunk):
        self.transcription_count += 1
        text = self.transcripts.popleft() if self.transcripts else ""
        return TranscriptionResult(
            success=bool(text),
            status="transcribed" if text else "no_transcription",
            text=text,
            confidence=1.0 if text else 0.0,
            error_message="" if text else "no_usable_speech",
        )


class DeterministicPiper:
    def __init__(self) -> None:
        self.texts = []
        self.started = False

    def start(self):
        self.started = True
        return TextToSpeechResultV1(success=True, status="started")

    def stop(self):
        self.started = False
        return TextToSpeechResultV1(success=True, status="stopped")

    def health_check(self, voice_profile_id=""):
        return TextToSpeechResultV1(success=True, status="healthy")

    def synthesize(self, request):
        self.texts.append(request.text)
        path = Path(request.output_wav_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(22050)
            output.writeframes(b"\x10\x00" * 2205)
        return TextToSpeechResultV1(
            success=True,
            status="synthesized",
            normalized_text=request.text,
            engine="deterministic_piper",
            voice_id="deterministic_male",
            resolved_voice_profile="en_US-hfc_male-medium",
            generated_audio_path=str(path),
            duration_seconds=0.1,
            processing_time_seconds=0.001,
        )


class DeterministicSpeaker:
    def __init__(self) -> None:
        self.played_paths = []
        self.playing = False

    def start(self):
        return SpeakerPlaybackResult(True, "started")

    def stop(self):
        self.playing = False
        return SpeakerPlaybackResult(True, "stopped")

    def health_check(self):
        return SpeakerPlaybackResult(True, "healthy")

    def play_wav(self, wav_path, device=None, timeout_seconds=None):
        assert Path(wav_path).is_file()
        self.playing = True
        self.played_paths.append(str(wav_path))
        self.playing = False
        return SpeakerPlaybackResult(
            True,
            "played",
            wav_path=str(wav_path),
            device=str(device or "deterministic"),
            duration_seconds=0.1,
        )


def _build_runtime(root: Path, clock: FakeClock):
    support = root / "support"
    skill_bus = SkillEventBus()
    manager = BrainSessionManager(
        config=BrainSessionConfig(
            inactivity_timeout_seconds=30,
            maximum_consecutive_failures=3,
        ),
        clock=clock,
        session_id_factory=iter(
            (
                "brain-session-wake-manual-1",
                "brain-session-wake-manual-2",
                "brain-session-wake-manual-3",
            )
        ).__next__,
    )
    owner_service = OwnerMemoryService(
        root / "owner_profile.json",
        event_bus=skill_bus,
        pending_path=root / "pending_owner_memory_action.json",
    )
    core_service = CoreService(
        owner_memory_service=owner_service,
        brain_session_manager=manager,
        register_default_pc=False,
        register_default_voice=False,
    )
    skill_manager = single_turn.create_skill_manager(
        core_service,
        event_history_store=EventHistoryStore(support / "events.json"),
        event_bus=skill_bus,
        memory_store=MemoryStore(
            short_path=support / "short_memory.json",
            long_path=support / "long_memory.json",
            event_bus=skill_bus,
        ),
        profile_store=UserProfileStore(support / "user_profile.json", event_bus=skill_bus),
        goals_store=GoalsStore(support / "goals.json", event_bus=skill_bus),
        notes_store=NotesStore(support / "notes.json", event_bus=skill_bus),
        tasks_store=TasksStore(support / "tasks.json", event_bus=skill_bus),
        conversation_context=ConversationContextManager(),
    )
    input_adapter = TrackedInputAdapter()
    output_adapter = TrackedOutputAdapter()
    wake_listener = QueuedStandbyWakeListener(config=WakeListenerConfig())
    runtime = BrainRuntime(
        core_service=core_service,
        command_handler=single_turn.build_existing_brain_handler(skill_manager),
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        config=BrainRuntimeConfig(),
        clock=clock,
        runtime_id_factory=lambda: "brain-runtime-wake-manual",
        standby_wake_listener=wake_listener,
    )
    return runtime, input_adapter, output_adapter, wake_listener


def _build_voice_runtime(root: Path, clock: FakeClock):
    base_runtime, _, _, _ = _build_runtime(root, clock)
    microphone = DeterministicMicrophone()
    whisper = DeterministicWhisper()
    piper = DeterministicPiper()
    speaker = DeterministicSpeaker()
    pipeline = SingleTurnVoicePipeline(
        microphone_adapter=microphone,
        speech_to_text_adapter=whisper,
        text_to_speech_adapter=piper,
        speaker_adapter=speaker,
        command_handler=base_runtime.command_handler,
        core_service=base_runtime.core_service,
    )
    request = SingleTurnVoiceRequestV1(
        microphone_device="deterministic",
        recording_output_path=str(root / "runtime_voice" / "command.wav"),
        capture_mode="auto_stop",
        playback_enabled=True,
        cleanup_policy="delete_on_success",
        tts_voice_profile="en_US-hfc_male-medium",
        speaker_device="deterministic",
        timeout_seconds=30,
        recording_timeout_seconds=5,
        transcription_timeout_seconds=5,
        brain_timeout_seconds=5,
        synthesis_timeout_seconds=5,
        playback_timeout_seconds=5,
    )
    gate = VoiceRuntimeGate(settle_delay_seconds=0)
    active_input = SingleTurnPipelineRuntimeInputAdapter(
        pipeline=pipeline,
        base_request=request,
        session_id_provider=lambda: base_runtime.session_manager.session_id,
        voice_io_gate=gate,
    )
    spoken = []
    output = SingleTurnPipelineRuntimeOutputAdapter(
        pipeline=pipeline,
        base_request=request,
        voice_io_gate=gate,
        output_func=spoken.append,
    )
    wake = QueuedStandbyWakeListener(config=WakeListenerConfig())
    runtime = BrainRuntime(
        core_service=base_runtime.core_service,
        command_handler=base_runtime.command_handler,
        input_adapter=active_input,
        output_adapter=output,
        config=BrainRuntimeConfig(),
        clock=clock,
        runtime_id_factory=lambda: "brain-runtime-wake-voice-manual",
        standby_wake_listener=wake,
    )
    return runtime, microphone, whisper, piper, speaker, spoken, wake


def run_verification(output_func: Callable[[str], None] = print) -> int:
    with TemporaryDirectory(prefix="ares-standby-wake-runtime-") as temporary:
        clock = FakeClock()
        (
            runtime,
            microphone,
            whisper,
            piper,
            speaker,
            spoken,
            wake,
        ) = _build_voice_runtime(Path(temporary), clock)
        output_func("ARES standby wake runtime verification (deterministic, hardware-free)")

        illegal = runtime.session_manager.begin_processing(reason="manual_illegal_transition")
        if illegal.success or runtime.session_manager.state != BRAIN_STOPPED:
            output_func("FAIL: illegal transition changed lifecycle state")
            return 1
        output_func("Illegal STOPPED -> PROCESSING rejected; state preserved")

        started = runtime.start()
        if not started.success or runtime.session_manager.state != BRAIN_STANDBY:
            output_func("FAIL: runtime did not boot to STANDBY")
            return 1
        output_func("STOPPED -> BOOTING -> INITIALIZING -> STANDBY")

        wake.push(None)
        silent = runtime.poll_once()
        if silent.status != "standby_listening" or runtime.session_manager.session_id:
            output_func("FAIL: no-speech wake poll created a session")
            return 1
        output_func("No speech: STANDBY; no session")

        wake.push("I read about Ares yesterday")
        unrelated = runtime.poll_once()
        if unrelated.status != "standby_listening" or spoken:
            output_func("FAIL: unrelated speech activated or produced output")
            return 1
        output_func("Unrelated speech: rejected silently")

        wake.push("Aris.")
        activated = runtime.poll_once()
        first_session = runtime.session_manager.session_id
        if not activated.success or spoken != ["Yes Gabi."] or not first_session:
            output_func("FAIL: wake activation acknowledgement/session failed")
            return 1
        output_func(
            f"Constrained alias 'Aris' resolved to 'Ares': ACTIVE; "
            f"session={first_session}; ARES: Yes Gabi."
        )

        microphone.push_speech()
        whisper.push("calculate 2 plus 2")
        calculation = runtime.poll_once()
        if calculation.response_text != "Result: 4":
            output_func(f"FAIL: calculator returned {calculation.response_text!r}")
            return 1
        output_func("Command 1: Result: 4")

        microphone.push_speech()
        whisper.push("Remember that my favorite color is blue.")
        saved = runtime.poll_once()
        microphone.push_speech()
        whisper.push("What is my favorite color?")
        recalled = runtime.poll_once()
        if not saved.success or "blue" not in recalled.response_text.casefold():
            output_func("FAIL: central owner-memory create/recall failed")
            return 1
        if runtime.session_manager.session_id != first_session:
            output_func("FAIL: active commands replaced the session ID")
            return 1
        output_func("Commands 2-3: owner memory create/recall; session unchanged")

        wake_listens_before = wake.snapshot().listen_count
        runtime.output_adapter.write(
            RuntimeOutputMessage(
                category="diagnostic",
                text="ARES output contains the name Ares.",
                session_id=first_session,
            )
        )
        microphone.push_no_speech()
        runtime.poll_once()
        if wake.snapshot().listen_count != wake_listens_before:
            output_func("FAIL: speaker output was fed into standby wake listening")
            return 1
        output_func("Self-wake guard: active output did not invoke standby listener")

        microphone.push_speech()
        whisper.push("RS goodbye")
        standby = runtime.poll_once()
        if standby.status != "standby_entered" or runtime.session_manager.state != BRAIN_STANDBY:
            output_func("FAIL: lifecycle-only RS goodbye did not return to standby")
            return 1
        output_func("RS goodbye -> Ares goodbye: RETURNING_TO_STANDBY -> STANDBY")

        wake.push("hey, Ares")
        runtime.poll_once()
        second_session = runtime.session_manager.session_id
        if not second_session or second_session == first_session:
            output_func("FAIL: second wake did not create a new session")
            return 1
        output_func(f"Second wake: new session={second_session}")

        clock.advance(29.999)
        microphone.push_no_speech()
        runtime.poll_once()
        if runtime.session_manager.state != BRAIN_ACTIVE:
            output_func("FAIL: inactivity expired before boundary")
            return 1
        output_func("Inactivity 29.999s: ACTIVE")
        clock.advance(0.001)
        microphone.push_no_speech()
        runtime.poll_once()
        if runtime.session_manager.state != BRAIN_STANDBY or runtime.session_manager.session_id:
            output_func("FAIL: inactivity boundary did not clear session")
            return 1
        output_func("Inactivity 30.000s: STANDBY; session cleared")

        wake.push("hello Aries")
        runtime.poll_once()
        microphone.push_speech()
        whisper.push("Ares shut down")
        stopped = runtime.poll_once()
        if not stopped.success or runtime.session_manager.state != BRAIN_STOPPED:
            output_func("FAIL: explicit shutdown did not stop runtime")
            return 1
        if (
            wake.snapshot().listener_state != "stopped"
            or microphone.started
            or piper.started
            or speaker.playing
        ):
            output_func("FAIL: adapters were not stopped during shutdown")
            return 1
        output_func("Ares shut down: SHUTTING_DOWN -> STOPPED; adapters stopped")

        event_payload = json.dumps(
            [event.to_dict() for event in runtime.events() + runtime.session_manager.events()],
            sort_keys=True,
        ).casefold()
        forbidden = (
            "calculate 2 plus 2",
            "favorite color is blue",
            "what is my favorite color",
            "aris",
            "rs goodbye",
            "ares shut down",
            "raw_transcript",
            "audio_bytes",
        )
        leaked = next((value for value in forbidden if value in event_payload), "")
        if leaked:
            output_func(f"FAIL: operational event leaked private content: {leaked}")
            return 1
        if list(Path(temporary).rglob("*.wav")):
            output_func("FAIL: deterministic verification left temporary WAV files")
            return 1
        if microphone.capture_count != whisper.transcription_count + 3:
            output_func("FAIL: deterministic no-speech captures reached Whisper")
            return 1
        if len(speaker.played_paths) != len(piper.texts):
            output_func("FAIL: deterministic Piper/speaker output counts diverged")
            return 1
        output_func(
            "Injected constrained-standby/active-Whisper/Piper/speaker route: PASS"
        )
        output_func("Event privacy: PASS; temporary audio: none; worker threads: none")
        output_func("Standby wake runtime verification passed.")
        return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
