from datetime import datetime

from skills.base import Skill, SkillContext, SkillResponse
from skills.builtin.calculator import CalculatorSkill
from skills.builtin.calendar import CalendarSkill
from skills.builtin.device_action import DeviceActionSkill
from skills.builtin.goals import GoalsSkill
from skills.builtin.market import MarketSkill
from skills.builtin.memory_recall import MemoryRecallSkill
from skills.builtin.notes import NotesSkill
from skills.builtin.tasks import TasksSkill
from skills.builtin.weather import WeatherSkill
from skills.plugin import SkillPlugin


class TimeDateSkill(Skill):
    name = "time_date"
    description = "Answers simple local time and date questions."
    version = "0.1"
    intent_names = ("time_date",)
    triggers = (
        "time",
        "date",
        "today",
        "what day",
        "current time",
        "current date",
    )

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        now = datetime.now()
        low = (text or "").lower()

        asks_time = "time" in low
        asks_date = "date" in low or "today" in low or "what day" in low

        if asks_time and asks_date:
            answer = now.strftime("It is %H:%M on %A, %Y-%m-%d.")
        elif asks_date:
            answer = now.strftime("Today is %A, %Y-%m-%d.")
        else:
            answer = now.strftime("The local time is %H:%M.")

        return SkillResponse(
            text=answer,
            skill=self.name,
            metadata={"local_iso": now.isoformat(timespec="seconds")},
        )


def create_builtin_plugin() -> SkillPlugin:
    return SkillPlugin.create(
        name="builtin",
        version="0.1",
        description="Built-in ARES skills.",
        skills=[
            MemoryRecallSkill(),
            CalculatorSkill(),
            CalendarSkill(),
            DeviceActionSkill(),
            GoalsSkill(),
            MarketSkill(),
            NotesSkill(),
            TasksSkill(),
            WeatherSkill(),
            TimeDateSkill(),
        ],
    )
