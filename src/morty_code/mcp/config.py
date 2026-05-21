from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal


ConfigScope = Literal["user", "project"]


def add_mcp_server(
    *,
    name: str,
    scope: str,
    command: str,
    args: list[str],
    env: dict[str, str],
    workspace_root: str | Path,
) -> Path:
    """写入 stdio MCP server 配置，并返回被修改的配置文件路径。"""

    normalized_scope = _normalize_scope(scope)
    if not name or any(not (char.isalnum() or char in {"-", "_"}) for char in name):
        raise ValueError("MCP server name can only contain letters, numbers, hyphens, and underscores")
    if not command:
        raise ValueError("MCP server command is required")

    path = _config_path(normalized_scope, Path(workspace_root))
    config = _read_config(path)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"Invalid MCP config at {path}: mcpServers must be an object")
    if name in servers:
        raise ValueError(f"MCP server {name} already exists in {normalized_scope} config")
    servers[name] = {
        "type": "stdio",
        "command": command,
        "args": list(args),
        "env": dict(env),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_mcp_servers(workspace_root: str | Path) -> dict[str, dict[str, Any]]:
    """按 user -> project 顺序加载 MCP server，project 同名配置覆盖 user。"""

    return {
        name: {
            key: value
            for key, value in entry.items()
            if not key.startswith("_")
        }
        for name, entry in load_mcp_server_entries(workspace_root).items()
    }


def load_mcp_server_entries(workspace_root: str | Path) -> dict[str, dict[str, Any]]:
    """加载 MCP server 配置，并保留 scope/config_path 等展示和写回元数据。"""

    root = Path(workspace_root)
    merged: dict[str, dict[str, Any]] = {}
    for scope in ("user", "project"):
        config_path = _config_path(scope, root)
        config = _read_config(config_path)
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            continue
        for name, value in servers.items():
            if isinstance(value, dict):
                entry = dict(value)
                # 下划线字段只给 Morty runtime 使用，不暴露给 MCP server。
                entry["_scope"] = scope
                entry["_config_path"] = str(config_path)
                merged[str(name)] = entry
    return merged


def set_mcp_server_disabled(
    *,
    name: str,
    disabled: bool,
    workspace_root: str | Path,
) -> Path:
    """修改 MCP server 的 disabled 标记，用于 `/mcp <server> disable/enable`。"""

    entries = load_mcp_server_entries(workspace_root)
    entry = entries.get(name)
    if not entry:
        raise ValueError(f"MCP server not found: {name}")
    config_path = Path(str(entry["_config_path"]))
    config = _read_config(config_path)
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict) or name not in servers or not isinstance(servers[name], dict):
        raise ValueError(f"Invalid MCP config at {config_path}: server {name} is missing")
    servers[name]["disabled"] = disabled
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return config_path


def parse_env_assignments(values: list[str] | None) -> dict[str, str]:
    """解析 CLI 的 KEY=value 环境变量列表。"""

    env: dict[str, str] = {}
    for raw in values or []:
        if "=" not in raw:
            raise ValueError(f"Invalid env assignment {raw!r}; expected KEY=value")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid env assignment {raw!r}; key is empty")
        env[key] = value
    return env


def _config_path(scope: ConfigScope, workspace_root: Path) -> Path:
    if scope == "user":
        return _morty_home() / "mcp.json"
    return workspace_root / ".morty" / "mcp.json"


def _morty_home() -> Path:
    return Path(os.environ.get("MORTY_HOME") or Path.home() / ".morty").expanduser()


def _normalize_scope(scope: str) -> ConfigScope:
    if scope in {"user", "project"}:
        return scope  # type: ignore[return-value]
    if scope == "local":
        return "project"
    raise ValueError("MCP config scope must be user or project")


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid MCP config JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Invalid MCP config at {path}: root must be an object")
    return data
