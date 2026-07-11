from typing import Any, Dict, List, Optional

from core.CapabilityManifest import (
    CapabilityManifest,
    CapabilityManifestRegistry,
    ManifestPolicy,
    MODULE_TYPE_SKILL,
    build_skill_manifest,
)
from skills.base import Skill
from skills.selector import ToolSelector


class SkillRegistry:
    def __init__(
        self,
        selector: Optional[ToolSelector] = None,
        manifest_registry: Optional[CapabilityManifestRegistry] = None,
        manifest_policy: Optional[ManifestPolicy] = None,
    ):
        self._skills: Dict[str, Skill] = {}
        self.selector = selector or ToolSelector()
        self.manifest_registry = manifest_registry or CapabilityManifestRegistry(
            policy=manifest_policy,
        )

    def register(
        self,
        skill: Skill,
        manifest: Optional[CapabilityManifest | Dict[str, Any]] = None,
    ) -> Skill:
        if not isinstance(skill, Skill):
            raise TypeError("Registered object must implement Skill")

        name = (skill.name or "").strip()
        if not name:
            raise ValueError("Skill name is required")

        if name in self._skills:
            raise ValueError(f"Skill already registered: {name}")

        self._ensure_skill_manifest(skill, manifest)
        self._skills[name] = skill
        return skill

    def unregister(self, name: str) -> Optional[Skill]:
        clean_name = (name or "").strip()
        skill = self._skills.pop(clean_name, None)
        if skill:
            self.manifest_registry.unregister_manifest(clean_name)
        return skill

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get((name or "").strip())

    def all(self) -> List[Skill]:
        return list(self._skills.values())

    def matching(self, text, run_before_intents: Optional[bool] = None) -> List[Skill]:
        selections = self.selector.matching(
            text,
            self.all(),
            run_before_intents=run_before_intents,
        )
        return [selection.skill for selection in selections]

    def first_match(self, text, run_before_intents: Optional[bool] = None) -> Optional[Skill]:
        selection = self.select(text, run_before_intents=run_before_intents)
        return selection.skill if selection else None

    def select(self, text, run_before_intents: Optional[bool] = None):
        return self.selector.select(
            text,
            self.all(),
            run_before_intents=run_before_intents,
        )

    def _ensure_skill_manifest(
        self,
        skill: Skill,
        manifest: Optional[CapabilityManifest | Dict[str, Any]],
    ) -> CapabilityManifest:
        name = (skill.name or "").strip()
        existing = self.manifest_registry.get_manifest(name)
        if existing and manifest is None and existing.module_type == MODULE_TYPE_SKILL:
            return existing

        parsed = (
            manifest
            if isinstance(manifest, CapabilityManifest)
            else CapabilityManifest.from_dict(manifest)
            if manifest is not None
            else build_skill_manifest(
                name,
                capabilities=self._skill_capabilities(skill),
                description=skill.description or "ARES registered skill.",
                module_version="v1",
                metadata={
                    "source": "skill_registry",
                    "skill_version": skill.version,
                    "intent_names": list(skill.intent_names),
                    "triggers": list(skill.triggers),
                },
            )
        )
        if parsed.module_name != name:
            raise ValueError("Skill manifest module_name must match skill name")
        if parsed.module_type != MODULE_TYPE_SKILL:
            raise ValueError("Skill manifest module_type must be skill")
        if existing and existing.to_dict() != parsed.to_dict():
            raise ValueError(f"Conflicting skill manifest already registered: {name}")
        if existing:
            return existing
        return self.manifest_registry.register_manifest(parsed)

    def _skill_capabilities(self, skill: Skill) -> List[str]:
        explicit = [capability.strip() for capability in skill.capabilities if capability.strip()]
        if explicit:
            return explicit
        name = (skill.name or "").strip()
        capabilities = [f"skill.{name}"]
        capabilities.extend(
            f"intent.{intent.strip()}"
            for intent in skill.intent_names
            if intent.strip()
        )
        return capabilities
