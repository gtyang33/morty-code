from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from morty_code.types.messages import Message


class CompactAgent:
    """第一阶段使用规则摘要代替真正的 no-tools agent。"""

    async def summarize(self, messages: list[Message]) -> list[Message]:
        kept = []
        for message in messages[-6:]:
            if message.type == "user":
                kept.append(str(message.payload.get("content", "")).strip())
        summary = " | ".join([item for item in kept if item]) or "No recent user messages."
        return [
            Message(
                uuid=str(uuid4()),
                timestamp=datetime.utcnow().isoformat(),
                type="system",
                payload={
                    "subtype": "compact_boundary",
                    "summary": summary,
                },
            )
        ]

    async def compact_messages(self, messages: list[Message]) -> tuple[list[Message], list[Message]]:
        summary_messages = await self.summarize(messages)
        # 第一阶段保留尾部最近消息，模拟 compact 后的 retained tail。
        return summary_messages, messages[-8:]
