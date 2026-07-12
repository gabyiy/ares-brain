from pathlib import Path
import wave

import pytest

from core import (
    ALSA_SPEAKER_STATUS_DISABLED,
    ALSA_SPEAKER_STATUS_PLAYBACK_FAILED,
    ALSA_SPEAKER_STATUS_PLAYED,
    SpeakerPlaybackResult,
    TextToSpeechResultV1,
)
from scripts import manual_verify_linux_tts as manual_tts


def _write_valid_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x00\x01" * 2205)


def _healthy_result() -> TextToSpeechResultV1:
    speaker = SpeakerPlaybackResult(
        success=True,
        status="healthy",
        data={
            "aplay_path": "/usr/bin/aplay",
            "selected_device": "plughw:CARD=Device,DEV=0",
            "device_available": True,
        },
    )
    return TextToSpeechResultV1(
        success=True,
        status="healthy",
        engine="piper",
        voice_id="en_US-amy-low",
        data={
            "piper_binary_path": "/opt/ares/piper",
            "speaker": speaker.to_dict(),
        },
    )


def _failed_health(status: str, speaker_status: str = "healthy") -> TextToSpeechResultV1:
    speaker_success = speaker_status == "healthy"
    speaker = SpeakerPlaybackResult(
        success=speaker_success,
        status=speaker_status,
        error_message="" if speaker_success else speaker_status,
    )
    return TextToSpeechResultV1(
        success=False,
        status=status,
        error_message=status,
        data={"speaker": speaker.to_dict()},
    )


def _successful_synthesis(request, wav_mode="valid", playback_success=True):
    output_path = Path(request.output_wav_path)
    if wav_mode == "valid":
        _write_valid_wav(output_path)
    elif wav_mode == "empty":
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"")
    elif wav_mode == "corrupt":
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"not a wav")

    playback_status = ALSA_SPEAKER_STATUS_DISABLED
    playback = {"enabled": False}
    success = True
    status = "synthesized"
    error_message = ""
    if request.playback_enabled:
        playback_status = (
            ALSA_SPEAKER_STATUS_PLAYED
            if playback_success
            else ALSA_SPEAKER_STATUS_PLAYBACK_FAILED
        )
        success = playback_success
        status = "synthesized" if playback_success else "playback_failed"
        error_message = "" if playback_success else "aplay_exit_2"
        playback = SpeakerPlaybackResult(
            success=playback_success,
            status=playback_status,
            wav_path=str(output_path),
            device="plughw:CARD=Device,DEV=0",
            error_message=error_message,
            data={
                "process": {
                    "args": [
                        "/usr/bin/aplay",
                        "-D",
                        "plughw:CARD=Device,DEV=0",
                        str(output_path),
                    ],
                    "command": (
                        "/usr/bin/aplay -D plughw:CARD=Device,DEV=0 "
                        f"{output_path}"
                    ),
                    "returncode": 0 if playback_success else 2,
                    "stdout": "",
                    "stderr": "" if playback_success else "device busy",
                    "timed_out": False,
                    "error_message": "",
                }
            },
        ).to_dict()

    return TextToSpeechResultV1(
        success=success,
        status=status,
        normalized_text=request.text,
        engine="piper",
        voice_id=request.voice_id,
        generated_audio_path=str(output_path),
        duration_seconds=0.1,
        processing_time_seconds=0.25,
        playback_status=playback_status,
        error_message=error_message,
        data={
            "process": {
                "args": [
                    "/opt/ares/piper",
                    "--model",
                    "/opt/ares/voice.onnx",
                    "--output_file",
                    str(output_path),
                ],
                "command": (
                    "/opt/ares/piper --model /opt/ares/voice.onnx "
                    f"--output_file {output_path}"
                ),
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
                "error_message": "",
            },
            "playback": playback,
        },
    )


def _patch_runtime(monkeypatch, health, synthesize):
    state = {"speaker_kwargs": None, "adapter_kwargs": None, "requests": []}

    class FakeSpeaker:
        def __init__(self, **kwargs):
            state["speaker_kwargs"] = kwargs

    class FakeAdapter:
        def __init__(self, **kwargs):
            state["adapter_kwargs"] = kwargs

        def health_check(self):
            return health

        def synthesize(self, request):
            state["requests"].append(request)
            return synthesize(request)

    monkeypatch.setattr(manual_tts, "LinuxAlsaSpeakerAdapter", FakeSpeaker)
    monkeypatch.setattr(manual_tts, "LinuxPiperTextToSpeechAdapter", FakeAdapter)
    return state


def _base_args(tmp_path, playback=False):
    args = [
        "--text",
        "Hello Gabriel",
        "--piper-command",
        "/opt/ares/piper",
        "--model",
        str(tmp_path / "voice.onnx"),
        "--config",
        str(tmp_path / "voice.onnx.json"),
        "--output",
        str(tmp_path / "speech.wav"),
        "--device",
        "plughw:CARD=Device,DEV=0",
    ]
    if playback:
        args.append("--playback")
    return args


