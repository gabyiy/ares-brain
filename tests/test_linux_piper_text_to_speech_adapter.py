from pathlib import Path
import wave

from core import (
    ALSA_SPEAKER_STATUS_DISABLED,
    ALSA_SPEAKER_STATUS_PLAYBACK_FAILED,
    ALSA_SPEAKER_STATUS_PLAYED,
    CONTRACT_TEXT_TO_SPEECH_REQUEST,
    CONTRACT_TEXT_TO_SPEECH_RESULT,
    LinuxPiperTextToSpeechAdapter,
    PIPER_STATUS_CONFIG_MISSING,
    PIPER_STATUS_EMPTY_TEXT,
    PIPER_STATUS_EXECUTABLE_MISSING,
    PIPER_STATUS_FAILED,
    PIPER_STATUS_INVALID_VOICE,
    PIPER_STATUS_INVALID_WAV,
    PIPER_STATUS_MODEL_MISSING,
    PIPER_STATUS_OUTPUT_EMPTY,
    PIPER_STATUS_OUTPUT_MISSING,
    PIPER_STATUS_PLAYBACK_FAILED,
    PIPER_STATUS_PROFILE_CONFIG_INVALID,
    PIPER_STATUS_SYNTHESIZED,
    PIPER_STATUS_TEXT_TOO_LONG,
    PIPER_STATUS_TIMEOUT,
    RESOURCE_ERROR_HEAVY_MODULE_LIMIT,
    ResourceDeclaration,
    ResourceManager,
    ResourcePolicy,
    SafeTextProcessResult,
    SpeakerPlaybackResult,
    TextToSpeechRequestV1,
    VoiceProfile,
    VoiceProfileRegistry,
    build_service_manifest,
    default_voice_related_manifests,
)


class FakeTextRunner:
    def __init__(
        self,
        available=True,
        returncode=0,
        timed_out=False,
        stdout="",
        stderr="",
        write_mode="valid",
    ):
        self.available = available
        self.returncode = returncode
        self.timed_out = timed_out
        self.stdout = stdout
        self.stderr = stderr
        self.write_mode = write_mode
        self.calls = []

    def which(self, executable):
        return "/usr/local/bin/piper" if self.available else None

    def run(self, args, timeout_seconds, input_text=""):
        safe_args = list(args)
        self.calls.append(
            {
                "args": safe_args,
                "timeout_seconds": timeout_seconds,
                "input_text": input_text,
            }
        )
        if (
            self.returncode == 0
            and not self.timed_out
            and "--output_file" in safe_args
            and self.write_mode != "missing"
        ):
            output_path = safe_args[safe_args.index("--output_file") + 1]
            if self.write_mode == "valid":
                write_valid_wav(output_path)
            elif self.write_mode == "empty":
                open(output_path, "wb").close()
            elif self.write_mode == "corrupt":
                with open(output_path, "wb") as handle:
                    handle.write(b"not a wav")
        return SafeTextProcessResult(
            args=safe_args,
            returncode=-1 if self.timed_out else self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
            error_message="process_timeout" if self.timed_out else "",
        )


class FakeSpeakerAdapter:
    def __init__(self, health_success=True, playback_success=True):
        self.health_success = health_success
        self.playback_success = playback_success
        self.play_calls = []
        self.audio_hardware_accessed = False

    def start(self):
        return self.health_check()

    def stop(self):
        return SpeakerPlaybackResult(success=True, status="stopped")

    def health_check(self):
        return SpeakerPlaybackResult(
            success=self.health_success,
            status="healthy" if self.health_success else "speaker_unavailable",
            error_message="" if self.health_success else "speaker_unavailable",
        )

    def get_status(self):
        return self.health_check()

    def get_capabilities(self):
        return SpeakerPlaybackResult(
            success=True,
            status="capabilities",
            data={"capabilities": ["voice.playback"]},
        )

    def play_wav(self, path, device=None, timeout_seconds=None):
        self.play_calls.append(
            {"path": str(path), "device": device, "timeout_seconds": timeout_seconds}
        )
        self.audio_hardware_accessed = True
        return SpeakerPlaybackResult(
            success=self.playback_success,
            status=ALSA_SPEAKER_STATUS_PLAYED
            if self.playback_success
            else ALSA_SPEAKER_STATUS_PLAYBACK_FAILED,
            error_message="" if self.playback_success else "speaker_failed",
            metadata={"audio_hardware_accessed": True},
        )


