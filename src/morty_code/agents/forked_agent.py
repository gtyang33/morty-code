from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from morty_code.types.messages import Message
from morty_code.types.runtime_state import (
    CacheSafeParams,
    ToolUseContext,
    clone_tool_use_context_for_fork,
)


@dataclass
class ForkedAgentResult:
    """forked agent 的完整结果，供后台任务记录 metadata 和隔离状态。"""

    messages: list[Message]
    metadata_events: list[dict[str, object]]
    isolated_context: ToolUseContext


class ForkedAgentRunner:
    """共享 cache-safe 前缀，但 clone mutable state。"""

    def __init__(self, query_loop, transcript_store=None) -> None:
        """初始化对象状态。"""
        self.query_loop = query_loop
        self.transcript_store = transcript_store

    def clone_mutable_state(
        self,
        context: ToolUseContext,
        *,
        fork_label: str = "forked_agent",
        skip_cache_write: bool = False,
    ) -> ToolUseContext:
        """克隆可变运行状态。"""
        return clone_tool_use_context_for_fork(
            context,
            fork_label=fork_label,
            skip_cache_write=skip_cache_write,
        )

    async def run(
        self,
        cache_safe: CacheSafeParams,
        prompt_messages: list[Message],
        tool_context: ToolUseContext,
        max_turns: int = 4,
        fork_label: str = "forked_agent",
        skip_transcript: bool = False,
        skip_cache_write: bool = True,
    ) -> list[Message]:
        """执行核心流程。"""
        result = await self.run_with_result(
            cache_safe=cache_safe,
            prompt_messages=prompt_messages,
            tool_context=tool_context,
            max_turns=max_turns,
            fork_label=fork_label,
            skip_transcript=skip_transcript,
            skip_cache_write=skip_cache_write,
        )
        return result.messages

    async def run_with_result(
        self,
        cache_safe: CacheSafeParams,
        prompt_messages: list[Message],
        tool_context: ToolUseContext,
        max_turns: int = 4,
        fork_label: str = "forked_agent",
        skip_transcript: bool = False,
        skip_cache_write: bool = True,
    ) -> ForkedAgentResult:
        """执行核心流程。"""
        if self.query_loop is None:
            return ForkedAgentResult(messages=[], metadata_events=[], isolated_context=tool_context)
        isolated_context = self.clone_mutable_state(
            tool_context,
            fork_label=fork_label,
            skip_cache_write=skip_cache_write,
        )
        combined_messages = [*cache_safe.messages, *prompt_messages]
        metadata_events: list[dict[str, object]] = [
            {
                "type": "forked_agent_context",
                "label": fork_label,
                "parent_message_count": len(cache_safe.messages),
                "prompt_count": len(prompt_messages),
                "max_turns": max_turns,
                "skip_cache_write": skip_cache_write,
                "read_file_state_count": len(isolated_context.read_file_state),
                "content_replacement_count": len(isolated_context.content_replacement_state.replacements),
            }
        ]
        if self.transcript_store is not None and prompt_messages and not skip_transcript:
            await self.transcript_store.append_messages(prompt_messages, is_sidechain=True)
            await self.transcript_store.append_event(
                {
                    "type": "forked_agent_start",
                    "timestamp": datetime.utcnow().isoformat(),
                    "label": fork_label,
                    "parent_message_count": len(cache_safe.messages),
                    "prompt_count": len(prompt_messages),
                    "model": isolated_context.model,
                    "skip_cache_write": skip_cache_write,
                }
            )
        try:
            result = await self.query_loop.run(
                messages=combined_messages,
                cache_safe=cache_safe,
                tool_context=isolated_context,
                max_iterations=max_turns,
            )
        except Exception as exc:  # noqa: BLE001 - fork 失败必须留 metadata，不能污染主线程。
            failure = {
                "type": "forked_agent_failed",
                "timestamp": datetime.utcnow().isoformat(),
                "label": fork_label,
                "error": str(exc),
            }
            metadata_events.append(failure)
            if self.transcript_store is not None and not skip_transcript:
                await self.transcript_store.append_event(failure)
            return ForkedAgentResult(
                messages=[],
                metadata_events=metadata_events,
                isolated_context=isolated_context,
            )

        metadata_events.extend(result.metadata_events)
        finish_event = {
            "type": "forked_agent_finish",
            "timestamp": datetime.utcnow().isoformat(),
            "label": fork_label,
            "message_count": len(result.new_messages),
            "metadata_count": len(result.metadata_events),
        }
        metadata_events.append(finish_event)
        if self.transcript_store is not None and not skip_transcript:
            if result.new_messages:
                await self.transcript_store.append_messages(result.new_messages, is_sidechain=True)
            for event in result.metadata_events:
                await self.transcript_store.append_event(event)
            await self.transcript_store.append_event(finish_event)
        return ForkedAgentResult(
            messages=result.new_messages,
            metadata_events=metadata_events,
            isolated_context=isolated_context,
        )

    def make_prompt_message(self, content: str) -> Message:
        """给后台 maintenance job 快速构造 sidechain prompt。"""

        return Message(
            uuid=str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="user",
            payload={"content": content, "mode": "forked_agent"},
            is_meta=True,
            origin={"source": "forked_agent"},
        )
