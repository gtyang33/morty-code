from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from morty_code import _handle_mcp_cli, _start_mcp_background_loader
from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.input.process_user_input import UserInputProcessor
from morty_code.mcp.config import add_mcp_server, load_mcp_server_entries, load_mcp_servers
from morty_code.mcp.manager import create_mcp_tool_registry
from morty_code.tools.tool_registry import ToolRegistry
from morty_code.tools.tool_runner import ToolRunner
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ContentReplacementState, QueuedCommand, ToolUseContext


def test_add_mcp_server_writes_user_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MORTY_HOME", str(tmp_path / "home"))

    add_mcp_server(
        name="postgres",
        scope="user",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-postgres"],
        env={"DATABASE_URL": "your-db-url"},
        workspace_root=tmp_path / "workspace",
    )

    servers = load_mcp_servers(tmp_path / "workspace")

    assert servers["postgres"]["type"] == "stdio"
    assert servers["postgres"]["command"] == "npx"
    assert servers["postgres"]["args"] == ["-y", "@modelcontextprotocol/server-postgres"]
    assert servers["postgres"]["env"] == {"DATABASE_URL": "your-db-url"}


def test_mcp_add_cli_accepts_command_after_double_dash(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MORTY_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    exit_code = _handle_mcp_cli(
        [
            "add",
            "postgres",
            "-s",
            "user",
            "-e",
            "DATABASE_URL=your-db-url",
            "--",
            "npx",
            "-y",
            "@modelcontextprotocol/server-postgres",
        ],
        workspace_root=workspace,
    )

    assert exit_code == 0
    assert "Added stdio MCP server postgres" in capsys.readouterr().out
    servers = load_mcp_servers(workspace)
    assert servers["postgres"]["command"] == "npx"
    assert servers["postgres"]["args"] == ["-y", "@modelcontextprotocol/server-postgres"]


def test_mcp_slash_command_lists_servers_and_tools(tmp_path) -> None:
    context = ToolUseContext(
        tools=["read_file", "mcp__demo__echo"],
        model="test-model",
        permission_mode="default",
        app_state={
            "mcp_servers": {
                "demo": {
                    "type": "stdio",
                    "command": "python",
                    "args": ["fake_mcp_server.py"],
                    "_scope": "user",
                    "_config_path": str(tmp_path / "home" / "mcp.json"),
                }
            },
            "mcp_statuses": {"demo": {"status": "connected", "tools": 1}},
        },
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )
    processor = UserInputProcessor(AttachmentManager())

    processed = asyncio.run(
        processor.process(
            QueuedCommand(value="/mcp", mode="prompt"),
            context,
            [],
        )
    )

    content = str(processed.messages[0].payload["content"])
    assert processed.should_query is False
    assert "Manage MCP servers" in content
    assert "1 server" in content
    assert "User MCPs" in content
    assert "demo · connected · 1 tool" in content


def test_mcp_slash_command_shows_server_detail_and_actions(tmp_path) -> None:
    config_path = tmp_path / "home" / "mcp.json"
    context = ToolUseContext(
        tools=["mcp__mysql_query__mysql_query"],
        model="test-model",
        permission_mode="default",
        app_state={
            "mcp_servers": {
                "mysql_query": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "@benborla29/mcp-server-mysql"],
                    "_scope": "user",
                    "_config_path": str(config_path),
                }
            },
            "mcp_statuses": {
                "mysql_query": {
                    "status": "connected",
                    "tools": 1,
                    "capabilities": ["tools", "resources"],
                }
            },
        },
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )
    processor = UserInputProcessor(AttachmentManager())

    processed = asyncio.run(
        processor.process(
            QueuedCommand(value="/mcp mysql_query", mode="prompt"),
            context,
            [],
        )
    )

    content = str(processed.messages[0].payload["content"])
    assert "Mysql Query MCP Server" in content
    assert "Status: connected" in content
    assert "Command: npx" in content
    assert "Args: -y @benborla29/mcp-server-mysql" in content
    assert f"Config location: {config_path}" in content
    assert "Capabilities: tools · resources" in content
    assert "Tools: 1 tool" in content
    assert "/mcp mysql_query tools" in content
    assert "/mcp mysql_query reconnect" in content
    assert "/mcp mysql_query disable" in content


