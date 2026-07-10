import re
from typing import Any, Callable, Iterable, Optional

from core.VoiceLoop import DEFAULT_VOICE_SESSION_MAX_TURNS, VoiceSessionLoop, VoiceSessionResult
from core.VoiceService import MockVoiceInputAdapter, MockVoiceOutputAdapter
from skills.base import Skill, SkillContext, SkillResponse


MAX_VOICE_SESSION_TURNS = DEFAULT_VOICE_SESSION_MAX_TURNS


class VoiceSessionSkill(Skill):
    name = "voice_session"
    description = "Starts a bounded mock Voice City session with no audio hardware access."
    version = "0.1"
    intent_names = ("voice_session",)
    triggers = (
        "start voice session",
        "start mock voice",
        "run voice test",
    )
    selection_keywords = (
        "voice session",
        "mock voice",
        "voice test",
    )
    selection_priority = 0.1

    def __init__(
        self,
        mock_inputs: Optional[Iterable[str]] = None,
        text_handler: Optional[Callable[[str], Any]] = None,
        default_max_turns: int = DEFAULT_VOICE_SESSION_MAX_TURNS,
    ):
        self.mock_inputs = [str(item or "") for item in (mock_inputs or [])]
        self.text_handler = text_handler or _default_text_handler
        self.default_max_turns = _coerce_max_turns(default_max_turns)

    def can_handle(self, text: str) -> bool:
        return _looks_like_voice_session(text)

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        parsed = self._parse_from_context(text, context)
        max_turns = _coerce_max_turns(parsed.get("max_turns"), default=self.default_max_turns)
        mock_inputs = parsed.get("mock_inputs")
        transcripts = (
            [str(item or "") for item in mock_inputs]
            if isinstance(mock_inputs, list)
            else list(self.mock_inputs)
        )

        input_adapter = MockVoiceInputAdapter(
            transcripts=transcripts,
            source="voice_session_skill",
            voice_input="mock_voice_session",
        )
        output_adapter = MockVoiceOutputAdapter(
            source="voice_session_skill",
            voice_output="mock_voice_session",
        )
        session = VoiceSessionLoop(
            input_adapter=input_adapter,
            output_adapter=output_adapter,
            text_handler=self.text_handler,
            max_turns=max_turns,
        )
        result = session.run()

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
                "max_turns": entities.get("max_turns"),
                "mock_inputs": entities.get("mock_inputs"),
            }
        return {
            "action": "start",
            "max_turns": _extract_max_turns(text),
            "mock_inputs": None,
        }


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
