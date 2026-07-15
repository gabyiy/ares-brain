from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.OwnerMemory import OwnerMemoryValidationError, normalize_owner_fact_key  # noqa: E402
from core.OwnerLongTermMemory import (  # noqa: E402
    GENERAL_MEMORY_TYPES,
    extract_memory_topics,
)
from memory import OwnerMemoryService  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect central ARES owner memory without modifying it.")
    parser.add_argument("--profile", default="", help="optional isolated owner-profile path")
    parser.add_argument("--pending-state", default="", help="optional isolated pending-action path")
    parser.add_argument("--key", default="", help="show one normalized owner fact")
    parser.add_argument("--summary", action="store_true", help="show schema and bounded counts")
    parser.add_argument("--facts", action="store_true", help="show keyed owner facts")
    parser.add_argument("--memories", action="store_true", help="show general long-term memories")
    parser.add_argument("--topic", default="", help="filter general memories by topic")
    parser.add_argument("--type", default="", dest="memory_type", help="filter by general memory type")
    parser.add_argument("--count", action="store_true", help="show keyed and general memory counts")
    parser.add_argument("--pending", action="store_true", help="show transient pending deletion state")
    parser.add_argument("--json", action="store_true", help="print sanitized deterministic JSON")
    return parser


def run_inspection(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    service = OwnerMemoryService(
        args.profile or None,
        pending_path=args.pending_state or None,
    )
    report = service.inspect(include_values=True)
    if report.get("validation_state") == "invalid" or report.get("storage_error"):
        output_func(f"Owner memory is invalid: {report.get('storage_error') or report.get('error_message') or 'validation failed'}")
        return 2
    if report.get("pending_validation_state") == "invalid":
        output_func(f"Pending owner-memory state is invalid: {report.get('pending_error') or 'validation failed'}")
        return 2

    facts = list(report.get("facts") or [])
    memories = list(report.get("memories") or [])
    if args.key:
        try:
            normalized_key = normalize_owner_fact_key(args.key)
        except OwnerMemoryValidationError as error:
            output_func(f"Invalid owner fact key: {error.code}")
            return 2
        facts = [fact for fact in facts if fact.get("normalized_key") == normalized_key]
    if args.memory_type:
        if args.memory_type not in GENERAL_MEMORY_TYPES:
            output_func(f"Invalid owner memory type: {args.memory_type}")
            return 2
        memories = [memory for memory in memories if memory.get("memory_type") == args.memory_type]
    if args.topic:
        requested_topics = set(extract_memory_topics(args.topic))
        if not requested_topics:
            output_func("Invalid owner memory topic.")
            return 2
        memories = [
            memory
            for memory in memories
            if requested_topics & set(memory.get("topics") or ())
        ]
    if not args.memories and not args.topic and not args.memory_type and args.key:
        memories = []

    safe_report = {
        "profile_path": report.get("profile_path"),
        "exists": Path(str(report.get("profile_path") or "")).exists(),
        "schema_name": report.get("schema_name"),
        "schema_version": report.get("detected_version"),
        "target_version": report.get("current_target_version"),
        "validation_state": report.get("validation_state"),
        "fact_count": report.get("fact_count", 0),
        "active_memory_count": report.get("memory_count", 0),
        "stored_memory_count": report.get("stored_memory_count", 0),
        "pending_delete_all": report.get("pending_delete_all", False),
        "last_backup": report.get("last_backup", ""),
        "facts": facts,
        "memories": memories,
        "pending_state_path": report.get("pending_state_path"),
        "pending_action_status": report.get("pending_action_status", "missing"),
        "pending_validation_state": report.get("pending_validation_state", "missing"),
        "pending_action": report.get("pending_action"),
    }
    if args.json:
        output_func(json.dumps(safe_report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    explicit_view = bool(
        args.summary
        or args.facts
        or args.memories
        or args.count
        or args.pending
        or args.key
        or args.topic
        or args.memory_type
    )
    show_summary = args.summary or args.count or not explicit_view
    show_facts = args.facts or bool(args.key) or not explicit_view
    show_memories = args.memories or bool(args.topic) or bool(args.memory_type) or not explicit_view
    if show_summary:
        output_func(f"Owner profile: {safe_report['profile_path']}")
        output_func(f"State: {safe_report['validation_state']}")
        output_func(f"Schema: {safe_report['schema_name']} v{safe_report['schema_version'] or safe_report['target_version']}")
        output_func(f"Keyed fact count: {safe_report['fact_count']}")
        output_func(f"Active general memory count: {safe_report['active_memory_count']}")
    if show_facts and facts:
        output_func("Saved keyed facts:")
        for fact in facts:
            output_func(f"- {fact['display_key']}: {_format_value(fact.get('value'))}")
    elif show_facts:
        output_func("Saved facts: none")
    if show_memories and memories:
        output_func("General long-term memories:")
        for memory in memories:
            output_func(
                f"- {memory.get('memory_id')} [{memory.get('memory_type')}/{memory.get('status')}]: "
                f"{memory.get('canonical_text')} topics={','.join(memory.get('topics') or [])} "
                f"created={memory.get('created_at')} updated={memory.get('updated_at')}"
            )
    elif show_memories:
        output_func("General long-term memories: none")
    if args.pending:
        output_func(f"Pending state: {safe_report['pending_action_status']}")
        output_func(f"Pending state path: {safe_report['pending_state_path']}")
        pending = dict(safe_report.get("pending_action") or {})
        if pending:
            output_func(
                "Pending deletion: "
                f"{pending.get('operation')} / {pending.get('target_kind')} / "
                f"{pending.get('candidate_count')} candidate(s), expires {pending.get('expires_at')}"
            )
    return 0


def _format_value(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def main() -> None:
    raise SystemExit(run_inspection())


if __name__ == "__main__":
    main()
