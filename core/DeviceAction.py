import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from core.PCService import PCService, PCServiceResult, WindowsPCService


DeviceActionHandler = Callable[[Mapping[str, Any]], "DeviceActionResult"]
AppLauncherHandler = Callable[["AppLaunchConfig"], bool]
APP_ALLOWLIST_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "apps.json"

DANGER_SAFE = "safe"
DANGER_CONFIRMATION_REQUIRED = "confirmation_required"
DANGER_FORBIDDEN = "forbidden"

_CONFIRMATION_REQUIRED_ACTIONS = {
    "lock_pc",
    "open_app",
    "restart",
    "shutdown",
    "sleep_pc",
}
_IMPLEMENTED_CONFIRMATION_ACTIONS = {"lock_pc", "open_app", "sleep_pc"}
_FORBIDDEN_ACTIONS = {
    "arbitrary_shell",
    "command",
    "delete",
    "exec",
    "format",
    "poweroff",
    "reboot",
    "run",
    "run_command",
    "shell",
}


@dataclass(frozen=True)
class DeviceAction:
    name: str
    description: str
    danger_classification: str = DANGER_SAFE
    requires_confirmation: bool = False
    dangerous: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "danger_classification": self.danger_classification,
            "requires_confirmation": bool(self.requires_confirmation),
            "dangerous": bool(self.dangerous),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AppLaunchConfig:
    app_id: str
    display_name: str
    command_placeholder: str
    enabled: bool = False
    requires_confirmation: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_id": _normalize_app_id(self.app_id),
            "display_name": self.display_name,
            "command_placeholder": self.command_placeholder,
            "enabled": bool(self.enabled),
            "requires_confirmation": bool(self.requires_confirmation),
            "metadata": dict(self.metadata),
        }


class AppAllowlistConfigError(ValueError):
    """Raised when app launcher allowlist config is invalid."""


class AppAllowlistLoader:
    def __init__(self, path: Optional[Any] = None):
        self.path = Path(path) if path is not None else APP_ALLOWLIST_CONFIG_PATH

    def load(self) -> List[AppLaunchConfig]:
        try:
            raw_config = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise AppAllowlistConfigError(f"App allowlist config not found: {self.path}") from error
        except OSError as error:
            raise AppAllowlistConfigError(f"App allowlist config could not be read: {self.path}") from error
        except json.JSONDecodeError as error:
            raise AppAllowlistConfigError(f"App allowlist config is not valid JSON: {self.path}") from error

        if not isinstance(raw_config, Mapping):
            raise AppAllowlistConfigError("App allowlist config must be a JSON object")
        apps = raw_config.get("apps")
        if not isinstance(apps, list):
            raise AppAllowlistConfigError("App allowlist config requires an apps list")

        configs: List[AppLaunchConfig] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(apps):
            config = self._parse_app(item, index)
            app_id = _normalize_app_id(config.app_id)
            if app_id in seen_ids:
                raise AppAllowlistConfigError(f"Duplicate app_id in app allowlist: {app_id}")
            seen_ids.add(app_id)
            configs.append(config)
        return configs

    def _parse_app(self, item: Any, index: int) -> AppLaunchConfig:
        if not isinstance(item, Mapping):
            raise AppAllowlistConfigError(f"App allowlist entry {index} must be an object")

        app_id = self._required_text(item, "app_id", index)
        normalized_app_id = _normalize_app_id(app_id)
        if not normalized_app_id:
            raise AppAllowlistConfigError(f"App allowlist entry {index} requires a valid app_id")

        display_name = self._required_text(item, "display_name", index)
        command_placeholder = self._required_command(item, index)
        enabled = self._required_bool(item, "enabled", index)
        requires_confirmation = self._required_bool(item, "requires_confirmation", index)
        if not requires_confirmation:
            raise AppAllowlistConfigError(
                f"App allowlist entry {index} must require confirmation"
            )

        metadata = item.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise AppAllowlistConfigError(f"App allowlist entry {index} metadata must be an object")

        return AppLaunchConfig(
            app_id=normalized_app_id,
            display_name=display_name,
            command_placeholder=command_placeholder,
            enabled=enabled,
            requires_confirmation=True,
            metadata=dict(metadata),
        )

    def _required_text(self, item: Mapping[str, Any], key: str, index: int) -> str:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            raise AppAllowlistConfigError(f"App allowlist entry {index} requires {key}")
        return value.strip()

    def _required_bool(self, item: Mapping[str, Any], key: str, index: int) -> bool:
        if key not in item or not isinstance(item.get(key), bool):
            raise AppAllowlistConfigError(f"App allowlist entry {index} requires boolean {key}")
        return bool(item[key])

    def _required_command(self, item: Mapping[str, Any], index: int) -> str:
        command_keys = (
            "command",
            "path",
            "command_placeholder",
            "command_path_placeholder",
            "path_placeholder",
        )
        for key in command_keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise AppAllowlistConfigError(f"App allowlist entry {index} requires command/path")


