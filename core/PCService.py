import ctypes
import getpass
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional


AppLauncherHandler = Callable[[Any], bool]
AppResolver = Callable[[str], Optional[Any]]
AvailableActionsProvider = Callable[[], Iterable[str]]
CapabilityActionsProvider = Callable[[], Iterable[Any]]
ApplicationsProvider = Callable[[], Iterable[Any]]


@dataclass(frozen=True)
class PCServiceResult:
    success: bool
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PCStatus:
    status: str
    operating_system: str
    hostname: str
    current_user: str
    python_version: str
    uptime_seconds: Optional[float] = None
    available_actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "source": "pc_service",
            "operating_system": self.operating_system,
            "hostname": self.hostname,
            "current_user": self.current_user,
            "python_version": self.python_version,
            "uptime_seconds": self.uptime_seconds,
            "available_actions": list(self.available_actions),
            "checks": {
                "device_actions": "safe",
                "shell_execution": "disabled",
                "remote_control": "disabled",
                "network_access": "not_used",
                "process_enumeration": "disabled",
                "hardware_telemetry": "disabled",
            },
        }


@dataclass(frozen=True)
class PCCapabilities:
    supported_device_actions: List[Dict[str, Any]] = field(default_factory=list)
    supported_applications: List[Dict[str, Any]] = field(default_factory=list)
    available_status_providers: List[str] = field(default_factory=list)
    available_services: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": "pc_service",
            "supported_device_actions": [dict(action) for action in self.supported_device_actions],
            "supported_applications": [dict(app) for app in self.supported_applications],
            "available_status_providers": list(self.available_status_providers),
            "available_services": list(self.available_services),
            "safeguards": {
                "network_access": "not_used",
                "internet": "disabled",
                "remote_execution": "disabled",
                "process_enumeration": "disabled",
                "hardware_telemetry": "disabled",
                "arbitrary_shell": "disabled",
            },
        }


class PCService:
    def lock(self) -> PCServiceResult:
        raise NotImplementedError

    def sleep(self) -> PCServiceResult:
        raise NotImplementedError

    def open_app(self, app_id: Any) -> PCServiceResult:
        raise NotImplementedError

    def get_status(self) -> PCServiceResult:
        raise NotImplementedError

    def get_capabilities(self) -> PCServiceResult:
        raise NotImplementedError

    def status(self) -> PCServiceResult:
        return self.get_status()

    def health_check(self) -> PCServiceResult:
        return self.get_status()


