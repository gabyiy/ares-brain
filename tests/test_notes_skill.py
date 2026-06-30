from events import EventBus
from memory import NotesStore
from skills import SkillContext
from skills.builtin.notes import NotesSkill


def _notes_context(tmp_path):
    store = NotesStore(path=tmp_path / "notes.json", event_bus=EventBus())
    return store, SkillContext(notes_store=store)


def test_notes_skill_adds_lists_searches_and_deletes_note(tmp_path):
    store, context = _notes_context(tmp_path)
    skill = NotesSkill()

    saved = skill.handle("save note calibrate rover sensors", context)
    note = store.list()[0]
    listed = skill.handle("list my notes", context)
    matched = skill.handle("search notes rover", context)
    deleted = skill.handle(f"delete note {note.id}", context)

    assert saved.text == f"Saved note {note.id}: calibrate rover sensors"
    assert f"- {note.id}: calibrate rover sensors" in listed.text
    assert f"- {note.id}: calibrate rover sensors" in matched.text
    assert deleted.text == f"Deleted note {note.id}."
    assert store.list() == []


def test_notes_skill_rejects_empty_note_text(tmp_path):
    store, context = _notes_context(tmp_path)
    response = NotesSkill().handle("remember this   ", context)

    assert response.text == "I need note text to save."
    assert response.metadata["error"] == "empty_note"
    assert store.list() == []


def test_notes_skill_requires_confirmation_before_delete_all(tmp_path):
    store, context = _notes_context(tmp_path)
    skill = NotesSkill()
    store.add("first note")
    store.add("second note")

    requested = skill.handle("delete all notes", context)
    confirmed = skill.handle("confirm delete all notes", context)

    assert requested.text == "Please confirm by typing: confirm delete all notes."
    assert requested.metadata["confirmation_required"] is True
    assert confirmed.text == "Deleted 2 notes."
    assert store.list() == []


def test_notes_skill_reports_missing_notes_store():
    response = NotesSkill().handle("save note calibrate rover sensors", SkillContext())

    assert response.text == "Notes storage is not available."
    assert response.metadata["error"] == "missing_notes_store"
