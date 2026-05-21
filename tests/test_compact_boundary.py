from __future__ import annotations

import asyncio

from morty_code.compact.auto_compact import AutoCompactDecider
from morty_code.compact.compact_agent import CompactAgent
from morty_code.runtime.query_engine import QueryEngine
from morty_code.transcript.conversation_recovery import ConversationRecovery
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ContentReplacementState, ToolUseContext


class RecordingTranscriptStore:
    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.events: list[dict[str, object]] = []

    async def append_messages(self, messages: list[Message]) -> None:
        self.messages.extend(messages)

    async def append_event(self, event: dict[str, object]) -> None:
        self.events.append(event)


def message(message_type: str, text: str, uuid: str) -> Message:
    return Message(
        uuid=uuid,
        timestamp="2026-05-19T00:00:00",
        type=message_type,  # type: ignore[arg-type]
        payload={"content": [{"type": "text", "text": text}]},
    )


def compact_boundary(uuid: str, summary: str = "summary") -> Message:
    return Message(
        uuid=uuid,
        timestamp="2026-05-19T00:00:00",
        type="system",
        payload={
            "subtype": "compact_boundary",
            "content": "Conversation compacted",
            "summary": summary,
        },
    )


def test_recovery_starts_from_latest_compact_boundary() -> None:
    """验证恢复会从最后一个 compact boundary 开始，避免旧历史回流。"""

    messages = [
        message("user", "old request", "old-user"),
        compact_boundary("boundary-1", "old summary"),
        message("user", "after first compact", "after-first"),
        compact_boundary("boundary-2", "latest summary"),
        message("assistant", "latest retained tail", "tail"),
    ]

    recovered = ConversationRecovery().recover(messages)

    assert [item.uuid for item in recovered] == ["boundary-2", "tail"]


def test_auto_compact_writes_retained_tail_after_boundary_to_transcript() -> None:
    """验证 compact 后 transcript 里有 boundary、summary、retained tail 的完整切点。"""

    transcript = RecordingTranscriptStore()
    engine = QueryEngine(
        prompt_builder=None,
        input_dispatcher=None,
        input_processor=None,
        query_loop=None,
        transcript_store=transcript,
        auto_compact_decider=AutoCompactDecider(token_threshold=1),
        compact_agent=CompactAgent(max_summary_chars=1000),
    )
    old = message("user", "old context " * 20, "old")
    retained = message("assistant", "recent answer", "retained")
    engine.messages = [old, retained]
    context = ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="default",
        app_state={},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )

    visible_messages = asyncio.run(engine._maybe_compact(context, force=True, trigger="manual"))

    assert [item.uuid for item in visible_messages] == [
        engine.messages[0].uuid,
        engine.messages[1].uuid,
    ]
    assert transcript.messages[0].payload["subtype"] == "compact_boundary"
    assert transcript.messages[1].payload["is_compact_summary"] is True
    retained_copy = transcript.messages[-1]
    assert retained_copy.uuid != "retained"
    assert retained_copy.payload == retained.payload
    assert retained_copy.origin == {
        "source": "post_compact_retained",
        "original_uuid": "retained",
    }
    assert transcript.events[-1]["retained_transcript_count"] == 2
