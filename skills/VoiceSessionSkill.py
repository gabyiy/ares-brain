import re
from typing import Any, Callable, Iterable, Optional

from core.EventBus import PRIORITY_HIGH, PRIORITY_NORMAL
from core.VoiceLoop import DEFAULT_VOICE_SESSION_MAX_TURNS, VoiceSessionLoop, VoiceSessionResult
from core.VoiceService import MockVoiceInputAdapter, MockVoiceOutputAdapter, VoiceInputAdapter, VoiceOutputAdapter
from skills.base import Skill, SkillContext, SkillResponse


MAX_VOICE_SESSION_TURNS = DEFAULT_VOICE_SESSION_MAX_TURNS
VOICE_SESSION_EVENT_SOURCE = "voice_session_skill"


class VoiceSessionSkill(Skill):
    name = "voice_session"
    description = "Starts a bounded mock Voice City session with no audio hardware access."
    version = "0.1"
    intent_names = ("voice_session",)
    triggers = (
        "start voice session",
        "start mock voice",
        "run voice test",
        "what happened in voice session",
        "show last voice session",
        "voice session status",
    )
    selection_keywords = (
        "voice session",
        "mock voice",
        "voice test",
        "last voice session",
        "voice session status",
    )
    selection_priority = 0.1

    def __init__(
        self,
        mock_inputs: Optional[Iterable[str]] = None,
        text_handler: Optional[Callable[[str], Any]] = None,
        default_max_turns: int = DEFAULT_VOICE_SESSION_MAX_TURNS,
        input_adapter: Optional[VoiceInputAdapter] = None,
        output_adapter: Optional[VoiceOutputAdapter] = None,
    ):
        self.mock_inputs = [str(item or "") for item in (mock_inputs or [])]
        self.text_handler = text_handler or _default_text_handler
        self.default_max_turns = _coerce_max_turns(default_max_turns)
        self.input_adapter = input_adapter
        self.output_adapter = output_adapter

    def can_handle(self, text: str) -> bool:
        return _looks_like_voice_session(text) or _looks_like_voice_session_status(text)

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        parsed = self._parse_from_context(text, context)
        if parsed.get("action") == "status":
            return self._handle_status(context)

        max_turns = _coerce_max_turns(parsed.get("max_turns"), default=self.default_max_turns)
        mock_inputs = parsed.get("mock_inputs")
        transcripts = (
            [str(item or "") for item in mock_inputs]
            if isinstance(mock_inputs, list)
            else list(self.mock_inputs)
        )

        input_adapter = self.input_adapter or MockVoiceInputAdapter(
            transcripts=transcripts,
            source=VOICE_SESSION_EVENT_SOURCE,
            voice_input="mock_voice_session",
        )
        output_adapter = self.output_adapter or MockVoiceOutputAdapter(
            source=VOICE_SESSION_EVENT_SOURCE,
            voice_output="mock_voice_session",
        )
        session = VoiceSessionLoop(
            input_adapter=input_adapter,
            output_adapter=output_adapter,
            text_handler=self.text_handler,
            max_turns=max_turns,
        )
        event_records = [
            _record_voice_session_event(
                context,
                "voice_session.started",
                PRIORITY_NORMAL,
                {
                    "command": text,
                    "max_turns": max_turns,
                    "mock_input_count": len(transcripts),
                    "mock_adapters_only": True,
                    "audio_hardware_access": "disabled",
                },
            )
        ]
        result = session.run()
        event_records.extend(_record_result_events(context, result))

        return SkillResponse(
            text=_format_session_summary(result),
            skill=self.name,
            metadata={
                "action": "start",
                "max_turns": max_turns,
                "turn_count": len(result.turns),
                "stop_reason": result.stop_reason,
                "status": result.status,
                "success": result.success,
                "transcript": [dict(entry) for entry in result.transcript],
                "result": result.to_dict(),
                "event_history_records": [
                    record.to_dict() for record in event_records if record is not None
                ],
                "mock_adapters_only": True,
                "audio_hardware_access": "disabled",
                "microphone": "disabled",
                "speaker": "disabled",
                "wake_word": "disabled",
                "background_listening": "disabled",
                "gpt": "disabled",
                "internet": "disabled",
                "audio_hardware_accessed": session.audio_hardware_accessed,
            },
        )

    def _parse_from_context(self, text: str, context: SkillContext):
        intent = context.metadata.get("intent") if context.metadata else None
        if getattr(intent, "intent_name", None) == "voice_session":
            entities = dict(getattr(intent, "extracted_entities", {}) or {})
            return {
                "action": entities.get("action") or "start",
                "query_type": entities.get("query_type"),
                "max_turns": entities.get("max_turns"),
                "mock_inputs": entities.get("mock_inputs"),
            }
        return {
            "action": "status" if _looks_like_voice_session_status(text) else "start",
            "query_type": "latest" if _looks_like_voice_session_status(text) else None,
            "max_turns": _extract_max_turns(text),
            "mock_inputs": None,
        }

    def _handle_status(self, context: SkillContext) -> SkillResponse:
        store = getattr(context, "event_history_store", None)
        records = _latest_voice_session_records(store) if store else []
        if not records:
            return SkillResponse(
                text="No voice session events found.",
                skill=self.name,
                metadata={
                    "action": "status",
                    "status": "no_session",
                    "event_count": 0,
                    "mock_adapters_only": True,
                    "audio_hardware_access": "disabled",
                    "microphone": "disabled",
                    "speaker": "disabled",
                    "wake_word": "disabled",
                    "background_listening": "disabled",
                    "gpt": "disabled",
                    "internet": "disabled",
                },
            )

        summary = _voice_session_status_summary(records)
        return SkillResponse(
            text=summary["text"],
            skill=self.name,
            metadata={
                "action": "status",
                "status": summary["status"],
                "event_count": len(records),
                "event_types": summary["event_types"],
                "started": summary["started"],
                "stopped": summary["stopped"],
                "failure": summary["failure"],
                "max_turns_reached": summary["max_turns_reached"],
                "records": [record.to_dict() for record in records],
                "mock_adapters_only": True,
                "audio_hardware_access": "disabled",
                "microphone": "disabled",
                "speaker": "disabled",
                "wake_word": "disabled",
                "background_listening": "disabled",
                "gpt": "disabled",
                "internet": "disabled",
            },
        )


