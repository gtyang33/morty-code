from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from morty_code.mcp.client import McpStdioClient
from morty_code.tools.tool_registry import ToolRegistry, ToolSpec


async def create_mcp_tool_registry(
    configs: dict[str, dict[str, Any]],
    *,
    workspace_root: str | Path | None = None,
    statuses: dict[str, dict[str, object]] | None = None,
    startup_timeout: float | None = None,
) -> ToolRegistry:
    """连接已配置的 MCP server，并把其 tools 包装成 Morty ToolSpec。

    例子：
    `uv run morty-code mcp add mysql_query -s user -e MYSQL_HOST=127.0.0.1 ... -- npx @benborla29/mcp-server-mysql`
    会先由 CLI 写入 user 级 MCP 配置：
    `{name: "mysql_query", command: "npx", args: ["@benborla29/mcp-server-mysql"], env: {...}}`。
    Morty 启动时读取该配置，用 stdio 拉起 `npx @benborla29/mcp-server-mysql`，
    发送 MCP `initialize` 和 `tools/list`。如果 server 返回一个名为
    `mysql_query` 的 tool，这里会包装成内部工具名
    `mcp__mysql_query__mysql_query`，注册到 ToolRegistry，并把 MCP tool 的
    `inputSchema` 作为模型可见的参数 schema。模型后续调用这个内部工具名时，
    handler 会把输入再转回 MCP `tools/call`，实际调用原始 tool 名
    `mysql_query`。
    """

    registry = ToolRegistry()
    timeout = _startup_timeout(startup_timeout)
    for server_name, config in configs.items():
        if config.get("disabled") and statuses is not None:
            statuses[server_name] = {"status": "disabled", "tools": []}
    server_results = await asyncio.gather(
        *(
            _load_stdio_server_tools(
                server_name=server_name,
                config=configs[server_name],
                workspace_root=workspace_root,
                timeout=timeout,
            )
            for server_name in sorted(configs)
            if configs[server_name].get("type", "stdio") == "stdio"
            and not configs[server_name].get("disabled")
        )
    )
    for server_name, config, tools, error in server_results:
        if error is not None:
            if statuses is not None:
                statuses[server_name] = {
                    "status": "failed",
                    "error": error,
                }
            continue
        if statuses is not None:
            statuses[server_name] = {
                "status": "connected",
                "tools": _tool_statuses(server_name, tools),
                "capabilities": ["tools"],
            }
        for spec in _wrap_mcp_tools(
            server_name=server_name,
            config=config,
            tools=tools,
            workspace_root=workspace_root,
        ):
            registry.register(spec)
    return registry


async def _load_stdio_server_tools(
    *,
    server_name: str,
    config: dict[str, Any],
    workspace_root: str | Path | None,
    timeout: float,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], str | None]:
    """并发探测单个 stdio MCP server，失败只返回错误，不抛到主启动。"""

    client = McpStdioClient(
        name=server_name,
        command=str(config.get("command") or ""),
        args=[str(arg) for arg in config.get("args", [])],
        env={str(key): str(value) for key, value in dict(config.get("env", {})).items()},
        cwd=workspace_root,
    )
    try:
        tools = await asyncio.wait_for(
            _connect_and_list_tools(client),
            timeout=timeout,
        )
        return server_name, config, tools, None
    except asyncio.TimeoutError:
        return server_name, config, [], f"MCP server {server_name} startup timed out after {timeout:g}s"
    except Exception as exc:  # noqa: BLE001 - 单个 MCP server 失败不能拖垮主会话。
        return server_name, config, [], str(exc)
    finally:
        await client.close(grace_period=0.1)


def _wrap_mcp_tools(
    *,
    server_name: str,
    config: dict[str, Any],
    tools: list[dict[str, Any]],
    workspace_root: str | Path | None,
) -> list[ToolSpec]:
    specs: list[ToolSpec] = []
    for tool in tools:
        original_name = str(tool.get("name") or "")
        if not original_name:
            continue
        # MCP tool 对模型暴露时必须带 server 前缀，避免不同 MCP server
        # 返回同名 tool 时冲突。比如 server `mysql_query` 返回原始 tool
        # `mysql_query`，Morty 内部注册名就是 `mcp__mysql_query__mysql_query`。
        wrapped_name = f"mcp__{_normalize_name(server_name)}__{_normalize_name(original_name)}"
        specs.append(
            ToolSpec(
                name=wrapped_name,
                description=str(tool.get("description") or f"MCP tool {server_name}.{original_name}"),
                prompt=str(tool.get("description") or f"MCP tool {server_name}.{original_name}"),
                handler=_make_handler(
                    server_name=server_name,
                    command=str(config.get("command") or ""),
                    args=[str(arg) for arg in config.get("args", [])],
                    env={str(key): str(value) for key, value in dict(config.get("env", {})).items()},
                    cwd=workspace_root,
                    original_name=original_name,
                ),
                input_schema=_input_schema(tool),
            )
        )
    return specs


def _tool_statuses(server_name: str, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 MCP tools/list 结果转换为 `/mcp` 可展示的轻量元数据。"""

    result: list[dict[str, Any]] = []
    for tool in tools:
        original_name = str(tool.get("name") or "")
        if not original_name:
            continue
        result.append(
            {
                "name": original_name,
                "wrapped_name": f"mcp__{_normalize_name(server_name)}__{_normalize_name(original_name)}",
                "description": str(tool.get("description") or ""),
                "input_schema": _input_schema(tool),
            }
        )
    return result


async def _connect_and_list_tools(client: McpStdioClient) -> list[dict[str, Any]]:
    """启动 MCP server 并拉取 tools；外层统一负责超时和失败隔离。"""

    await client.connect()
    return await client.list_tools()


def _startup_timeout(explicit: float | None) -> float:
    """读取 MCP 启动超时，避免 npx/数据库连接卡住 Morty 主启动。"""

    if explicit is not None:
        return max(0.1, explicit)
    raw = os.environ.get("MORTY_MCP_STARTUP_TIMEOUT", "5")
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 5.0


def merge_tool_registries(*registries: ToolRegistry | None) -> ToolRegistry:
    """按顺序合并工具注册表；后续同名工具会覆盖前面的定义。"""

    merged: dict[str, ToolSpec] = {}
    for registry in registries:
        if registry is None:
            continue
        for name in registry.list_names():
            tool = registry.find(name)
            if tool is not None:
                merged[name] = tool
    return ToolRegistry(list(merged.values()))


def _make_handler(
    *,
    server_name: str,
    command: str,
    args: list[str],
    env: dict[str, str],
    cwd: str | Path | None,
    original_name: str,
):
    server_args = list(args)

    async def handler(tool_input: dict[str, object]) -> dict[str, object]:
        # Morty 的 query loop 目前每轮通过 asyncio.run 启动新 event loop。
        # MCP subprocess/StreamReader 不能跨 loop 复用，所以每次 tools/call
        # 都在当前 loop 建立短连接；之后可演进为专用后台 loop 长连接。
        client = McpStdioClient(
            name=server_name,
            command=command,
            args=server_args,
            env=env,
            cwd=cwd,
        )
        try:
            await client.connect()
            return await client.call_tool(original_name, tool_input)
        finally:
            await client.close()

    return handler


def _input_schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema")
    if isinstance(schema, dict):
        return schema
    return {"type": "object", "properties": {}}


def _normalize_name(name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return normalized or "unnamed"
