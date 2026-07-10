from core.IntentParser import IntentParser
from core.Planner import Planner
from events import get_global_bus
from skills import SkillContext, SkillManager, ToolSelector, VoiceSessionSkill
from skills.builtin import create_builtin_plugin


def test_voice_session_parser_detects_start_phrases():
    parser = IntentParser()

    for phrase in ("start voice session", "start mock voice", "run voice test"):
        intent = parser.parse(phrase)
        assert intent.intent_name == "voice_session"
        assert intent.confidence == 0.96
        assert intent.extracted_entities["action"] == "start"


def test_voice_session_parser_extracts_max_turns():
    intent = IntentParser().parse("start voice session for 2 turns")

    assert intent.intent_name == "voice_session"
    assert intent.extracted_entities["max_turns"] == 2


def test_voice_session_planner_creates_skill_step():
    intent = IntentParser().parse("start mock voice for 2 turns")
    plan = Planner().plan(intent)

    assert plan.intent_name == "voice_session"
    assert len(plan.steps) == 1
    assert plan.steps[0].target == "voice_session"
    assert plan.steps[0].action == "start"
    assert plan.steps[0].entities["max_turns"] == 2
    assert "no audio hardware access" in plan.steps[0].description


def test_voice_session_tool_selector_routes_to_skill():
    skill = VoiceSessionSkill()
    selection = ToolSelector().select("run voice test", [skill])

    assert selection is not None
    assert selection.skill.name == "voice_session"
    assert selection.plan.steps[0].target == "voice_session"


def test_voice_session_skill_start_returns_transcript_summary():
    skill = VoiceSessionSkill(
        mock_inputs=["hello"],
        text_handler=lambda text: f"handled: {text}",
    )

    response = skill.handle("start voice session for 1 turn", SkillContext())

    assert response.skill == "voice_session"
    assert "Mock voice session max_turns_reached: 1 turn(s)" in response.text
    assert "- Turn 1: hello -> handled: hello" in response.text
    assert response.metadata["turn_count"] == 1
    assert response.metadata["transcript"][0]["user"] == "hello"
    assert response.metadata["transcript"][0]["assistant"] == "handled: hello"
    assert response.metadata["mock_adapters_only"] is True
    assert response.metadata["audio_hardware_accessed"] is False


def test_voice_session_skill_stop_phrase_stops_session():
    handled = []
    skill = VoiceSessionSkill(
        mock_inputs=["hello", "stop", "after"],
        text_handler=lambda text: handled.append(text) or f"handled: {text}",
    )

    response = skill.handle("start mock voice for 3 turns", SkillContext())

    assert response.metadata["status"] == "stopped"
    assert response.metadata["stop_reason"] == "stop_phrase"
    assert response.metadata["turn_count"] == 2
    assert handled == ["hello"]
    assert response.metadata["transcript"][1]["status"] == "stopped"
    assert "stop phrase received" in response.text


def test_voice_session_skill_respects_max_turns():
    handled = []
    skill = VoiceSessionSkill(
        mock_inputs=["one", "two", "three"],
        text_handler=lambda text: handled.append(text) or f"handled: {text}",
    )

    response = skill.handle("run voice test for 2 turns", SkillContext())

    assert response.metadata["max_turns"] == 2
    assert response.metadata["turn_count"] == 2
    assert handled == ["one", "two"]
    assert [entry["user"] for entry in response.metadata["transcript"]] == ["one", "two"]


def test_voice_session_skill_empty_session_is_safe():
    skill = VoiceSessionSkill(mock_inputs=[], default_max_turns=2)

    response = skill.handle("start voice session", SkillContext())

    assert response.metadata["status"] == "max_turns_reached"
    assert response.metadata["turn_count"] == 2
    assert all(entry["status"] == "no_input" for entry in response.metadata["transcript"])
    assert "No mock voice input was provided." in response.text
    assert response.metadata["microphone"] == "disabled"
    assert response.metadata["speaker"] == "disabled"


def test_voice_session_skill_manager_live_path_uses_execution_pipeline():
    bus = get_global_bus()
    bus.clear_history()
    manager = SkillManager(event_bus=bus)
    manager.register_plugin(create_builtin_plugin())

    response = manager.handle("run voice test for 1 turn")

    assert response.skill == "voice_session"
    assert "Mock voice session" in response.text
    assert manager.last_plan.steps[0].target == "voice_session"
    assert manager.last_execution.step_results[0].target == "voice_session"
    assert manager.last_execution.step_results[0].returned_data["metadata"]["mock_adapters_only"] is True
    assert manager.last_execution.step_results[0].returned_data["metadata"]["audio_hardware_accessed"] is False
