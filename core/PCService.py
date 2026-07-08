import ctypes
import platform
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional


AppLauncherHandler = Callable[[Any], bool]
AppResolver = Callable[[str], Optional[Any]]


@dataclass(frozen=True)
class PCServiceResult:
    success: bool
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class PCService:
    def lock(self) -> PCServiceResult:
        raise NotImplementedError

    def sleep(self) -> PCServiceResult:
        raise NotImplementedError

    def open_app(self, app_id: Any) -> PCServiceResult:
        raise NotImplementedError

    def status(self) -> PCServiceResult:
        raise NotImplementedError


class WindowsPCService(PCService):
    def __init__(
        self,
        lock_impl: Optional[Callable[[], bool]] = None,
        sleep_impl: Optional[Callable[[], bool]] = None,
        app_launcher: Optional[AppLauncherHandler] = None,
        app_resolver: Optional[AppResolver] = None,
        platform_system: Optional[Callable[[], str]] = None,
    ):
        self._lock_impl = lock_impl or _lock_windows_session
        self._sleep_impl = sleep_impl or _sleep_windows_session
        self._app_launcher = app_launcher or _launch_windows_app
        self._app_resolver = app_resolver or (lambda app_id: None)
        self._platform_system = platform_system or platform.system

    def lock(self) -> PCServiceResult:
        current_platform = self._current_platform()
        if current_platform.lower() != "windows":
            return PCServiceResult(
                success=False,
                text="Windows session lock is unsupported on this platform.",
                error_message="unsupported_platform",
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "platform": current_platform,
                    "supported": False,
                },
            )

        try:
            locked = bool(self._lock_impl())
        except (AttributeError, OSError, RuntimeError) as error:
            return PCServiceResult(
                success=False,
                text="Windows session lock failed safely.",
                error_message=f"{type(error).__name__}: {error}",
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "platform": current_platform,
                    "supported": True,
                },
            )

        if not locked:
            return PCServiceResult(
                success=False,
                text="Windows session lock failed safely.",
                error_message="lock_failed",
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "platform": current_platform,
                    "supported": True,
                },
            )

        return PCServiceResult(
            success=True,
            text="Windows session lock requested.",
            data={"action": "lock_pc"},
            metadata={
                "danger_classification": "confirmation_required",
                "confirmation_required": True,
                "executed": True,
                "platform": current_platform,
                "supported": True,
            },
        )

    def sleep(self) -> PCServiceResult:
        current_platform = self._current_platform()
        if current_platform.lower() != "windows":
            return PCServiceResult(
                success=False,
                text="Windows sleep is unsupported on this platform.",
                error_message="unsupported_platform",
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "platform": current_platform,
                    "supported": False,
                },
            )

        try:
            slept = bool(self._sleep_impl())
        except (AttributeError, OSError, RuntimeError) as error:
            return PCServiceResult(
                success=False,
                text="Windows sleep failed safely.",
                error_message=f"{type(error).__name__}: {error}",
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "platform": current_platform,
                    "supported": True,
                },
            )

        if not slept:
            return PCServiceResult(
                success=False,
                text="Windows sleep failed safely.",
                error_message="sleep_failed",
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "platform": current_platform,
                    "supported": True,
                },
            )

        return PCServiceResult(
            success=True,
            text="Windows sleep requested.",
            data={"action": "sleep_pc"},
            metadata={
                "danger_classification": "confirmation_required",
                "confirmation_required": True,
                "executed": True,
                "platform": current_platform,
                "supported": True,
            },
        )

    def open_app(self, app_id: Any) -> PCServiceResult:
        if _unsafe_app_id_input(app_id):
            return PCServiceResult(
                success=False,
                text="App id is not allowed for open_app.",
                error_message="invalid_app_id",
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "allowlist_only": True,
                },
            )

        normalized_app_id = _normalize_app_id(app_id)
        if not normalized_app_id:
            return PCServiceResult(
                success=False,
                text="App id is required for open_app.",
                error_message="missing_app_id",
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "allowlist_only": True,
                },
            )

        app = self._app_resolver(normalized_app_id)
        if not app:
            return PCServiceResult(
                success=False,
                text=f"App is not allowlisted: {normalized_app_id}",
                error_message="unknown_app",
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "app_id": normalized_app_id,
                    "allowlist_only": True,
                },
            )

        app_data = _app_to_dict(app)
        if not bool(app_data.get("enabled")):
            return PCServiceResult(
                success=False,
                text=f"App is disabled: {normalized_app_id}",
                error_message="disabled_app",
                data={"app": app_data},
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "app_id": normalized_app_id,
                    "allowlist_only": True,
                },
            )

        current_platform = self._current_platform()
        if current_platform.lower() != "windows":
            return PCServiceResult(
                success=False,
                text="Windows app launch is unsupported on this platform.",
                error_message="unsupported_platform",
                data={"app": app_data},
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "app_id": normalized_app_id,
                    "allowlist_only": True,
                    "platform": current_platform,
                    "supported": False,
                },
            )

        display_name = str(app_data.get("display_name") or normalized_app_id)
        try:
            launched = bool(self._app_launcher(app))
        except (OSError, RuntimeError, ValueError) as error:
            return PCServiceResult(
                success=False,
                text=f"Windows app launch failed safely: {display_name}.",
                error_message=f"{type(error).__name__}: {error}",
                data={"app": app_data},
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "app_id": normalized_app_id,
                    "allowlist_only": True,
                    "platform": current_platform,
                    "supported": True,
                },
            )

        if not launched:
            return PCServiceResult(
                success=False,
                text=f"Windows app launch failed safely: {display_name}.",
                error_message="launch_failed",
                data={"app": app_data},
                metadata={
                    "danger_classification": "confirmation_required",
                    "confirmation_required": True,
                    "executed": False,
                    "app_id": normalized_app_id,
                    "allowlist_only": True,
                    "platform": current_platform,
                    "supported": True,
                },
            )

        return PCServiceResult(
            success=True,
            text=f"Windows app launch requested: {display_name}.",
            data={"app": app_data},
            metadata={
                "danger_classification": "confirmation_required",
                "confirmation_required": True,
                "executed": True,
                "app_id": normalized_app_id,
                "allowlist_only": True,
                "platform": current_platform,
                "supported": True,
            },
        )

    def status(self) -> PCServiceResult:
        data = {
            "status": "ok",
            "source": "mock",
            "checks": {
                "device_actions": "safe",
                "shell_execution": "disabled",
                "remote_control": "disabled",
            },
        }
        return PCServiceResult(
            success=True,
            text="System status mock: ok.",
            data=data,
            metadata={"safe": True, "mock": True},
        )

    def _current_platform(self) -> str:
        return str(self._platform_system() or "").strip() or "unknown"


