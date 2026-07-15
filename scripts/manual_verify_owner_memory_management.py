from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from memory import (  # noqa: E402
    DEFAULT_OWNER_PROFILE_PATH,
    DEFAULT_PENDING_OWNER_MEMORY_ACTION_PATH,
    OwnerMemoryService,
    PendingOwnerMemoryActionStore,
    resolve_owner_profile_path,
    resolve_pending_owner_memory_path,
)
from scripts.manual_verify_owner_memory import run_owner_memory_text  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify safe central owner-memory management with isolated files."
    )
    parser.add_argument("--profile", default="", help="isolated owner-profile path")
    parser.add_argument("--pending-state", default="", help="isolated pending-action path")
    parser.add_argument("--reset", action="store_true", help="remove only the isolated test files first")
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
        if not args.profile or not args.pending_state:
            output_func(json.dumps({"success": False, "error": "missing_isolated_paths"}))
            return 2
        payload = run_owner_memory_text(
            args.child_command,
            resolve_owner_profile_path(args.profile),
            resolve_pending_owner_memory_path(args.pending_state),
        )
        output_func(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0

    if args.profile:
        profile = resolve_owner_profile_path(args.profile)
        pending = resolve_pending_owner_memory_path(
            args.pending_state or profile.with_name("pending_owner_memory_action.json")
        )
        return _run_sequence(profile, pending, args.reset, args.verbose, output_func, runner)
    with TemporaryDirectory(prefix="ares_owner_memory_management_") as directory:
        root = Path(directory)
        return _run_sequence(
            root / "owner_profile.json",
            root / "pending_owner_memory_action.json",
            True,
            args.verbose,
            output_func,
            runner,
        )


def _run_sequence(
    profile: Path,
    pending: Path,
    reset: bool,
    verbose: bool,
    output_func: Callable[[str], None],
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> int:
    profile = resolve_owner_profile_path(profile)
    pending = resolve_pending_owner_memory_path(pending)
    if reset:
        if profile == DEFAULT_OWNER_PROFILE_PATH or pending == DEFAULT_PENDING_OWNER_MEMORY_ACTION_PATH:
            output_func("FAIL: --reset refuses to remove canonical production state.")
            return 2
        profile.unlink(missing_ok=True)
        pending.unlink(missing_ok=True)

    def command(text: str) -> dict[str, Any]:
        payload = _run_child(text, profile, pending, runner)
        if verbose:
            output_func(
                f"{text!r} -> {payload.get('selected_skill')} / "
                f"{payload.get('storage_status')} / {payload.get('response')}"
            )
        return payload

    for text in (
        "Remember that my favorite color is red.",
        "Remember that my favorite game is EVE Online.",
        "Remember that I like going to the gym.",
        "Remember that I like video games.",
        "Remember that I enjoy strategy games.",
        "Remember that I prefer wireless mice.",
    ):
        payload = command(text)
        if payload.get("selected_skill") != "owner_memory" or payload.get("storage_status") not in {"created", "updated"}:
            return _fail(output_func, "initial memory creation did not use owner_memory")
    if _active_count(profile, pending) != 4:
        return _fail(output_func, "expected four active general memories")

    requested = command("Forget that I like going to the gym.")
    if requested.get("storage_status") != "confirmation_required" or _active_count(profile, pending) != 4:
        return _fail(output_func, "specific deletion changed memory before confirmation")
    if OwnerMemoryService(profile, pending_path=pending).inspect().get("pending_action_status") != "active":
        return _fail(output_func, "specific deletion did not create pending state")
    confirmed = command("Yes, delete it.")
    if confirmed.get("storage_status") != "deleted_specific" or _active_count(profile, pending) != 3:
        return _fail(output_func, "specific deletion confirmation failed")

    ambiguous = command("Forget my gaming memories.")
    if ambiguous.get("storage_status") != "ambiguous" or pending.exists():
        return _fail(output_func, "ambiguous gaming deletion was not refused")
    topic = command("Forget everything about gaming.")
    if topic.get("storage_status") != "confirmation_required" or _active_count(profile, pending) != 3:
        return _fail(output_func, "topic deletion changed memory before confirmation")
    deleted_topic = command("Yes, delete them.")
    if deleted_topic.get("storage_status") != "deleted_topic" or _active_count(profile, pending) != 1:
        return _fail(output_func, "topic deletion confirmation failed")
    report = OwnerMemoryService(profile, pending_path=pending).inspect(include_values=True)
    if "favorite_game" not in {fact.get("normalized_key") for fact in report.get("facts") or ()}:
        return _fail(output_func, "topic deletion removed a keyed favorite-game fact")

    requested = command("Delete my preference for wireless mice.")
    cancelled = command("Never mind.")
    if requested.get("storage_status") != "confirmation_required" or cancelled.get("storage_status") != "cancelled":
        return _fail(output_func, "cancellation flow failed")
    if _active_count(profile, pending) != 1 or pending.exists():
        return _fail(output_func, "cancellation did not preserve memory and clear pending state")

    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    pending_store = PendingOwnerMemoryActionStore(pending, clock=lambda: now[0], ttl_seconds=60)
    expiring_service = OwnerMemoryService(profile, pending_store=pending_store)
    expiring_request = run_owner_memory_text(
        "Delete my preference for wireless mice.",
        profile,
        pending,
        owner_memory_service=expiring_service,
    )
    now[0] += timedelta(seconds=61)
    expired = run_owner_memory_text(
        "Yes, delete it.",
        profile,
        pending,
        owner_memory_service=expiring_service,
    )
    if expiring_request.get("storage_status") != "confirmation_required" or expired.get("storage_status") != "expired":
        return _fail(output_func, "expired pending operation was not refused")
    if _active_count(profile, pending) != 1:
        return _fail(output_func, "expiry mutated owner memory")

    for text in ("Remember that I like tea.", "Remember that I enjoy books."):
        if command(text).get("storage_status") not in {"created", "updated"}:
            return _fail(output_func, "delete-all setup failed")
    all_request = command("Forget all my general long-term memories.")
    if all_request.get("storage_status") != "confirmation_required" or _active_count(profile, pending) != 3:
        return _fail(output_func, "delete-all request mutated memory before confirmation")
    all_confirmed = command("Confirm delete all general memories.")
    if all_confirmed.get("storage_status") != "deleted_all_general" or _active_count(profile, pending) != 0:
        return _fail(output_func, "delete-all confirmation failed")
    report = OwnerMemoryService(profile, pending_path=pending).inspect(include_values=True)
    fact_keys = {fact.get("normalized_key") for fact in report.get("facts") or ()}
    if not {"favorite_color", "favorite_game"}.issubset(fact_keys):
        return _fail(output_func, "general delete-all removed keyed facts")

    keyed_request = command("Forget my favorite color.")
    keyed_cancel = command("No, cancel.")
    if keyed_request.get("storage_status") != "confirmation_required" or keyed_cancel.get("storage_status") != "cancelled":
        return _fail(output_func, "keyed-fact cancellation failed")
    recalled = command("What is my favorite color?")
    if recalled.get("response") != "Your favorite color is red.":
        return _fail(output_func, "keyed fact did not survive cancellation")

    command("Remember that I like chess.")
    command("Forget that I like chess.")
    before_corruption = _active_count(profile, pending)
    pending.write_text("{corrupt", encoding="utf-8")
    corrupt = command("Yes, delete it.")
    if corrupt.get("storage_status") != "invalid_pending" or _active_count(profile, pending) != before_corruption:
        return _fail(output_func, "corrupt pending state was not refused safely")

    forbidden = {"voice_owner_profile.json", "speech_memory.json", "microphone_memory.json", "voice_facts.json"}
    if {item.name for item in profile.parent.rglob("*.json")} & forbidden:
        return _fail(output_func, "voice-specific owner-memory state was created")

    output_func("PASS: specific, topic, general-all, and keyed-fact deletions require central confirmation.")
    output_func("PASS: cancellation, expiry, corruption, cross-process confirmation, and keyed/general separation are safe.")
    output_func(f"PASS: isolated owner profile: {profile}")
    output_func(f"PASS: isolated pending state: {pending}")
    return 0


def _run_child(
    text: str,
    profile: Path,
    pending: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    completed = runner(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--profile",
            str(profile),
            "--pending-state",
            str(pending),
            "--child-command",
            text,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"child exited {completed.returncode}: {completed.stderr.strip()}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _active_count(profile: Path, pending: Path) -> int:
    report = OwnerMemoryService(profile, pending_path=pending).inspect(include_values=True)
    return int(report.get("memory_count") or 0)


def _fail(output_func: Callable[[str], None], message: str) -> int:
    output_func(f"FAIL: {message}.")
    return 1


def main() -> None:
    raise SystemExit(run_verification())


if __name__ == "__main__":
    main()
