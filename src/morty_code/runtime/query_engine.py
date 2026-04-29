from __future__ import annotations

import asyncio

from morty_code.compact.auto_compact import AutoCompactDecider
from morty_code.compact.compact_agent import CompactAgent
from morty_code.compact.compact_rebuild import (
    build_reinjection_attachments,
    rebuild_post_compact_messages,
)
from morty_code.memory.durable_memory import DurableMemoryStore
from morty_code.memory.memory_extractor import MemoryExtractor
from morty_code.memory.session_memory import SessionMemoryStore
from morty_code.runtime.queue_manager import QueueManager
from morty_code.transcript.conversation_recovery import ConversationRecovery
from morty_code.transcript.session_restore import SessionRestore
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


class QueryEngine:
    """顶层入口，负责把输入层、prompt 层、query loop 串起来。"""

    def __init__(
        self,
        prompt_builder,
        input_dispatcher,
        input_processor,
        query_loop,
        transcript_store,
        auto_compact_decider: AutoCompactDecider | None = None,
        compact_agent: CompactAgent | None = None,
        memory_extractor: MemoryExtractor | None = None,
    ) -> None:
        self.prompt_builder = prompt_builder
        self.input_dispatcher = input_dispatcher
        self.input_processor = input_processor
        self.query_loop = query_loop
        self.transcript_store = transcript_store
        self.auto_compact_decider = auto_compact_decider or AutoCompactDecider(
            token_threshold=12000
        )
        self.compact_agent = compact_agent or CompactAgent()
        self.memory_extractor = memory_extractor or MemoryExtractor()
        self.messages: list[Message] = []
        self.queue_manager = QueueManager()
        self.recovery = ConversationRecovery()
        self.session_restore = SessionRestore()

    async def submit_message(
        self,
        raw_input: str,
        tool_context: ToolUseContext,
        pasted_contents: dict[int, dict[str, object]] | None = None,
    ) -> list[Message]:
        queued_commands = await self.input_dispatcher.submit(
            raw_input=raw_input,
            mode="prompt",
            pasted_contents=pasted_contents,
        )
        self.queue_manager.extend(queued_commands)
        queued_commands = self.queue_manager.drain()

        new_messages: list[Message] = []
        should_query = False
        index = 0
        while queued_commands:
            command = queued_commands.pop(0)
            processed = await self.input_processor.process(
                command=command,
                context=tool_context,
                messages=self.messages,
                skip_attachments=index > 0,
            )
            new_messages.extend(processed.messages)
            if index == 0:
                should_query = processed.should_query
            if processed.allowed_tools is not None:
                tool_context.tools = processed.allowed_tools
            if processed.model is not None:
                tool_context.model = processed.model
            if processed.next_input is not None:
                self.queue_manager.enqueue(
                    type(command)(
                        value=processed.next_input,
                        mode="prompt",
                        skip_slash_commands=True,
                        is_meta=True,
                        origin={"source": "processed_next_input"},
                    )
                )
                queued_commands.extend(self.queue_manager.drain())
            index += 1

        self.messages.extend(new_messages)
        if new_messages:
            await self.transcript_store.append_messages(new_messages)
        if not should_query:
            return new_messages

        await self._maybe_compact(tool_context)
        system_prompt, user_context, system_context = await self.prompt_builder.build_for_context(
            tool_context
        )
        result = await self.query_loop.run(
            messages=self.messages,
            cache_safe=CacheSafeParams(
                system_prompt=system_prompt,
                user_context=user_context,
                system_context=system_context,
                messages=list(self.messages),
            ),
            tool_context=tool_context,
        )
        self.messages.extend(result.new_messages)
        if result.new_messages:
            await self.transcript_store.append_messages(result.new_messages)
            self._write_memories(tool_context, result.new_messages)
        return result.new_messages

    def submit_message_sync(
        self,
        raw_input: str,
        tool_context: ToolUseContext,
        pasted_contents: dict[int, dict[str, object]] | None = None,
    ) -> list[Message]:
        return asyncio.run(self.submit_message(raw_input, tool_context, pasted_contents))

    async def restore_from_transcript(self, metadata: dict[str, object] | None = None) -> dict[str, object]:
        loaded = await self.transcript_store.load_session()
        recovered_messages = self.recovery.recover(loaded.messages)
        restored = self.session_restore.restore(
            recovered_messages,
            metadata or {"cwd": ".", "model": "echo-model"},
        )
        self.messages = recovered_messages
        self.transcript_store._last_parent_uuid = loaded.last_parent_uuid
        return restored

    async def _maybe_compact(self, tool_context: ToolUseContext) -> None:
        approximate_tokens = sum(len(str(message.payload)) for message in self.messages)
        if not self.auto_compact_decider.should_compact(approximate_tokens):
            return
        try:
            summary_messages, messages_to_keep = await self.compact_agent.compact_messages(
                self.messages
            )
            reinjected = build_reinjection_attachments(tool_context)
            rebuilt = rebuild_post_compact_messages(summary_messages, messages_to_keep, reinjected)
            self.messages = rebuilt
            await self.transcript_store.append_messages([*summary_messages, *reinjected])
            await self.transcript_store.append_event(
                {
                    "type": "compact",
                    "approximate_tokens": approximate_tokens,
                    "summary_count": len(summary_messages),
                    "reinjected_count": len(reinjected),
                }
            )
            self.auto_compact_decider.record_success()
        except Exception as exc:  # noqa: BLE001 - compact 失败不能中断主对话。
            self.auto_compact_decider.record_failure()
            await self.transcript_store.append_event(
                {
                    "type": "compact_failed",
                    "approximate_tokens": approximate_tokens,
                    "error": str(exc),
                }
            )

    def _write_memories(self, tool_context: ToolUseContext, new_messages: list[Message]) -> None:
        summaries = self.memory_extractor.extract(new_messages)
        if not summaries:
            return
        if tool_context.session_memory_path:
            session_store = SessionMemoryStore(tool_context.session_memory_path)
            for summary in summaries:
                session_store.append_note(summary)
        if tool_context.durable_memory_dir:
            durable_store = DurableMemoryStore(tool_context.durable_memory_dir)
            for summary in summaries:
                durable_store.append_summary(summary)
