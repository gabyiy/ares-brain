from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from memory.schema_migrations import (  # noqa: E402
    DEFAULT_STALE_LOCK_SECONDS,
    inspect_store_lock,
    recover_store_lock,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect an ARES JSON-store or runtime ownership lock safely."
    )
    parser.add_argument("path", help="Store/runtime target path; the .lock suffix is resolved automatically.")
    parser.add_argument("--recover-if-owner-dead", action="store_true")
    parser.add_argument(
        "--stale-after-seconds",
        type=float,
        default=DEFAULT_STALE_LOCK_SECONDS,
    )
    return parser


def run(argv: Optional[Sequence[str]] = None, *, output_func=print) -> int:
    args = build_parser().parse_args(argv)
    target = _resolve_target(args.path)
    try:
        inspection = inspect_store_lock(
            target,
            stale_after_seconds=args.stale_after_seconds,
        )
    except ValueError as error:
        output_func(f"Lock inspection configuration error: {error}")
        return 2

    _print_inspection(inspection, output_func)
    if not args.recover_if_owner_dead:
        return 2 if inspection.lock_exists and not inspection.metadata_valid else 0

    recovery = recover_store_lock(
        target,
        stale_after_seconds=args.stale_after_seconds,
    )
    output_func(f"Recovery status: {recovery.status}")
    output_func(f"Recovered: {'yes' if recovery.recovered else 'no'}")
    if recovery.error_message:
        output_func(f"Recovery reason: {recovery.error_message}")
    return 0 if recovery.success else 1


def _resolve_target(value: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError("path is required")
    path = Path(text).expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def _print_inspection(inspection, output_func) -> None:
    output_func(f"Store/runtime path: {inspection.store_path}")
    output_func(f"Lock path: {inspection.lock_path}")
    output_func(f"Lock exists: {'yes' if inspection.lock_exists else 'no'}")
    if not inspection.lock_exists:
        return
    output_func(f"Metadata format: {inspection.metadata_format or 'unknown'}")
    output_func(f"Metadata valid: {'yes' if inspection.metadata_valid else 'no'}")
    output_func(f"Owner PID: {inspection.owner_pid or 'unknown'}")
    output_func(f"Owner hostname: {inspection.owner_hostname or 'unknown'}")
    output_func(f"Owner kind: {inspection.owner_kind or 'unknown'}")
    output_func(f"Owner process: {inspection.owner_process_state}")
    output_func(f"Created at: {inspection.created_at or 'unknown'}")
    output_func(f"Lock age seconds: {inspection.lock_age_seconds:.3f}")
    output_func(f"Expired: {'yes' if inspection.expired else 'no'}")
    output_func(
        "Safe recovery possible: "
        f"{'yes' if inspection.safe_recovery_possible else 'no'}"
    )
    if inspection.error_message:
        output_func(f"Inspection warning: {inspection.error_message}")


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
