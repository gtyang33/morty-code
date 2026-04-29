from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


class ForkedAgentRunner:
    """共享 cache-safe 前缀，但 clone mutable state。"""

    def __init__(self, query_loop, transcript_store=None) -> None:
        self.query_loop = query_loop
        self.transcript_store = transcript_store

    def clone_mutable_state(self, context: ToolUseContext) -> ToolUseContext:
        return deepcopy(context)

    async def run(
        self,
        cache_safe: CacheSafeParams,
        prompt_messages: list[Message],
        tool_context: ToolUseContext,
        max_turns: int = 4,
    ) -> list[Message]:
        if self.query_loop is None:
            return []
        isolated_context = self.clone_mutable_state(tool_context)
        combined_messages = [*cache_safe.messages, *prompt_messages]
        if self.transcript_store is not None and prompt_messages:
            await self.transcript_store.append_messages(prompt_messages, is_sidechain=True)
            await self.transcript_store.append_event(
                {
                    "type": "forked_agent_start",
                    "timestamp": datetime.utcnow().isoformat(),
                    "prompt_count": len(prompt_messages),
                    "model": isolated_context.model,
                }
            )
        result = await self.query_loop.run(
            messages=combined_messages,
            cache_safe=cache_safe,
            tool_context=isolated_context,
        )
        bounded = result.new_messages[:max_turns]
        if self.transcript_store is not None and bounded:
            await self.transcript_store.append_messages(bounded, is_sidechain=True)
            await self.transcript_store.append_event(
                {
                    "type": "forked_agent_finish",
                    "timestamp": datetime.utcnow().isoformat(),
                    "message_count": len(bounded),
                }
            )
        return bounded

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
