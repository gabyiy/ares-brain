import pytest

from events import EventBus
from memory import NotesStore


def test_notes_store_adds_duplicate_notes_with_unique_ids_and_persists(tmp_path):
    path = tmp_path / "notes.json"
    store = NotesStore(path=path, event_bus=EventBus())

    first = store.add("calibrate rover sensors")
    second = store.add("calibrate rover sensors")

    assert first.id != second.id
    assert first.id.startswith("note-")
    assert first.timestamp

    reloaded = NotesStore(path=path, event_bus=EventBus())
    notes = reloaded.list()

    assert [note.text for note in notes] == [
        "calibrate rover sensors",
        "calibrate rover sensors",
    ]
    assert {note.id for note in notes} == {first.id, second.id}


def test_notes_store_search_and_delete(tmp_path):
    store = NotesStore(path=tmp_path / "notes.json", event_bus=EventBus())
    calibration = store.add("calibrate rover sensors")
    store.add("buy batteries")

    matches = store.search("rover")
    deleted = store.delete(calibration.id)

    assert [note.id for note in matches] == [calibration.id]
    assert deleted.id == calibration.id
    assert [note.text for note in store.list()] == ["buy batteries"]
    assert store.delete(calibration.id) is None


def test_notes_store_rejects_empty_notes_and_empty_search(tmp_path):
    store = NotesStore(path=tmp_path / "notes.json", event_bus=EventBus())

    with pytest.raises(ValueError, match="Note text is required"):
        store.add("   ")

    with pytest.raises(ValueError, match="Search keyword is required"):
        store.search("   ")
