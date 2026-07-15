from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from memory import DEFAULT_OWNER_PROFILE_PATH, resolve_owner_profile_path  # noqa: E402
from scripts.manual_verify_owner_memory import run_owner_memory_text  # noqa: E402


STEPS = (
    ("Remember that my birthday is June 8.", "I will remember that your birthday is June 8.", "created"),
    ("When is my birthday?", "Your birthday is June 8.", "recalled"),
    ("Remember that I live in Madrid.", "I will remember that you live in Madrid.", "created"),
    ("Where do I live?", "You live in Madrid.", "recalled"),
    ("Remember that my favorite game is EVE Online.", "I will remember that your favorite game is EVE Online.", "created"),
    ("Change my favorite game to StarCraft.", "I updated your favorite game from EVE Online to StarCraft.", "updated"),
    ("What game do I like?", "Your favorite game is StarCraft.", "recalled"),
    ("What do you remember about me?", "", "listed"),
    ("Forget my city.", "Your saved city is Madrid. Should I delete that fact?", "confirmation_required"),
    ("Yes, delete it.", "I deleted your city fact.", "deleted_keyed_fact"),
    ("Where do I live?", "I do not know your city yet.", "missing"),
    ("When is my birthday?", "Your birthday is June 8.", "recalled"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify general central owner memory across fresh processes.")
    parser.add_argument("--profile", default="", help="isolated owner-profile path")
    parser.add_argument("--reset", action="store_true", help="remove the isolated profile before verification")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--child-command", default="", help=argparse.SUPPRESS)
    return parser


def run_verification(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    args = build_parser().parse_args(argv)
    if args.child_command:
        if not args.profile:
            output_func(json.dumps({"success": False, "error": "missing_profile"}))
            return 2
        result = run_owner_memory_text(args.child_command, Path(args.profile))
        output_func(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["success"] else 2

    if args.profile:
        path = resolve_owner_profile_path(args.profile)
        return _run_sequence(path, args.reset, args.verbose, output_func, runner)
    with TemporaryDirectory(prefix="ares_general_owner_memory_") as directory:
        return _run_sequence(Path(directory) / "owner_profile.json", True, args.verbose, output_func, runner)


def _run_sequence(
    profile_path: Path,
    reset: bool,
    verbose: bool,
    output_func: Callable[[str], None],
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> int:
    profile_path = resolve_owner_profile_path(profile_path)
    if reset:
        if profile_path == DEFAULT_OWNER_PROFILE_PATH:
            output_func("FAIL: --reset refuses to delete the canonical production profile.")
            return 2
        profile_path.unlink(missing_ok=True)

    for index, (command, expected, expected_status) in enumerate(STEPS, start=1):
        completed = runner(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--profile",
                str(profile_path),
                "--child-command",
                command,
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            output_func(f"FAIL step {index}: child exited {completed.returncode}: {completed.stderr.strip()}")
            return 1
        try:
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            output_func(f"FAIL step {index}: child returned invalid JSON.")
            return 1
        if payload.get("selected_skill") != "owner_memory" or payload.get("storage_status") != expected_status:
            output_func(f"FAIL step {index}: command did not use central owner memory: {payload!r}")
            return 1
        if expected and payload.get("response") != expected:
            output_func(f"FAIL step {index}: expected {expected!r}, got {payload.get('response')!r}.")
            return 1
        if not expected and not str(payload.get("response") or "").startswith("I remember that "):
            output_func(f"FAIL step {index}: list response was not produced: {payload!r}")
            return 1
        if verbose:
            output_func(f"PASS step {index}: {expected_status} in a fresh process.")

    forbidden = {"voice_owner_profile.json", "speech_memory.json", "voice_facts.json", "microphone_memory.json"}
    created_names = {item.name for item in profile_path.parent.rglob("*.json")}
    if created_names & forbidden:
        output_func("FAIL: a voice-specific owner-memory file was created.")
        return 1
    output_func("PASS: general owner facts persisted through the central Brain path across fresh processes.")
    output_func(f"PASS: isolated profile: {profile_path}")
    output_func("PASS: no voice-specific owner-memory file was created.")
    return 0


def main() -> None:
    raise SystemExit(run_verification())


if __name__ == "__main__":
    main()