def write_valid_wav(path, frames=b"\x00\x01" * 160):
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(frames)


def create_adapter(
    tmp_path,
    runner=None,
    speaker=None,
    model=True,
    config=True,
    clock_values=None,
    max_text_chars=500,
):
    model_dir = tmp_path / "models" / "piper"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / "en_US-hfc_male-medium.onnx"
    config_path = model_dir / "en_US-hfc_male-medium.onnx.json"
    if model:
        model_path.write_bytes(b"fake model")
    if config:
        config_path.write_text(
            '{"audio": {"sample_rate": 22050}, "language": {"code": "en_US"}}',
            encoding="utf-8",
        )
    amy_model = model_dir / "en_US-amy-low.onnx"
    amy_config = model_dir / "en_US-amy-low.onnx.json"
    amy_model.write_bytes(b"fake amy model")
    amy_config.write_text(
        '{"audio": {"sample_rate": 16000}, "language": {"code": "en_US"}}',
        encoding="utf-8",
    )
    registry = VoiceProfileRegistry(
        [
            VoiceProfile(
                profile_id="en_US-hfc_male-medium",
                display_name="ARES Male English",
                engine="piper",
                language="en",
                locale="en_US",
                gender="male",
                quality="medium",
                model_path="models/piper/en_US-hfc_male-medium.onnx",
                config_path="models/piper/en_US-hfc_male-medium.onnx.json",
                expected_sample_rate_hz=22050,
                enabled=True,
                is_default=True,
                minimum_model_size_bytes=1,
            ),
            VoiceProfile(
                profile_id="en_US-amy-low",
                display_name="Amy English",
                engine="piper",
                language="en",
                locale="en_US",
                gender="female",
                quality="low",
                model_path="models/piper/en_US-amy-low.onnx",
                config_path="models/piper/en_US-amy-low.onnx.json",
                expected_sample_rate_hz=16000,
                enabled=True,
                is_default=False,
                minimum_model_size_bytes=1,
            ),
            VoiceProfile(
                profile_id="disabled-voice",
                display_name="Disabled Voice",
                engine="piper",
                language="en",
                locale="en_US",
                gender="unknown",
                quality="low",
                model_path="models/piper/disabled.onnx",
                config_path="models/piper/disabled.onnx.json",
                expected_sample_rate_hz=16000,
                enabled=False,
                is_default=False,
                minimum_model_size_bytes=1,
            ),
        ],
        project_root=tmp_path,
    )
    values = list(clock_values or [1.0, 1.25])

    def clock():
        return values.pop(0) if values else 1.25

    return LinuxPiperTextToSpeechAdapter(
        piper_command="piper",
        voice_registry=registry,
        project_root=tmp_path,
        output_dir=tmp_path / "tts",
        runner=runner or FakeTextRunner(),
        speaker_adapter=speaker or FakeSpeakerAdapter(),
        clock=clock,
        max_text_chars=max_text_chars,
    )


def request(text="Hello Gabriel", playback=False, output_path=None, voice_profile=""):
    return TextToSpeechRequestV1(
        text=text,
        voice_profile_id=voice_profile,
        output_wav_path=str(output_path) if output_path else None,
        playback_enabled=playback,
        timeout_seconds=12,
    )


def test_linux_piper_health_check_passes_with_runtime_model_config_and_speaker(tmp_path):
    adapter = create_adapter(tmp_path)

    result = adapter.health_check()

    assert result.success is True
    assert result.status == "healthy"
    assert result.resolved_voice_profile == "en_US-hfc_male-medium"
    assert result.gender == "male"
    assert result.data["piper_binary_path"] == "/usr/local/bin/piper"
    assert result.data["speaker"]["success"] is True