@dataclass(frozen=True)
class DeviceActionConfirmationRequest:
    token: str
    action_name: str
    classification: str
    reason: str
    prompt: str

    @classmethod
    def create(cls, action_name: str, reason: str):
        normalized = _device_action_alias(action_name)
        prompt = _confirmation_prompt(normalized)
        return cls(
            token=f"device-action-confirmation:{normalized}",
            action_name=normalized,
            classification=DANGER_CONFIRMATION_REQUIRED,
            reason=reason,
            prompt=prompt,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "action_name": self.action_name,
            "classification": self.classification,
            "reason": self.reason,
            "prompt": self.prompt,
        }


@dataclass(frozen=True)
class DeviceActionSafetyDecision:
    action_name: str
    classification: str
    reason: str = ""

    @property
    def is_safe(self) -> bool:
        return self.classification == DANGER_SAFE

    @property
    def requires_confirmation(self) -> bool:
        return self.classification == DANGER_CONFIRMATION_REQUIRED

    @property
    def forbidden(self) -> bool:
        return self.classification == DANGER_FORBIDDEN

    def confirmation_request(self) -> Optional[DeviceActionConfirmationRequest]:
        if not self.requires_confirmation:
            return None
        return DeviceActionConfirmationRequest.create(self.action_name, self.reason)

    def to_dict(self) -> Dict[str, Any]:
        request = self.confirmation_request()
        return {
            "action_name": self.action_name,
            "classification": self.classification,
            "reason": self.reason,
            "confirmation_request": request.to_dict() if request else None,
        }


@dataclass(frozen=True)
class DeviceActionResult:
    action_name: str
    success: bool
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_name": self.action_name,
            "success": bool(self.success),
            "text": self.text,
            "data": dict(self.data),
            "error_message": self.error_message,
            "metadata": dict(self.metadata),
        }


class DeviceActionRegistry:
    def __init__(self, actions: Optional[Iterable[tuple[DeviceAction, DeviceActionHandler]]] = None):
        self._actions: Dict[str, DeviceAction] = {}
        self._handlers: Dict[str, DeviceActionHandler] = {}
        for action, handler in actions or ():
            self.register(action, handler)

    def register(self, action: DeviceAction, handler: DeviceActionHandler) -> DeviceAction:
        if not isinstance(action, DeviceAction):
            raise TypeError("Device action must be a DeviceAction")
        if not callable(handler):
            raise TypeError("Device action handler must be callable")

        name = _normalize_action_name(action.name)
        if not name:
            raise ValueError("Device action name is required")
        safety = classify_device_action(name)
        requested_classification = _normalize_classification(action.danger_classification)
        confirmation_action_allowed = (
            name in _IMPLEMENTED_CONFIRMATION_ACTIONS
            and safety.requires_confirmation
            and requested_classification == DANGER_CONFIRMATION_REQUIRED
            and action.requires_confirmation
        )
        if not confirmation_action_allowed:
            if not safety.is_safe or requested_classification != DANGER_SAFE or action.dangerous:
                raise ValueError("Non-safe device actions are not implemented yet")
            if action.requires_confirmation:
                raise ValueError("Confirmed device actions are not implemented yet")
        if name in self._actions:
            raise ValueError(f"Device action already registered: {name}")

        normalized = DeviceAction(
            name=name,
            description=action.description,
            danger_classification=safety.classification,
            requires_confirmation=action.requires_confirmation,
            dangerous=action.dangerous,
            metadata=dict(action.metadata),
        )
        self._actions[name] = normalized
        self._handlers[name] = handler
        return normalized

    def get(self, name: str) -> Optional[DeviceAction]:
        return self._actions.get(_normalize_action_name(name))

    def list_actions(self) -> List[DeviceAction]:
        return list(self._actions.values())

    def execute(self, name: str, parameters: Optional[Mapping[str, Any]] = None) -> DeviceActionResult:
        action_name = _device_action_alias(name)
        clean_parameters = dict(parameters or {})
        safety = classify_device_action(action_name)
        approved_confirmation_action = (
            action_name in _IMPLEMENTED_CONFIRMATION_ACTIONS
            and bool(clean_parameters.get("confirmation_approved"))
        )
        if safety.requires_confirmation and not approved_confirmation_action:
            return _confirmation_required_result(safety)
        if safety.forbidden:
            return _forbidden_result(safety)

        action = self._actions.get(action_name)
        if not action:
            return DeviceActionResult(
                action_name=action_name,
                success=False,
                text=f"Device action is not available: {action_name}",
                error_message=f"Device action is not available: {action_name}",
                metadata={"missing_action": action_name},
            )

        handler = self._handlers[action_name]
        return handler(clean_parameters)


