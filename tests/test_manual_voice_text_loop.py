from core import NullVoiceOutput
from events import get_global_bus
import memory.v1 as memory_v1
from scripts import manual_voice_text_loop as manual_script
from skills.base import SkillResponse


def isolate_memory_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_v1, "SHORT_MEMORY_FILE", tmp_path / "short.json")
    monkeypatch.setattr(memory_v1, "LONG_MEMORY_FILE", tmp_path / "long.json")
    monkeypatch.setenv("ARES_USER_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("ARES_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("ARES_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("ARES_GOALS_PATH", str(tmp_path / "goals.json"))
    get_global_bus().clear_history()


def test_manual_voice_text_script_imports_safely():
    assert callable(manual_script.run_manual_simulation)
    assert callable(manual_script.main)
    assert manual_script.WARNING.startswith("WARNING: Voice City text simulation is text-only")


def test_manual_voice_text_input_reaches_voice_loop():
    outputs = []
    handled = []

    def handle_text(text):
        handled.append(text)
        return SkillResponse(text="Handled typed voice text.", skill="test")

    exit_code = manual_script.run_manual_simulation(
        input_func=lambda prompt: "hello from typed voice",
        output_func=outputs.append,
        text_handler=handle_text,
    )

    assert exit_code == 0
    assert handled == ["hello from typed voice"]
    assert outputs == [
        manual_script.WARNING,
        "Type one line of text to simulate recognized speech.",
        "ARES: Handled typed voice text.",
    ]


def test_manual_voice_text_loop_uses_existing_execution_path(monkeypatch, tmp_path):
    isolate_memory_paths(monkeypatch, tmp_path)
    outputs = []

    exit_code = manual_script.run_manual_simulation(
        input_func=lambda prompt: "calculate 2 + 2",
        output_func=outputs.append,
    )

    assert exit_code == 0
    assert outputs[-1] == "ARES: Result: 4"


def test_manual_voice_text_empty_input_exits_safely():
    outputs = []
    handled = []

    exit_code = manual_script.run_manual_simulation(
        input_func=lambda prompt: "   ",
        output_func=outputs.append,
        text_handler=lambda text: handled.append(text),
    )

    assert exit_code == 0
    assert handled == []
    assert outputs == [
        manual_script.WARNING,
        "Type one line of text to simulate recognized speech.",
        "No input entered. Voice text loop exited safely.",
    ]


def test_manual_voice_text_loop_does_not_access_audio_hardware():
    voice_output = NullVoiceOutput()
    loop = manual_script.create_voice_loop(
        "what time is it",
        text_handler=lambda text: "The local time is placeholder.",
        voice_output=voice_output,
    )

    result = loop.run_once()

    assert result.success is True
    assert loop.voice_input.audio_hardware_accessed is False
    assert loop.voice_output.audio_hardware_accessed is False
    assert result.data["voice_input"]["metadata"]["audio_hardware_accessed"] is False
    assert result.data["voice_output"]["metadata"]["audio_hardware_accessed"] is False
    assert result.data["voice_input"]["data"]["microphone"] == "disabled"
    assert result.data["voice_output"]["data"]["speaker"] == "disabled"
