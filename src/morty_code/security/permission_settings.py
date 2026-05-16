from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUPPORTED_PERMISSION_MODES = {
    "acceptEdits",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
}


@dataclass
class PermissionSettings:
    """从项目文件和环境变量合并后的权限设置。

    Claude Code 会按 user/project/local/cli 等来源合并权限规则。morty-code
    当前先实现项目内 shared + local + env 三层，执行层只消费合并结果。
    """

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    default_mode: str | None = None
    sources: list[str] = field(default_factory=list)


def load_permission_settings(
    root: Path | str,
    *,
    env_allow: list[str] | None = None,
    env_deny: list[str] | None = None,
    env_ask: list[str] | None = None,
    env_default_mode: str | None = None,
) -> PermissionSettings:
    """加载项目权限配置。

    支持两个文件：
    - `.morty/permissions.json`：项目共享配置。
    - `.morty/permissions.local.json`：本机配置，优先级更高。
    """

    root_path = Path(root)
    merged = PermissionSettings()
    for path in [
        root_path / ".morty" / "permissions.json",
        root_path / ".morty" / "permissions.local.json",
    ]:
        if not path.exists():
            continue
        _merge_settings(merged, _read_settings_file(path), str(path))

    _merge_list(merged.allow, env_allow or [])
    _merge_list(merged.deny, env_deny or [])
    _merge_list(merged.ask, env_ask or [])
    if env_allow or env_deny or env_ask:
        merged.sources.append("env")
    if env_default_mode:
        merged.default_mode = _normalize_mode(env_default_mode, "env")
        merged.sources.append("env:permission_mode")
    return merged


def _read_settings_file(path: Path) -> dict[str, Any]:
    """内部读取持久化内容。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid permission settings JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"permission settings must be an object: {path}")
    return data


def _merge_settings(target: PermissionSettings, data: dict[str, Any], source: str) -> None:
    """内部合并相邻或相关的数据块。"""
    permissions = data.get("permissions", data)
    if not isinstance(permissions, dict):
        raise ValueError(f"permissions must be an object: {source}")
    _merge_list(target.allow, _as_string_list(permissions.get("allow"), source, "allow"))
    _merge_list(target.deny, _as_string_list(permissions.get("deny"), source, "deny"))
    _merge_list(target.ask, _as_string_list(permissions.get("ask"), source, "ask"))
    default_mode = permissions.get("defaultMode", permissions.get("default_mode"))
    if default_mode is not None:
        target.default_mode = _normalize_mode(str(default_mode), source)
    target.sources.append(source)


def _as_string_list(value: object, source: str, key: str) -> list[str]:
    """内部转换为目标表示。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"permissions.{key} must be a list: {source}")
    result = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _merge_list(target: list[str], values: list[str]) -> None:
    """内部合并相邻或相关的数据块。"""
    for value in values:
        if value not in target:
            target.append(value)


def _normalize_mode(mode: str, source: str) -> str:
    """内部规范化消息结构。"""
    if mode not in SUPPORTED_PERMISSION_MODES:
        allowed = ", ".join(sorted(SUPPORTED_PERMISSION_MODES))
        raise ValueError(f"unsupported permission mode '{mode}' in {source}; expected one of: {allowed}")
    return mode