def test_linux_piper_generates_valid_text_without_playback_by_default(tmp_path):
    output_path = tmp_path / "out.wav"
    runner = FakeTextRunner()
    speaker = FakeSpeakerAdapter()
    adapter = create_adapter(tmp_path, runner=runner, speaker=speaker, clock_values=[5.0, 5.4])

    result = adapter.synthesize(request(output_path=output_path))

    assert result.success is True
    assert result.status == PIPER_STATUS_SYNTHESIZED
    assert result.normalized_text == "Hello Gabriel"
    assert result.requested_voice_profile == ""
    assert result.resolved_voice_profile == "en_US-hfc_male-medium"
    assert result.voice_display_name == "ARES Male English"
    assert result.language == "en"
    assert result.locale == "en_US"
    assert result.gender == "male"
    assert result.quality == "medium"
    assert result.model_path.endswith("en_US-hfc_male-medium.onnx")
    assert result.config_path.endswith("en_US-hfc_male-medium.onnx.json")
    assert result.generated_audio_path == str(output_path)
    assert result.duration_seconds > 0
    assert result.processing_time_seconds == 0.4
    assert result.playback_status == ALSA_SPEAKER_STATUS_DISABLED
    assert result.metadata["subprocess_shell"] is False
    assert result.metadata["audio_hardware_accessed"] is False
    assert speaker.play_calls == []
    assert runner.calls[0]["args"] == [
        "/usr/local/bin/piper",
        "--model",
        str(tmp_path / "models" / "piper" / "en_US-hfc_male-medium.onnx"),
        "--config",
        str(tmp_path / "models" / "piper" / "en_US-hfc_male-medium.onnx.json"),
        "--output_file",
        str(output_path),
    ]
    assert runner.calls[0]["input_text"] == "Hello Gabriel"
    assert runner.calls[0]["timeout_seconds"] == 12


def test_linux_piper_rejects_empty_text(tmp_path):
    adapter = create_adapter(tmp_path)

    result = adapter.synthesize(request(text="   "))

    assert result.success is False
    assert result.status == PIPER_STATUS_EMPTY_TEXT
    assert result.error_message == "empty_text"


def test_linux_piper_rejects_oversized_text(tmp_path):
    adapter = create_adapter(tmp_path, max_text_chars=5)

    result = adapter.synthesize(request(text="too many words"))

    assert result.success is False
    assert result.status == PIPER_STATUS_TEXT_TOO_LONG
    assert result.data["max_text_chars"] == 5


def test_linux_piper_missing_executable_fails_safely(tmp_path):
    adapter = create_adapter(tmp_path, runner=FakeTextRunner(available=False))

    result = adapter.health_check()

    assert result.success is False
    assert result.status == PIPER_STATUS_EXECUTABLE_MISSING


def test_linux_piper_missing_model_fails_safely(tmp_path):
    adapter = create_adapter(tmp_path, model=False)

    result = adapter.health_check()

    assert result.success is False
    assert result.status == PIPER_STATUS_MODEL_MISSING


def test_linux_piper_missing_model_config_fails_safely(tmp_path):
    adapter = create_adapter(tmp_path, config=False)

    result = adapter.health_check()

    assert result.success is False
    assert result.status == PIPER_STATUS_CONFIG_MISSING


def test_linux_piper_health_rejects_invalid_output_directory(tmp_path):
    adapter = create_adapter(tmp_path)
    adapter.output_dir.write_text("not a directory", encoding="utf-8")

    result = adapter.health_check()

    assert result.success is False
    assert result.status == "tts_output_unwritable"
    assert result.error_message.startswith("output_unwritable:")


def test_linux_piper_rejects_invalid_voice_profile(tmp_path):
    adapter = create_adapter(tmp_path)

    result = adapter.synthesize(request(voice_profile="other-voice"))

    assert result.success is False
    assert result.status == PIPER_STATUS_INVALID_VOICE
    assert result.requested_voice_profile == "other-voice"
    assert result.error_message == "unknown_profile"


