from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import SafeProcessResult, SafeSubprocessRunner  # noqa: E402


WARNING = (
    "WARNING: This helper configures ALSA mixer controls only when --apply is "
    "provided. It does not record audio, play audio, run wake word detection, "
    "start background listening, call GPT, run TTS, or start a conversation loop."
)


@dataclass(frozen=True)
class AlsaMixerCommandPlan:
    commands: List[List[str]]
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AlsaMixerConfigurationResult:
    success: bool
    status: str
    message: str
    commands: List[List[str]] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "commands": [list(command) for command in self.commands],
            "data": dict(self.data),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Disable ALSA microphone playback monitoring while preserving capture."
    )
    parser.add_argument("--card", default="", help="Optional ALSA card index, for example 1.")
    parser.add_argument("--mic-playback-control", default="Mic", help="Mic playback monitor control.")
    parser.add_argument("--capture-control", default="Capture", help="Mic capture control.")
    parser.add_argument("--speaker-control", default="Speaker", help="Speaker playback control.")
    parser.add_argument("--capture-volume", default="80%", help="Capture volume, for example 80%.")
    parser.add_argument("--speaker-volume", default="70%", help="Speaker volume, for example 70%.")
    parser.add_argument("--amixer-command", default="amixer", help="Local amixer executable.")
    parser.add_argument("--apply", action="store_true", help="Actually run amixer commands.")
    return parser


def run_mixer_configuration(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    runner: Optional[SafeSubprocessRunner] = None,
) -> int:
    args = build_parser().parse_args(list(argv or []))
    runner = runner or SafeSubprocessRunner()
    result = configure_monitoring(
        card=args.card,
        mic_playback_control=args.mic_playback_control,
        capture_control=args.capture_control,
        speaker_control=args.speaker_control,
        capture_volume=args.capture_volume,
        speaker_volume=args.speaker_volume,
        amixer_command=args.amixer_command,
        apply=args.apply,
        runner=runner,
        output_func=output_func,
    )
    output_func(f"{'PASS' if result.success else 'FAIL'}: {result.message}")
    for command in result.commands:
        output_func("Command: " + " ".join(command))
    return 0 if result.success else 1


def configure_monitoring(
    card: str = "",
    mic_playback_control: str = "Mic",
    capture_control: str = "Capture",
    speaker_control: str = "Speaker",
    capture_volume: str = "80%",
    speaker_volume: str = "70%",
    amixer_command: str = "amixer",
    apply: bool = False,
    runner: Optional[SafeSubprocessRunner] = None,
    output_func: Callable[[str], None] = print,
) -> AlsaMixerConfigurationResult:
    runner = runner or SafeSubprocessRunner()
    output_func(WARNING)
    amixer_path = runner.which(amixer_command)
    if not amixer_path:
        return AlsaMixerConfigurationResult(
            success=False,
            status="amixer_missing",
            message="Could not locate amixer.",
        )

    plan = build_monitoring_command_plan(
        amixer_path=amixer_path,
        card=card,
        mic_playback_control=mic_playback_control,
        capture_control=capture_control,
        speaker_control=speaker_control,
        capture_volume=capture_volume,
        speaker_volume=speaker_volume,
    )
    if not apply:
        return AlsaMixerConfigurationResult(
            success=True,
            status="dry_run",
            message="Dry run only. Re-run with --apply to configure ALSA mixer controls.",
            commands=plan.commands,
            data=plan.data,
        )

    results: List[Dict[str, Any]] = []
    for command in plan.commands:
        result = runner.run(command, timeout_seconds=10.0)
        results.append(_safe_process_data(result))
        if result.timed_out:
            return AlsaMixerConfigurationResult(
                success=False,
                status="amixer_timeout",
                message="Timed out while configuring ALSA mixer controls.",
                commands=plan.commands,
                data={"processes": results, **plan.data},
            )
        if result.returncode != 0:
            return AlsaMixerConfigurationResult(
                success=False,
                status="amixer_failed",
                message=f"amixer failed with exit code {result.returncode}.",
                commands=plan.commands,
                data={"processes": results, **plan.data},
            )

    return AlsaMixerConfigurationResult(
        success=True,
        status="configured",
        message="ALSA microphone monitoring is muted and capture remains enabled.",
        commands=plan.commands,
        data={"processes": results, **plan.data},
    )


def build_monitoring_command_plan(
    amixer_path: str,
    card: str = "",
    mic_playback_control: str = "Mic",
    capture_control: str = "Capture",
    speaker_control: str = "Speaker",
    capture_volume: str = "80%",
    speaker_volume: str = "70%",
) -> AlsaMixerCommandPlan:
    base = [amixer_path]
    card_value = str(card or "").strip()
    if card_value:
        if not card_value.isdigit():
            raise ValueError("ALSA card must be a numeric index")
        base.extend(["-c", card_value])

    commands = [
        [*base, "sset", str(mic_playback_control), "0%", "mute"],
        [*base, "sset", str(capture_control), str(capture_volume), "cap"],
        [*base, "sset", str(speaker_control), str(speaker_volume), "unmute"],
    ]
    return AlsaMixerCommandPlan(
        commands=commands,
        data={
            "mic_playback_monitoring": "muted",
            "mic_capture": "enabled",
            "mic_capture_volume": str(capture_volume),
            "speaker_playback": "enabled",
            "speaker_playback_volume": str(speaker_volume),
        },
    )


def _safe_process_data(result: SafeProcessResult) -> Dict[str, Any]:
    return {
        "args": list(result.args),
        "command": " ".join(str(arg) for arg in result.args),
        "returncode": result.returncode,
        "stdout_preview": str(result.stdout or "")[:500],
        "stderr_preview": str(result.stderr or "")[:500],
        "timed_out": result.timed_out,
        "error_message": result.error_message,
    }


def main() -> None:
    raise SystemExit(run_mixer_configuration(sys.argv[1:]))


if __name__ == "__main__":
    main()
