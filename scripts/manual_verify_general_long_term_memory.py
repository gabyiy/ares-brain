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

from memory import DEFAULT_OWNER_PROFILE_PATH, OwnerMemoryService, resolve_owner_profile_path  # noqa: E402
from scripts.manual_verify_owner_memory import run_owner_memory_text  # noqa: E402


STEPS = (
    {
        "text": "Remember in your locked term memory that I love going to the gym",
        "skill": "owner_memory",
        "status": "created",
        "response": "I will remember that you love going to the gym.",
        "action": "save",
        "memory_type": "preference",
        "fact_text": "I love going to the gym",
        "normalized_trigger": "remember longterm that",
    },
    {
        "text": "Remembering a long term memory that I like video games",
        "skill": "owner_memory",
        "status": "created",
        "response": "I will remember that you like video games.",
        "action": "save",
        "memory_type": "preference",
        "fact_text": "I like video games",
        "normalized_trigger": "remember longterm that",
    },
    {
        "text": "What do I like?",
        "skill": "owner_memory",
        "status": "recalled",
        "response": "You like video games and you love going to the gym.",
    },
    {
        "text": "What do I like doing?",
        "skill": "owner_memory",
        "status": "recalled",
        "response": "You like video games and you love going to the gym.",
    },
    {
        "text": "What do you remember about the gym?",
        "skill": "owner_memory",
        "status": "recalled",
        "response": "You told me that you love going to the gym.",
    },
    {
        "text": "What do you remember about video games?",
        "skill": "owner_memory",
        "status": "recalled",
        "response": "You told me that you like video games.",
    },
    {
        "text": "Remember in your locked term memory that I love going to the gym",
        "skill": "owner_memory",
        "status": "duplicate",
        "response": "I already remember that you love going to the gym.",
        "memory_type": "preference",
        "fact_text": "I love going to the gym",
    },
    {
        "text": "Remembering a long term memory that I like video games",
        "skill": "owner_memory",
        "status": "duplicate",
        "response": "I already remember that you like video games.",
        "memory_type": "preference",
        "fact_text": "I like video games",
    },
    {
        "text": "I went to the gym today.",
        "skill_not": "owner_memory",
    },
    {
        "text": "Remind me to go to the gym tomorrow.",
        "skill": "tasks",
    },
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify explicit general long-term owner memory across fresh Brain processes."
    )
    parser.add_argument("--profile", default="", help="isolated owner-profile path")
    parser.add_argument("--reset", action="store_true", help="remove only the isolated profile before verification")
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
        return 0

    if args.profile:
        return _run_sequence(
            resolve_owner_profile_path(args.profile),
            args.reset,
            args.verbose,
            output_func,
            runner,
        )
    with TemporaryDirectory(prefix="ares_general_long_term_memory_") as directory:
        return _run_sequence(
            Path(directory) / "owner_profile.json",
            True,
            args.verbose,
            output_func,
            runner,
        )


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

    initial_report = OwnerMemoryService(profile_path).inspect(include_values=True)
    initial_active_count = int(initial_report.get("memory_count") or 0)
    output_func(f"Initial active_memory_count: {initial_active_count}")

    for index, expectation in enumerate(STEPS, start=1):
        completed = runner(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--profile",
                str(profile_path),
                "--child-command",
                str(expectation["text"]),
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
        if not _matches(expectation, payload):
            output_func(f"FAIL step {index}: unexpected route/result: {payload!r}")
            return 1
        if verbose:
            output_func(
                f"PASS step {index}: {payload.get('selected_skill')} / "
                f"{payload.get('storage_status') or 'non-memory'} in a fresh process."
            )

    report = OwnerMemoryService(profile_path).inspect(include_values=True)
    active = [memory for memory in report.get("memories") or [] if memory.get("status") == "active"]
    target_objects = [str(memory.get("object") or "") for memory in active]
    if (
        report.get("detected_version") != 3
        or len(active) != initial_active_count + 2
        or target_objects.count("going to the gym") != 1
        or target_objects.count("video games") != 1
    ):
        output_func(f"FAIL: the two v3 preferences did not survive without duplicates: {report!r}")
        return 1
    forbidden = {
        "voice_owner_profile.json",
        "speech_memory.json",
        "voice_facts.json",
        "microphone_memory.json",
    }
    created_names = {item.name for item in profile_path.parent.rglob("*.json")}
    if created_names & forbidden:
        output_func("FAIL: a voice-specific owner-memory file was created.")
        return 1
    output_func(f"Final active_memory_count: {len(active)}")
    output_func("PASS: general long-term memories persisted through the central Brain path across fresh processes.")
    output_func("PASS: locked-term normalization, multiple-preference recall, duplicate prevention, explicit-only, and task-collision behavior matched.")
    output_func(f"PASS: isolated profile: {profile_path}")
    output_func("PASS: no voice-specific memory file was created.")
    return 0


def _matches(expectation: dict[str, Any], payload: dict[str, Any]) -> bool:
    if expectation.get("skill_not") and payload.get("selected_skill") == expectation["skill_not"]:
        return False
    if expectation.get("skill") and payload.get("selected_skill") != expectation["skill"]:
        return False
    if expectation.get("status") and payload.get("storage_status") != expectation["status"]:
        return False
    if expectation.get("response") and payload.get("response") != expectation["response"]:
        return False
    if expectation.get("response_prefix") and not str(payload.get("response") or "").startswith(expectation["response_prefix"]):
        return False
    if expectation.get("action") and payload.get("memory_action") != expectation["action"]:
        return False
    if expectation.get("memory_type") and payload.get("memory_type") != expectation["memory_type"]:
        return False
    if expectation.get("fact_text") and payload.get("extracted_fact_text") != expectation["fact_text"]:
        return False
    if expectation.get("normalized_trigger") and payload.get("normalized_memory_trigger") != expectation["normalized_trigger"]:
        return False
    return True


def main() -> None:
    raise SystemExit(run_verification())


if __name__ == "__main__":
    main()