class LocalDeviceActionAdapter:
    name = "local_device_action"
    description = "Local device action adapter with safe and explicitly confirmed built-in actions."

    def __init__(
        self,
        registry: Optional[DeviceActionRegistry] = None,
        lock_impl: Optional[Callable[[], bool]] = None,
        sleep_impl: Optional[Callable[[], bool]] = None,
        app_launcher: Optional[AppLauncherHandler] = None,
        app_allowlist: Optional[Iterable[Any]] = None,
        app_allowlist_path: Optional[Any] = None,
        platform_system: Optional[Callable[[], str]] = None,
        pc_service: Optional[PCService] = None,
    ):
        self.registry = registry or DeviceActionRegistry()
        loaded_allowlist = (
            app_allowlist
            if app_allowlist is not None
            else AppAllowlistLoader(app_allowlist_path).load()
        )
        self._app_allowlist = _build_app_allowlist(loaded_allowlist)
        self._pc_service = pc_service or WindowsPCService(
            lock_impl=lock_impl,
            sleep_impl=sleep_impl,
            app_launcher=app_launcher,
            app_resolver=self._app_allowlist.get,
            platform_system=platform_system,
        )
        if registry is None:
            self._register_safe_builtins()
            self._register_confirmation_builtins()

    def execute(self, action_name: str, parameters: Optional[Mapping[str, Any]] = None) -> DeviceActionResult:
        return self.registry.execute(_adapter_action_alias(action_name), parameters)

    def list_actions(self) -> List[DeviceAction]:
        return self.registry.list_actions()

    def list_apps(self) -> List[AppLaunchConfig]:
        return list(self._app_allowlist.values())

    def classify(self, action_name: str) -> DeviceActionSafetyDecision:
        return classify_device_action(_adapter_action_alias(action_name))

    def _register_safe_builtins(self) -> None:
        self.registry.register(
            DeviceAction(
                name="echo",
                description="Return the provided message without executing commands.",
            ),
            _echo_action,
        )
        self.registry.register(
            DeviceAction(
                name="system_status_mock",
                description="Return deterministic mock system status without inspecting the host.",
            ),
            lambda parameters: _device_action_result(
                "system_status_mock",
                self._pc_service.status(),
            ),
        )
        self.registry.register(
            DeviceAction(
                name="list_actions",
                description="List available safe local device actions.",
            ),
            lambda parameters: _list_actions_action(self.registry),
        )
        self.registry.register(
            DeviceAction(
                name="list_apps",
                description="List allowlisted local apps without launching them.",
            ),
            lambda parameters: _list_apps_action(self.list_apps()),
        )

    def _register_confirmation_builtins(self) -> None:
        self.registry.register(
            DeviceAction(
                name="lock_pc",
                description="Lock the current Windows session after explicit confirmation.",
                danger_classification=DANGER_CONFIRMATION_REQUIRED,
                requires_confirmation=True,
                dangerous=True,
                metadata={"platform": "windows"},
            ),
            self._lock_pc_action,
        )
        self.registry.register(
            DeviceAction(
                name="sleep_pc",
                description="Put the Windows PC to sleep after explicit confirmation.",
                danger_classification=DANGER_CONFIRMATION_REQUIRED,
                requires_confirmation=True,
                dangerous=True,
                metadata={"platform": "windows"},
            ),
            self._sleep_pc_action,
        )
        self.registry.register(
            DeviceAction(
                name="open_app",
                description="Open an enabled allowlisted Windows app after explicit confirmation.",
                danger_classification=DANGER_CONFIRMATION_REQUIRED,
                requires_confirmation=True,
                dangerous=True,
                metadata={"allowlist_only": True, "platform": "windows"},
            ),
            self._open_app_action,
        )

    def _lock_pc_action(self, parameters: Mapping[str, Any]) -> DeviceActionResult:
        if not bool(parameters.get("confirmation_approved")):
            return _confirmation_required_result(classify_device_action("lock_pc"))

        return _device_action_result("lock_pc", self._pc_service.lock())

    def _sleep_pc_action(self, parameters: Mapping[str, Any]) -> DeviceActionResult:
        if not bool(parameters.get("confirmation_approved")):
            return _confirmation_required_result(classify_device_action("sleep_pc"))

        return _device_action_result("sleep_pc", self._pc_service.sleep())

    def _open_app_action(self, parameters: Mapping[str, Any]) -> DeviceActionResult:
        if not bool(parameters.get("confirmation_approved")):
            return _confirmation_required_result(classify_device_action("open_app"))

        raw_app_id = parameters.get("app_id") or parameters.get("app") or parameters.get("name")
        return _device_action_result("open_app", self._pc_service.open_app(raw_app_id))


