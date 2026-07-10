from core import (
    EVENT_DECISION_ESCALATED,
    EVENT_DECISION_RECORDED,
    PRIORITY_CRITICAL,
    PRIORITY_LOW,
    CoreEventDecisionResult,
    Event,
    IntentParser,
    Planner,
)
from events import EventHistoryStore
from skills import SkillContext, SkillManager
from skills.EventHistorySkill import EventHistorySkill


def _event(source="voice", type="voice.status", priority=PRIORITY_LOW):
    return Event(
        source=source,
        type=type,
        priority=priority,
        payload={"sample": True},
    )


def _result(decision=EVENT_DECISION_RECORDED):
    return CoreEventDecisionResult(
        success=True,
        decision=decision,
        text=f"{decision} event",
        data={"escalated": decision == EVENT_DECISION_ESCALATED},
        metadata={"safe": True},
    )


def test_event_history_skill_shows_recent_events(tmp_path):
    store = EventHistoryStore(path=tmp_path / "event_history.json")
    store.add(_event(source="voice", type="voice.placeholder", priority=PRIORITY_LOW), _result())
    store.add(
        _event(source="pc", type="device.safety", priority=PRIORITY_CRITICAL),
        _result(EVENT_DECISION_ESCALATED),
    )
    skill = EventHistorySkill()

    response = skill.handle("what happened recently", SkillContext(event_history_store=store))

    assert response.skill == "event_history"
    assert response.metadata["query_type"] == "recent"
    assert response.metadata["count"] == 2
    assert response.text.startswith("Recent events:")
    assert "[critical] pc.device.safety: escalated" in response.text
    assert "[low] voice.voice.placeholder: recorded" in response.text


def test_event_history_skill_shows_critical_events_only(tmp_path):
    store = EventHistoryStore(path=tmp_path / "event_history.json")
    store.add(_event(source="voice", type="voice.placeholder", priority=PRIORITY_LOW), _result())
    store.add(
        _event(source="pc", type="device.safety", priority=PRIORITY_CRITICAL),
        _result(EVENT_DECISION_ESCALATED),
    )
    skill = EventHistorySkill()

    response = skill.handle("show critical events", SkillContext(event_history_store=store))

    assert response.skill == "event_history"
    assert response.metadata["query_type"] == "critical"
    assert response.metadata["count"] == 1
    assert response.text.startswith("Critical events:")
    assert "[critical] pc.device.safety: escalated" in response.text
    assert "voice.placeholder" not in response.text


def test_event_history_skill_empty_history_is_safe(tmp_path):
    store = EventHistoryStore(path=tmp_path / "event_history.json")
    skill = EventHistorySkill()

    recent = skill.handle("show recent events", SkillContext(event_history_store=store))
    critical = skill.handle("show critical events", SkillContext(event_history_store=store))

    assert recent.text == "No event history is available yet."
    assert recent.metadata == {"query_type": "recent", "count": 0}
    assert critical.text == "No critical events found."
    assert critical.metadata == {"query_type": "critical", "count": 0}


def test_event_history_intent_parser_phrases():
    parser = IntentParser()

    recent_question = parser.parse("what happened recently")
    recent_command = parser.parse("show recent events")
    critical = parser.parse("show critical events")

    assert recent_question.intent_name == "event_history"
    assert recent_question.extracted_entities["query_type"] == "recent"
    assert recent_command.intent_name == "event_history"
    assert recent_command.extracted_entities["query_type"] == "recent"
    assert critical.intent_name == "event_history"
    assert critical.extracted_entities["query_type"] == "critical"
    assert critical.extracted_entities["priority"] == "critical"


def test_event_history_planner_creates_skill_step():
    intent = IntentParser().parse("show critical events")
    plan = Planner().plan(intent)

    assert plan.intent_name == "event_history"
    assert len(plan.steps) == 1
    assert plan.steps[0].target == "event_history"
    assert plan.steps[0].action == "critical"
    assert plan.steps[0].entities["priority"] == "critical"


def test_event_history_skill_manager_live_path(tmp_path):
    store = EventHistoryStore(path=tmp_path / "event_history.json")
    store.add(
        _event(source="pc", type="device.safety", priority=PRIORITY_CRITICAL),
        _result(EVENT_DECISION_ESCALATED),
    )
    manager = SkillManager(event_history_store=store)
    manager.register(EventHistorySkill())

    response = manager.handle("show critical events")

    assert response.skill == "event_history"
    assert response.text.startswith("Critical events:")
    assert "[critical] pc.device.safety: escalated" in response.text
    assert manager.last_plan.steps[0].target == "event_history"
    assert manager.last_execution.step_results[0].target == "event_history"
