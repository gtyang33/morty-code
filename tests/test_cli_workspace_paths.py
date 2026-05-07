from __future__ import annotations

import os

import pytest

from morty_code import _resolve_cli_path, _resolve_workspace_root
from morty_code.transcript.transcript_store import TranscriptStore


def test_resolve_workspace_root_defaults_to_process_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert _resolve_workspace_root(None) == tmp_path.resolve()


def test_resolve_workspace_root_rejects_missing_directory(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        _resolve_workspace_root(str(tmp_path / "missing"))


def test_relative_cli_path_is_resolved_under_workspace_root(tmp_path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()

    resolved = _resolve_cli_path(".morty/sessions/demo.jsonl", workspace)

    assert resolved == (workspace / ".morty/sessions/demo.jsonl").resolve()


def test_latest_session_store_uses_most_recent_non_empty_jsonl(tmp_path) -> None:
    session_dir = tmp_path / ".morty" / "sessions"
    session_dir.mkdir(parents=True)
    older = session_dir / "older.jsonl"
    newer = session_dir / "newer.jsonl"
    empty = session_dir / "empty.jsonl"
    older.write_text("{}\n", encoding="utf-8")
    newer.write_text("{}\n", encoding="utf-8")
    empty.write_text("", encoding="utf-8")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    store = TranscriptStore.latest_in_session_dir(session_dir)

    assert store is not None
    assert store.path == newer
    assert store.session_id == "newer"


def test_latest_session_store_returns_none_when_no_sessions(tmp_path) -> None:
    assert TranscriptStore.latest_in_session_dir(tmp_path / "missing") is None
