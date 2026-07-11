from core import SafeProcessResult
from scripts import configure_linux_alsa_monitoring as monitoring


class FakeAmixerRunner:
    def __init__(self, available=True, returncode=0):
        self.available = available
        self.returncode = returncode
        self.calls = []

    def which(self, executable):
        return "/usr/bin/amixer" if self.available else None

    def run(self, args, timeout_seconds):
        safe_args = list(args)
        self.calls.append({"args": safe_args, "timeout_seconds": timeout_seconds})
        return SafeProcessResult(args=safe_args, returncode=self.returncode)


def test_monitoring_command_plan_mutes_mic_playback_but_keeps_capture_enabled():
    plan = monitoring.build_monitoring_command_plan(
        amixer_path="/usr/bin/amixer",
        card="1",
        capture_volume="80%",
        speaker_volume="70%",
    )

    assert plan.commands[0] == ["/usr/bin/amixer", "-c", "1", "sset", "Mic", "0%", "mute"]
    assert plan.commands[1] == ["/usr/bin/amixer", "-c", "1", "sset", "Capture", "80%", "cap"]
    assert plan.commands[2] == [
        "/usr/bin/amixer",
        "-c",
        "1",
        "sset",
        "Speaker",
        "70%",
        "unmute",
    ]
    assert plan.data["mic_playback_monitoring"] == "muted"
    assert plan.data["mic_capture"] == "enabled"


def test_monitoring_helper_dry_run_does_not_execute_amixer():
    runner = FakeAmixerRunner()

    result = monitoring.configure_monitoring(
        card="1",
        apply=False,
        runner=runner,
        output_func=lambda _line: None,
    )

    assert result.success is True
    assert result.status == "dry_run"
    assert runner.calls == []


def test_monitoring_helper_apply_executes_separate_capture_and_playback_controls():
    runner = FakeAmixerRunner()

    result = monitoring.configure_monitoring(
        card="1",
        apply=True,
        runner=runner,
        output_func=lambda _line: None,
    )

    assert result.success is True
    assert result.status == "configured"
    assert len(runner.calls) == 3
    assert runner.calls[0]["args"][-2:] == ["0%", "mute"]
    assert runner.calls[1]["args"][-2:] == ["80%", "cap"]
    assert runner.calls[2]["args"][-2:] == ["70%", "unmute"]


def test_monitoring_helper_missing_amixer_fails_safely():
    result = monitoring.configure_monitoring(
        apply=True,
        runner=FakeAmixerRunner(available=False),
        output_func=lambda _line: None,
    )

    assert result.success is False
    assert result.status == "amixer_missing"


def test_monitoring_helper_amixer_failure_is_reported():
    result = monitoring.configure_monitoring(
        apply=True,
        runner=FakeAmixerRunner(returncode=1),
        output_func=lambda _line: None,
    )

    assert result.success is False
    assert result.status == "amixer_failed"
    assert result.data["processes"][0]["returncode"] == 1


def test_monitoring_script_entrypoint_is_import_safe():
    assert callable(monitoring.run_mixer_configuration)