def _record_result_events(context: SkillContext, result: VoiceSessionResult):
    events = []
    if result.status == "stopped":
        events.append(
            _record_voice_session_event(
                context,
                "voice_session.stopped",
                PRIORITY_NORMAL,
                _result_payload(result),
            )
        )
    elif result.status == "max_turns_reached":
        events.append(
            _record_voice_session_event(
                context,
                "voice_session.max_turns_reached",
                PRIORITY_NORMAL,
                _result_payload(result),
            )
        )

    if _has_adapter_failure(result):
        events.append(
            _record_voice_session_event(
                context,
                "voice_session.adapter_failure",
                PRIORITY_HIGH,
                {
                    **_result_payload(result),
                    "error_message": result.error_message,
                    "failed_turn_status": _failed_turn_status(result),
                },
                decision="escalated",
            )
        )
    return events


def _record_voice_session_event(
    context: SkillContext,
    event_type: str,
    priority: str,
    payload,
    decision: str = "recorded",
):
    store = getattr(context, "event_history_store", None)
    if not store:
        return None

    return store.add(
        {
            "source": VOICE_SESSION_EVENT_SOURCE,
            "type": event_type,
            "priority": priority,
            "payload": dict(payload or {}),
        },
        {
            "success": decision != "failed",
            "decision": decision,
            "text": f"Voice session event recorded: {event_type}",
            "data": {
                "event_type": event_type,
            },
            "metadata": {
                "safe": True,
                "mock_adapters_only": True,
                "audio_hardware_access": "disabled",
            },
        },
    )


def _latest_voice_session_records(store):
    records = [
        record
        for record in store.list()
        if record.source == VOICE_SESSION_EVENT_SOURCE and str(record.type).startswith("voice_session.")
    ]
    if not records:
        return []

    latest_session_id = _record_session_id(records[-1])
    if latest_session_id:
        session_records = [
            record for record in records if _record_session_id(record) == latest_session_id
        ]
        if session_records:
            return session_records

    for index in range(len(records) - 1, -1, -1):
        if records[index].type == "voice_session.started":
            return records[index:]

    return [records[-1]]