class WindowsPCService(PCService):
    def __init__(
        self,
        lock_impl: Optional[Callable[[], bool]] = None,
        sleep_impl: Optional[Callable[[], bool]] = None,
        app_launcher: Optional[AppLauncherHandler] = None,
        app_resolver: Optional[AppResolver] = None,
        platform_system: Optional[Callable[[], str]] = None,
        hostname_provider: Optional[Callable[[], str]] = None,
        current_user_provider: Optional[Callable[[], str]] = None,
        python_version_provider: Optional[Callable[[], str]] = None,
        uptime_provider: Optional[Callable[[], Optional[float]]] = None,
        available_actions_provider: Optional[AvailableActionsProvider] = None,
        capability_actions_provider: Optional[CapabilityActionsProvider] = None,
        applications_provider: Optional[ApplicationsProvider] = None,
        status_providers_provider: Optional[AvailableActionsProvider] = None,
        services_provider: Optional[AvailableActionsProvider] = None,
    ):
        self._lock_impl = lock_impl or _lock_windows_session
        self._sleep_impl = sleep_impl or _sleep_windows_session
        self._app_launcher = app_launcher or _launch_windows_app
        self._app_resolver = app_resolver or (lambda app_id: None)
        self._platform_system = platform_system or platform.system
        self._hostname_provider = hostname_provider or platform.node
        self._current_user_provider = current_user_provider or _current_user
        self._python_version_provider = python_version_provider or _python_version
        self._uptime_provider = uptime_provider
        self._available_actions_provider = available_actions_provider or (lambda: [])
        self._capability_actions_provider = capability_actions_provider or (lambda: [])
        self._applications_provider = applications_provider or (lambda: [])
        self._status_providers_provider = status_providers_provider or (lambda: ["pc_status"])
        self._services_provider = services_provider or (
            lambda: ["pc_service", "windows_pc_service"]
        )

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

    def get_status(self) -> PCServiceResult:
        status = PCStatus(
            status="ok",
            operating_system=self._current_platform(),
            hostname=_safe_text(self._hostname_provider(), fallback="unknown"),
            current_user=_safe_text(self._current_user_provider(), fallback="unknown"),
            python_version=_safe_text(self._python_version_provider(), fallback="unknown"),
            uptime_seconds=self._status_uptime_seconds(),
            available_actions=_unique_action_names(self._available_actions_provider()),
        )
        return PCServiceResult(
            success=True,
            text="System status: ok.",
            data=status.to_dict(),
            metadata={"safe": True, "source": "pc_service"},
        )

    def get_capabilities(self) -> PCServiceResult:
        capabilities = PCCapabilities(
            supported_device_actions=_capability_action_dicts(
                self._capability_actions_provider(),
                self._available_actions_provider(),
            ),
            supported_applications=_capability_application_dicts(self._applications_provider()),
            available_status_providers=_unique_action_names(self._status_providers_provider()),
            available_services=_unique_action_names(self._services_provider()),
        )
        return PCServiceResult(
            success=True,
            text="PC capabilities discovered.",
            data=capabilities.to_dict(),
            metadata={"safe": True, "source": "pc_service"},
        )

    def _current_platform(self) -> str:
        return str(self._platform_system() or "").strip() or "unknown"

    def _status_uptime_seconds(self) -> Optional[float]:
        if self._uptime_provider is not None:
            return self._uptime_provider()
        return _windows_uptime_seconds(self._current_platform())


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


def _safe_text(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def _unique_action_names(action_names: Iterable[str]) -> List[str]:
    unique: List[str] = []
    seen: set[str] = set()
    for action_name in action_names:
        name = str(action_name or "").strip()
        if name and name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def _capability_action_dicts(
    capability_actions: Iterable[Any],
    fallback_action_names: Iterable[str],
) -> List[Dict[str, Any]]:
    action_dicts = [_action_to_dict(action) for action in capability_actions]
    if not action_dicts:
        action_dicts = [{"name": name} for name in _unique_action_names(fallback_action_names)]
    return _unique_dicts_by_key(action_dicts, "name")


def _capability_application_dicts(applications: Iterable[Any]) -> List[Dict[str, Any]]:
    return _unique_dicts_by_key((_app_to_dict(app) for app in applications), "app_id")


def _action_to_dict(action: Any) -> Dict[str, Any]:
    if hasattr(action, "to_dict") and callable(action.to_dict):
        data = dict(action.to_dict())
    elif isinstance(action, Mapping):
        data = dict(action)
    else:
        data = {"name": str(action or "").strip()}
    data["name"] = str(data.get("name") or "").strip()
    return data


def _unique_dicts_by_key(items: Iterable[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    unique: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        value = str(item.get(key) or "").strip()
        if value and value not in seen:
            seen.add(value)
            unique.append(dict(item))
    return unique


def _current_user() -> str:
    try:
        return getpass.getuser()
    except (ImportError, KeyError, OSError):
        return "unknown"


def _python_version() -> str:
    return ".".join(str(part) for part in sys.version_info[:3])


def _windows_uptime_seconds(current_platform: str) -> Optional[float]:
    if str(current_platform or "").strip().lower() != "windows":
        return None
    try:
        milliseconds = int(ctypes.windll.kernel32.GetTickCount64())
    except (AttributeError, OSError):
        return None
    return round(milliseconds / 1000, 3)


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
