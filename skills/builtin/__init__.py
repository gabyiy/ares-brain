from skills.builtin.calculator import CalculatorSkill
from skills.builtin.goals import GoalsSkill
from skills.builtin.memory_recall import MemoryRecallSkill
from skills.builtin.notes import NotesSkill
from skills.builtin.tasks import TasksSkill
from skills.builtin.time_date import TimeDateSkill, create_builtin_plugin
from skills.builtin.weather import WeatherSkill

__all__ = [
    "CalculatorSkill",
    "GoalsSkill",
    "MemoryRecallSkill",
    "NotesSkill",
    "TasksSkill",
    "TimeDateSkill",
    "WeatherSkill",
    "create_builtin_plugin",
]
