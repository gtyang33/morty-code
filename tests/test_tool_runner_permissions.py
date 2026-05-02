from __future__ import annotations

import asyncio

from morty_code.tools.tool_registry import ToolRegistry, ToolSpec
from morty_code.tools.tool_runner import ToolRunner
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ContentReplacementState, ToolUseContext


def make_context(app_state: dict[str, object]) -> ToolUseContext:
    return ToolUseContext(
        tools=["demo"],
        model="test-model",
        permission_mode="default",
        app_state=app_state,
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


def make_cache() -> CacheSafeParams:
    return CacheSafeParams(system_prompt=[], user_context={}, system_context={}, messages=[])


def make_tool_message(payload: dict[str, object]) -> Message:
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
                    "input": payload,
                }
            ]
        },
    )


def test_tool_runner_uses_external_permission_allow_with_updated_input() -> None:
    seen_payloads: list[dict[str, object]] = []

    async def handler(args: dict[str, object]) -> dict[str, object]:
        seen_payloads.append(args)
        return {"received": args["value"]}

    async def approve(request: dict[str, object]) -> dict[str, object]:
        assert request["tool_name"] == "demo"
        return {"behavior": "allow", "updatedInput": {"value": "changed"}}

    registry = ToolRegistry(
        [
            ToolSpec(
                name="demo",
                description="demo",
                handler=handler,
            )
        ]
    )
    context = make_context(
        {
            "always_ask_tools": ["demo"],
            "permission_request_handler": approve,
        }
    )

    result_messages = asyncio.run(
        ToolRunner(registry).run(make_tool_message({"value": "original"}), context, make_cache())
    )

    tool_result = result_messages[0].payload["content"][0]
    assert tool_result["is_error"] is False
    assert tool_result["content"] == {"received": "changed"}
    assert seen_payloads == [{"value": "changed"}]


def test_tool_runner_turns_external_permission_deny_into_tool_error() -> None:
    async def handler(args: dict[str, object]) -> dict[str, object]:
        raise AssertionError("denied tool should not execute")

    def deny(request: dict[str, object]) -> dict[str, object]:
        return {"behavior": "deny", "message": "host denied"}

    registry = ToolRegistry([ToolSpec(name="demo", description="demo", handler=handler)])
    context = make_context(
        {
            "always_ask_tools": ["demo"],
            "permission_request_handler": deny,
        }
    )

    result_messages = asyncio.run(
        ToolRunner(registry).run(make_tool_message({"value": "original"}), context, make_cache())
    )

    tool_result = result_messages[0].payload["content"][0]
    assert tool_result["is_error"] is True
    assert "host denied" in tool_result["content"]
