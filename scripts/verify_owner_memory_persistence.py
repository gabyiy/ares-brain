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
        "Your saved favorite color is red. Should I delete that fact?",
        "confirmation_required",
    ),
    (
        "yes delete it",
        "I deleted your favorite-color fact.",
        "deleted_keyed_fact",
    ),
    (
        "what is my favorite color",
        "I do not know your favorite color yet.",
        "missing",
    ),
)

ROUTING_PRIORITY_CHECK = (
    "remember that modified white color is blue",
    "I will remember that your favorite color is blue.",
    "created",
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
        routing_profile_path = Path(directory) / "routing_priority_owner_profile.json"
        routing_text, routing_response, routing_status = ROUTING_PRIORITY_CHECK
        priority_result = _run_child(
            runner,
            routing_profile_path,
            routing_text,
        )
        if isinstance(priority_result, str):
            output_func(f"FAIL routing priority: {priority_result}")
            return False
        priority_payload, priority_stderr = priority_result
        if (
            priority_payload.get("response") != routing_response
            or priority_payload.get("storage_status") != routing_status
            or priority_payload.get("parsed_intent") != "owner_memory"
            or priority_payload.get("selected_skill") != "owner_memory"
        ):
            output_func(
                "FAIL routing priority: imperfect explicit owner-memory speech "
                f"did not select owner_memory: {priority_payload!r}."
            )
            if priority_stderr:
                output_func(priority_stderr)
            return False
        if verbose:
            output_func(
                "PASS routing priority: imperfect explicit speech selected owner_memory."
            )

        for index, (text, expected_response, expected_status) in enumerate(
            EXPECTED_STEPS,
            start=1,
        ):
            child_result = _run_child(runner, profile_path, text)
            if isinstance(child_result, str):
                output_func(f"FAIL step {index}: {child_result}")
                return False
            payload, _ = child_result
            if (
                payload.get("response") != expected_response
                or payload.get("storage_status") != expected_status
                or payload.get("parsed_intent") != "owner_memory"
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


def _run_child(
    runner: ProcessRunner,
    profile_path: Path,
    text_value: str,
) -> tuple[dict[str, Any], str] | str:
    completed = runner(
        [
            sys.executable,
            str(MANUAL_SCRIPT),
            "--profile-path",
            str(profile_path),
            "--text",
            text_value,
            "--json",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        message = f"child process exited {completed.returncode}."
        if completed.stderr:
            message = f"{message} {completed.stderr.strip()}"
        return message
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return "child process returned invalid JSON."
    return payload, completed.stderr.strip()


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
