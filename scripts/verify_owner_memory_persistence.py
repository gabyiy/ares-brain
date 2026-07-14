from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
MANUAL_SCRIPT = REPO_ROOT / "scripts" / "manual_verify_owner_memory.py"

EXPECTED_STEPS = (
    (
        "remember that my favorite color is blue",
        "I will remember that your favorite color is blue.",
        "created",
    ),
    (
        "what is my favorite color",
        "Your favorite color is blue.",
        "recalled",
    ),
    (
        "remember that my favorite color is red",
        "I updated your favorite color to red.",
        "updated",
    ),
    (
        "what is my favorite color",
        "Your favorite color is red.",
        "recalled",
    ),
    (
        "forget my favorite color",
        "I forgot your favorite color.",
        "forgotten",
    ),
    (
        "what is my favorite color",
        "I do not know your favorite color yet.",
        "missing",
    ),
)

ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify explicit owner-memory persistence across fresh Python processes."
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def verify_persistence(
    *,
    output_func: Callable[[str], None] = print,
    runner: ProcessRunner = subprocess.run,
    verbose: bool = False,
) -> bool:
    with TemporaryDirectory(prefix="ares_owner_memory_") as directory:
        profile_path = Path(directory) / "owner_profile.json"
        for index, (text, expected_response, expected_status) in enumerate(
            EXPECTED_STEPS,
            start=1,
        ):
            completed = runner(
                [
                    sys.executable,
                    str(MANUAL_SCRIPT),
                    "--profile-path",
                    str(profile_path),
                    "--text",
                    text,
                    "--json",
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                output_func(
                    f"FAIL step {index}: child process exited {completed.returncode}."
                )
                if completed.stderr:
                    output_func(completed.stderr.strip())
                return False
            try:
                payload = json.loads(completed.stdout.strip().splitlines()[-1])
            except (IndexError, json.JSONDecodeError):
                output_func(f"FAIL step {index}: child process returned invalid JSON.")
                return False
            if (
                payload.get("response") != expected_response
                or payload.get("storage_status") != expected_status
                or payload.get("selected_skill") != "owner_memory"
            ):
                output_func(
                    f"FAIL step {index}: expected {expected_status!r} / "
                    f"{expected_response!r}, got {payload!r}."
                )
                return False
            if verbose:
                output_func(
                    f"PASS step {index}: {expected_status} in a fresh process."
                )

        if not profile_path.exists():
            output_func("FAIL: profile was not persisted during the process sequence.")
            return False
        try:
            stored = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            output_func("FAIL: final profile is not valid UTF-8 JSON.")
            return False
        if stored.get("schema_name") != "ares.owner_profile":
            output_func("FAIL: final profile has the wrong schema name.")
            return False
        if dict(stored.get("data") or {}).get("facts") != {}:
            output_func("FAIL: forgotten fact remained in the final profile.")
            return False

    output_func("PASS: explicit owner memory persisted across separate processes.")
    output_func("PASS: update, forget, and missing recall behavior matched exactly.")
    output_func("PASS: verification used an isolated temporary profile only.")
    return True


def run_verification(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    return 0 if verify_persistence(output_func=output_func, verbose=args.verbose) else 1


def main() -> None:
    raise SystemExit(run_verification())


if __name__ == "__main__":
    main()
