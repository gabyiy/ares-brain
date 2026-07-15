from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    BrainRuntime,
    BrainRuntimeConfig,
    BrainSessionConfig,
    BrainSessionManager,
    ConsoleRuntimeInputAdapter,
    ConsoleRuntimeOutputAdapter,
    CoreService,
    QueuedRuntimeInputAdapter,
)
from scripts import manual_verify_single_turn_voice as single_turn  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the persistent ARES Brain Runtime in explicit foreground text mode."
    )
    parser.add_argument("--inactivity-timeout", type=float, default=30.0)
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--command-timeout", type=float, default=30.0)
    parser.add_argument("--standby-response", default="")
    parser.add_argument("--shutdown-response", default="ARES is shutting down.")
    parser.add_argument(
        "--command",
        action="append",
        default=[],
        help="queue one deterministic input instead of reading the console; may be repeated",
    )
    return parser


def create_runtime(args: argparse.Namespace) -> BrainRuntime:
    session_config = BrainSessionConfig(
        inactivity_timeout_seconds=args.inactivity_timeout,
        maximum_consecutive_failures=args.max_consecutive_failures,
    )
    manager = BrainSessionManager(config=session_config)
    core_service = CoreService(brain_session_manager=manager)
    skill_manager = single_turn.create_skill_manager(core_service)
    return BrainRuntime(
        core_service=core_service,
        command_handler=single_turn.build_existing_brain_handler(skill_manager),
        input_adapter=(
            QueuedRuntimeInputAdapter(args.command)
            if args.command
            else ConsoleRuntimeInputAdapter()
        ),
        output_adapter=ConsoleRuntimeOutputAdapter(),
        config=BrainRuntimeConfig(
            inactivity_timeout_seconds=args.inactivity_timeout,
            maximum_consecutive_failures=args.max_consecutive_failures,
            input_polling_interval_seconds=args.poll_interval,
            command_timeout_seconds=args.command_timeout,
            standby_response=args.standby_response,
            shutdown_response=args.shutdown_response,
        ),
    )


def run_text_runtime(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime = create_runtime(args)
    except (TypeError, ValueError) as error:
        print(f"Configuration error: {error}")
        return 2
    print("ARES persistent Brain Runtime: foreground text verification")
    print("Type 'Ares' to activate, 'goodbye Ares' for standby, or 'shutdown Ares' to exit.")
    result = runtime.run()
    if result.status == "stopped" and result.success:
        return 0
    print(f"Runtime stopped with status: {result.status} ({result.error_code})")
    return 1


def main() -> int:
    return run_text_runtime()


if __name__ == "__main__":
    raise SystemExit(main())
