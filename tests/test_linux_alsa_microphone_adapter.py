from pathlib import Path
import wave

from core import (
    ALSA_STATUS_ARECORD_MISSING,
    ALSA_STATUS_INVALID_DEVICE,
    ALSA_STATUS_NO_CAPTURE_DEVICE,
    ALSA_STATUS_OUTPUT_EMPTY,
    ALSA_STATUS_OUTPUT_MISSING,
    ALSA_STATUS_RECORDING_FAILED,
    ALSA_STATUS_RECORDING_TIMEOUT,
    LinuxAlsaMicrophoneAdapter,
    SafeProcessResult,
    parse_arecord_capture_devices,
    resolve_alsa_capture_device,
)
from scripts import manual_verify_linux_alsa_microphone as manual_alsa


ARECORD_DEVICES = """**** List of CAPTURE Hardware Devices ****
card 1: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: Headset [USB Headset], device 0: Headset Mic [Headset Mic]
  Subdevices: 1/1
"""


class FakeArecordRunner:
    def __init__(
        self,
        available=True,
        devices_stdout=ARECORD_DEVICES,
        list_returncode=0,
        record_returncode=0,
        record_stderr="",
        timed_out=False,
        writer=None,
    ):
        self.available = available
        self.devices_stdout = devices_stdout
        self.list_returncode = list_returncode
        self.record_returncode = record_returncode
        self.record_stderr = record_stderr
        self.timed_out = timed_out
        self.writer = writer
        self.calls = []

    def which(self, executable):
        return "/usr/bin/arecord" if self.available else None

    def run(self, args, timeout_seconds):
        safe_args = list(args)
        self.calls.append({"args": safe_args, "timeout_seconds": timeout_seconds})
        if safe_args[-1] == "-l":
            return SafeProcessResult(
                args=safe_args,
                returncode=self.list_returncode,
                stdout=self.devices_stdout,
                stderr="list failed" if self.list_returncode else "",
            )
        if self.timed_out:
            return SafeProcessResult(
                args=safe_args,
                returncode=-1,
                timed_out=True,
                error_message="process_timeout",
            )
        if self.writer:
            self.writer(safe_args[-1])
        return SafeProcessResult(
            args=safe_args,
            returncode=self.record_returncode,
            stderr=self.record_stderr,
        )


def write_valid_wav(path, frames=b"\x00\x01" * 160):
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(frames)


def write_44100_wav(path):
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(44100)
        wav_file.writeframes(b"\x10\x20" * 4410)


def write_empty_file(path):
    with open(path, "wb") as handle:
        handle.write(b"")


def test_parse_arecord_capture_devices_returns_structured_devices():
    devices = parse_arecord_capture_devices(ARECORD_DEVICES)

    assert devices == [
        {
            "card_index": 1,
            "card_id": "Device",
            "card_name": "USB PnP Sound Device",
            "device_index": 0,
            "device_id": "USB Audio",
            "device_name": "USB Audio",
            "alsa_device": "hw:1,0",
            "raw_line": "card 1: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]",
        },
        {
            "card_index": 2,
            "card_id": "Headset",
            "card_name": "USB Headset",
            "device_index": 0,
            "device_id": "Headset Mic",
            "device_name": "Headset Mic",
            "alsa_device": "hw:2,0",
            "raw_line": "card 2: Headset [USB Headset], device 0: Headset Mic [Headset Mic]",
        },
    ]


def test_capture_device_resolution_only_converts_raw_numeric_hardware_ids():
    assert resolve_alsa_capture_device("hw:2,0", require_conversion=True) == "plughw:2,0"
    assert resolve_alsa_capture_device("plughw:2,0", require_conversion=True) == "plughw:2,0"
    assert resolve_alsa_capture_device("default", require_conversion=True) == "default"
    assert resolve_alsa_capture_device("hw:2,0", require_conversion=False) == "hw:2,0"


def test_linux_alsa_missing_arecord_fails_safely():
    adapter = LinuxAlsaMicrophoneAdapter(runner=FakeArecordRunner(available=False))

    result = adapter.list_capture_devices()

    assert result.success is False
    assert result.status == ALSA_STATUS_ARECORD_MISSING
    assert result.error_message == "arecord_missing"
    assert adapter.audio_hardware_accessed is False