def _voice_session_status_summary(records):
    event_types = [record.type for record in records]
    started = "voice_session.started" in event_types
    stopped = "voice_session.stopped" in event_types
    failure = "voice_session.adapter_failure" in event_types
    max_turns_reached = "voice_session.max_turns_reached" in event_types
    final_payload = _record_payload(records[-1])
    status = str(final_payload.get("status") or _status_from_event_types(event_types))

    lines = ["Last mock voice session status:"]
    lines.append(f"- started: {'yes' if started else 'no'}")
    lines.append(f"- stopped: {'yes' if stopped else 'no'}")
    lines.append(f"- failure: {_failure_summary(records) if failure else 'none'}")
    lines.append(f"- max_turns: {_max_turns_summary(records) if max_turns_reached else 'not reached'}")
    lines.append(f"- events: {', '.join(event_types)}")
    lines.append("Safeguards: mock adapters only; microphone, speaker, wake word, background listening, GPT, and internet are disabled.")

    return {
        "text": "\n".join(lines),
        "status": status,
        "event_types": event_types,
        "started": started,
        "stopped": stopped,
        "failure": failure,
        "max_turns_reached": max_turns_reached,
    }


def _record_payload(record) -> dict:
    event = dict(getattr(record, "event", {}) or {})
    return dict(event.get("payload", {}) or {})


def _record_session_id(record) -> str:
    return str(_record_payload(record).get("session_id") or "").strip()


def _status_from_event_types(event_types) -> str:
    if "voice_session.adapter_failure" in event_types:
        return "failed"
    if "voice_session.stopped" in event_types:
        return "stopped"
    if "voice_session.max_turns_reached" in event_types:
        return "max_turns_reached"
    return "started"


def _failure_summary(records) -> str:
    for record in records:
        if record.type == "voice_session.adapter_failure":
            payload = _record_payload(record)
            detail = str(payload.get("failed_turn_status") or "adapter_failure")
            message = str(payload.get("error_message") or "").strip()
            return f"{detail} ({message})" if message else detail
    return "adapter_failure"


def _max_turns_summary(records) -> str:
    for record in records:
        if record.type == "voice_session.max_turns_reached":
            payload = _record_payload(record)
            turn_count = payload.get("turn_count")
            max_turns = payload.get("max_turns")
            return f"reached after {turn_count} turn(s), limit {max_turns}"
    return "reached"


def _result_payload(result: VoiceSessionResult):
    return {
        "status": result.status,
        "turn_count": len(result.turns),
        "stop_reason": result.stop_reason,
        "max_turns": result.data.get("max_turns"),
        "success": result.success,
        "mock_adapters_only": True,
        "audio_hardware_access": "disabled",
    }


def _has_adapter_failure(result: VoiceSessionResult) -> bool:
    return any(turn.status in {"input_error", "output_error"} for turn in result.turns)


def _failed_turn_status(result: VoiceSessionResult) -> str:
    for turn in result.turns:
        if turn.status in {"input_error", "output_error"}:
            return turn.status
    return ""


def _default_text_handler(text: str) -> str:
    clean_text = str(text or "").strip()
    if not clean_text:
        return ""
    return f"Mock voice session handled: {clean_text}"


def _format_session_summary(result: VoiceSessionResult) -> str:
    lines = [
        f"Mock voice session {result.status}: {len(result.turns)} turn(s), stop reason: {result.stop_reason}."
    ]

    if not result.turns or all(not turn.input_text for turn in result.turns):
        lines.append("No mock voice input was provided.")
    else:
        for turn in result.turns:
            if turn.status == "no_input":
                lines.append(f"- Turn {turn.turn_number}: no input.")
            elif turn.status == "stopped":
                lines.append(f"- Turn {turn.turn_number}: stop phrase received.")
            elif turn.success:
                lines.append(f"- Turn {turn.turn_number}: {turn.input_text} -> {turn.response_text}")
            else:
                message = turn.error_message or "unknown error"
                lines.append(f"- Turn {turn.turn_number}: failed safely ({message}).")

    lines.append("Safeguards: mock adapters only; microphone, speaker, wake word, background listening, GPT, and internet are disabled.")
    return "\n".join(lines)


def _looks_like_voice_session(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    phrases = {
        "start voice session",
        "start mock voice",
        "run voice test",
    }
    return any(normalized == phrase or normalized.startswith(f"{phrase} ") for phrase in phrases)


def _looks_like_voice_session_status(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    phrases = {
        "what happened in voice session",
        "show last voice session",
        "voice session status",
    }
    return normalized in phrases


def _extract_max_turns(text: str):
    match = re.search(r"\b(?:max\s+turns?|for)\s+(\d+)(?:\s+turns?)?\b", text or "", flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1)


def _coerce_max_turns(value, default: int = DEFAULT_VOICE_SESSION_MAX_TURNS) -> int:
    try:
        turns = int(value)
    except (TypeError, ValueError):
        turns = int(default)

    if turns < 1:
        return 1
    if turns > MAX_VOICE_SESSION_TURNS:
        return MAX_VOICE_SESSION_TURNS
    return turns
