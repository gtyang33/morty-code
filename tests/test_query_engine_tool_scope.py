from __future__ import annotations

from pathlib import Path

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.compact.auto_compact import AutoCompactDecider
from morty_code.input.handle_input import InputDispatcher
from morty_code.input.process_user_input import UserInputProcessor
from morty_code.runtime.query_engine import QueryEngine
from morty_code.runtime.query_loop import QueryLoopResult
from morty_code.transcript.transcript_store import TranscriptStore
from morty_code.types.runtime_state import CacheSafeParams, ContentReplacementState, ToolUseContext


class StubPromptBuilder:
    async def build_for_context(self, context: ToolUseContext) -> tuple[list[str], dict[str, str], dict[str, str]]:
        return ["system"], {}, {}


class RecordingQueryLoop:
    def __init__(self) -> None:
        self.seen_tools: list[list[str]] = []

    async def run(
        self,
        *,
        messages,
        cache_safe: CacheSafeParams,
        tool_context: ToolUseContext,
        on_new_messages=None,
    ) -> QueryLoopResult:
        self.seen_tools.append(list(tool_context.tools))
        return QueryLoopResult(new_messages=[], metadata_events=[])


def test_slash_command_tool_scope_is_restored_after_query(tmp_path: Path) -> None:
    query_loop = RecordingQueryLoop()
    engine = QueryEngine(
        prompt_builder=StubPromptBuilder(),
        input_dispatcher=InputDispatcher(),
        input_processor=UserInputProcessor(AttachmentManager()),
        query_loop=query_loop,
        transcript_store=TranscriptStore(tmp_path / "session.jsonl", "session"),
        auto_compact_decider=AutoCompactDecider(token_threshold=999999),
    )
    context = ToolUseContext(
        tools=["read_file", "write_file", "bash"],
        model="test-model",
        permission_mode="default",
        app_state={},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )

    engine.submit_message_sync("/memory", context)

    assert query_loop.seen_tools == [[]]
    assert context.tools == ["read_file", "write_file", "bash"]
