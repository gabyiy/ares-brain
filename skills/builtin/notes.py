import re

from skills.base import Skill, SkillContext, SkillResponse


class NotesSkill(Skill):
    name = "notes"
    description = "Stores, lists, searches, and deletes local notes."
    version = "0.1"
    run_before_intents = True
    triggers = (
        "remember this",
        "save note",
        "take a note",
        "list my notes",
        "show my notes",
        "delete note",
        "delete all notes",
        "confirm delete all notes",
        "search notes",
    )
    selection_keywords = (
        "notes",
        "note",
        "remember this",
        "save this",
    )
    selection_priority = 0.08

    def can_handle(self, text: str) -> bool:
        return self._parse(text)["action"] is not None

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        store = getattr(context, "notes_store", None)
        if not store:
            return self._response(
                "Notes storage is not available.",
                error="missing_notes_store",
            )

        parsed = self._parse(text)
        action = parsed["action"]

        if action == "add":
            note_text = parsed["text"]
            if not note_text:
                return self._response("I need note text to save.", error="empty_note")
            note = store.add(note_text)
            return self._response(
                f"Saved note {note.id}: {note.text}",
                note_id=note.id,
                action="add",
            )

        if action == "list":
            return self._format_notes(store.list(), "Your notes:", empty="You do not have any notes yet.")

        if action == "search":
            keyword = parsed["keyword"]
            if not keyword:
                return self._response("I need a keyword to search notes.", error="empty_keyword")
            matches = store.search(keyword)
            return self._format_notes(
                matches,
                f'Matching notes for "{keyword}":',
                empty=f'No notes matched "{keyword}".',
                action="search",
                keyword=keyword,
            )

        if action == "delete":
            note_id = parsed["note_id"]
            deleted = store.delete(note_id)
            if not deleted:
                return self._response(f"I could not find note {note_id}.", action="delete", missing=True)
            return self._response(f"Deleted note {note_id}.", action="delete", note_id=note_id)

        if action == "delete_all_request":
            return self._response(
                "Please confirm by typing: confirm delete all notes.",
                action="delete_all_request",
                confirmation_required=True,
            )

        if action == "delete_all_confirm":
            count = store.clear()
            return self._response(f"Deleted {count} notes.", action="delete_all_confirm", count=count)

        return self._response("I do not know how to handle that note request.", error="unknown_notes_action")

    def _parse(self, text: str):
        raw = (text or "").strip()
        low = raw.lower().strip()

        if low in ("list my notes", "show my notes"):
            return {"action": "list"}

        if low == "delete all notes":
            return {"action": "delete_all_request"}

        if low in ("confirm delete all notes", "delete all notes confirm", "delete all notes confirmed"):
            return {"action": "delete_all_confirm"}

        delete_match = re.match(r"^delete\s+note\s+(\S+)$", raw, flags=re.IGNORECASE)
        if delete_match:
            return {"action": "delete", "note_id": delete_match.group(1).strip()}

        search_match = re.match(r"^search\s+notes\s*(.*)$", raw, flags=re.IGNORECASE)
        if search_match:
            return {"action": "search", "keyword": self._clean_note_text(search_match.group(1))}

        for pattern in (
            r"^remember\s+this\s*(.*)$",
            r"^save\s+note\s*(.*)$",
            r"^take\s+a\s+note\s*(.*)$",
        ):
            match = re.match(pattern, raw, flags=re.IGNORECASE)
            if match:
                return {"action": "add", "text": self._clean_note_text(match.group(1))}

        return {"action": None}

    def _clean_note_text(self, text: str) -> str:
        return (text or "").strip().lstrip(":-. ").strip()

    def _format_notes(self, notes, heading: str, empty: str, **metadata) -> SkillResponse:
        if not notes:
            return self._response(empty, count=0, **metadata)

        lines = [heading]
        for note in notes:
            lines.append(f"- {note.id}: {note.text}")

        return self._response("\n".join(lines), count=len(notes), **metadata)

    def _response(self, text: str, **metadata) -> SkillResponse:
        return SkillResponse(text=text, skill=self.name, metadata=metadata)
