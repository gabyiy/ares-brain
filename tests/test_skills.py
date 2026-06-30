from datetime import datetime

import pytest

from events import EventBus
from memory import UserProfileStore
from skills import Skill, SkillContext, SkillManager, SkillRegistry, SkillResponse
from skills.builtin.memory_recall import MemoryRecallSkill
from skills.builtin.time_date import TimeDateSkill
import skills.builtin.time_date as time_date_module


class EchoSkill(Skill):
    name = "echo"
    description = "Echoes test input."
    triggers = ("echo",)

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        return SkillResponse(text=f"echo: {text}", skill=self.name)


class PriorityEchoSkill(EchoSkill):
    name = "priority_echo"
    run_before_intents = True


def test_skill_registry_and_manager_handle_registered_skill():
    bus = EventBus(raise_handler_errors=True)
    manager = SkillManager(event_bus=bus)

    manager.register(EchoSkill())

    response = manager.handle("echo hello")

    assert response.text == "echo: echo hello"
    assert response.skill == "echo"
    assert bus.history("skill.registered")
    assert bus.history("skill.detected")
    assert bus.history("skill.response_generated")


def test_skill_registry_rejects_duplicates_and_filters_priority():
    registry = SkillRegistry()

    registry.register(EchoSkill())
    registry.register(PriorityEchoSkill())

    with pytest.raises(ValueError, match="Skill already registered"):
        registry.register(EchoSkill())

    assert registry.first_match("echo", run_before_intents=False).name == "echo"
    assert registry.first_match("echo", run_before_intents=True).name == "priority_echo"


def test_time_date_skill_uses_local_datetime(monkeypatch):
    class FakeDateTime:
        @classmethod
        def now(cls):
            return datetime(2026, 6, 30, 9, 15)

    monkeypatch.setattr(time_date_module, "datetime", FakeDateTime)

    skill = TimeDateSkill()

    assert skill.handle("what time is it", SkillContext()).text == "The local time is 09:15."
    assert skill.handle("what date is it", SkillContext()).text == "Today is Tuesday, 2026-06-30."


def test_memory_recall_skill_answers_from_profile(tmp_path):
    profile = UserProfileStore(path=tmp_path / "profile.json", event_bus=EventBus())
    profile.learn_from_text("My name is Gabi")
    profile.learn_from_text("I live in Madrid")
    profile.learn_from_text("My birthday is June 30")
    profile.learn_from_text("My favorite tank is Leopard 2")

    skill = MemoryRecallSkill()
    context = SkillContext(profile_store=profile)

    assert skill.handle("What is my name?", context).text == "Your name is Gabi."
    assert skill.handle("Where do I live?", context).text == "You live in Madrid."
    assert skill.handle("When is my birthday?", context).text == "Your birthday is June 30."
    assert skill.handle("What is my favorite tank?", context).text == "Your favorite tank is Leopard 2."
    assert skill.handle("What is my favorite aircraft?", context).text == "I do not know that yet."
