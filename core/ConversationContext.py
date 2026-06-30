from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ConversationTurn:
    timestamp: str
    user_message: str
    assistant_response: str
    detected_skill: Optional[str] = None


class ConversationContextManager:
    """In-memory short-term conversation context."""

    def __init__(self, max_turns: int = 20):
        self.max_turns = max(1, int(max_turns))
        self._turns: List[ConversationTurn] = []

    def record_turn(
        self,
        user_message: str,
        assistant_response: str,
        detected_skill: Optional[str] = None,
    ) -> ConversationTurn:
        turn = ConversationTurn(
            timestamp=_utc_now(),
            user_message=str(user_message or ""),
            assistant_response=str(assistant_response or ""),
            detected_skill=(str(detected_skill).strip() or None) if detected_skill else None,
        )
        self._turns.append(turn)
        if len(self._turns) > self.max_turns:
            self._turns = self._turns[-self.max_turns :]
        return turn

    def last_message(self) -> Optional[ConversationTurn]:
        if not self._turns:
            return None
        return self._turns[-1]

    def last_user_message(self) -> Optional[str]:
        turn = self.last_message()
        return turn.user_message if turn else None

    def last_assistant_message(self) -> Optional[str]:
        turn = self.last_message()
        return turn.assistant_response if turn else None

    def last_skill(self) -> Optional[str]:
        turn = self.last_message()
        return turn.detected_skill if turn else None

    def history(self, limit: Optional[int] = None) -> List[ConversationTurn]:
        if limit is None:
            return list(self._turns)
        clean_limit = max(0, int(limit))
        if clean_limit == 0:
            return []
        return list(self._turns[-clean_limit:])

    def clear(self) -> None:
        self._turns.clear()


_GLOBAL_CONTEXT = ConversationContextManager()


def get_global_conversation_context() -> ConversationContextManager:
    return _GLOBAL_CONTEXT