def test_mcp_slash_command_shows_server_tools(tmp_path) -> None:
    context = ToolUseContext(
        tools=["mcp__mysql_query__mysql_query"],
        model="test-model",
        permission_mode="default",
        app_state={
            "mcp_servers": {
                "mysql_query": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["@benborla29/mcp-server-mysql"],
                    "_scope": "user",
                    "_config_path": str(tmp_path / "home" / "mcp.json"),
                }
            },
            "mcp_statuses": {
                "mysql_query": {
                    "status": "connected",
                    "tools": [
                        {
                            "name": "mysql_query",
                            "wrapped_name": "mcp__mysql_query__mysql_query",
                            "description": "Run a MySQL query.",
                            "input_schema": {"required": ["sql"]},
                        }
                    ],
                }
            },
        },
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )
    processor = UserInputProcessor(AttachmentManager())

    processed = asyncio.run(
        processor.process(
            QueuedCommand(value="/mcp mysql_query tools", mode="prompt"),
            context,
            [],
        )
    )

    content = str(processed.messages[0].payload["content"])
    assert "Tools for mysql_query" in content
    assert "mcp__mysql_query__mysql_query" in content
    assert "Original name: mysql_query" in content
    assert "Required: sql" in content


def test_load_mcp_server_entries_preserves_scope_and_config_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MORTY_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"

    add_mcp_server(
        name="mysql_query",
        scope="user",
        command="npx",
        args=["@benborla29/mcp-server-mysql"],
        env={},
        workspace_root=workspace,
    )

    entries = load_mcp_server_entries(workspace)

    assert entries["mysql_query"]["_scope"] == "user"
    assert entries["mysql_query"]["_config_path"] == str(tmp_path / "home" / "mcp.json")


def test_mcp_registry_skips_failed_server_and_records_status(tmp_path) -> None:
    statuses: dict[str, dict[str, object]] = {}
    configs = {
        "demo": {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(tmp_path / "missing_server.py")],
            "env": {},
        }
    }

    registry = asyncio.run(create_mcp_tool_registry(configs, statuses=statuses))

    assert registry.list_names() == []
    assert statuses["demo"]["status"] == "failed"
    assert "exited before responding to initialize" in str(statuses["demo"]["error"])


def test_mcp_registry_times_out_unresponsive_server(tmp_path) -> None:
    server = tmp_path / "silent_mcp_server.py"
    server.write_text(
        "import time\nwhile True:\n    time.sleep(1)\n",
        encoding="utf-8",
    )
    statuses: dict[str, dict[str, object]] = {}
    configs = {
        "silent": {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(server)],
            "env": {},
        }
    }

    started = time.monotonic()
    registry = asyncio.run(
        create_mcp_tool_registry(configs, statuses=statuses, startup_timeout=0.2)
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert registry.list_names() == []
    assert statuses["silent"]["status"] == "failed"
    assert "timed out" in str(statuses["silent"]["error"])


def test_mcp_registry_starts_servers_concurrently(tmp_path) -> None:
    server = tmp_path / "silent_mcp_server.py"
    server.write_text(
        "import time\nwhile True:\n    time.sleep(1)\n",
        encoding="utf-8",
    )
    configs = {
        "silent_a": {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(server)],
            "env": {},
        },
        "silent_b": {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(server)],
            "env": {},
        },
    }

    started = time.monotonic()
    asyncio.run(create_mcp_tool_registry(configs, startup_timeout=0.3))
    elapsed = time.monotonic() - started

    assert elapsed < 0.9


def test_mcp_stdio_tool_is_registered_and_callable(tmp_path) -> None:
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1.0.0"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo text from MCP.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            ]
        }
    elif method == "tools/call":
        params = request.get("params") or {}
        result = {"content": [{"type": "text", "text": "mcp:" + params["arguments"]["text"]}]}
    else:
        result = {}
    if "id" in request:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\\n")
        sys.stdout.flush()
