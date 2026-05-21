from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from morty_code.agents.subagent_tool import register_subagent_tool
from morty_code.agents.task_notifications import drain_task_notifications
from morty_code.agents.task_registry import get_subagent_task_registry
from morty_code.runtime.query_loop import QueryLoopResult
from morty_code.tools.tool_registry import ToolRegistry
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ContentReplacementState, ToolUseContext


class FinalAnswerQueryLoop:
    async def run(
        self,
        *,
        messages,
        cache_safe,
        tool_context,
        max_iterations=None,
        on_new_messages=None,
    ) -> QueryLoopResult:
        return QueryLoopResult(
            new_messages=[
                Message(
                    uuid=str(uuid4()),
                    timestamp=datetime.utcnow().isoformat(),
                    type="assistant",
                    payload={"content": [{"type": "text", "text": "clean subagent answer"}]},
                )
            ],
            metadata_events=[],
        )


def make_context(tmp_path: Path) -> ToolUseContext:
    return ToolUseContext(
        tools=["read_file", "grep_text", "bash", "spawn_agent"],
        model="test-model",
        permission_mode="default",
        app_state={
            "session_id": "session-1",
            "subagent_transcripts_dir": str(tmp_path / "subagents"),
            "subagent_tasks_dir": str(tmp_path / "tasks"),
        },
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


def make_cache() -> CacheSafeParams:
    return CacheSafeParams(system_prompt=[], user_context={}, system_context={}, messages=[])


def test_spawn_agent_returns_clean_output_file_without_transcript_path(tmp_path: Path) -> None:
    registry = ToolRegistry([])
    register_subagent_tool(FinalAnswerQueryLoop(), registry)
    tool = registry.find("spawn_agent")
    assert tool is not None

    result = asyncio.run(
        tool.handler(
            {"prompt": "review this code", "subagent_type": "general-purpose"},
            make_context(tmp_path),
            make_cache(),
        )
    )

    output_file = Path(str(result["output_file"]))
    assert result["output"] == "clean subagent answer"
    assert output_file.read_text(encoding="utf-8") == "clean subagent answer"
    assert "transcript_path" not in result
    assert "metadata_events" not in result


def test_spawn_agent_prompt_tells_model_not_to_read_transcripts(tmp_path: Path) -> None:
    registry = ToolRegistry([])
    register_subagent_tool(FinalAnswerQueryLoop(), registry)
    tool = registry.find("spawn_agent")
    assert tool is not None

    schema = registry.api_tool_schemas({"spawn_agent"})[0]["function"]
    prompt = str(schema["description"])

    assert tool.description == "Delegate a bounded task to an isolated subagent and return its concise result."
    assert "Use the returned output or output_file" in prompt
    assert "Do not read .morty/subagents" in prompt
    assert "Do not parse transcript JSONL" in prompt
    assert "When run_in_background is true" in prompt
    assert "do not duplicate the same work" in prompt
    assert "do not poll task_output" in prompt


def test_background_spawn_enqueues_task_notification(tmp_path: Path) -> None:
    registry = ToolRegistry([])
    register_subagent_tool(FinalAnswerQueryLoop(), registry)
    tool = registry.find("spawn_agent")
    assert tool is not None
    context = make_context(tmp_path)

    result = asyncio.run(
        tool.handler(
            {
                "prompt": "review this code",
                "subagent_type": "general-purpose",
                "description": "Review code",
                "run_in_background": True,
            },
            context,
            make_cache(),
        )
    )

    task_registry = get_subagent_task_registry(str(tmp_path / "tasks"))
    deadline = time.time() + 3
    task = None
    while time.time() < deadline:
        task = task_registry.get(str(result["task_id"]))
        if task is not None and task.status == "completed":
            break
        time.sleep(0.02)

    assert task is not None
    assert task.status == "completed"
    [notification] = drain_task_notifications(context.app_state)
    assert notification.mode == "task-notification"
    assert "<status>completed</status>" in str(notification.value)
    assert "<result>clean subagent answer</result>" in str(notification.value)
    assert f"<output_file>{result['output_file']}</output_file>" in str(notification.value)
