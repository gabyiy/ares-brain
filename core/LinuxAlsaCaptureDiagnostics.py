from __future__ import annotations

import re
from typing import Any, Dict, Optional

from core.LinuxAlsaMicrophone import SafeSubprocessRunner
from core.WavAudio import (
    CANONICAL_CHANNELS,
    CANONICAL_SAMPLE_RATE_HZ,
    CANONICAL_SAMPLE_WIDTH_BYTES,
)


def inspect_linux_alsa_capture(
    device: str,
    *,
    runner: Optional[Any] = None,
) -> Dict[str, Any]:
    """Read bounded mixer diagnostics without changing any ALSA setting."""

    process_runner = runner or SafeSubprocessRunner()
    diagnostics: Dict[str, Any] = {
        "success": True,
        "status": "capture_hardware_inspected",
        "capture_device": str(device or ""),
        "sample_rate_hz": CANONICAL_SAMPLE_RATE_HZ,
        "channels": CANONICAL_CHANNELS,
        "sample_width_bytes": CANONICAL_SAMPLE_WIDTH_BYTES,
        "mixer_capture_levels": [],
        "input_gain_controls": [],
        "automatic_gain_controls": [],
        "mixer_inspection_available": False,
        "settings_modified": False,
        "safe": True,
    }
    amixer_path = process_runner.which("amixer")
    if not amixer_path:
        diagnostics["status"] = "amixer_unavailable"
        return diagnostics
    card = _alsa_card_identifier(device)
    args = [amixer_path]
    if card:
        args.extend(["-c", card])
    args.append("scontents")
    result = process_runner.run(args, 5.0)
    diagnostics["mixer_command"] = list(args)
    diagnostics["subprocess_shell"] = False
    diagnostics["mixer_exit_status"] = int(result.returncode)
    if result.returncode != 0:
        diagnostics["success"] = False
        diagnostics["status"] = "amixer_inspection_failed"
        diagnostics["error_message"] = str(
            result.error_message or result.stderr or "amixer failed"
        )[:200]
        return diagnostics
    capture_levels, input_gain, automatic_gain = _parse_capture_mixer_state(
        result.stdout
    )
    diagnostics["mixer_inspection_available"] = True
    diagnostics["mixer_capture_levels"] = capture_levels
    diagnostics["input_gain_controls"] = input_gain
    diagnostics["automatic_gain_controls"] = automatic_gain
    diagnostic_lines = capture_levels + input_gain
    diagnostics["extreme_gain_warning"] = any(
        _line_has_extreme_gain(line) for line in diagnostic_lines
    )
    return diagnostics


def _alsa_card_identifier(device: str) -> str:
    clean = str(device or "")
    numeric = re.fullmatch(r"(?:plug)?hw:(\d+),\d+", clean)
    if numeric:
        return numeric.group(1)
    named = re.search(r"(?:^|,)CARD=([A-Za-z0-9_.-]+)", clean)
    return named.group(1) if named else ""


def _parse_capture_mixer_state(
    stdout: str,
) -> tuple[list[str], list[str], list[str]]:
    capture_levels: list[str] = []
    input_gain: list[str] = []
    automatic_gain: list[str] = []
    current_control = ""
    for raw_line in str(stdout or "").splitlines():
        line = " ".join(raw_line.split())
        lowered = line.casefold()
        if not line:
            continue
        if lowered.startswith("simple mixer control"):
            current_control = line[:100]
        if "capture" in lowered and "%" in line and len(capture_levels) < 8:
            capture_levels.append(line[:160])
        if (
            any(
                label in f"{current_control} {line}".casefold()
                for label in ("mic boost", "microphone boost", "input gain", "capture gain")
            )
            and ("%" in line or "[on]" in lowered or "[off]" in lowered)
            and len(input_gain) < 8
        ):
            input_gain.append(f"{current_control} | {line}"[:160])
        if (
            "automatic gain" in lowered
            or re.search(r"\bagc\b", lowered)
            or "auto gain" in lowered
        ) and len(automatic_gain) < 8:
            automatic_gain.append(line[:160])
    return capture_levels, input_gain, automatic_gain


def _line_has_extreme_gain(line: str) -> bool:
    percentages = [int(value) for value in re.findall(r"\[(\d{1,3})%\]", line)]
    if any(value >= 95 for value in percentages):
        return True
    lowered = str(line or "").casefold()
    return "boost" in lowered and any(
        marker in lowered for marker in ("[on]", "100%", "max")
    )