""".lstrip(),
        encoding="utf-8",
    )
    configs = {
        "demo": {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(server)],
            "env": {},
        }
    }

    async def exercise_mcp_tool() -> dict[str, object]:
        registry = await create_mcp_tool_registry(configs)
        tool = registry.find("mcp__demo__echo")

        assert tool is not None
        assert tool.description == "Echo text from MCP."
        assert tool.input_schema["required"] == ["text"]

        context = ToolUseContext(
            tools=["mcp__demo__echo"],
            model="test-model",
            permission_mode="default",
            app_state={},
            read_file_state={},
            content_replacement_state=ContentReplacementState(),
        )
        message = Message(
            uuid="assistant-1",
            timestamp="2026-05-20T00:00:00+00:00",
            type="assistant",
            payload={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "mcp__demo__echo",
                        "input": {"text": "hello"},
                    }
                ]
            },
        )
        cache = CacheSafeParams(system_prompt=[], user_context={}, system_context={}, messages=[])

        result = await ToolRunner(registry).run(message, context, cache)
        return result[0].payload["content"][0]

    tool_result = asyncio.run(exercise_mcp_tool())
    assert tool_result["is_error"] is False
    assert tool_result["content"] == {"content": [{"type": "text", "text": "mcp:hello"}]}


def test_mcp_tool_can_be_called_from_later_event_loop(tmp_path) -> None:
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}
    elif method == "tools/call":
        params = request.get("params") or {}
        result = {"content": [{"type": "text", "text": "later:" + params["arguments"]["text"]}]}
    else:
        result = {}
    if "id" in request:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\\n")
        sys.stdout.flush()
""".lstrip(),
        encoding="utf-8",
    )
    configs = {
        "demo": {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(server)],
            "env": {},
        }
    }

    registry = asyncio.run(create_mcp_tool_registry(configs))
    tool = registry.find("mcp__demo__echo")
    assert tool is not None

    result = asyncio.run(tool.handler({"text": "hello"}))

    assert result == {"content": [{"type": "text", "text": "later:hello"}]}


def test_mcp_background_loader_injects_tools_without_replacing_registry(tmp_path) -> None:
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo.", "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    if "id" in request:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\\n")
        sys.stdout.flush()
""".lstrip(),
        encoding="utf-8",
    )
    configs = {
        "demo": {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(server)],
            "env": {},
        }
    }
    registry = ToolRegistry()
    context = ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="default",
        app_state={"tool_schemas": [], "mcp_statuses": {"demo": {"status": "pending"}}},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )
    statuses = {"demo": {"status": "pending"}}

    thread = _start_mcp_background_loader(
        mcp_configs=configs,
        workspace_root=tmp_path,
        registry=registry,
        tool_context=context,
        statuses=statuses,
    )
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert registry.find("mcp__demo__echo") is not None
    assert "mcp__demo__echo" in context.tools
    assert context.app_state["mcp_statuses"]["demo"]["status"] == "connected"
    assert any(
        schema["function"]["name"] == "mcp__demo__echo"
        for schema in context.app_state["tool_schemas"]
    )


def test_mcp_registry_shutdown_does_not_emit_closed_loop_transport_error(tmp_path) -> None:
    server = tmp_path / "persistent_mcp_server.py"
    server.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo.", "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    if "id" in request:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\\n")
        sys.stdout.flush()
""".lstrip(),
        encoding="utf-8",
    )
    script = tmp_path / "exercise_mcp_shutdown.py"
    script.write_text(
        f"""
import asyncio
import gc
import sys

from morty_code.mcp.manager import create_mcp_tool_registry

async def main():
    await create_mcp_tool_registry({{
        "demo": {{
            "type": "stdio",
            "command": sys.executable,
            "args": [{str(server)!r}],
            "env": {{}},
        }}
    }})

asyncio.run(main())
gc.collect()
""".lstrip(),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert "Event loop is closed" not in completed.stderr
    assert "BaseSubprocessTransport.__del__" not in completed.stderr
