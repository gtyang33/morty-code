from __future__ import annotations

from copy import deepcopy

from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


class ForkedAgentRunner:
    """共享 cache-safe 前缀，但 clone mutable state。"""

    def __init__(self, query_loop) -> None:
        self.query_loop = query_loop

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
        result = await self.query_loop.run(
            messages=combined_messages,
            cache_safe=cache_safe,
            tool_context=isolated_context,
        )
        return result.new_messages[: max_turns + 1]
