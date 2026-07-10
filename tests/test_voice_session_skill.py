from core.IntentParser import IntentParser
from core.Planner import Planner
from core.VoiceService import MockVoiceInputAdapter, MockVoiceOutputAdapter
from events import EventHistoryStore, get_global_bus
from skills import EventHistorySkill, SkillContext, SkillManager, ToolSelector, VoiceSessionSkill
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


def test_voice_session_skill_records_start_event(tmp_path):
    store = EventHistoryStore(path=tmp_path / "events.json")
    skill = VoiceSessionSkill(
        mock_inputs=["hello"],
        text_handler=lambda text: f"handled: {text}",
    )

    response = skill.handle("start voice session for 1 turn", SkillContext(event_history_store=store))

    records = store.list()
    assert records[0].source == "voice_session_skill"
    assert records[0].type == "voice_session.started"
    assert records[0].priority == "normal"
    assert records[0].decision == "recorded"
    assert records[0].event["payload"]["max_turns"] == 1
    assert records[0].event["payload"]["mock_adapters_only"] is True
    assert response.metadata["event_history_records"][0]["type"] == "voice_session.started"


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


def test_voice_session_skill_records_stop_event(tmp_path):
    store = EventHistoryStore(path=tmp_path / "events.json")
    skill = VoiceSessionSkill(
        mock_inputs=["stop"],
        text_handler=lambda text: f"handled: {text}",
    )

    skill.handle("start mock voice for 2 turns", SkillContext(event_history_store=store))

    records = store.list()
    assert [record.type for record in records] == [
        "voice_session.started",
        "voice_session.stopped",
    ]
    assert records[1].event["payload"]["stop_reason"] == "stop_phrase"
    assert records[1].event["payload"]["turn_count"] == 1


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


def test_voice_session_skill_records_max_turns_event(tmp_path):
    store = EventHistoryStore(path=tmp_path / "events.json")
    skill = VoiceSessionSkill(
        mock_inputs=["one", "two"],
        text_handler=lambda text: f"handled: {text}",
    )

    skill.handle("run voice test for 1 turn", SkillContext(event_history_store=store))

    records = store.list()
    assert [record.type for record in records] == [
        "voice_session.started",
        "voice_session.max_turns_reached",
    ]
    assert records[1].event["payload"]["stop_reason"] == "max_turns"
    assert records[1].event["payload"]["max_turns"] == 1


def test_voice_session_skill_records_adapter_failure_event(tmp_path):
    store = EventHistoryStore(path=tmp_path / "events.json")
    skill = VoiceSessionSkill(
        input_adapter=MockVoiceInputAdapter(fail=True),
        output_adapter=MockVoiceOutputAdapter(),
    )

    response = skill.handle("run voice test", SkillContext(event_history_store=store))

    records = store.list()
    assert response.metadata["status"] == "failed"
    assert [record.type for record in records] == [
        "voice_session.started",
        "voice_session.adapter_failure",
    ]
    assert records[1].priority == "high"
    assert records[1].decision == "escalated"
    assert records[1].event["payload"]["failed_turn_status"] == "input_error"
    assert records[1].event["payload"]["error_message"] == "mock_input_failure"


def test_event_history_skill_shows_voice_session_events(tmp_path):
    store = EventHistoryStore(path=tmp_path / "events.json")
    skill = VoiceSessionSkill(
        mock_inputs=["stop"],
        text_handler=lambda text: f"handled: {text}",
    )
    skill.handle("start voice session", SkillContext(event_history_store=store))

    response = EventHistorySkill().handle("show recent events", SkillContext(event_history_store=store))

    assert response.skill == "event_history"
    assert "voice_session_skill.voice_session.started: recorded" in response.text
    assert "voice_session_skill.voice_session.stopped: recorded" in response.text


def test_voice_session_skill_empty_session_is_safe():
    skill = VoiceSessionSkill(mock_inputs=[], default_max_turns=2)

    response = skill.handle("start voice session", SkillContext())

    assert response.metadata["status"] == "max_turns_reached"
    assert response.metadata["turn_count"] == 2
    assert all(entry["status"] == "no_input" for entry in response.metadata["transcript"])
    assert "No mock voice input was provided." in response.text
    assert response.metadata["microphone"] == "disabled"
    assert response.metadata["speaker"] == "disabled"


def test_voice_session_skill_manager_live_path_uses_execution_pipeline(tmp_path):
    bus = get_global_bus()
    bus.clear_history()
    store = EventHistoryStore(path=tmp_path / "events.json")
    manager = SkillManager(event_bus=bus, event_history_store=store)
    manager.register_plugin(create_builtin_plugin())

    response = manager.handle("run voice test for 1 turn")

    assert response.skill == "voice_session"
    assert "Mock voice session" in response.text
    assert manager.last_plan.steps[0].target == "voice_session"
    assert manager.last_execution.step_results[0].target == "voice_session"
    assert manager.last_execution.step_results[0].returned_data["metadata"]["mock_adapters_only"] is True
    assert manager.last_execution.step_results[0].returned_data["metadata"]["audio_hardware_accessed"] is False
    assert [record.type for record in store.list()] == [
        "voice_session.started",
        "voice_session.max_turns_reached",
    ]