def test_linux_piper_selects_optional_amy_profile(tmp_path):
    output_path = tmp_path / "amy.wav"
    runner = FakeTextRunner()
    adapter = create_adapter(tmp_path, runner=runner)

    result = adapter.synthesize(
        request(output_path=output_path, voice_profile="en_US-amy-low")
    )

    assert result.success is True
    assert result.requested_voice_profile == "en_US-amy-low"
    assert result.resolved_voice_profile == "en_US-amy-low"
    assert result.voice_display_name == "Amy English"
    assert result.gender == "female"
    assert result.quality == "low"
    assert runner.calls[0]["args"][2].endswith("en_US-amy-low.onnx")


def test_linux_piper_selects_explicit_default_male_profile(tmp_path):
    adapter = create_adapter(tmp_path)

    result = adapter.synthesize(
        request(voice_profile="en_US-hfc_male-medium")
    )

    assert result.success is True
    assert result.requested_voice_profile == "en_US-hfc_male-medium"
    assert result.resolved_voice_profile == "en_US-hfc_male-medium"
    assert result.gender == "male"


def test_linux_piper_rejects_disabled_voice_profile(tmp_path):
    adapter = create_adapter(tmp_path)

    result = adapter.synthesize(request(voice_profile="disabled-voice"))

    assert result.success is False
    assert result.status == PIPER_STATUS_INVALID_VOICE
    assert result.error_message == "profile_disabled"


def test_linux_piper_reports_malformed_profile_config_safely(tmp_path):
    config = tmp_path / "voices.json"
    config.write_text("{not json", encoding="utf-8")
    adapter = LinuxPiperTextToSpeechAdapter(
        piper_command="piper",
        voice_profiles_config_path=config,
        project_root=tmp_path,
        output_dir=tmp_path / "tts",
        runner=FakeTextRunner(),
        speaker_adapter=FakeSpeakerAdapter(),
    )

    result = adapter.health_check()

    assert result.success is False
    assert result.status == PIPER_STATUS_PROFILE_CONFIG_INVALID
    assert result.error_message == "malformed_config"


def test_linux_piper_timeout_is_structured(tmp_path):
    adapter = create_adapter(tmp_path, runner=FakeTextRunner(timed_out=True))

    result = adapter.synthesize(request())

    assert result.success is False
    assert result.status == PIPER_STATUS_TIMEOUT
    assert result.data["process"]["timed_out"] is True


def test_linux_piper_nonzero_process_is_structured(tmp_path):
    adapter = create_adapter(
        tmp_path,
        runner=FakeTextRunner(returncode=2, stderr="bad model"),
    )

    result = adapter.synthesize(request())

    assert result.success is False
    assert result.status == PIPER_STATUS_FAILED
    assert result.error_message == "piper_exit_2"
    assert result.data["process"]["stderr_preview"] == "bad model"
    assert result.data["process"]["stderr"] == "bad model"


def test_linux_piper_invalid_output_directory_fails_safely(tmp_path):
    adapter = create_adapter(tmp_path)
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")

    result = adapter.synthesize(request(output_path=blocked_parent / "speech.wav"))

    assert result.success is False
    assert result.status == "tts_output_unwritable"
    assert result.error_message.startswith("output_unwritable:")


def test_linux_piper_missing_output_file_is_rejected(tmp_path):
    adapter = create_adapter(tmp_path, runner=FakeTextRunner(write_mode="missing"))

    result = adapter.synthesize(request())

    assert result.success is False
    assert result.status == PIPER_STATUS_OUTPUT_MISSING


def test_linux_piper_empty_output_file_is_rejected(tmp_path):
    adapter = create_adapter(tmp_path, runner=FakeTextRunner(write_mode="empty"))

    result = adapter.synthesize(request())

    assert result.success is False
    assert result.status == PIPER_STATUS_OUTPUT_EMPTY


def test_linux_piper_corrupt_wav_is_rejected(tmp_path):
    adapter = create_adapter(tmp_path, runner=FakeTextRunner(write_mode="corrupt"))

    result = adapter.synthesize(request())

    assert result.success is False
    assert result.status == PIPER_STATUS_INVALID_WAV


