from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from asyncio.subprocess import Process
from subprocess import DEVNULL
from typing import Any


class McpStdioClient:
    """最小 MCP stdio JSON-RPC client，仅覆盖 tools/list 和 tools/call。"""

    def __init__(
        self,
        *,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = str(cwd) if cwd is not None else None
        self._process: Process | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._process is not None:
            return
        process_env = {**_default_inherited_env(), **self.env}
        self._process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=DEVNULL,
            env=process_env,
            cwd=self.cwd,
        )
        await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "morty-code", "version": "0.1.0"},
            },
        )
        await self.notify("notifications/initialized", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return [tool for tool in tools if isinstance(tool, dict)]

    async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> dict[str, Any]:
        result = await self.request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments,
            },
        )
        return result if isinstance(result, dict) else {"result": result}

    async def request(self, method: str, params: dict[str, object]) -> dict[str, Any]:
        await self.connect() if self._process is None and method != "initialize" else None
        process = self._require_process()
        async with self._lock:
            request_id = self._next_id
            self._next_id += 1
            await self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            while True:
                raw = await process.stdout.readline()  # type: ignore[union-attr]
                if not raw:
                    raise RuntimeError(f"MCP server {self.name} exited before responding to {method}")
                message = json.loads(raw.decode("utf-8"))
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(f"MCP server {self.name} returned error: {message['error']}")
                result = message.get("result", {})
                return result if isinstance(result, dict) else {"result": result}

    async def notify(self, method: str, params: dict[str, object]) -> None:
        self._require_process()
        await self._write_message({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self, grace_period: float = 1.0) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=max(0.0, grace_period))
            except asyncio.TimeoutError:
                if process.returncode is None:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    if process.returncode is None:
                        process.kill()
                    try:
                        # 某些 MCP server 或 sandbox 环境下，kill 后的 wait 仍可能卡住；
                        # 关闭路径不能反过来拖死主进程。
                        await asyncio.wait_for(process.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self._close_transport(process)
            try:
                # BaseSubprocessTransport.close() 会用 call_soon 安排 pipe
                # connection_lost；在 asyncio.run 关闭 loop 前主动让这些回调跑完。
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            except RuntimeError:
                pass

    def _require_process(self) -> Process:
        if self._process is None:
            raise RuntimeError(f"MCP server {self.name} is not connected")
        return self._process

    async def _write_message(self, message: dict[str, object]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise RuntimeError(f"MCP server {self.name} stdin is closed")
        process.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        await process.stdin.drain()

    def _close_transport(self, process: Process) -> None:
        """显式关闭 asyncio subprocess transport，避免 loop 关闭后 __del__ 再补关。"""

        transport = getattr(process, "_transport", None)
        if transport is None:
            return
        try:
            transport.close()
        except RuntimeError:
            pass


def _default_inherited_env() -> dict[str, str]:
    """按 Claude SDK 的思路只继承启动 MCP server 必需的安全环境变量。"""

    keys = ["HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"]
    return {
        key: value
        for key in keys
        if (value := os.environ.get(key)) is not None and not value.startswith("()")
    }
