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
    capture_levels, automatic_gain = _parse_capture_mixer_state(result.stdout)
    diagnostics["mixer_inspection_available"] = True
    diagnostics["mixer_capture_levels"] = capture_levels
    diagnostics["automatic_gain_controls"] = automatic_gain
    return diagnostics


def _alsa_card_identifier(device: str) -> str:
    clean = str(device or "")
    numeric = re.fullmatch(r"(?:plug)?hw:(\d+),\d+", clean)
    if numeric:
        return numeric.group(1)
    named = re.search(r"(?:^|,)CARD=([A-Za-z0-9_.-]+)", clean)
    return named.group(1) if named else ""


def _parse_capture_mixer_state(stdout: str) -> tuple[list[str], list[str]]:
    capture_levels: list[str] = []
    automatic_gain: list[str] = []
    for raw_line in str(stdout or "").splitlines():
        line = " ".join(raw_line.split())
        lowered = line.casefold()
        if not line:
            continue
        if "capture" in lowered and "%" in line and len(capture_levels) < 8:
            capture_levels.append(line[:160])
        if (
            "automatic gain" in lowered
            or re.search(r"\bagc\b", lowered)
            or "auto gain" in lowered
        ) and len(automatic_gain) < 8:
            automatic_gain.append(line[:160])
    return capture_levels, automatic_gain