def _echo_action(parameters: Mapping[str, Any]) -> DeviceActionResult:
    message = str(parameters.get("message") or parameters.get("text") or "").strip()
    return DeviceActionResult(
        action_name="echo",
        success=True,
        text=message,
        data={"message": message},
        metadata={"safe": True},
    )


def _device_action_result(action_name: str, service_result: PCServiceResult) -> DeviceActionResult:
    return DeviceActionResult(
        action_name=action_name,
        success=service_result.success,
        text=service_result.text,
        data=dict(service_result.data),
        error_message=service_result.error_message,
        metadata=dict(service_result.metadata),
    )


def _list_actions_action(registry: DeviceActionRegistry) -> DeviceActionResult:
    actions = [action.to_dict() for action in registry.list_actions()]
    return DeviceActionResult(
        action_name="list_actions",
        success=True,
        text=f"Available device actions: {', '.join(action['name'] for action in actions)}.",
        data={"actions": actions},
        metadata={"safe": True},
    )


def _list_apps_action(apps: Iterable[AppLaunchConfig]) -> DeviceActionResult:
    app_dicts = [app.to_dict() for app in apps]
    labels = [
        app["app_id"] if app["enabled"] else f"{app['app_id']} (disabled)"
        for app in app_dicts
    ]
    return DeviceActionResult(
        action_name="list_apps",
        success=True,
        text=f"Allowlisted apps: {', '.join(labels)}.",
        data={"apps": app_dicts},
        metadata={"safe": True, "allowlist_only": True},
    )


def classify_device_action(action_name: str) -> DeviceActionSafetyDecision:
    normalized = _device_action_alias(action_name)
    if normalized in _CONFIRMATION_REQUIRED_ACTIONS:
        return DeviceActionSafetyDecision(
            action_name=normalized,
            classification=DANGER_CONFIRMATION_REQUIRED,
            reason=_confirmation_reason(normalized),
        )
    forbidden_name = _forbidden_action_alias(normalized)
    if forbidden_name:
        return DeviceActionSafetyDecision(
            action_name=forbidden_name,
            classification=DANGER_FORBIDDEN,
            reason=f"{forbidden_name} is forbidden and is not implemented",
        )
    return DeviceActionSafetyDecision(
        action_name=normalized,
        classification=DANGER_SAFE,
    )


def _confirmation_required_result(safety: DeviceActionSafetyDecision) -> DeviceActionResult:
    request = safety.confirmation_request()
    return DeviceActionResult(
        action_name=safety.action_name,
        success=False,
        text=f'Confirmation required for device action "{safety.action_name}". Device action was not executed.',
        error_message="confirmation_required",
        metadata={
            "danger_classification": DANGER_CONFIRMATION_REQUIRED,
            "confirmation_required": True,
            "executed": False,
            "reason": safety.reason,
            "confirmation_request": request.to_dict() if request else None,
        },
    )


def _forbidden_result(safety: DeviceActionSafetyDecision) -> DeviceActionResult:
    return DeviceActionResult(
        action_name=safety.action_name,
        success=False,
        text=f'Device action "{safety.action_name}" is forbidden and was not executed.',
        error_message="forbidden",
        metadata={
            "danger_classification": DANGER_FORBIDDEN,
            "forbidden": True,
            "executed": False,
            "reason": safety.reason,
        },
    )


