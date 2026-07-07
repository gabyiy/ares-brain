import re
from typing import Dict

from core.DeviceAction import LocalDeviceActionAdapter
from skills.base import Skill, SkillContext, SkillResponse


class DeviceActionSkill(Skill):
    name = "device_action"
    description = "Runs safe mock-only local device actions."
    version = "0.1"
    intent_names = ("device_action",)
    run_before_intents = True
    triggers = (
        "echo",
        "list device actions",
        "device actions",
        "system status",
        "device status",
        "shutdown",
        "restart",
        "sleep",
        "lock",
        "run command",
        "open app",
        "arbitrary shell",
    )
    selection_keywords = (
        "device",
        "status",
        "actions",
    )
    selection_priority = 0.12

    def __init__(self, adapter=None):
        self.adapter = adapter or LocalDeviceActionAdapter()

    def can_handle(self, text: str) -> bool:
        return bool(_parse_device_action_text(text))

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        parsed = self._parse_from_context(text, context)
        if not parsed:
            return self._response(
                "Device action is not available.",
                error="unknown_device_action",
            )

        if parsed["dangerous"]:
            return self._response(
                f"I cannot run that device action safely: {parsed['reason']}.",
                error=parsed["reason"],
                action_name=parsed["action_name"],
                dangerous=True,
            )

        adapter = getattr(context, "device_action_adapter", None) or self.adapter
        action_name = parsed["action_name"]
        parameters = dict(parsed.get("parameters") or {})
        result = adapter.execute(action_name, parameters)
        if not result.success:
            return self._response(
                result.text or result.error_message,
                error=result.error_message,
                action_name=result.action_name,
                data=dict(result.data),
                action_metadata=dict(result.metadata),
            )

        return self._response(
            result.text,
            action_name=result.action_name,
            data=dict(result.data),
            action_metadata=dict(result.metadata),
        )

    def _parse_from_context(self, text: str, context: SkillContext) -> Dict[str, object]:
        intent = context.metadata.get("intent") if context.metadata else None
        if getattr(intent, "intent_name", None) == "device_action":
            entities = dict(getattr(intent, "extracted_entities", {}) or {})
            return _normalize_parsed_device_action(entities, fallback_text=text)
        return _parse_device_action_text(text)

    def _response(self, text: str, **metadata) -> SkillResponse:
        return SkillResponse(text=text, skill=self.name, metadata=metadata)


def _parse_device_action_text(text: str) -> Dict[str, object]:
    raw_text = (text or "").strip()
    normalized = _normalize(raw_text)
    if not normalized:
        return {}

    dangerous_reason = _dangerous_reason(normalized)
    if dangerous_reason:
        return {
            "action": "reject",
            "action_name": _first_token(normalized),
            "parameters": {},
            "dangerous": True,
            "reason": dangerous_reason,
        }

    if normalized.startswith("echo "):
        message = raw_text.strip()[len("echo") :].strip()
        if not message:
            return {
                "action": "echo",
                "action_name": "echo",
                "parameters": {},
                "dangerous": False,
                "reason": "missing_echo_text",
            }
        return {
            "action": "echo",
            "action_name": "echo",
            "parameters": {"message": message},
            "dangerous": False,
            "reason": "",
        }

    if normalized in {"list device actions", "show device actions", "list available device actions"}:
        return {
            "action": "list",
            "action_name": "list_actions",
            "parameters": {},
            "dangerous": False,
            "reason": "",
        }

    if normalized in {"system status", "device status"}:
        return {
            "action": "status",
            "action_name": "system_status_mock",
            "parameters": {},
            "dangerous": False,
            "reason": "",
        }

    if normalized.startswith("device action "):
        action_name = _normalize_action_name(normalized[len("device action ") :])
        return {
            "action": "execute",
            "action_name": action_name,
            "parameters": {},
            "dangerous": False,
            "reason": "",
        }

    return {}


def _normalize_parsed_device_action(entities: Dict[str, object], fallback_text: str) -> Dict[str, object]:
    if not entities:
        return _parse_device_action_text(fallback_text)

    action_name = str(entities.get("action_name") or "").strip()
    parameters = dict(entities.get("parameters") or {})
    if not action_name:
        parsed = _parse_device_action_text(fallback_text)
        action_name = str(parsed.get("action_name") or "")
        parameters = dict(parsed.get("parameters") or parameters)

    return {
        "action": str(entities.get("action") or "execute"),
        "action_name": action_name,
        "parameters": parameters,
        "dangerous": bool(entities.get("dangerous")),
        "reason": str(entities.get("reason") or ""),
    }


def _dangerous_reason(normalized_text: str) -> str:
    if normalized_text in {"shutdown", "restart", "sleep", "lock", "arbitrary shell"}:
        return f"{normalized_text} is not available"
    if normalized_text.startswith("run command"):
        return "run command is not available"
    if normalized_text.startswith("open app"):
        return "open app is not available"
    if normalized_text == "delete" or normalized_text.startswith("delete "):
        return "delete is not available as a device action"
    if "arbitrary shell" in normalized_text or normalized_text.startswith("shell "):
        return "arbitrary shell is not available"
    return ""


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", (value or "").lower()))


def _normalize_action_name(value: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _first_token(value: str) -> str:
    tokens = value.split()
    return tokens[0] if tokens else ""
