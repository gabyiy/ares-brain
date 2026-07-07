from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional


DeviceActionHandler = Callable[[Mapping[str, Any]], "DeviceActionResult"]

_DANGEROUS_ACTION_NAMES = {
    "command",
    "exec",
    "format",
    "poweroff",
    "reboot",
    "restart",
    "run",
    "shell",
    "shutdown",
}


@dataclass(frozen=True)
class DeviceAction:
    name: str
    description: str
    requires_confirmation: bool = False
    dangerous: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "requires_confirmation": bool(self.requires_confirmation),
            "dangerous": bool(self.dangerous),
            "metadata": dict(self.metadata),
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
        if name in _DANGEROUS_ACTION_NAMES or action.dangerous:
            raise ValueError("Dangerous device actions are not implemented yet")
        if action.requires_confirmation:
            raise ValueError("Confirmed device actions are not implemented yet")
        if name in self._actions:
            raise ValueError(f"Device action already registered: {name}")

        normalized = DeviceAction(
            name=name,
            description=action.description,
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
        action_name = _normalize_action_name(name)
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
        return handler(dict(parameters or {}))


class LocalDeviceActionAdapter:
    name = "local_device_action"
    description = "Safe local device action adapter with mock-only built-in actions."

    def __init__(self, registry: Optional[DeviceActionRegistry] = None):
        self.registry = registry or DeviceActionRegistry()
        if registry is None:
            self._register_safe_builtins()

    def execute(self, action_name: str, parameters: Optional[Mapping[str, Any]] = None) -> DeviceActionResult:
        return self.registry.execute(_adapter_action_alias(action_name), parameters)

    def list_actions(self) -> List[DeviceAction]:
        return self.registry.list_actions()

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


def _adapter_action_alias(action_name: str) -> str:
    normalized = _normalize_action_name(action_name)
    aliases = {
        "list_available_actions": "list_actions",
        "show_available_actions": "list_actions",
        "system_status": "system_status_mock",
        "status": "system_status_mock",
    }
    return aliases.get(normalized, normalized)


def _normalize_action_name(name: str) -> str:
    return "_".join(str(name or "").strip().lower().replace("-", " ").split())
