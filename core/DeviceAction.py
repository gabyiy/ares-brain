import ctypes
import platform
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional


DeviceActionHandler = Callable[[Mapping[str, Any]], "DeviceActionResult"]

DANGER_SAFE = "safe"
DANGER_CONFIRMATION_REQUIRED = "confirmation_required"
DANGER_FORBIDDEN = "forbidden"

_CONFIRMATION_REQUIRED_ACTIONS = {
    "lock_pc",
    "open_app",
    "restart",
    "shutdown",
    "sleep",
}
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
            name == "lock_pc"
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
        approved_lock_pc = action_name == "lock_pc" and bool(clean_parameters.get("confirmation_approved"))
        if safety.requires_confirmation and not approved_lock_pc:
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
    description = "Safe local device action adapter with mock-only built-in actions."

    def __init__(
        self,
        registry: Optional[DeviceActionRegistry] = None,
        lock_impl: Optional[Callable[[], bool]] = None,
        platform_system: Optional[Callable[[], str]] = None,
    ):
        self.registry = registry or DeviceActionRegistry()
        self._lock_impl = lock_impl or _lock_windows_session
        self._platform_system = platform_system or platform.system
        if registry is None:
            self._register_safe_builtins()
            self._register_confirmation_builtins()

    def execute(self, action_name: str, parameters: Optional[Mapping[str, Any]] = None) -> DeviceActionResult:
        return self.registry.execute(_adapter_action_alias(action_name), parameters)

    def list_actions(self) -> List[DeviceAction]:
        return self.registry.list_actions()

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
            _system_status_mock_action,
        )
        self.registry.register(
            DeviceAction(
                name="list_actions",
                description="List available safe local device actions.",
            ),
            lambda parameters: _list_actions_action(self.registry),
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

    def _lock_pc_action(self, parameters: Mapping[str, Any]) -> DeviceActionResult:
        if not bool(parameters.get("confirmation_approved")):
            return _confirmation_required_result(classify_device_action("lock_pc"))

        current_platform = str(self._platform_system() or "").strip() or "unknown"
        if current_platform.lower() != "windows":
            return DeviceActionResult(
                action_name="lock_pc",
                success=False,
                text="Windows session lock is unsupported on this platform.",
                error_message="unsupported_platform",
                metadata={
                    "danger_classification": DANGER_CONFIRMATION_REQUIRED,
                    "confirmation_required": True,
                    "executed": False,
                    "platform": current_platform,
                    "supported": False,
                },
            )

        try:
            locked = bool(self._lock_impl())
        except (AttributeError, OSError, RuntimeError) as error:
            return DeviceActionResult(
                action_name="lock_pc",
                success=False,
                text="Windows session lock failed safely.",
                error_message=f"{type(error).__name__}: {error}",
                metadata={
                    "danger_classification": DANGER_CONFIRMATION_REQUIRED,
                    "confirmation_required": True,
                    "executed": False,
                    "platform": current_platform,
                    "supported": True,
                },
            )

        if not locked:
            return DeviceActionResult(
                action_name="lock_pc",
                success=False,
                text="Windows session lock failed safely.",
                error_message="lock_failed",
                metadata={
                    "danger_classification": DANGER_CONFIRMATION_REQUIRED,
                    "confirmation_required": True,
                    "executed": False,
                    "platform": current_platform,
                    "supported": True,
                },
            )

        return DeviceActionResult(
            action_name="lock_pc",
            success=True,
            text="Windows session lock requested.",
            data={"action": "lock_pc"},
            metadata={
                "danger_classification": DANGER_CONFIRMATION_REQUIRED,
                "confirmation_required": True,
                "executed": True,
                "platform": current_platform,
                "supported": True,
            },
        )


def _echo_action(parameters: Mapping[str, Any]) -> DeviceActionResult:
    message = str(parameters.get("message") or parameters.get("text") or "").strip()
    return DeviceActionResult(
        action_name="echo",
        success=True,
        text=message,
        data={"message": message},
        metadata={"safe": True},
    )


def _system_status_mock_action(parameters: Mapping[str, Any]) -> DeviceActionResult:
    data = {
        "status": "ok",
        "source": "mock",
        "checks": {
            "device_actions": "safe",
            "shell_execution": "disabled",
            "remote_control": "disabled",
        },
    }
    return DeviceActionResult(
        action_name="system_status_mock",
        success=True,
        text="System status mock: ok.",
        data=data,
        metadata={"safe": True, "mock": True},
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


def classify_device_action(action_name: str) -> DeviceActionSafetyDecision:
    normalized = _device_action_alias(action_name)
    if normalized in _CONFIRMATION_REQUIRED_ACTIONS:
        return DeviceActionSafetyDecision(
            action_name=normalized,
            classification=DANGER_CONFIRMATION_REQUIRED,
            reason=_confirmation_reason(normalized),
        )
    if normalized in _FORBIDDEN_ACTIONS:
        return DeviceActionSafetyDecision(
            action_name=normalized,
            classification=DANGER_FORBIDDEN,
            reason=f"{normalized} is forbidden and is not implemented",
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
    }
    return aliases.get(normalized, normalized)


def _confirmation_reason(action_name: str) -> str:
    if action_name == "lock_pc":
        return "lock_pc requires explicit confirmation before locking the Windows session"
    return f"{action_name} requires explicit confirmation and is not implemented"


def _confirmation_prompt(action_name: str) -> str:
    if action_name == "lock_pc":
        return (
            'Confirmation required for device action "lock_pc". '
            "This will lock the current Windows session if confirmed."
        )
    return (
        f'Confirmation required for device action "{action_name}". '
        "This placeholder cannot execute real OS commands yet."
    )


def _normalize_classification(value: str) -> str:
    normalized = str(value or DANGER_SAFE).strip().lower()
    if normalized in {DANGER_SAFE, DANGER_CONFIRMATION_REQUIRED, DANGER_FORBIDDEN}:
        return normalized
    return DANGER_FORBIDDEN


def _normalize_action_name(name: str) -> str:
    return "_".join(str(name or "").strip().lower().replace("-", " ").split())


def _lock_windows_session() -> bool:
    return bool(ctypes.windll.user32.LockWorkStation())
