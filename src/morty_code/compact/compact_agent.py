from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from morty_code.types.messages import Message


class CompactAgent:
    """规则版 compact agent。

    真实 Claude Code 会用 no-tools 子 agent 做总结；这里先用确定性摘要保持
    Python MVP 无外部依赖，同时保留 compact boundary 的状态迁移语义。
    """

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
                    "source_message_count": len(messages),
                },
            )
        ]

    async def compact_messages(self, messages: list[Message]) -> tuple[list[Message], list[Message]]:
        summary_messages = await self.summarize(messages)
        # 保留尾部最近消息，作为 compact 后可继续执行的 retained tail。
        return summary_messages, messages[-8:]
