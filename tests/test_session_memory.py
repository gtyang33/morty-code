from __future__ import annotations

from morty_code.memory.session_memory import SessionMemoryStore


def test_session_memory_prunes_old_notes_to_budget(tmp_path) -> None:
    store = SessionMemoryStore(tmp_path / "session.md", max_chars=90, max_notes=3)

    store.append_note("first note should be dropped")
    store.append_note("second note survives")
    store.append_note("third note survives")
    store.append_note("fourth note survives")

    text = store.read()
    assert text.startswith("# Session Memory\n")
    assert "first note should be dropped" not in text
    assert "second note survives" in text
    assert "third note survives" in text
    assert "fourth note survives" in text
    assert len(text) <= 90


def test_session_memory_max_notes_zero_keeps_only_header(tmp_path) -> None:
    store = SessionMemoryStore(tmp_path / "session.md", max_chars=1000, max_notes=0)

    store.append_note("temporary note")

    assert store.read() == "# Session Memory\n"