def test_manual_tts_accepts_nested_structured_health_success():
    payload = _healthy_result().to_dict()

    assert manual_tts._is_healthy_tts_result(payload) is True
    payload["data"]["speaker"]["success"] = False
    assert manual_tts._is_healthy_tts_result(payload) is False


def test_manual_tts_succeeds_without_playback_by_default(tmp_path, monkeypatch):
    state = _patch_runtime(
        monkeypatch,
        _healthy_result(),
        lambda request: _successful_synthesis(request),
    )
    messages = []

    exit_code = manual_tts.run_manual_verification(
        _base_args(tmp_path),
        output_func=messages.append,
    )

    assert exit_code == 0
    assert state["requests"][0].playback_enabled is False
    assert any("tts_health_status: healthy" in message for message in messages)
    assert any("wav_sample_rate_hz: 22050" in message for message in messages)
    assert "aplay_command: not_run (playback disabled)" in messages
    assert messages[-1] == "PASS: TTS verification completed."


def test_manual_tts_succeeds_with_explicit_playback(tmp_path, monkeypatch):
    state = _patch_runtime(
        monkeypatch,
        _healthy_result(),
        lambda request: _successful_synthesis(request),
    )
    messages = []

    exit_code = manual_tts.run_manual_verification(
        _base_args(tmp_path, playback=True),
        output_func=messages.append,
    )

    assert exit_code == 0
    assert state["requests"][0].playback_enabled is True
    assert any(message.startswith("aplay_command: /usr/bin/aplay -D") for message in messages)
    assert "playback_status: played" in messages


@pytest.mark.parametrize(
    ("health", "expected_status"),
    [
        (_failed_health("piper_executable_missing"), "piper_executable_missing"),
        (_failed_health("piper_model_missing"), "piper_model_missing"),
        (_failed_health("piper_model_config_missing"), "piper_model_config_missing"),
        (_failed_health("speaker_unavailable", "aplay_missing"), "speaker_unavailable"),
        (_failed_health("speaker_unavailable", "invalid_device"), "speaker_unavailable"),
    ],
)
def test_manual_tts_health_failures_return_exit_code_two(
    tmp_path,
    monkeypatch,
    health,
    expected_status,
):
    state = _patch_runtime(
        monkeypatch,
        health,
        lambda request: _successful_synthesis(request),
    )
    messages = []

    exit_code = manual_tts.run_manual_verification(
        _base_args(tmp_path),
        output_func=messages.append,
    )

    assert exit_code == 2
    assert state["requests"] == []
    assert f"tts_health_status: {expected_status}" in messages
    assert messages[-1] == "FAIL: TTS health check failed."


def test_manual_tts_piper_failure_returns_one_and_prints_raw_output(
    tmp_path,
    monkeypatch,
):
    def failed_synthesis(request):
        return TextToSpeechResultV1(
            success=False,
            status="tts_failed",
            normalized_text=request.text,
            engine="piper",
            voice_id=request.voice_id,
            generated_audio_path=request.output_wav_path,
            playback_status="not_attempted",
            error_message="piper_exit_3",
            data={
                "process": {
                    "command": "/opt/ares/piper --model bad",
                    "returncode": 3,
                    "stdout": "raw piper output",
                    "stderr": "invalid model",
                    "timed_out": False,
                    "error_message": "",
                }
            },
        )

    _patch_runtime(monkeypatch, _healthy_result(), failed_synthesis)
    messages = []

    exit_code = manual_tts.run_manual_verification(
        _base_args(tmp_path),
        output_func=messages.append,
    )

    assert exit_code == 1
    assert "piper_exit_code: 3" in messages
    assert "piper_stdout: raw piper output" in messages
    assert "piper_stderr: invalid model" in messages


@pytest.mark.parametrize("wav_mode", ["missing", "empty", "corrupt"])
def test_manual_tts_rejects_missing_empty_or_corrupt_wav(
    tmp_path,
    monkeypatch,
    wav_mode,
):
    _patch_runtime(
        monkeypatch,
        _healthy_result(),
        lambda request: _successful_synthesis(request, wav_mode=wav_mode),
    )
    messages = []

    exit_code = manual_tts.run_manual_verification(
        _base_args(tmp_path),
        output_func=messages.append,
    )

    assert exit_code == 1
    assert "wav_valid: False" in messages
    assert messages[-1] == "FAIL: TTS verification failed."


def test_manual_tts_playback_failure_preserves_wav_and_returns_one(
    tmp_path,
    monkeypatch,
):
    output_path = tmp_path / "speech.wav"
    _patch_runtime(
        monkeypatch,
        _healthy_result(),
        lambda request: _successful_synthesis(request, playback_success=False),
    )
    messages = []

    exit_code = manual_tts.run_manual_verification(
        _base_args(tmp_path, playback=True),
        output_func=messages.append,
    )

    assert exit_code == 1
    assert output_path.exists()
    assert "aplay_exit_code: 2" in messages
    assert "aplay_stderr: device busy" in messages
    assert messages[-1] == "FAIL: TTS verification failed."
