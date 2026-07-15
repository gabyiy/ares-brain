from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    BRAIN_ACTIVE,
    BRAIN_STANDBY,
    BRAIN_STOPPED,
    BrainRuntime,
    BrainRuntimeConfig,
    BrainSessionConfig,
    BrainSessionManager,
    CollectingRuntimeOutputAdapter,
    ConversationContextManager,
    CoreService,
    QueuedRuntimeInputAdapter,
    RuntimeInputResult,
)
from events import EventBus as SkillEventBus, EventHistoryStore  # noqa: E402
from memory import (  # noqa: E402
    GoalsStore,
    MemoryStore,
    NotesStore,
    OwnerMemoryService,
    TasksStore,
    UserProfileStore,
)
from scripts import manual_verify_single_turn_voice as single_turn  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def _build_runtime(root: Path, clock: FakeClock):
    profile_path = root / "owner_profile.json"
    support = root / "support"
    skill_bus = SkillEventBus()
    manager = BrainSessionManager(
        config=BrainSessionConfig(
            inactivity_timeout_seconds=30,
            maximum_consecutive_failures=3,
        ),
        clock=clock,
        session_id_factory=iter(
            ("brain-session-runtime-manual-1", "brain-session-runtime-manual-2", "brain-session-runtime-manual-3")
        ).__next__,
    )
    owner_service = OwnerMemoryService(
        profile_path,
        event_bus=skill_bus,
        pending_path=root / "pending_owner_memory_action.json",
    )
    core_service = CoreService(
        owner_memory_service=owner_service,
        brain_session_manager=manager,
        register_default_pc=False,
        register_default_voice=False,
    )
    skill_manager = single_turn.create_skill_manager(
        core_service,
        event_history_store=EventHistoryStore(support / "events.json"),
        event_bus=skill_bus,
        memory_store=MemoryStore(
            short_path=support / "short_memory.json",
            long_path=support / "long_memory.json",
            event_bus=skill_bus,
        ),
        profile_store=UserProfileStore(support / "user_profile.json", event_bus=skill_bus),
        goals_store=GoalsStore(support / "goals.json", event_bus=skill_bus),
        notes_store=NotesStore(support / "notes.json", event_bus=skill_bus),
        tasks_store=TasksStore(support / "tasks.json", event_bus=skill_bus),
        conversation_context=ConversationContextManager(),
    )
    input_adapter = QueuedRuntimeInputAdapter()
    output_adapter = CollectingRuntimeOutputAdapter()
    runtime = BrainRuntime(
        core_service=core_service,
        command_handler=single_turn.build_existing_brain_handler(skill_manager),
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        config=BrainRuntimeConfig(),
        clock=clock,
        runtime_id_factory=lambda: "brain-runtime-manual",
    )
    return runtime, input_adapter, output_adapter


def run_verification(output_func: Callable[[str], None] = print) -> int:
    with TemporaryDirectory(prefix="ares-brain-runtime-") as temporary:
        clock = FakeClock()
        runtime, input_adapter, output_adapter = _build_runtime(Path(temporary), clock)

        output_func("ARES Brain Runtime verification (deterministic text mode)")
        illegal = runtime.session_manager.begin_processing(reason="manual_illegal_transition")
        if illegal.success or runtime.session_manager.state != BRAIN_STOPPED:
            output_func("FAIL: illegal lifecycle transition was not rejected safely")
            return 1
        output_func("Rejected: STOPPED -> PROCESSING; state preserved")

        started = runtime.start()
        if not started.success or runtime.session_manager.state != BRAIN_STANDBY:
            output_func("FAIL: runtime did not boot to STANDBY")
            return 1
        output_func("STOPPED -> BOOTING -> INITIALIZING -> STANDBY")

        ignored = runtime.handle_text("calculate 100 plus 1")
        if ignored.status != "ignored_in_standby" or runtime.session_manager.session_id:
            output_func("FAIL: ordinary standby input was not ignored")
            return 1
        output_func("STANDBY ordinary command: ignored; no session created")

        activated = runtime.handle_text("Ares")
        first_session = runtime.session_manager.session_id
        if not activated.success or not first_session or output_adapter.texts[-1] != "Yes Gabi.":
            output_func("FAIL: activation or acknowledgement failed")
            return 1
        output_func(f"STANDBY -> ACTIVE: {first_session}; ARES: Yes Gabi.")

        calculator = runtime.handle_text("calculate 2 plus 2")
        if calculator.response_text != "Result: 4" or runtime.session_manager.state != BRAIN_ACTIVE:
            output_func(f"FAIL: calculator route returned {calculator.response_text!r}")
            return 1
        output_func("ACTIVE -> PROCESSING -> RESPONDING -> ACTIVE: Result: 4")

        saved = runtime.handle_text("Remember that my favorite color is blue.")
        recalled = runtime.handle_text("What is my favorite color?")
        if (
            not saved.success
            or not recalled.success
            or "blue" not in recalled.response_text.lower()
            or runtime.session_manager.session_id != first_session
        ):
            output_func("FAIL: central owner-memory route or session continuity failed")
            return 1
        output_func("Owner memory create/recall: PASS; session ID unchanged")

        clock.advance(29.999)
        input_adapter.push(RuntimeInputResult.timeout())
        before = runtime.poll_once()
        if before.status != "input_timeout" or runtime.session_manager.state != BRAIN_ACTIVE:
            output_func("FAIL: session expired before the timeout boundary")
            return 1
        output_func("Inactivity at 29.999s: ACTIVE")

        clock.advance(0.001)
        input_adapter.push(RuntimeInputResult.timeout())
        boundary = runtime.poll_once()
        if boundary.status != "standby_entered" or runtime.session_manager.session_id:
            output_func("FAIL: exact inactivity boundary did not return to STANDBY")
            return 1
        output_func("Inactivity at 30.000s: RETURNING_TO_STANDBY -> STANDBY")

        second = runtime.handle_text("hello, Ares")
        second_session = runtime.session_manager.session_id
        if not second.success or not second_session or second_session == first_session:
            output_func("FAIL: reactivation did not create a new session ID")
            return 1
        output_func(f"Reactivated with new session: {second_session}")

        standby = runtime.handle_text("goodbye Ares")
        if standby.status != "standby_entered" or runtime.session_manager.state != BRAIN_STANDBY:
            output_func("FAIL: goodbye did not return to STANDBY")
            return 1
        output_func("goodbye Ares: STANDBY; runtime remains alive")

        third = runtime.handle_text("wake up Ares")
        stopped = runtime.handle_text("shutdown Ares")
        if not third.success or not stopped.success or runtime.session_manager.state != BRAIN_STOPPED:
            output_func("FAIL: explicit shutdown did not stop the runtime")
            return 1
        output_func("shutdown Ares: SHUTTING_DOWN -> STOPPED")

        safe_events = runtime.events() + runtime.session_manager.events()
        serialized = json.dumps([event.to_dict() for event in safe_events], sort_keys=True).lower()
        forbidden = (
            "calculate 2 plus 2",
            "favorite color is blue",
            "what is my favorite color",
            "owner_memory_value",
            "transcript",
            "password",
        )
        leaked = next((marker for marker in forbidden if marker in serialized), "")
        if leaked:
            output_func(f"FAIL: private input marker present in operational events: {leaked}")
            return 1
        output_func(f"Operational events emitted: {len(safe_events)}")
        output_func("Event privacy: PASS")
        output_func("Brain Runtime verification passed.")
        return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
