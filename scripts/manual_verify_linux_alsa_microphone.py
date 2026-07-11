from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import LinuxAlsaMicrophoneAdapter  # noqa: E402


WARNING = (
    "WARNING: Linux ALSA microphone verification is local audio capture only. "
    "It does not run speech-to-text, wake word, GPT, internet, speaker output, "
    "or background listening."
)


def _default_output_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return REPO_ROOT / "data" / "manual_alsa_samples" / f"ares_alsa_sample_{timestamp}.wav"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manually verify Linux ALSA microphone capture through ARES Voice City adapter."
    )
    parser.add_argument("--device", default="", help="Optional ALSA device, for example hw:1,0")
    parser.add_argument("--seconds", type=int, default=3, help="Seconds to record when --record is used")
    parser.add_argument("--timeout", type=float, default=None, help="Optional arecord timeout in seconds")
    parser.add_argument(
        "--output",
        default="",
        help="Output WAV path for --record. Defaults to data/manual_alsa_samples/ares_alsa_sample_<timestamp>.wav",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Explicitly record a short WAV sample. Without this flag no recording starts.",
    )
    return parser


def run_manual_verification(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    adapter_factory=LinuxAlsaMicrophoneAdapter,
) -> int:
    args = build_parser().parse_args(list(argv or []))
    output_func(WARNING)
    output_func("Checking arecord and ALSA capture devices...")

    adapter = adapter_factory(
        device=args.device or None,
        record_seconds=args.seconds,
        timeout_seconds=args.timeout,
    )

    devices = adapter.list_capture_devices()
    output_func(f"Device list status: {devices.status}")
    if devices.data.get("devices"):
        for device in devices.data["devices"]:
            output_func(
                f"- {device['alsa_device']}: {device['card_name']} / {device['device_name']}"
            )
    else:
        output_func(devices.text or devices.error_message)

    health = adapter.health_check()
    output_func(f"Health status: {health.status}")
    output_func(health.text or health.error_message)

    if not args.record:
        output_func("No recording requested. Re-run with --record to capture a short WAV sample.")
        return 0 if devices.success and health.success else 2

    output_path = Path(args.output).expanduser() if args.output else _default_output_path()
    output_func(f"Recording explicitly requested. Output: {output_path}")
    record = adapter.record_wav(
        output_path=output_path,
        seconds=args.seconds,
        timeout_seconds=args.timeout,
        overwrite=False,
    )
    output_func(f"Recording status: {record.status}")
    output_func(record.text or record.error_message)
    if record.success:
        output_func(f"WAV sample written: {record.data.get('wav_path', str(output_path))}")
        return 0
    return 3


def main() -> None:
    raise SystemExit(run_manual_verification(sys.argv[1:]))


if __name__ == "__main__":
    main()
