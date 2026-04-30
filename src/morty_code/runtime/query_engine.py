from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import uuid4

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
        await self.transcript_store.append_event(
            {
                "type": "turn_start",
                "raw_input_chars": len(raw_input),
                "queued_count": len(queued_commands),
                "message_count_before": len(self.messages),
            }
        )

        new_messages: list[Message] = []
        should_query = False
        should_compact = False
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
            should_compact = should_compact or processed.trigger_compact
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
        if should_compact:
            compact_messages = await self._maybe_compact(tool_context, force=True, trigger="manual")
            if compact_messages:
                await self.transcript_store.append_event(
                    {
                        "type": "turn_finish",
                        "queried_model": False,
                        "input_message_count": len(new_messages),
                        "output_message_count": len(compact_messages),
                        "message_count_after": len(self.messages),
                    }
                )
                return [*new_messages, *compact_messages]
        if not should_query:
            await self.transcript_store.append_event(
                {
                    "type": "turn_finish",
                    "queried_model": False,
                    "input_message_count": len(new_messages),
                    "output_message_count": 0,
                    "message_count_after": len(self.messages),
                }
            )
            return new_messages

        await self._maybe_compact(tool_context)
        messages_for_query = self._messages_after_compact_boundary()
        system_prompt, user_context, system_context = await self.prompt_builder.build_for_context(
            tool_context
        )
        try:
            result = await self.query_loop.run(
                messages=messages_for_query,
                cache_safe=CacheSafeParams(
                    system_prompt=system_prompt,
                    user_context=user_context,
                    system_context=system_context,
                    messages=list(messages_for_query),
                ),
                tool_context=tool_context,
            )
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，保证 transcript 不因异常断链。
            error_message = self._assistant_error_message(str(exc))
            self.messages.append(error_message)
            await self.transcript_store.append_event(
                {
                    "type": "turn_failed",
                    "error": str(exc),
                    "message_count_before_failure": len(self.messages),
                }
            )
            await self.transcript_store.append_messages([error_message])
            return [error_message]
        self.messages.extend(result.new_messages)
        for event in result.metadata_events:
            await self.transcript_store.append_event(event)
        if result.new_messages:
            await self.transcript_store.append_messages(result.new_messages)
            self._write_memories(tool_context, result.new_messages)
        await self.transcript_store.append_event(
            {
                "type": "turn_finish",
                "queried_model": True,
                "input_message_count": len(new_messages),
                "output_message_count": len(result.new_messages),
                "message_count_after": len(self.messages),
            }
        )
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
        self.session_restore.restore_content_replacement_events(
            loaded.metadata_events,
            restored["tool_context"].content_replacement_state,
        )
        self.messages = recovered_messages
        self.transcript_store._last_parent_uuid = loaded.last_parent_uuid
        return restored

    async def _maybe_compact(
        self,
        tool_context: ToolUseContext,
        force: bool = False,
        trigger: str = "auto",
    ) -> list[Message]:
        approximate_tokens = sum(len(str(message.payload)) for message in self.messages)
        if not force and not self.auto_compact_decider.should_compact(approximate_tokens):
            return []
        try:
            summary_messages, messages_to_keep = await self.compact_agent.compact_messages(
                self.messages,
                trigger=trigger,
            )
            reinjected = build_reinjection_attachments(tool_context)
            rebuilt = rebuild_post_compact_messages(summary_messages, messages_to_keep, reinjected)
            self.messages = rebuilt
            compact_messages = [*summary_messages, *reinjected]
            await self.transcript_store.append_messages(compact_messages)
            await self.transcript_store.append_event(
                {
                    "type": "compact",
                    "trigger": trigger,
                    "approximate_tokens": approximate_tokens,
                    "summary_count": len(summary_messages),
                    "messages_to_keep_count": len(messages_to_keep),
                    "reinjected_count": len(reinjected),
                }
            )
            self.auto_compact_decider.record_success()
            return compact_messages
        except Exception as exc:  # noqa: BLE001 - compact 失败不能中断主对话。
            self.auto_compact_decider.record_failure()
            await self.transcript_store.append_event(
                {
                    "type": "compact_failed",
                    "approximate_tokens": approximate_tokens,
                    "error": str(exc),
                }
            )
            return []

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

    def _messages_after_compact_boundary(self) -> list[Message]:
        for index in range(len(self.messages) - 1, -1, -1):
            message = self.messages[index]
            if message.type == "system" and message.payload.get("subtype") == "compact_boundary":
                return self.messages[index:]
        return self.messages

    def _assistant_error_message(self, content: str) -> Message:
        return Message(
            uuid=str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="assistant",
            payload={
                "content": [{"type": "text", "text": f"Runtime error: {content}"}],
                "is_api_error": True,
            },
            is_meta=True,
        )
