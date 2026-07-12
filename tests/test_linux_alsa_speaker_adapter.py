import wave

from core import (
    ALSA_SPEAKER_STATUS_APLAY_MISSING,
    ALSA_SPEAKER_STATUS_INVALID_DEVICE,
    ALSA_SPEAKER_STATUS_INVALID_WAV,
    ALSA_SPEAKER_STATUS_PLAYBACK_FAILED,
    ALSA_SPEAKER_STATUS_PLAYBACK_TIMEOUT,
    ALSA_SPEAKER_STATUS_PLAYED,
    LinuxAlsaSpeakerAdapter,
    SafeProcessResult,
)


class FakeSpeakerRunner:
    def __init__(self, available=True, returncode=0, timed_out=False, stderr=""):
        self.available = available
        self.returncode = returncode
        self.timed_out = timed_out
        self.stderr = stderr
        self.calls = []

    def which(self, executable):
        return "/usr/bin/aplay" if self.available else None

    def run(self, args, timeout_seconds):
        safe_args = list(args)
        self.calls.append({"args": safe_args, "timeout_seconds": timeout_seconds})
        return SafeProcessResult(
            args=safe_args,
            returncode=-1 if self.timed_out else self.returncode,
            stderr=self.stderr,
            timed_out=self.timed_out,
            error_message="process_timeout" if self.timed_out else "",
        )


def write_valid_wav(path, frames=b"\x00\x01" * 160):
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(frames)


def test_linux_alsa_speaker_health_check_requires_aplay():
    adapter = LinuxAlsaSpeakerAdapter(runner=FakeSpeakerRunner(available=False))

    result = adapter.health_check()

    assert result.success is False
    assert result.status == ALSA_SPEAKER_STATUS_APLAY_MISSING
    assert result.metadata["audio_hardware_accessed"] is False


def test_linux_alsa_speaker_plays_valid_wav_with_argument_list(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeSpeakerRunner()
    adapter = LinuxAlsaSpeakerAdapter(
        device="plughw:CARD=Device,DEV=0",
        runner=runner,
    )

    result = adapter.play_wav(wav_path, timeout_seconds=7)

    assert result.success is True
    assert result.status == ALSA_SPEAKER_STATUS_PLAYED
    assert result.metadata["audio_hardware_accessed"] is True
    assert runner.calls[0]["args"] == [
        "/usr/bin/aplay",
        "-D",
        "plughw:CARD=Device,DEV=0",
        str(wav_path),
    ]
    assert runner.calls[0]["timeout_seconds"] == 7


def test_linux_alsa_speaker_rejects_shell_like_device(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeSpeakerRunner()
    adapter = LinuxAlsaSpeakerAdapter(device="plughw:0;rm -rf /", runner=runner)

    result = adapter.play_wav(wav_path)

    assert result.success is False
    assert result.status == ALSA_SPEAKER_STATUS_INVALID_DEVICE
    assert result.metadata["audio_hardware_accessed"] is False
    assert runner.calls == []


def test_linux_alsa_speaker_rejects_corrupt_wav(tmp_path):
    wav_path = tmp_path / "corrupt.wav"
    wav_path.write_bytes(b"not a wav")
    runner = FakeSpeakerRunner()
    adapter = LinuxAlsaSpeakerAdapter(runner=runner)

    result = adapter.play_wav(wav_path)

    assert result.success is False
    assert result.status == ALSA_SPEAKER_STATUS_INVALID_WAV
    assert result.metadata["audio_hardware_accessed"] is False
    assert runner.calls == []


def test_linux_alsa_speaker_reports_timeout(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeSpeakerRunner(timed_out=True)
    adapter = LinuxAlsaSpeakerAdapter(runner=runner)

    result = adapter.play_wav(wav_path)

    assert result.success is False
    assert result.status == ALSA_SPEAKER_STATUS_PLAYBACK_TIMEOUT
    assert result.metadata["audio_hardware_accessed"] is True


def test_linux_alsa_speaker_reports_nonzero_exit(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeSpeakerRunner(returncode=2, stderr="device busy")
    adapter = LinuxAlsaSpeakerAdapter(runner=runner)

    result = adapter.play_wav(wav_path)

    assert result.success is False
    assert result.status == ALSA_SPEAKER_STATUS_PLAYBACK_FAILED
    assert result.data["process"]["stderr_preview"] == "device busy"