def _app_to_dict(app: Any) -> Dict[str, Any]:
    if hasattr(app, "to_dict") and callable(app.to_dict):
        return dict(app.to_dict())
    if isinstance(app, Mapping):
        return dict(app)
    return {
        "app_id": _normalize_app_id(getattr(app, "app_id", "")),
        "display_name": str(getattr(app, "display_name", "")),
        "command_placeholder": str(getattr(app, "command_placeholder", "")),
        "enabled": bool(getattr(app, "enabled", False)),
        "requires_confirmation": True,
        "metadata": dict(getattr(app, "metadata", {}) or {}),
    }


def _unsafe_app_id_input(value: Any) -> bool:
    text = str(value or "")
    return bool(re.search(r"[\\/:;&|<>`$\r\n]", text) or ".." in text)


def _normalize_app_id(value: Any) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _launch_windows_app(app: Any) -> bool:
    app_data = _app_to_dict(app)
    command = str(app_data.get("command_placeholder") or "").strip()
    _validate_windows_launch_command(command)
    subprocess.Popen([command], shell=False, close_fds=True)
    return True


def _validate_windows_launch_command(command: str) -> None:
    if not command:
        raise ValueError("missing_windows_launch_command")
    if command.startswith("placeholder://"):
        raise ValueError("missing_windows_launch_command")
    if re.search(r"[;&|<>`$\r\n]", command):
        raise ValueError("unsafe_windows_launch_command")


def _lock_windows_session() -> bool:
    return bool(ctypes.windll.user32.LockWorkStation())


def _sleep_windows_session() -> bool:
    return bool(ctypes.windll.powrprof.SetSuspendState(False, True, False))
