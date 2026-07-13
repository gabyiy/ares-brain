from core import MicrophoneResult, VoiceActivityCaptureResultV1
from scripts import manual_verify_voice_activity_capture as manual_vad


class FakeAdapter:
    def __init__(self, health_success=True, capture_success=True):
        self.health_success = health_success
        self.capture_success = capture_success
        self.calls = []

    def list_capture_devices(self):
        self.calls.append("list")
        return MicrophoneResult(True, "devices", data={"devices": []})

    def health_check(self):
        self.calls.append("health")
        return MicrophoneResult(
            self.health_success,
            "healthy" if self.health_success else "device_error",
            error_message="device_unavailable" if not self.health_success else "",
        )

    def start(self):
        self.calls.append("start")
        return MicrophoneResult(True, "started")

    def record_until_silence(self, output_path, **kwargs):
        self.calls.append(("capture", str(output_path), kwargs))
        return VoiceActivityCaptureResultV1(
            success=self.capture_success,
            status="completed_after_silence" if self.capture_success else "no_speech_timeout",
            wav_path=str(output_path) if self.capture_success else "",
            speech_detected=self.capture_success,
            duration_seconds=0.4 if self.capture_success else 0.0,
            speech_duration_seconds=0.3 if self.capture_success else 0.0,
            peak_amplitude=1000,
            ambient_rms=35.0,
            speech_rms=600.0,
            stop_reason="completed_after_silence" if self.capture_success else "no_speech_timeout",
            error_message="" if self.capture_success else "speech_not_detected_before_timeout",
            data={
                "process": {
                    "args": ["/usr/bin/arecord", "-t", "raw", "-"],
                    "shell": False,
                }
            },
        )

    def cancel_current(self):
        self.calls.append("cancel")

    def stop(self):
        self.calls.append("stop")
        return MicrophoneResult(True, "stopped")


def test_manual_vad_success_prints_calibration_metrics(tmp_path):
    output = []
    adapter = FakeAdapter()

    exit_code = manual_vad.run_manual_verification(
        [
            "--output", str(tmp_path / "capture.wav"),
            "--speech-start-rms", "220",
            "--silence-rms", "110",
        ],
        output_func=output.append,
        adapter=adapter,
    )

    assert exit_code == 0
    assert adapter.calls[0:3] == ["list", "health", "start"]
    assert adapter.calls[-1] == "stop"
    assert any("Ambient RMS: 35.000" in line for line in output)
    assert any("Speech RMS: 600.000" in line for line in output)
    assert any("start=220.0, silence=110.0" in line for line in output)
    assert output[-1] == "PASS"


def test_manual_vad_health_failure_never_starts_capture():
    adapter = FakeAdapter(health_success=False)
    output = []

    exit_code = manual_vad.run_manual_verification(
        [], output_func=output.append, adapter=adapter
    )

    assert exit_code == 2
    assert adapter.calls == ["list", "health"]
    assert any("FAIL" in line for line in output)


def test_manual_vad_no_speech_returns_failure_exit_code():
    adapter = FakeAdapter(capture_success=False)
    output = []

    exit_code = manual_vad.run_manual_verification(
        [], output_func=output.append, adapter=adapter
    )

    assert exit_code == 2
    assert output[-1] == "FAIL"


def test_manual_vad_import_does_not_access_hardware():
    assert callable(manual_vad.main)
    assert callable(manual_vad.run_manual_verification)


def test_manual_vad_script_contains_no_subprocess_or_alsa_implementation():
    source = manual_vad.Path(manual_vad.__file__).read_text(encoding="utf-8")

    assert "import subprocess" not in source
    assert "subprocess." not in source
    assert "shell=True" not in source
    assert "Popen(" not in source
