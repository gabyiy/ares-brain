from events import EventBus
from memory import GoalsStore
from skills import SkillContext
from skills.builtin.goals import GoalsSkill


def _goals_context(tmp_path):
    store = GoalsStore(path=tmp_path / "goals.json", event_bus=EventBus())
    return store, SkillContext(goals_store=store)


def test_goals_skill_adds_lists_and_shows_goal(tmp_path):
    store, context = _goals_context(tmp_path)
    skill = GoalsSkill()

    saved = skill.handle(
        "add goal Build ARES description Long-term local assistant priority high",
        context,
    )
    goal = store.list()[0]
    listed = skill.handle("list goals", context)
    shown = skill.handle(f"show goal {goal.id}", context)

    assert saved.text == f"Saved goal {goal.id}: Build ARES"
    assert f"- {goal.id} [active, priority high]: Build ARES" in listed.text
    assert f"Goal {goal.id}: Build ARES" in shown.text
    assert "Description: Long-term local assistant" in shown.text
    assert "Milestones: none" in shown.text


def test_goals_skill_completes_pauses_deletes_and_adds_milestone(tmp_path):
    store, context = _goals_context(tmp_path)
    skill = GoalsSkill()
    goal = store.add("Build ARES")

    paused = skill.handle(f"pause goal {goal.id}", context)
    milestone = skill.handle(f"add milestone to goal {goal.id} Write architecture", context)
    completed = skill.handle(f"complete goal {goal.id}", context)
    shown = skill.handle(f"show goal {goal.id}", context)
    deleted = skill.handle(f"delete goal {goal.id}", context)

    assert paused.text == f"Paused goal {goal.id}."
    assert milestone.text == f"Added milestone to goal {goal.id}: Write architecture"
    assert completed.text == f"Completed goal {goal.id}."
    assert "Write architecture" in shown.text
    assert deleted.text == f"Deleted goal {goal.id}."
    assert store.list() == []


def test_goals_skill_rejects_empty_goal_and_milestone(tmp_path):
    store, context = _goals_context(tmp_path)
    skill = GoalsSkill()
    goal = store.add("Build ARES")

    empty_goal = skill.handle("add goal   ", context)
    empty_milestone = skill.handle(f"add milestone to goal {goal.id}   ", context)

    assert empty_goal.text == "I need a goal title to save."
    assert empty_goal.metadata["error"] == "empty_goal"
    assert empty_milestone.text == "I need milestone text to save."
    assert empty_milestone.metadata["error"] == "empty_milestone"


def test_goals_skill_reports_missing_store_and_goal(tmp_path):
    skill = GoalsSkill()

    missing_store = skill.handle("add goal Build ARES", SkillContext())
    missing_goal = skill.handle(
        "show goal missing",
        SkillContext(goals_store=GoalsStore(path=tmp_path / "goals.json", event_bus=EventBus())),
    )

    assert missing_store.text == "Goal storage is not available."
    assert missing_store.metadata["error"] == "missing_goals_store"
    assert missing_goal.text == "I could not find goal missing."
    assert missing_goal.metadata["missing"] is True