def test_linux_alsa_no_capture_devices_fails_safely():
    adapter = LinuxAlsaMicrophoneAdapter(runner=FakeArecordRunner(devices_stdout=""))

    result = adapter.health_check()

    assert result.success is False
    assert result.status == ALSA_STATUS_NO_CAPTURE_DEVICE
    assert result.error_message == "no_capture_device"


def test_linux_alsa_invalid_selected_device_fails_health_check():
    adapter = LinuxAlsaMicrophoneAdapter(device="hw:9,9", runner=FakeArecordRunner())

    result = adapter.health_check()

    assert result.success is False
    assert result.status == ALSA_STATUS_INVALID_DEVICE
    assert result.error_message == "alsa_device_not_found"
    assert result.data["selected_device"] == "hw:9,9"


def test_linux_alsa_plughw_device_maps_to_listed_hardware_for_health_check():
    adapter = LinuxAlsaMicrophoneAdapter(device="plughw:2,0", runner=FakeArecordRunner())

    result = adapter.health_check()

    assert result.success is True
    assert result.data["selected_device"] == "plughw:2,0"


def test_linux_alsa_record_wav_success_uses_argument_list_and_validates_output(tmp_path):
    runner = FakeArecordRunner(writer=write_valid_wav)
    output = tmp_path / "sample.wav"
    adapter = LinuxAlsaMicrophoneAdapter(device="hw:1,0", runner=runner)

    result = adapter.record_wav(output, seconds=2, timeout_seconds=8)

    assert result.success is True
    assert result.status == "recorded"
    assert result.chunk is not None
    assert result.chunk.byte_count > 0
    assert result.data["wav"]["status"] == "valid_wav"
    assert output.exists()
    record_call = runner.calls[-1]
    assert isinstance(record_call["args"], list)
    assert "-D" in record_call["args"]
    assert "hw:1,0" in record_call["args"]
    assert "shell=True" not in " ".join(record_call["args"])
    assert result.metadata["subprocess_shell"] is False
    assert result.metadata["speech_engine_accessed"] is False


def test_linux_alsa_read_chunk_records_after_start(tmp_path):
    runner = FakeArecordRunner(writer=write_valid_wav)
    adapter = LinuxAlsaMicrophoneAdapter(runner=runner, record_seconds=1)

    start = adapter.start()
    result = adapter.read_chunk(timeout_seconds=4)

    assert start.success is True
    assert result.success is True
    assert result.status == "recorded"
    assert result.chunk is not None
    assert adapter.read_count == 1


