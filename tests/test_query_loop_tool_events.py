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


class ToolThenFinalModel:
    def __init__(self) -> None:
        self.calls = 0

    async def respond(
        self,
        messages,
        system_prompt,
        user_context,
        system_context,
    ) -> Message:
        self.calls += 1
        if self.calls == 1:
            return Message(
                uuid="assistant-tool",
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
        return Message(
            uuid="assistant-final",
            timestamp="2026-05-03T00:00:01",
            type="assistant",
            payload={"content": [{"type": "text", "text": "final summary"}]},
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


def test_query_loop_finalizes_when_tool_iteration_limit_is_reached() -> None:
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
    model = ToolThenFinalModel()
    loop = QueryLoop(model, ToolRunner(registry), max_iterations=1)

    result = asyncio.run(loop.run([], cache, context))

    assert model.calls == 2
    assert result.new_messages[-1].payload["content"][0]["text"] == "final summary"
    assert any(
        event.get("type") == "tool-iteration-limit-finalize"
        for event in result.metadata_events
    )


def test_query_loop_emits_live_messages_during_tool_iterations() -> None:
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
    model = ToolThenFinalModel()
    loop = QueryLoop(model, ToolRunner(registry), max_iterations=1)
    emitted: list[list[str]] = []

    asyncio.run(
        loop.run(
            [],
            cache,
            context,
            on_new_messages=lambda messages: emitted.append([message.uuid for message in messages]),
        )
    )

    assert len(emitted) == 3
    assert emitted[0] == ["assistant-tool"]
    assert len(emitted[1]) == 1
    assert emitted[2][-1] == "assistant-final"
