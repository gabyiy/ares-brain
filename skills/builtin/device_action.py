import re
from typing import Dict

from core.DeviceAction import (
    DANGER_CONFIRMATION_REQUIRED,
    DANGER_FORBIDDEN,
    DANGER_SAFE,
    LocalDeviceActionAdapter,
    classify_device_action,
)
from skills.base import Skill, SkillContext, SkillResponse


class DeviceActionSkill(Skill):
    name = "device_action"
    description = "Runs safe or explicitly confirmed local device actions."
    version = "0.1"
    intent_names = ("device_action",)
    run_before_intents = True
    triggers = (
        "echo",
        "list device actions",
        "list apps",
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
        "delete",
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

        classification = parsed["danger_classification"]
        confirmation_approved = bool(parsed.get("confirmation_approved"))
        if classification == DANGER_CONFIRMATION_REQUIRED and not confirmation_approved:
            return self._confirmation_required_response(parsed)

        if classification == DANGER_FORBIDDEN:
            return self._forbidden_response(parsed)

        adapter = getattr(context, "device_action_adapter", None) or self.adapter
        action_name = parsed["action_name"]
        parameters = dict(parsed.get("parameters") or {})
        if confirmation_approved:
            parameters["confirmation_approved"] = True
            if parsed.get("confirmation_id"):
                parameters["confirmation_id"] = parsed["confirmation_id"]
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
            danger_classification=result.metadata.get("danger_classification", DANGER_SAFE),
            executed=result.metadata.get("executed", True),
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

    def _confirmation_required_response(self, parsed: Dict[str, object]) -> SkillResponse:
        action_name = str(parsed["action_name"])
        safety = classify_device_action(action_name)
        request = safety.confirmation_request()
        return self._response(
            f'Confirmation required for device action "{action_name}". Device action was not executed.',
            error="confirmation_required",
            action_name=action_name,
            danger_classification=DANGER_CONFIRMATION_REQUIRED,
            confirmation_required=True,
            executed=False,
            reason=safety.reason,
            confirmation_request=request.to_dict() if request else None,
        )

    def _forbidden_response(self, parsed: Dict[str, object]) -> SkillResponse:
        action_name = str(parsed["action_name"])
        safety = classify_device_action(action_name)
        return self._response(
            f'Device action "{action_name}" is forbidden and was not executed.',
            error="forbidden",
            action_name=action_name,
            danger_classification=DANGER_FORBIDDEN,
            forbidden=True,
            executed=False,
            reason=safety.reason,
        )


def _parse_device_action_text(text: str) -> Dict[str, object]:
    raw_text = (text or "").strip()
    normalized = _normalize(raw_text)
    if not normalized:
        return {}

    if normalized in {"list apps", "show apps", "list available apps"}:
        return {
            "action": "list",
            "action_name": "list_apps",
            "parameters": {},
            "danger_classification": DANGER_SAFE,
            "confirmation_required": False,
            "forbidden": False,
            "reason": "",
        }

    if normalized.startswith("open app"):
        app_id = _normalize_action_name(normalized[len("open app") :])
        safety = classify_device_action("open_app")
        return {
            "action": safety.classification,
            "action_name": safety.action_name,
            "app_id": app_id,
            "parameters": {"app_id": app_id} if app_id else {},
            "danger_classification": safety.classification,
            "confirmation_required": safety.requires_confirmation,
            "forbidden": safety.forbidden,
            "reason": safety.reason,
        }

    action_name = _dangerous_action_name(normalized)
    safety = classify_device_action(action_name) if action_name else None
    if safety and not safety.is_safe:
        return {
            "action": safety.classification,
            "action_name": safety.action_name,
            "parameters": {},
            "danger_classification": safety.classification,
            "confirmation_required": safety.requires_confirmation,
            "forbidden": safety.forbidden,
            "reason": safety.reason,
        }

    if normalized.startswith("echo "):
        message = raw_text.strip()[len("echo") :].strip()
        if not message:
            return {
                "action": "echo",
                "action_name": "echo",
                "parameters": {},
                "danger_classification": DANGER_SAFE,
                "confirmation_required": False,
                "forbidden": False,
                "reason": "missing_echo_text",
            }
        return {
            "action": "echo",
            "action_name": "echo",
            "parameters": {"message": message},
            "danger_classification": DANGER_SAFE,
            "confirmation_required": False,
            "forbidden": False,
            "reason": "",
        }

    if normalized in {"list device actions", "show device actions", "list available device actions"}:
        return {
            "action": "list",
            "action_name": "list_actions",
            "parameters": {},
            "danger_classification": DANGER_SAFE,
            "confirmation_required": False,
            "forbidden": False,
            "reason": "",
        }

    if normalized in {"system status", "device status"}:
        return {
            "action": "status",
            "action_name": "system_status_mock",
            "parameters": {},
            "danger_classification": DANGER_SAFE,
            "confirmation_required": False,
            "forbidden": False,
            "reason": "",
        }

    if normalized.startswith("device action "):
        action_name = _normalize_action_name(normalized[len("device action ") :])
        safety = classify_device_action(action_name)
        return {
            "action": "execute",
            "action_name": action_name,
            "parameters": {},
            "danger_classification": safety.classification,
            "confirmation_required": safety.requires_confirmation,
            "forbidden": safety.forbidden,
            "reason": safety.reason,
        }

    return {}


def _normalize_parsed_device_action(entities: Dict[str, object], fallback_text: str) -> Dict[str, object]:
    if not entities:
        return _parse_device_action_text(fallback_text)

    action_name = str(entities.get("action_name") or "").strip()
    parameters = dict(entities.get("parameters") or {})
    app_id = ""
    if action_name == "open_app":
        app_id = _normalize_action_name(str(entities.get("app_id") or parameters.get("app_id") or ""))
        if app_id:
            parameters["app_id"] = app_id
    confirmation_approved = bool(entities.get("confirmation_approved"))
    confirmation_id = str(entities.get("confirmation_id") or "")
    classification = str(entities.get("danger_classification") or "").strip()
    if not classification:
        classification = DANGER_FORBIDDEN if entities.get("dangerous") else ""
    if not action_name:
        parsed = _parse_device_action_text(fallback_text)
        action_name = str(parsed.get("action_name") or "")
        parameters = dict(parsed.get("parameters") or parameters)
        classification = str(parsed.get("danger_classification") or classification or DANGER_SAFE)

    safety = classify_device_action(action_name)
    if safety.classification != DANGER_SAFE:
        classification = safety.classification
    classification = classification or DANGER_SAFE

    return {
        "action": str(entities.get("action") or "execute"),
        "action_name": action_name,
        "parameters": parameters,
        "danger_classification": classification,
        "confirmation_required": classification == DANGER_CONFIRMATION_REQUIRED,
        "forbidden": classification == DANGER_FORBIDDEN,
        "reason": str(entities.get("reason") or safety.reason),
        "confirmation_approved": confirmation_approved,
        "confirmation_id": confirmation_id,
        "app_id": app_id,
    }


def _dangerous_action_name(normalized_text: str) -> str:
    if normalized_text in {"lock", "lock pc", "lock computer", "lock session", "lock windows", "lock windows session"}:
        return "lock_pc"
    if normalized_text in {"sleep", "sleep pc", "sleep computer", "sleep session", "sleep windows", "sleep windows pc"}:
        return "sleep_pc"
    if normalized_text in {"shutdown", "restart"}:
        return normalized_text
    if normalized_text.startswith("run command"):
        return "run_command"
    if normalized_text.startswith("open app"):
        return "open_app"
    if normalized_text == "delete" or normalized_text.startswith("delete "):
        return "delete"
    if (
        normalized_text == "arbitrary shell"
        or "arbitrary shell" in normalized_text
        or normalized_text.startswith("shell ")
    ):
        return "arbitrary_shell"
    return ""


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", (value or "").lower()))


def _normalize_action_name(value: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", (value or "").lower()))
