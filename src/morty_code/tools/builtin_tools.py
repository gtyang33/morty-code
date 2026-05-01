from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import time
from pathlib import Path

from morty_code.security import (
    assert_safe_bash_command,
    assert_safe_read_path,
    assert_safe_write_path,
)
from morty_code.tools.tool_registry import ToolRegistry, ToolSpec
from morty_code.types.runtime_state import CacheSafeParams, FileViewState, ToolUseContext


_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".jj",
    ".venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
}


def create_local_tool_registry(
    cwd: str | Path,
    max_read_chars: int = 20000,
    max_dir_entries: int = 200,
) -> ToolRegistry:
    """创建受 cwd 约束的本地工具集。"""

    root = Path(cwd).expanduser().resolve()

    async def read_file(
        args: dict[str, object],
        context: ToolUseContext,
        _cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        path = _resolve_under_root(root, str(args.get("path", "")))
        assert_safe_read_path(root, path)
        if path.is_dir():
            raise IsADirectoryError(str(path))
        content = path.read_text(encoding="utf-8", errors="replace")
        offset = _optional_int(args.get("offset"), default=0, minimum=0)
        limit = _optional_int(args.get("limit"), default=0, minimum=0)
        lines = content.splitlines()
        if limit:
            visible_lines = lines[offset : offset + limit]
            visible = "\n".join(visible_lines)
            partial = True
        elif offset:
            visible = "\n".join(lines[offset:])
            partial = True
        else:
            visible = content[:max_read_chars]
            partial = len(content) > len(visible)
        stat = path.stat()
        context.read_file_state[str(path)] = FileViewState(
            path=str(path),
            content=visible,
            timestamp=stat.st_mtime * 1000,
            offset=offset if offset else None,
            limit=limit if limit else None,
            is_partial_view=partial,
        )
        return {
            "path": str(path),
            "content": visible,
            "line_count": len(lines),
            "truncated": partial,
        }

    async def list_dir(args: dict[str, object]) -> dict[str, object]:
        path = _resolve_under_root(root, str(args.get("path", ".")))
        if not path.is_dir():
            raise ValueError(f"{path} is not a directory")
        entries = [
            {
                "name": child.name,
                "kind": "directory" if child.is_dir() else "file",
            }
            for child in path.iterdir()
        ]
        entries.sort(key=lambda item: (str(item["kind"]), str(item["name"])))
        return {
            "path": str(path),
            "entries": entries[:max_dir_entries],
            "truncated": len(entries) > max_dir_entries,
        }

    async def glob_files(args: dict[str, object]) -> dict[str, object]:
        pattern = str(args.get("pattern") or "").strip()
        if not pattern:
            raise ValueError("pattern is required")
        base = _resolve_under_root(root, str(args.get("path") or "."))
        if not base.is_dir():
            raise ValueError(f"{base} is not a directory")
        limit = _optional_int(args.get("limit"), default=100, minimum=1, maximum=1000)
        started = time.time()
        matches: list[str] = []
        for child in _walk_files(base):
            rel = child.relative_to(root).as_posix()
            local = child.relative_to(base).as_posix()
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(local, pattern):
                matches.append(rel)
                if len(matches) >= limit:
                    break
        return {
            "filenames": matches,
            "num_files": len(matches),
            "truncated": len(matches) >= limit,
            "duration_ms": int((time.time() - started) * 1000),
        }

    async def grep_text(args: dict[str, object]) -> dict[str, object]:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            raise ValueError("pattern is required")
        regex = re.compile(pattern, re.IGNORECASE if args.get("ignore_case") else 0)
        base = _resolve_under_root(root, str(args.get("path") or "."))
        glob = str(args.get("glob") or "*")
        output_mode = str(args.get("output_mode") or "files_with_matches")
        limit = _optional_int(args.get("limit"), default=100, minimum=1, maximum=1000)
        files = [base] if base.is_file() else list(_walk_files(base))
        filenames: list[str] = []
        content_lines: list[str] = []
        counts: dict[str, int] = {}
        for file_path in files:
            rel = file_path.relative_to(root).as_posix()
            if glob and not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(file_path.name, glob):
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            file_count = 0
            for line_no, line in enumerate(text.splitlines(), start=1):
                if not regex.search(line):
                    continue
                file_count += 1
                if output_mode == "content":
                    content_lines.append(f"{rel}:{line_no}: {line}")
                    if len(content_lines) >= limit:
                        break
            if file_count:
                filenames.append(rel)
                counts[rel] = file_count
            if len(filenames) >= limit or len(content_lines) >= limit:
                break
        return {
            "mode": output_mode,
            "filenames": filenames[:limit],
            "num_files": len(filenames[:limit]),
            "content": "\n".join(content_lines[:limit]) if output_mode == "content" else None,
            "counts": counts if output_mode == "count" else None,
            "truncated": len(filenames) >= limit or len(content_lines) >= limit,
        }

    async def write_file(
        args: dict[str, object],
        context: ToolUseContext,
        _cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        path = _resolve_for_write(root, str(args.get("path", "")))
        assert_safe_write_path(root, path)
        content = str(args.get("content", ""))
        existed = path.exists()
        original = path.read_text(encoding="utf-8", errors="replace") if existed else None
        if existed:
            _ensure_fresh_read(path, context)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        context.read_file_state[str(path)] = FileViewState(
            path=str(path),
            content=content[:max_read_chars],
            timestamp=path.stat().st_mtime * 1000,
            is_partial_view=len(content) > max_read_chars,
        )
        return {
            "path": str(path),
            "operation": "update" if existed else "create",
            "bytes_written": len(content.encode("utf-8")),
            "diff": _simple_diff(original or "", content),
        }

    async def edit_file(
        args: dict[str, object],
        context: ToolUseContext,
        _cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        path = _resolve_under_root(root, str(args.get("path", "")))
        assert_safe_write_path(root, path)
        old = str(args.get("old_string", ""))
        new = str(args.get("new_string", ""))
        replace_all = bool(args.get("replace_all") is True)
        if old == new:
            raise ValueError("old_string and new_string are identical")
        _ensure_fresh_read(path, context)
        original = path.read_text(encoding="utf-8", errors="replace")
        count = original.count(old)
        if not old:
            raise ValueError("old_string is required for edit_file; use write_file to create files")
        if count == 0:
            raise ValueError("old_string not found")
        if count > 1 and not replace_all:
            raise ValueError(f"old_string appears {count} times; set replace_all=true or make it unique")
        updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
        path.write_text(updated, encoding="utf-8")
        context.read_file_state[str(path)] = FileViewState(
            path=str(path),
            content=updated[:max_read_chars],
            timestamp=path.stat().st_mtime * 1000,
            is_partial_view=len(updated) > max_read_chars,
        )
        return {
            "path": str(path),
            "replacements": count if replace_all else 1,
            "diff": _simple_diff(original, updated),
        }

    async def bash(
        args: dict[str, object],
        context: ToolUseContext,
        _cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ValueError("command is required")
        assert_safe_bash_command(
            command,
            root=root,
            allow_dangerous=bool(context.app_state.get("allow_dangerous_bash", False)),
        )
        timeout_ms = _optional_int(args.get("timeout_ms"), default=120000, minimum=1000, maximum=600000)
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PWD": str(root)},
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            timed_out = True
        return {
            "command": command,
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "stdout": stdout.decode("utf-8", errors="replace")[-20000:],
            "stderr": stderr.decode("utf-8", errors="replace")[-12000:],
        }

    async def todo_write(
        args: dict[str, object],
        context: ToolUseContext,
        _cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        todos = args.get("todos")
        if not isinstance(todos, list):
            raise ValueError("todos must be a list")
        normalized = []
        for item in todos:
            if not isinstance(item, dict):
                raise ValueError("each todo must be an object")
            content = str(item.get("content") or "").strip()
            status = str(item.get("status") or "pending")
            if not content:
                raise ValueError("todo content is required")
            if status not in {"pending", "in_progress", "completed"}:
                raise ValueError(f"invalid todo status: {status}")
            normalized.append({"content": content, "status": status})
        old = list(context.app_state.get("todos", []))
        context.app_state["todos"] = normalized
        return {"old_todos": old, "new_todos": normalized}

    return ToolRegistry(
        [
            ToolSpec(
                name="read_file",
                description="Read a UTF-8 text file under the current workspace root.",
                handler=read_file,
                needs_context=True,
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path under the workspace root.",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Zero-based line offset.",
                            "minimum": 0,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of lines to read.",
                            "minimum": 1,
                        },
                    },
                    "required": ["path"],
                },
            ),
            ToolSpec(
                name="list_dir",
                description="List files and directories under the current workspace root.",
                handler=list_dir,
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative directory path under the workspace root.",
                            "default": ".",
                        }
                    },
                },
            ),
            ToolSpec(
                name="glob_files",
                description="Find files by glob pattern under the workspace root.",
                handler=glob_files,
                input_schema={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Glob pattern such as '*.py' or 'src/**/*.py'."},
                        "path": {"type": "string", "description": "Directory to search from.", "default": "."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                    },
                    "required": ["pattern"],
                },
            ),
            ToolSpec(
                name="grep_text",
                description="Search UTF-8 files with a regular expression.",
                handler=grep_text,
                input_schema={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Python regular expression to search for."},
                        "path": {"type": "string", "description": "File or directory to search.", "default": "."},
                        "glob": {"type": "string", "description": "Optional file glob filter.", "default": "*"},
                        "output_mode": {"type": "string", "enum": ["files_with_matches", "content", "count"], "default": "files_with_matches"},
                        "ignore_case": {"type": "boolean", "default": False},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                    },
                    "required": ["pattern"],
                },
            ),
            ToolSpec(
                name="write_file",
                description="Create or overwrite a UTF-8 file under the workspace root. Existing files must be read first.",
                handler=write_file,
                needs_context=True,
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path under the workspace root."},
                        "content": {"type": "string", "description": "Full file content to write."},
                    },
                    "required": ["path", "content"],
                },
            ),
            ToolSpec(
                name="edit_file",
                description="Replace exact text in a file under the workspace root. File must be read first.",
                handler=edit_file,
                needs_context=True,
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path under the workspace root."},
                        "old_string": {"type": "string", "description": "Exact text to replace."},
                        "new_string": {"type": "string", "description": "Replacement text."},
                        "replace_all": {"type": "boolean", "default": False},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            ),
            ToolSpec(
                name="bash",
                description="Run a shell command in the workspace root and return stdout/stderr.",
                handler=bash,
                needs_context=True,
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to execute."},
                        "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 600000, "default": 120000},
                    },
                    "required": ["command"],
                },
            ),
            ToolSpec(
                name="todo_write",
                description="Replace the session todo list.",
                handler=todo_write,
                needs_context=True,
                input_schema={
                    "type": "object",
                    "properties": {
                        "todos": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "content": {"type": "string"},
                                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                                },
                                "required": ["content", "status"],
                            },
                        }
                    },
                    "required": ["todos"],
                },
            ),
        ]
    )


def _resolve_under_root(root: Path, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("path is required")
    path = Path(raw_path).expanduser()
    resolved = (path if path.is_absolute() else root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"path escapes workspace root: {raw_path}") from exc
    if not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return resolved


def _resolve_for_write(root: Path, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("path is required")
    path = Path(raw_path).expanduser()
    resolved = (path if path.is_absolute() else root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"path escapes workspace root: {raw_path}") from exc
    return resolved


def _walk_files(base: Path):
    for child in base.rglob("*"):
        if any(part in _SKIP_DIRS for part in child.parts):
            continue
        if child.is_file():
            yield child


def _optional_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        parsed = default if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _ensure_fresh_read(path: Path, context: ToolUseContext) -> None:
    state = context.read_file_state.get(str(path))
    if state is None:
        raise PermissionError("file must be read before write/edit")
    if state.is_partial_view:
        raise PermissionError("file was only partially read; read the full file before write/edit")
    current_mtime = path.stat().st_mtime * 1000
    if state.timestamp is not None and current_mtime > state.timestamp + 1:
        raise PermissionError("file changed since it was read; read it again before write/edit")


def _simple_diff(old: str, new: str, max_chars: int = 12000) -> str:
    import difflib

    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )
    return diff[:max_chars]
