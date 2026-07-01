import pytest

from events import EventBus
from memory import GoalsStore


def test_goals_store_adds_goal_and_persists(tmp_path):
    path = tmp_path / "goals.json"
    store = GoalsStore(path=path, event_bus=EventBus())

    goal = store.add(
        "Build ARES memory",
        description="Keep local long-term goals",
        priority="high",
    )

    assert goal.id.startswith("goal-")
    assert goal.title == "Build ARES memory"
    assert goal.description == "Keep local long-term goals"
    assert goal.created_at.endswith("Z")
    assert goal.status == "active"
    assert goal.priority == "high"
    assert goal.milestones == []

    reloaded = GoalsStore(path=path, event_bus=EventBus())
    goals = reloaded.list()

    assert len(goals) == 1
    assert goals[0].id == goal.id
    assert goals[0].title == "Build ARES memory"
    assert goals[0].priority == "high"


def test_goals_store_updates_status_adds_milestone_and_deletes(tmp_path):
    store = GoalsStore(path=tmp_path / "goals.json", event_bus=EventBus())
    first = store.add("Build ARES")
    second = store.add("Deploy robot")

    paused = store.pause(first.id)
    milestone = store.add_milestone(first.id, "Design architecture")
    completed = store.complete(first.id)
    missing = store.complete("missing")
    deleted = store.delete(second.id)

    assert paused.status == "paused"
    assert milestone.milestones == ["Design architecture"]
    assert completed.status == "completed"
    assert completed.milestones == ["Design architecture"]
    assert missing is None
    assert deleted.id == second.id
    assert [goal.id for goal in store.list()] == [first.id]


def test_goals_store_rejects_empty_goal_and_milestone(tmp_path):
    store = GoalsStore(path=tmp_path / "goals.json", event_bus=EventBus())
    goal = store.add("Build ARES")

    with pytest.raises(ValueError, match="Goal title is required"):
        store.add("   ")

    with pytest.raises(ValueError, match="Milestone text is required"):
        store.add_milestone(goal.id, "   ")
