from __future__ import annotations

import asyncio
from pathlib import Path

from morty_code.agents.send_message_tool import register_send_message_tool
from morty_code.agents.task_registry import get_subagent_task_registry
from morty_code.runtime.query_loop import QueryLoop, QueryLoopResult
from morty_code.tools.tool_registry import ToolRegistry
from morty_code.tools.tool_runner import ToolRunner
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ContentReplacementState, ToolUseContext


class FinalModel:
    async def respond(
        self,
        messages,
        system_prompt,
        user_context,
        system_context,
    ) -> Message:
        return Message(
            uuid="assistant-final",
            timestamp="2026-05-19T00:00:00",
            type="assistant",
            payload={"content": [{"type": "text", "text": "done"}]},
        )


def make_context(tmp_path: Path) -> ToolUseContext:
    return ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="default",
        app_state={"subagent_tasks_dir": str(tmp_path / "tasks")},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


def make_cache() -> CacheSafeParams:
    return CacheSafeParams(system_prompt=[], user_context={}, system_context={}, messages=[])


def test_send_message_queues_message_for_running_subagent_by_agent_id(tmp_path: Path) -> None:
    registry_root = tmp_path / "tasks"
    task_registry = get_subagent_task_registry(registry_root)
    task = task_registry.create(
        task_id="task-1",
        agent_id="agent-1",
        agent_type="general-purpose",
        description="Worker",
        prompt="initial",
    )
    registry = ToolRegistry([])
    register_send_message_tool(registry)
    tool = registry.find("send_message")
    assert tool is not None

    result = asyncio.run(
        tool.handler(
            {"to": task.agent_id, "message": "please check the failing test"},
            make_context(tmp_path),
            make_cache(),
        )
    )

    assert result["success"] is True
    assert task_registry.get("task-1").pending_messages == ["please check the failing test"]


def test_send_message_routes_registered_name_to_agent(tmp_path: Path) -> None:
    task_registry = get_subagent_task_registry(tmp_path / "tasks")
    task_registry.create(
        task_id="task-2",
        agent_id="agent-2",
        agent_type="general-purpose",
        description="Builder",
        prompt="initial",
    )
    context = make_context(tmp_path)
    context.app_state["agent_name_registry"] = {"builder": "agent-2"}
    registry = ToolRegistry([])
    register_send_message_tool(registry)
    tool = registry.find("send_message")
    assert tool is not None

    result = asyncio.run(tool.handler({"to": "builder", "message": "continue"}, context, make_cache()))

    assert result["success"] is True
    assert task_registry.get("task-2").pending_messages == ["continue"]


def test_send_message_rejects_stopped_subagent_until_resume_exists(tmp_path: Path) -> None:
    task_registry = get_subagent_task_registry(tmp_path / "tasks")
    task = task_registry.create(
        task_id="task-3",
        agent_id="agent-3",
        agent_type="general-purpose",
        description="Stopped",
        prompt="initial",
    )
    task.status = "completed"
    task_registry.update(task)
    registry = ToolRegistry([])
    register_send_message_tool(registry)
    tool = registry.find("send_message")
    assert tool is not None

    result = asyncio.run(tool.handler({"to": "agent-3", "message": "resume"}, make_context(tmp_path), make_cache()))

    assert result["success"] is False
    assert "not supported yet" in str(result["message"])


def test_query_loop_injects_pending_send_message_for_current_subagent(tmp_path: Path) -> None:
    task_registry = get_subagent_task_registry(tmp_path / "tasks")
    task_registry.create(
        task_id="task-4",
        agent_id="agent-4",
        agent_type="general-purpose",
        description="Worker",
        prompt="initial",
    )
    task_registry.queue_pending_message("task-4", "new instruction")
    context = make_context(tmp_path)
    context.app_state["subagent_task_id"] = "task-4"
    loop = QueryLoop(FinalModel(), ToolRunner(ToolRegistry([])), max_iterations=1)

    result = asyncio.run(loop.run([], make_cache(), context))

    attachment = result.new_messages[-1]
    assert attachment.payload["attachment_type"] == "queued_command"
    assert attachment.payload["mode"] == "prompt"
    assert attachment.payload["prompt"] == "new instruction"
    assert task_registry.get("task-4").pending_messages == []
