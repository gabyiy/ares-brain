from pathlib import Path
import sys
from typing import Callable, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import LocalDeviceActionAdapter


APP_ID = "calculator"
CONFIRMATION_PHRASE = "YES_OPEN_CALCULATOR"


def run_manual_verification(
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
    adapter: Optional[LocalDeviceActionAdapter] = None,
    adapter_factory: Callable[[], LocalDeviceActionAdapter] = LocalDeviceActionAdapter,
) -> int:
    output_func("WARNING: This manual verification can open Windows Calculator.")
    output_func(f"App id: {APP_ID}")
    output_func(f"Type {CONFIRMATION_PHRASE} to continue.")

    try:
        confirmation = input_func("> ")
    except EOFError:
        output_func("No confirmation entered. Calculator was not opened.")
        return 1

    if confirmation != CONFIRMATION_PHRASE:
        output_func("Confirmation did not match. Calculator was not opened.")
        return 1

    device_adapter = adapter or adapter_factory()
    result = device_adapter.execute(
        "open_app",
        {"app_id": APP_ID, "confirmation_approved": True},
    )
    output_func(result.text or result.error_message)
    return 0 if result.success else 2


def main() -> None:
    raise SystemExit(run_manual_verification())


if __name__ == "__main__":
    main()
