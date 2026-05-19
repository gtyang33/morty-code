from __future__ import annotations

import asyncio
import json

from morty_code.api.errors import ModelProviderError
from morty_code.agents.task_notifications import enqueue_task_notification
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


class FailingModel:
    async def respond(
        self,
        messages,
        system_prompt,
        user_context,
        system_context,
    ) -> Message:
        raise ModelProviderError("request timed out", detail="request timed out after 120s")


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


def test_query_loop_injects_pending_task_notifications_after_iteration(tmp_path) -> None:
    registry = ToolRegistry([])
    context = ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="default",
        app_state={},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )
    enqueue_task_notification(
        context.app_state,
        task_id="task-1",
        output_file=str(tmp_path / "task.txt"),
        description="Review code",
        status="completed",
        final_message="done",
    )
    cache = CacheSafeParams(system_prompt=[], user_context={}, system_context={}, messages=[])
    loop = QueryLoop(ToolThenFinalModel(), ToolRunner(registry), max_iterations=1)

    result = asyncio.run(loop.run([], cache, context))

    attachment = result.new_messages[-1]
    assert attachment.payload["attachment_type"] == "queued_command"
    assert attachment.payload["mode"] == "task-notification"
    assert "<task-notification>" in str(attachment.payload["prompt"])
    assert "<result>done</result>" in str(attachment.payload["prompt"])
    assert context.app_state["task_notification_queue"] == []


def test_query_loop_dumps_prompt_when_model_provider_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MORTY_DUMP_PROMPT_ON_ERROR", "1")
    monkeypatch.setenv("MORTY_PROMPT_DUMP_DIR", str(tmp_path / "prompt-dumps"))
    registry = ToolRegistry([])
    context = ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="default",
        app_state={},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )
    cache = CacheSafeParams(
        system_prompt=["system rules"],
        user_context={"session_memory": "remembered context"},
        system_context={"cwd": "/repo", "prompt_cache_plan_json": "internal"},
        messages=[],
    )
    loop = QueryLoop(FailingModel(), ToolRunner(registry), max_api_retries=0)

    result = asyncio.run(
        loop.run(
            [
                Message(
                    uuid="user-1",
                    timestamp="2026-05-15T00:00:00",
                    type="user",
                    payload={"content": [{"type": "text", "text": "write docs"}]},
                )
            ],
            cache,
            context,
        )
    )

    dump_events = [
        event for event in result.metadata_events if event.get("type") == "prompt-dump"
    ]
    assert len(dump_events) == 1
    dump_path = tmp_path / "prompt-dumps" / str(dump_events[0]["filename"])
    payload = json.loads(dump_path.read_text(encoding="utf-8"))
    assert payload["error"]["detail"] == "request timed out after 120s"
    assert payload["request"]["system_prompt"] == ["system rules"]
    assert payload["request"]["user_context"] == {"session_memory": "remembered context"}
    assert payload["request"]["system_context"]["cwd"] == "/repo"
    assert "prompt_cache_plan_json" not in payload["request"]["system_context"]
    assert payload["request"]["messages"][0]["role"] == "user"
    error_text = result.new_messages[0].payload["content"][0]["text"]
    assert str(dump_path) in error_text
