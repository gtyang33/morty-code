from __future__ import annotations

import asyncio

from morty_code.tools.schema_validation import ToolInputValidationError, validate_tool_input
from morty_code.tools.tool_registry import ToolRegistry, ToolSpec
from morty_code.tools.tool_runner import ToolRunner
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ContentReplacementState, ToolUseContext


def make_cache() -> CacheSafeParams:
    return CacheSafeParams(system_prompt=[], user_context={}, system_context={}, messages=[])


def make_message(input_payload: dict[str, object]) -> Message:
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
                    "input": input_payload,
                }
            ]
        },
    )


def make_context() -> ToolUseContext:
    return ToolUseContext(
        tools=["demo"],
        model="test-model",
        permission_mode="default",
        app_state={},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


DEMO_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "count": {"type": "integer"},
        "mode": {"type": "string", "enum": ["fast", "slow"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    "required": ["name", "count", "mode"],
}


def test_validate_tool_input_reports_required_type_enum_and_nested_errors() -> None:
    try:
        validate_tool_input(
            "demo",
            DEMO_SCHEMA,
            {
                "count": "3",
                "mode": "medium",
                "items": [{}],
            },
        )
    except ToolInputValidationError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected validation error")

    assert "$.name: is required" in message
    assert "$.count: expected integer" in message
    assert "$.mode: expected one of ['fast', 'slow']" in message
    assert "$.items[0].id: is required" in message


def test_tool_runner_returns_validation_error_before_permission_or_handler() -> None:
    async def handler(args: dict[str, object]) -> dict[str, object]:
        raise AssertionError("invalid input should not execute")

    registry = ToolRegistry(
        [ToolSpec(name="demo", description="demo", handler=handler, input_schema=DEMO_SCHEMA)]
    )
    context = make_context()

    result_messages = asyncio.run(
        ToolRunner(registry).run(make_message({"name": "ok", "count": "bad", "mode": "fast"}), context, make_cache())
    )

    tool_result = result_messages[0].payload["content"][0]
    assert tool_result["is_error"] is True
    assert "InputValidationError for demo" in tool_result["content"]
    assert [event["phase"] for event in context.app_state["tool_execution_events"]] == [
        "validation_error"
    ]


def test_tool_runner_executes_when_schema_is_valid() -> None:
    async def handler(args: dict[str, object]) -> dict[str, object]:
        return {"count": args["count"]}

    registry = ToolRegistry(
        [ToolSpec(name="demo", description="demo", handler=handler, input_schema=DEMO_SCHEMA)]
    )
    context = make_context()

    result_messages = asyncio.run(
        ToolRunner(registry).run(
            make_message({"name": "ok", "count": 3, "mode": "fast", "items": [{"id": "a"}]}),
            context,
            make_cache(),
        )
    )

    tool_result = result_messages[0].payload["content"][0]
    assert tool_result["is_error"] is False
    assert tool_result["content"] == {"count": 3}


def test_tool_runner_recovers_json_raw_arguments_before_validation() -> None:
    async def handler(args: dict[str, object]) -> dict[str, object]:
        return {"name": args["name"], "count": args["count"]}

    registry = ToolRegistry(
        [ToolSpec(name="demo", description="demo", handler=handler, input_schema=DEMO_SCHEMA)]
    )
    context = make_context()

    result_messages = asyncio.run(
        ToolRunner(registry).run(
            make_message(
                {
                    "raw_arguments": (
                        '{"name": "ok", "count": 3, "mode": "fast", '
                        '"items": [{"id": "a"}]}'
                    )
                }
            ),
            context,
            make_cache(),
        )
    )

    tool_result = result_messages[0].payload["content"][0]
    assert tool_result["is_error"] is False
    assert tool_result["content"] == {"name": "ok", "count": 3}
