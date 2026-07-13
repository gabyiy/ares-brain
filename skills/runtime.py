from __future__ import annotations

from typing import Any

from skills.builtin import create_builtin_plugin
from skills.manager import SkillManager


def create_builtin_skill_manager(**dependencies: Any) -> SkillManager:
    """Create the shared runtime manager with every built-in skill registered."""

    manager = SkillManager(**dependencies)
    manager.register_plugin(create_builtin_plugin())
    return manager
