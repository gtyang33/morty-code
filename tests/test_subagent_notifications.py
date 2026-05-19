from __future__ import annotations

import asyncio
from pathlib import Path

from morty_code.agents.task_notifications import (
    drain_task_notifications,
    enqueue_task_notification,
)
from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.compact.auto_compact import AutoCompactDecider
from morty_code.input.handle_input import InputDispatcher
from morty_code.input.process_user_input import UserInputProcessor
from morty_code.runtime.query_engine import QueryEngine
from morty_code.runtime.query_loop import QueryLoopResult
from morty_code.transcript.transcript_store import TranscriptStore
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ContentReplacementState, QueuedCommand, ToolUseContext


class StubPromptBuilder:
    async def build_for_context(self, context: ToolUseContext) -> tuple[list[str], dict[str, str], dict[str, str]]:
        return ["system"], {}, {}


class RecordingQueryLoop:
    def __init__(self) -> None:
        self.seen_messages: list[list[Message]] = []

    async def run(
        self,
        *,
        messages,
        cache_safe: CacheSafeParams,
        tool_context: ToolUseContext,
        on_new_messages=None,
    ) -> QueryLoopResult:
        self.seen_messages.append(list(messages))
        return QueryLoopResult(
            new_messages=[
                Message(
                    uuid="assistant-1",
                    timestamp="2026-05-19T00:00:00",
                    type="assistant",
                    payload={"content": [{"type": "text", "text": "handled notification"}]},
                )
            ],
            metadata_events=[],
        )


def make_context(tmp_path: Path) -> ToolUseContext:
    return ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="default",
        app_state={},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


def test_enqueue_task_notification_uses_claude_style_xml(tmp_path: Path) -> None:
    app_state: dict[str, object] = {}
    output_file = tmp_path / "task.txt"

    enqueue_task_notification(
        app_state,
        task_id="task-1",
        output_file=str(output_file),
        description="Review code",
        status="completed",
        final_message="Looks good.",
        tool_use_id="tool-1",
    )

    commands = drain_task_notifications(app_state)

    assert len(commands) == 1
    assert commands[0].mode == "task-notification"
    assert commands[0].is_meta is True
    assert commands[0].skip_slash_commands is True
    assert commands[0].origin == {"source": "task_notification", "task_id": "task-1"}
    assert commands[0].value == (
        "<task-notification>\n"
        "<task_id>task-1</task_id>\n"
        "<tool_use_id>tool-1</tool_use_id>\n"
        f"<output_file>{output_file}</output_file>\n"
        "<status>completed</status>\n"
        "<summary>Agent &quot;Review code&quot; completed</summary>\n"
        "<result>Looks good.</result>\n"
        "</task-notification>"
    )


def test_task_notification_xml_escapes_dynamic_content(tmp_path: Path) -> None:
    app_state: dict[str, object] = {}

    enqueue_task_notification(
        app_state,
        task_id="task<&>",
        output_file=str(tmp_path / "a&b.txt"),
        description='A "quoted" <task>',
        status="failed",
        error="bad <failure> & worse",
    )

    [command] = drain_task_notifications(app_state)
    value = str(command.value)

    assert "task&lt;&amp;&gt;" in value
    assert "a&amp;b.txt" in value
    assert "A &quot;quoted&quot; &lt;task&gt;" in value
    assert "bad &lt;failure&gt; &amp; worse" in value


def test_drain_task_notifications_preserves_existing_commands() -> None:
    app_state: dict[str, object] = {
        "task_notification_queue": [
            QueuedCommand(value="first", mode="task-notification"),
            {"value": "second", "mode": "task-notification"},
        ]
    }

    commands = drain_task_notifications(app_state)

    assert [command.value for command in commands] == ["first", "second"]
    assert app_state["task_notification_queue"] == []


def test_query_engine_can_submit_pending_task_notifications(tmp_path: Path) -> None:
    query_loop = RecordingQueryLoop()
    engine = QueryEngine(
        prompt_builder=StubPromptBuilder(),
        input_dispatcher=InputDispatcher(),
        input_processor=UserInputProcessor(AttachmentManager()),
        query_loop=query_loop,
        transcript_store=TranscriptStore(tmp_path / "session.jsonl", "session"),
        auto_compact_decider=AutoCompactDecider(token_threshold=999999),
    )
    context = make_context(tmp_path)
    enqueue_task_notification(
        context.app_state,
        task_id="task-1",
        output_file=str(tmp_path / "task.txt"),
        description="Review code",
        status="completed",
        final_message="done",
    )

    messages = asyncio.run(engine.submit_pending_notifications(context))

    assert messages[-1].payload["content"][0]["text"] == "handled notification"
    assert len(query_loop.seen_messages) == 1
    notification_message = query_loop.seen_messages[0][0]
    assert notification_message.type == "user"
    assert notification_message.payload["mode"] == "task-notification"
    assert "<task-notification>" in str(notification_message.payload["content"])
    assert context.app_state["task_notification_queue"] == []


def test_query_engine_pending_notifications_noops_when_queue_empty(tmp_path: Path) -> None:
    query_loop = RecordingQueryLoop()
    engine = QueryEngine(
        prompt_builder=StubPromptBuilder(),
        input_dispatcher=InputDispatcher(),
        input_processor=UserInputProcessor(AttachmentManager()),
        query_loop=query_loop,
        transcript_store=TranscriptStore(tmp_path / "session.jsonl", "session"),
        auto_compact_decider=AutoCompactDecider(token_threshold=999999),
    )

    messages = asyncio.run(engine.submit_pending_notifications(make_context(tmp_path)))

    assert messages == []
    assert query_loop.seen_messages == []
