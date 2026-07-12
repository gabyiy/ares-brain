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
    parse_aplay_playback_devices,
)


APLAY_DEVICE_LIST = """\
**** List of PLAYBACK Hardware Devices ****
card 2: Device [USB Audio Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
"""


class FakeSpeakerRunner:
    def __init__(
        self,
        available=True,
        returncode=0,
        timed_out=False,
        stdout="",
        stderr="",
    ):
        self.available = available
        self.returncode = returncode
        self.timed_out = timed_out
        self.stdout = stdout
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
            stdout=self.stdout,
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


def test_linux_alsa_speaker_health_check_accepts_available_selected_device():
    runner = FakeSpeakerRunner(stdout=APLAY_DEVICE_LIST)
    adapter = LinuxAlsaSpeakerAdapter(
        device="plughw:CARD=Device,DEV=0",
        runner=runner,
    )

    result = adapter.health_check()

    assert result.success is True
    assert result.status == "healthy"
    assert result.data["device_available"] is True
    assert result.data["selected_device"] == "plughw:CARD=Device,DEV=0"
    assert result.data["process"]["args"] == ["/usr/bin/aplay", "-l"]
    assert result.metadata["audio_hardware_accessed"] is False


def test_linux_alsa_speaker_health_check_rejects_unavailable_selected_device():
    runner = FakeSpeakerRunner(stdout=APLAY_DEVICE_LIST)
    adapter = LinuxAlsaSpeakerAdapter(
        device="plughw:CARD=Missing,DEV=0",
        runner=runner,
    )

    result = adapter.health_check()

    assert result.success is False
    assert result.status == ALSA_SPEAKER_STATUS_INVALID_DEVICE
    assert result.error_message == "alsa_playback_device_not_found"
    assert "plughw:CARD=Device,DEV=0" in result.data["available_devices"]
    assert result.metadata["audio_hardware_accessed"] is False


def test_parse_aplay_playback_devices_returns_numeric_and_card_aliases():
    devices = parse_aplay_playback_devices(APLAY_DEVICE_LIST)

    assert devices[0]["card_id"] == "Device"
    assert devices[0]["alsa_devices"] == [
        "hw:2,0",
        "plughw:2,0",
        "hw:CARD=Device,DEV=0",
        "plughw:CARD=Device,DEV=0",
    ]


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
    assert adapter.playing is False


def test_linux_alsa_speaker_rejects_health_and_playback_while_already_playing(tmp_path):
    wav_path = tmp_path / "sample.wav"
    write_valid_wav(wav_path)
    runner = FakeSpeakerRunner()
    adapter = LinuxAlsaSpeakerAdapter(runner=runner)
    adapter.playing = True

    health = adapter.health_check()
    playback = adapter.play_wav(wav_path)

    assert health.success is False
    assert health.error_message == "speaker_playback_already_active"
    assert playback.success is False
    assert playback.error_message == "speaker_playback_already_active"
    assert runner.calls == []


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
    assert result.data["process"]["stderr"] == "device busy"
