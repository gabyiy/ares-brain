import re

from skills.base import Skill, SkillContext, SkillResponse


class MemoryRecallSkill(Skill):
    name = "memory_recall"
    description = "Answers personal profile questions from stored user facts."
    version = "0.1"
    intent_names = ("memory_recall",)
    run_before_intents = True
    triggers = (
        "what is my name",
        "where do i live",
        "what is my favorite",
        "when is my birthday",
    )

    def can_handle(self, text: str) -> bool:
        low = (text or "").lower().strip().rstrip("?")
        return (
            low == "what is my name"
            or low == "where do i live"
            or low == "when is my birthday"
            or bool(re.match(r"^what is my favorite\s+(.+)$", low))
        )

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        profile = getattr(context, "profile_store", None)
        if not profile:
            return self._unknown()

        low = (text or "").lower().strip().rstrip("?")

        if low == "what is my name":
            value = profile.get_value("name")
            if value:
                return self._answer(f"Your name is {value}.", "name")

        if low == "where do i live":
            value = profile.get_value("location")
            if value:
                return self._answer(f"You live in {value}.", "location")

        if low == "when is my birthday":
            value = profile.get_value("birthday")
            if value:
                return self._answer(f"Your birthday is {value}.", "birthday")

        favorite_match = re.match(r"^what is my favorite\s+(.+)$", low)
        if favorite_match:
            subject = favorite_match.group(1).strip()
            owner_profile = getattr(context, "owner_profile_store", None)
            if owner_profile is not None:
                owner_result = owner_profile.recall_fact(f"favorite_{subject}")
                if owner_result.success and owner_result.status == "recalled":
                    return self._answer(
                        f"Your favorite {subject} is {owner_result.value}.",
                        f"favorite_{subject}",
                    )
            value = profile.get_favorite(subject)
            if value:
                return self._answer(f"Your favorite {subject} is {value}.", f"favorite_{subject}")

        return self._unknown()

    def _answer(self, text: str, key: str) -> SkillResponse:
        return SkillResponse(text=text, skill=self.name, metadata={"profile_key": key})

    def _unknown(self) -> SkillResponse:
        return SkillResponse(
            text="I do not know that yet.",
            skill=self.name,
            metadata={"missing": True},
        )
