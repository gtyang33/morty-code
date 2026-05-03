from __future__ import annotations

import asyncio

from morty_code.runtime.query_loop import QueryLoop
from morty_code.tools.tool_registry import ToolRegistry, ToolSpec
from morty_code.tools.tool_runner import ToolRunner
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ContentReplacementState, ToolUseContext


class ToolCallingModel:
    async def respond(
        self,
        messages,
        system_prompt,
        user_context,
        system_context,
    ) -> Message:
        return Message(
            uuid="assistant-1",
            timestamp="2026-05-03T00:00:00",
            type="assistant",
            payload={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "demo",
                        "input": {"value": "ok"},
                    }
                ]
            },
        )


async def demo_handler(args: dict[str, object]) -> dict[str, object]:
    return {"value": args["value"]}


def test_query_loop_drains_tool_execution_events_into_metadata() -> None:
    registry = ToolRegistry([ToolSpec(name="demo", description="demo", handler=demo_handler)])
    context = ToolUseContext(
        tools=["demo"],
        model="test-model",
        permission_mode="default",
        app_state={},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )
    cache = CacheSafeParams(system_prompt=[], user_context={}, system_context={}, messages=[])
    loop = QueryLoop(ToolCallingModel(), ToolRunner(registry), max_iterations=1)

    result = asyncio.run(loop.run([], cache, context))

    events = [event for event in result.metadata_events if event.get("type") == "tool_execution"]
    assert [event["phase"] for event in events] == ["permission", "start", "success"]
    assert "tool_execution_events" not in context.app_state
