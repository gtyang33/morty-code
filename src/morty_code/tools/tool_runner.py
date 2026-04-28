from __future__ import annotations

from morty_code.types.messages import Message
from morty_code.types.runtime_state import ToolUseContext


class NullToolRunner:
    """第一阶段空工具执行器。"""

    async def run(
        self,
        assistant_message: Message,
        context: ToolUseContext,
    ) -> list[Message]:
        return []
