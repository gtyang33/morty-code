from __future__ import annotations

import pytest

from morty_code import _resolve_cli_path, _resolve_workspace_root


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