def _adapter_action_alias(action_name: str) -> str:
    normalized = _device_action_alias(action_name)
    aliases = {
        "list_available_actions": "list_actions",
        "list_available_apps": "list_apps",
        "show_apps": "list_apps",
        "show_available_actions": "list_actions",
        "system_status": "system_status_mock",
        "status": "system_status_mock",
    }
    return aliases.get(normalized, normalized)


def _device_action_alias(action_name: str) -> str:
    normalized = _normalize_action_name(action_name)
    aliases = {
        "lock": "lock_pc",
        "lock_computer": "lock_pc",
        "lock_session": "lock_pc",
        "lock_windows": "lock_pc",
        "lock_windows_session": "lock_pc",
        "sleep": "sleep_pc",
        "sleep_computer": "sleep_pc",
        "sleep_session": "sleep_pc",
        "sleep_windows": "sleep_pc",
        "sleep_windows_pc": "sleep_pc",
    }
    return aliases.get(normalized, normalized)


def _confirmation_reason(action_name: str) -> str:
    if action_name == "lock_pc":
        return "lock_pc requires explicit confirmation before locking the Windows session"
    if action_name == "open_app":
        return "open_app requires explicit confirmation before opening an allowlisted Windows app"
    if action_name == "sleep_pc":
        return "sleep_pc requires explicit confirmation before putting Windows to sleep"
    return f"{action_name} requires explicit confirmation and is not implemented"


def _confirmation_prompt(action_name: str) -> str:
    if action_name == "lock_pc":
        return (
            'Confirmation required for device action "lock_pc". '
            "This will lock the current Windows session if confirmed."
        )
    if action_name == "open_app":
        return (
            'Confirmation required for device action "open_app". '
            "This will open an enabled allowlisted Windows app if confirmed."
        )
    if action_name == "sleep_pc":
        return (
            'Confirmation required for device action "sleep_pc". '
            "This will put the Windows PC to sleep if confirmed."
        )
    return (
        f'Confirmation required for device action "{action_name}". '
        "This placeholder cannot execute real OS commands yet."
    )


def _forbidden_action_alias(action_name: str) -> str:
    normalized = _normalize_action_name(action_name)
    if normalized in _FORBIDDEN_ACTIONS:
        return normalized
    for prefix in _FORBIDDEN_ACTIONS:
        if normalized.startswith(f"{prefix}_"):
            return prefix
    return ""


def _normalize_classification(value: str) -> str:
    normalized = str(value or DANGER_SAFE).strip().lower()
    if normalized in {DANGER_SAFE, DANGER_CONFIRMATION_REQUIRED, DANGER_FORBIDDEN}:
        return normalized
    return DANGER_FORBIDDEN


def _normalize_action_name(name: str) -> str:
    return "_".join(str(name or "").strip().lower().replace("-", " ").split())


def _normalize_app_id(value: Any) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _build_app_allowlist(app_allowlist: Iterable[Any]) -> Dict[str, AppLaunchConfig]:
    configs: Dict[str, AppLaunchConfig] = {}
    for item in app_allowlist:
        config = _coerce_app_config(item)
        app_id = _normalize_app_id(config.app_id)
        if not app_id:
            raise ValueError("App allowlist entries require an app_id")
        if app_id in configs:
            raise ValueError(f"Duplicate app_id in app allowlist: {app_id}")
        configs[app_id] = AppLaunchConfig(
            app_id=app_id,
            display_name=config.display_name.strip() or app_id,
            command_placeholder=config.command_placeholder.strip() or "placeholder://missing",
            enabled=bool(config.enabled),
            requires_confirmation=True,
            metadata=dict(config.metadata),
        )
    return configs


def _coerce_app_config(item: Any) -> AppLaunchConfig:
    if isinstance(item, AppLaunchConfig):
        return item
    if isinstance(item, Mapping):
        app_id = str(item.get("app_id") or "")
        return AppLaunchConfig(
            app_id=app_id,
            display_name=str(item.get("display_name") or app_id),
            command_placeholder=str(
                item.get("command_placeholder")
                or item.get("command_path_placeholder")
                or item.get("path_placeholder")
                or item.get("command")
                or item.get("path")
                or "placeholder://missing"
            ),
            enabled=bool(item.get("enabled", False)),
            requires_confirmation=True,
            metadata=dict(item.get("metadata") or {}),
        )
    raise TypeError("App allowlist entries must be AppLaunchConfig or mapping objects")