def test_linux_alsa_uses_actual_44100_header_and_outputs_canonical_wav(tmp_path):
    runner = FakeArecordRunner(writer=write_44100_wav)
    output = tmp_path / "normalized.wav"
    adapter = LinuxAlsaMicrophoneAdapter(device="hw:2,0", runner=runner)

    result = adapter.record_wav(
        output,
        seconds=1,
        diagnostic_audio=True,
    )

    assert result.success is True
    assert result.data["requested_sample_rate_hz"] == 16000
    assert result.data["actual_sample_rate_hz"] == 44100
    assert result.data["normalized_sample_rate_hz"] == 16000
    assert result.chunk.sample_rate_hz == 16000
    assert result.data["normalization"]["data"]["byte_reinterpretation"] is False
    assert result.data["raw_wav_path"] != result.data["normalized_wav_path"]
    assert result.data["final_whisper_input_path"] == str(output)
    assert Path(result.data["raw_wav_path"]).exists()
    record_args = runner.calls[-1]["args"]
    assert record_args[record_args.index("-r") + 1] == "16000"
    with wave.open(str(output), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2


def test_linux_alsa_read_chunk_requires_start():
    adapter = LinuxAlsaMicrophoneAdapter(runner=FakeArecordRunner())

    result = adapter.read_chunk()

    assert result.success is False
    assert result.status == "not_started"
    assert result.error_message == "microphone_not_started"


def test_microphone_adapter_never_enables_monitoring_or_speaker_playback():
    source = Path("core/LinuxAlsaMicrophone.py").read_text(encoding="utf-8").lower()

    assert "aplay" not in source
    assert "amixer" not in source
    assert "mic playback" not in source


def test_linux_alsa_recording_timeout_fails_safely(tmp_path):
    adapter = LinuxAlsaMicrophoneAdapter(runner=FakeArecordRunner(timed_out=True))

    result = adapter.record_wav(tmp_path / "timeout.wav")

    assert result.success is False
    assert result.status == ALSA_STATUS_RECORDING_TIMEOUT
    assert result.error_message == "arecord_recording_timeout"


def test_linux_alsa_recording_nonzero_exit_fails_safely(tmp_path):
    adapter = LinuxAlsaMicrophoneAdapter(
        runner=FakeArecordRunner(record_returncode=1, record_stderr="input/output error")
    )

    result = adapter.record_wav(tmp_path / "failed.wav")

    assert result.success is False
    assert result.status == ALSA_STATUS_RECORDING_FAILED
    assert result.error_message == "arecord_exit_1"


def test_linux_alsa_invalid_device_process_error_is_classified(tmp_path):
    adapter = LinuxAlsaMicrophoneAdapter(
        runner=FakeArecordRunner(record_returncode=1, record_stderr="Unknown PCM hw:9,9")
    )

    result = adapter.record_wav(tmp_path / "invalid.wav", device="hw:9,9")

    assert result.success is False
    assert result.status == ALSA_STATUS_INVALID_DEVICE
    assert result.error_message == "arecord_exit_1"


def test_linux_alsa_missing_wav_output_fails_safely(tmp_path):
    adapter = LinuxAlsaMicrophoneAdapter(runner=FakeArecordRunner())

    result = adapter.record_wav(tmp_path / "missing.wav")

    assert result.success is False
    assert result.status == ALSA_STATUS_OUTPUT_MISSING
    assert result.error_message == "wav_output_missing"


def test_linux_alsa_empty_wav_output_fails_safely(tmp_path):
    adapter = LinuxAlsaMicrophoneAdapter(runner=FakeArecordRunner(writer=write_empty_file))

    result = adapter.record_wav(tmp_path / "empty.wav")

    assert result.success is False
    assert result.status == ALSA_STATUS_OUTPUT_EMPTY
    assert result.error_message == "wav_output_empty"


def test_linux_alsa_rejects_unsafe_device_identifier(tmp_path):
    adapter = LinuxAlsaMicrophoneAdapter(runner=FakeArecordRunner())

    result = adapter.record_wav(tmp_path / "unsafe.wav", device="hw:1,0;rm")

    assert result.success is False
    assert result.status == ALSA_STATUS_INVALID_DEVICE
    assert result.error_message == "invalid ALSA device identifier"


def test_linux_alsa_status_and_capabilities_are_structured():
    adapter = LinuxAlsaMicrophoneAdapter(device="hw:1,0", runner=FakeArecordRunner())

    status = adapter.get_status()
    capabilities = adapter.get_capabilities()

    assert status.success is True
    assert status.data["arecord_available"] is True
    assert status.data["selected_device"] == "hw:1,0"
    assert status.data["stt"] == "not_configured"
    assert capabilities.success is True
    assert capabilities.data["writes_wav_file"] is True
    assert capabilities.data["background_listening"] == "disabled"
    assert capabilities.data["stt"] == "not_configured"


def test_manual_linux_alsa_script_lists_devices_without_recording():
    outputs = []
    runner = FakeArecordRunner(writer=write_valid_wav)

    def factory(**kwargs):
        return LinuxAlsaMicrophoneAdapter(runner=runner, **kwargs)

    exit_code = manual_alsa.run_manual_verification(
        argv=[],
        output_func=outputs.append,
        adapter_factory=factory,
    )

    assert exit_code == 0
    assert any("No recording requested" in line for line in outputs)
    assert len(runner.calls) == 2
    assert all(call["args"][-1] == "-l" for call in runner.calls)


def test_manual_linux_alsa_script_records_only_with_explicit_flag(tmp_path):
    outputs = []
    runner = FakeArecordRunner(writer=write_valid_wav)

    def factory(**kwargs):
        return LinuxAlsaMicrophoneAdapter(runner=runner, **kwargs)

    output_path = tmp_path / "manual.wav"
    exit_code = manual_alsa.run_manual_verification(
        argv=["--record", "--seconds", "1", "--output", str(output_path)],
        output_func=outputs.append,
        adapter_factory=factory,
    )

    assert exit_code == 0
    assert output_path.exists()
    assert any("Recording explicitly requested" in line for line in outputs)
    record_targets = [call["args"][-1] for call in runner.calls if call["args"][-1] != "-l"]
    assert len(record_targets) == 1
    assert ".raw." in record_targets[0]
    assert Path(record_targets[0]).exists() is False