def test_linux_piper_explicit_playback_calls_speaker(tmp_path):
    speaker = FakeSpeakerAdapter(playback_success=True)
    adapter = create_adapter(tmp_path, speaker=speaker)

    result = adapter.synthesize(request(playback=True))

    assert result.success is True
    assert result.playback_status == ALSA_SPEAKER_STATUS_PLAYED
    assert len(speaker.play_calls) == 1
    assert result.metadata["audio_hardware_accessed"] is True


def test_linux_piper_speaker_failure_preserves_wav_and_text_fallback(tmp_path):
    speaker = FakeSpeakerAdapter(playback_success=False)
    adapter = create_adapter(tmp_path, speaker=speaker)

    result = adapter.synthesize(request(playback=True))

    assert result.success is False
    assert result.status == PIPER_STATUS_PLAYBACK_FAILED
    assert result.generated_audio_path
    assert Path(result.generated_audio_path).exists()
    assert result.data["text_output_fallback"] == "Hello Gabriel"
    assert result.data["playback"]["status"] == ALSA_SPEAKER_STATUS_PLAYBACK_FAILED


def test_linux_piper_lifecycle_start_stop_and_execute(tmp_path):
    adapter = create_adapter(tmp_path)

    started = adapter.start()
    executed = adapter.execute({"text": "lifecycle speech"})
    stopped = adapter.stop()

    assert started.success is True
    assert executed.success is True
    assert stopped.success is True
    assert adapter.started is False


def test_linux_piper_capability_manifest_declares_tts_and_speaker_adapters():
    manifests = {
        manifest.module_name: manifest for manifest in default_voice_related_manifests()
    }

    piper = manifests["linux_piper_text_to_speech_adapter"]
    speaker = manifests["linux_alsa_speaker_adapter"]

    assert "voice.speak" in piper.capabilities
    assert piper.produced_contracts[CONTRACT_TEXT_TO_SPEECH_RESULT] == ["v1"]
    assert piper.consumed_contracts[CONTRACT_TEXT_TO_SPEECH_REQUEST] == ["v1"]
    assert piper.resources.heavy_module is True
    assert piper.metadata["voice_profile_config"] == "config/voice_profiles.json"
    assert "voice.playback" in speaker.capabilities
    assert speaker.resources.heavy_module is False


def test_linux_piper_resource_budget_limits_one_heavy_voice_model():
    manager = ResourceManager(
        policy=ResourcePolicy(
            maximum_estimated_loaded_ram_mb=256,
            maximum_heavy_modules_loaded=1,
        )
    )
    first = build_service_manifest(
        module_name="piper_one",
        capabilities=["voice.speak"],
        resources=ResourceDeclaration(estimated_ram_mb=96, heavy_module=True),
    )
    second = build_service_manifest(
        module_name="piper_two",
        capabilities=["voice.speak"],
        resources=ResourceDeclaration(estimated_ram_mb=96, heavy_module=True),
    )

    assert manager.reserve(first).success is True
    blocked = manager.reserve(second)

    assert blocked.success is False
    assert blocked.status == RESOURCE_ERROR_HEAVY_MODULE_LIMIT


def test_core_service_does_not_import_tts_or_alsa_implementations():
    source = Path("core/CoreService.py").read_text(encoding="utf-8")

    assert "LinuxPiperTextToSpeechAdapter" not in source
    assert "LinuxAlsaSpeakerAdapter" not in source
    assert "piper" not in source.lower()
    assert "aplay" not in source.lower()
    assert "VoiceProfile" not in source
    assert "models/piper" not in source


def test_brain_routing_layers_do_not_import_piper_or_aplay():
    for path in (
        Path("core/intent_router.py"),
        Path("core/Planner.py"),
        Path("core/ExecutionPipeline.py"),
        Path("interfaces/text_repl.py"),
    ):
        source = path.read_text(encoding="utf-8").lower()
        assert "linuxpipertexttospeechadapter" not in source
        assert "linuxalsaspeakeradapter" not in source
        assert "subprocess" not in source
        assert "aplay" not in source
        assert "voiceprofileregistry" not in source
        assert "models/piper" not in source
