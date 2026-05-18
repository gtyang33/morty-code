from __future__ import annotations

import asyncio

import pytest

from morty_code.security import SecurityViolation
from morty_code.tools.builtin_tools import create_local_tool_registry
from morty_code.types.runtime_state import CacheSafeParams, ContentReplacementState, ToolUseContext


def make_context() -> ToolUseContext:
    return ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="default",
        app_state={},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


def make_cache() -> CacheSafeParams:
    return CacheSafeParams(system_prompt=[], user_context={}, system_context={}, messages=[])


def run_tool(root, name: str, args: dict[str, object], context: ToolUseContext | None = None):
    registry = create_local_tool_registry(root)
    tool = registry.find(name)
    assert tool is not None
    context = context or make_context()
    if tool.needs_context:
        return asyncio.run(tool.handler(args, context, make_cache()))
    return asyncio.run(tool.handler(args))


def test_create_dir_and_file_info(tmp_path) -> None:
    created = run_tool(tmp_path, "create_dir", {"path": "src/pkg"})
    info = run_tool(tmp_path, "file_info", {"path": "src/pkg"})

    assert created["operation"] == "create"
    assert info["kind"] == "directory"
    assert info["path"].endswith("src/pkg")


def test_move_path_moves_file_and_refuses_existing_destination(tmp_path) -> None:
    (tmp_path / "old.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "existing.txt").write_text("keep", encoding="utf-8")

    moved = run_tool(tmp_path, "move_path", {"source": "old.txt", "destination": "nested/new.txt"})

    assert moved["operation"] == "move"
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "nested/new.txt").read_text(encoding="utf-8") == "hello"
    with pytest.raises(FileExistsError):
        run_tool(tmp_path, "move_path", {"source": "nested/new.txt", "destination": "existing.txt"})


def test_multi_edit_requires_fresh_read_and_applies_edits(tmp_path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    context = make_context()

    with pytest.raises(PermissionError):
        run_tool(
            tmp_path,
            "multi_edit",
            {
                "path": "example.txt",
                "edits": [{"old_string": "beta", "new_string": "BETA"}],
            },
            context,
        )

    run_tool(tmp_path, "read_file", {"path": "example.txt"}, context)
    result = run_tool(
        tmp_path,
        "multi_edit",
        {
            "path": "example.txt",
            "edits": [
                {"old_string": "beta", "new_string": "BETA"},
                {"old_string": "alpha", "new_string": "ALPHA", "replace_all": True},
            ],
        },
        context,
    )

    assert result["edits"] == [
        {"index": 0, "replacements": 1},
        {"index": 1, "replacements": 2},
    ]
    assert path.read_text(encoding="utf-8") == "ALPHA\nBETA\nALPHA\n"
    assert "BETA" in context.read_file_state[str(path)].content


def test_edit_file_allows_partial_read_when_old_string_is_unique(tmp_path) -> None:
    path = tmp_path / "large.txt"
    path.write_text("header\nTARGET = 1\n" + "tail\n" * 100, encoding="utf-8")
    context = make_context()

    run_tool(tmp_path, "read_file", {"path": "large.txt", "offset": 0, "limit": 2}, context)
    result = run_tool(
        tmp_path,
        "edit_file",
        {
            "path": "large.txt",
            "old_string": "TARGET = 1",
            "new_string": "TARGET = 2",
        },
        context,
    )

    assert result["replacements"] == 1
    assert "TARGET = 2" in path.read_text(encoding="utf-8")


def test_multi_edit_rejects_ambiguous_replacement(tmp_path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("same same", encoding="utf-8")
    context = make_context()
    run_tool(tmp_path, "read_file", {"path": "example.txt"}, context)

    with pytest.raises(ValueError, match="appears 2 times"):
        run_tool(
            tmp_path,
            "multi_edit",
            {
                "path": "example.txt",
                "edits": [{"old_string": "same", "new_string": "other"}],
            },
            context,
        )


def test_append_file_supports_chunked_document_writes(tmp_path) -> None:
    context = make_context()

    first = run_tool(tmp_path, "append_file", {"path": "docs/report.md", "content": "# Title\n"}, context)
    second = run_tool(tmp_path, "append_file", {"path": "docs/report.md", "content": "body\n"}, context)

    path = tmp_path / "docs/report.md"
    assert first["operation"] == "create"
    assert second["operation"] == "append"
    assert path.read_text(encoding="utf-8") == "# Title\nbody\n"
    assert str(path) in context.read_file_state


def test_new_write_tools_respect_security_guards(tmp_path) -> None:
    (tmp_path / "safe.txt").write_text("x", encoding="utf-8")

    with pytest.raises(SecurityViolation):
        run_tool(tmp_path, "create_dir", {"path": ".git/hooks"})
    with pytest.raises(SecurityViolation):
        run_tool(tmp_path, "move_path", {"source": "safe.txt", "destination": ".env"})


def test_bash_runs_in_os_sandbox_by_default(tmp_path) -> None:
    result = run_tool(tmp_path, "bash", {"command": "printf ok"})

    assert result["exit_code"] == 0
    assert result["stdout"] == "ok"
    assert result["sandbox"]["enabled"] is True
    assert result["sandbox"]["backend"] == "bwrap"
