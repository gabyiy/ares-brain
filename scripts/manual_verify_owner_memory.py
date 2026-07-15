from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import ConversationContextManager, CoreService  # noqa: E402
from events import EventBus, EventHistoryStore  # noqa: E402
from memory import (  # noqa: E402
    DEFAULT_OWNER_PROFILE_PATH,
    GoalsStore,
    MemoryStore,
    NotesStore,
    OwnerMemoryService,
    TasksStore,
    UserProfileStore,
    resolve_owner_profile_path,
)
from scripts import manual_verify_single_turn_voice as single_turn  # noqa: E402


WARNING = (
    "WARNING: This is a text-only explicit owner-memory check. "
    "It does not access microphone or speaker hardware."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one explicit owner-memory command through the production Brain path."
    )
    parser.add_argument("--text", required=True, help="one explicit owner-memory command")
    parser.add_argument(
        "--profile-path",
        default=str(DEFAULT_OWNER_PROFILE_PATH),
        help="owner-profile JSON path; use a temporary path for isolated checks",
    )
    parser.add_argument("--pending-state", default="", help="optional isolated pending-action path")
    parser.add_argument("--json", action="store_true", help="print one JSON result line")
    return parser


def run_owner_memory_text(
    text: str,
    profile_path: Path,
    pending_path: Optional[Path] = None,
    *,
    owner_memory_service: Optional[OwnerMemoryService] = None,
) -> dict[str, Any]:
    path = resolve_owner_profile_path(profile_path)
    support_dir = path.parent / ".owner_memory_verification"
    event_bus = EventBus()
    service = owner_memory_service or OwnerMemoryService(
        path,
        event_bus=event_bus,
        pending_path=pending_path,
    )
    core_service = CoreService(owner_memory_service=service)
    manager = single_turn.create_skill_manager(
        core_service,
        event_history_store=EventHistoryStore(support_dir / "events.json"),
        event_bus=event_bus,
        memory_store=MemoryStore(
            short_path=support_dir / "short_memory.json",
            long_path=support_dir / "long_memory.json",
            event_bus=event_bus,
        ),
        profile_store=UserProfileStore(support_dir / "user_profile.json", event_bus=event_bus),
        goals_store=GoalsStore(support_dir / "goals.json", event_bus=event_bus),
        notes_store=NotesStore(support_dir / "notes.json", event_bus=event_bus),
        tasks_store=TasksStore(support_dir / "tasks.json", event_bus=event_bus),
        conversation_context=ConversationContextManager(),
    )
    intent = manager.parse_intent(text)
    response = single_turn.build_existing_brain_handler(manager)(text)
    metadata = dict(response.metadata or {})
    storage_result = dict(metadata.get("storage_result") or {})
    diagnostics = dict(metadata.get("owner_memory_diagnostics") or {})
    protected = bool(metadata.get("protected_key_rejected"))
    failure = str(metadata.get("error") or "")
    handled = response.skill == "owner_memory"
    return {
        "success": handled and not failure,
        "handled": handled,
        "response": response.text,
        "selected_skill": response.skill,
        "parsed_intent": intent.intent_name,
        "memory_action": str(metadata.get("memory_action") or ""),
        "normalized_fact_key": str(metadata.get("normalized_fact_key") or ""),
        "memory_kind": str(diagnostics.get("memory_kind") or ""),
        "memory_type": str(diagnostics.get("memory_type") or ""),
        "memory_id": str(diagnostics.get("memory_id") or ""),
        "persistence": str(diagnostics.get("persistence") or ""),
        "extracted_memory_phrase": str(diagnostics.get("extracted_memory_phrase") or ""),
        "extracted_fact_text": str(diagnostics.get("extracted_fact_text") or ""),
        "normalized_memory_trigger": str(diagnostics.get("normalized_memory_trigger") or ""),
        "routing_reason": str(diagnostics.get("routing_reason") or ""),
        "storage_status": str(
            metadata.get("storage_status") or storage_result.get("status") or ""
        ),
        "operation_result": str(metadata.get("operation_result") or ""),
        "protected_key_rejected": protected,
        "rejection_reason": str(metadata.get("rejection_reason") or ""),
        "error": failure,
        "profile_path": str(diagnostics.get("profile_path") or path),
        "file_existed_before": diagnostics.get("file_existed_before"),
        "parser_rule": str(diagnostics.get("parser_rule") or ""),
        "pending_state_path": str(diagnostics.get("pending_state_path") or service.pending_path),
        "pending_operation": str(diagnostics.get("pending_operation") or ""),
        "pending_status": str(diagnostics.get("pending_status") or ""),
        "pending_candidate_count": int(diagnostics.get("pending_candidate_count") or 0),
        "pending_expires_at": str(diagnostics.get("pending_expires_at") or ""),
        "pending_topic": str(diagnostics.get("pending_topic") or ""),
        "value_redacted": True,
    }


def run_manual_verification(
    argv: Optional[Sequence[str]] = None,
    output_func: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    result = run_owner_memory_text(
        args.text,
        Path(args.profile_path),
        Path(args.pending_state) if args.pending_state else None,
    )
    if args.json:
        output_func(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        output_func(WARNING)
        output_func(f"Intent: {result['parsed_intent']}")
        output_func(f"Selected skill: {result['selected_skill']}")
        output_func(f"Action: {result['memory_action']}")
        output_func(f"Fact key: {result['normalized_fact_key'] or '(rejected)'}")
        output_func(f"Memory kind: {result['memory_kind'] or '(none)'}")
        output_func(f"Memory type: {result['memory_type'] or '(none)'}")
        output_func(f"Normalized memory trigger: {result['normalized_memory_trigger'] or '(none)'}")
        output_func(f"Extracted fact: {result['extracted_fact_text'] or '(none)'}")
        output_func(f"Routing reason: {result['routing_reason'] or '(none)'}")
        output_func(f"Storage status: {result['storage_status'] or '(none)'}")
        output_func(f"ARES: {result['response']}")
    return 0 if result["success"] else 2


def main() -> None:
    raise SystemExit(run_manual_verification())


if __name__ == "__main__":
    main()
