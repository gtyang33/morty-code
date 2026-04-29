from __future__ import annotations

from pathlib import Path

from morty_code.tools.tool_registry import ToolRegistry, ToolSpec


def create_local_tool_registry(
    cwd: str | Path,
    max_read_chars: int = 20000,
    max_dir_entries: int = 200,
) -> ToolRegistry:
    """创建受 cwd 约束的本地只读工具集。"""

    root = Path(cwd).expanduser().resolve()

    async def read_file(args: dict[str, object]) -> dict[str, object]:
        path = _resolve_under_root(root, str(args.get("path", "")))
        content = path.read_text(encoding="utf-8", errors="replace")
        visible = content[:max_read_chars]
        return {
            "path": str(path),
            "content": visible,
            "truncated": len(content) > len(visible),
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

    return ToolRegistry(
        [
            ToolSpec(
                name="read_file",
                description="Read a UTF-8 text file under the current workspace root.",
                handler=read_file,
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path under the workspace root.",
                        }
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
