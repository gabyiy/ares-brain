from pathlib import Path
import sys
from typing import Callable, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    NullVoiceInput,
    NullVoiceOutput,
    VoiceLoop,
    VoiceOutput,
    VoiceServiceResult,
    get_global_conversation_context,
)
from events import get_global_bus  # noqa: E402
from memory import GoalsStore, MemoryStore, NotesStore, TasksStore, UserProfileStore  # noqa: E402
from skills import SkillManager  # noqa: E402
from skills.builtin import create_builtin_plugin  # noqa: E402


WARNING = "WARNING: Voice City text simulation is text-only. No microphone or speaker will be used."


class TypedTextVoiceInput(NullVoiceInput):
    """Manual text input adapter for VoiceLoop testing without audio hardware."""

    def __init__(self, transcript: str):
        super().__init__()
        self.transcript = transcript

    def listen_once(self) -> VoiceServiceResult:
        return VoiceServiceResult(
            success=True,
            text="Typed text was provided to the Voice City loop.",
            data={
                "source": "manual_voice_text_loop",
                "transcript": self.transcript,
                "voice_input": "typed_text",
                "microphone": "disabled",
                "stt": "disabled",
                "wake_word": "disabled",
                "background_listening": "disabled",
                "audio_hardware_access": "disabled",
            },
            metadata={
                "safe": True,
                "source": "manual_voice_text_loop",
                "audio_hardware_accessed": self.audio_hardware_accessed,
            },
        )


def create_skill_manager(event_bus=None) -> SkillManager:
    bus = event_bus or get_global_bus()
    manager = SkillManager(
        event_bus=bus,
        memory_store=MemoryStore(event_bus=bus),
        profile_store=UserProfileStore(event_bus=bus),
        goals_store=GoalsStore(event_bus=bus),
        notes_store=NotesStore(event_bus=bus),
        tasks_store=TasksStore(event_bus=bus),
        conversation_context=get_global_conversation_context(),
    )
    manager.register_plugin(create_builtin_plugin())
    return manager


def build_existing_text_handler(skill_manager: Optional[SkillManager] = None):
    manager = skill_manager or create_skill_manager()

    def handle_text(text: str):
        response = manager.handle(text)
        if response is None:
            return "I'm not sure how to answer that yet."
        return response

    return handle_text


def create_voice_loop(
    input_text: str,
    text_handler=None,
    voice_output: Optional[VoiceOutput] = None,
) -> VoiceLoop:
    clean_text = str(input_text or "")
    handler = text_handler
    if handler is None and clean_text.strip():
        handler = build_existing_text_handler()
    return VoiceLoop(
        voice_input=TypedTextVoiceInput(clean_text),
        voice_output=voice_output or NullVoiceOutput(),
        text_handler=handler,
    )


def run_manual_simulation(
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
    text_handler=None,
    voice_output: Optional[VoiceOutput] = None,
) -> int:
    output_func(WARNING)
    output_func("Type one line of text to simulate recognized speech.")

    try:
        typed_text = input_func("> ")
    except EOFError:
        output_func("No input received. Voice text loop exited safely.")
        return 1

    loop = create_voice_loop(
        typed_text,
        text_handler=text_handler,
        voice_output=voice_output,
    )
    result = loop.run_once()

    if result.status == "no_input":
        output_func("No input entered. Voice text loop exited safely.")
        return 0

    final_text = result.response_text or result.text or result.error_message
    output_func(f"ARES: {final_text}")
    return 0 if result.success else 2


def main() -> None:
    raise SystemExit(run_manual_simulation())


if __name__ == "__main__":
    main()
