from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.OwnerMemory import OwnerMemoryValidationError, normalize_owner_fact_key  # noqa: E402
from memory import OwnerMemoryService  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect central ARES owner memory without modifying it.")
    parser.add_argument("--profile", default="", help="optional isolated owner-profile path")
    parser.add_argument("--key", default="", help="show one normalized owner fact")
    parser.add_argument("--json", action="store_true", help="print sanitized deterministic JSON")
    return parser


def run_inspection(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    service = OwnerMemoryService(args.profile or None)
    report = service.inspect(include_values=True)
    if report.get("validation_state") == "invalid" or report.get("storage_error"):
        output_func(f"Owner memory is invalid: {report.get('storage_error') or report.get('error_message') or 'validation failed'}")
        return 2

    facts = list(report.get("facts") or [])
    if args.key:
        try:
            normalized_key = normalize_owner_fact_key(args.key)
        except OwnerMemoryValidationError as error:
            output_func(f"Invalid owner fact key: {error.code}")
            return 2
        facts = [fact for fact in facts if fact.get("normalized_key") == normalized_key]

    safe_report = {
        "profile_path": report.get("profile_path"),
        "exists": Path(str(report.get("profile_path") or "")).exists(),
        "schema_name": report.get("schema_name"),
        "schema_version": report.get("detected_version"),
        "target_version": report.get("current_target_version"),
        "validation_state": report.get("validation_state"),
        "fact_count": report.get("fact_count", 0),
        "last_backup": report.get("last_backup", ""),
        "facts": facts,
    }
    if args.json:
        output_func(json.dumps(safe_report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    output_func(f"Owner profile: {safe_report['profile_path']}")
    output_func(f"State: {safe_report['validation_state']}")
    output_func(f"Schema: {safe_report['schema_name']} v{safe_report['schema_version'] or safe_report['target_version']}")
    if not facts:
        output_func("Saved facts: none")
        return 0
    output_func("Saved facts:")
    for fact in facts:
        output_func(f"- {fact['display_key']}: {_format_value(fact.get('value'))}")
    return 0


def _format_value(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def main() -> None:
    raise SystemExit(run_inspection())


if __name__ == "__main__":
    main()
