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
                    "content": "Conversation compacted",
                    "trigger": "auto",
                    "summary": summary,
                    "source_message_count": len(messages),
                },
            )
            ,
            Message(
                uuid=str(uuid4()),
                timestamp=datetime.utcnow().isoformat(),
                type="user",
                payload={
                    "content": (
                        "<system-reminder>\n"
                        "Conversation compacted. Continue from this summary:\n"
                        f"{summary}\n"
                        "</system-reminder>"
                    ),
                    "is_compact_summary": True,
                },
                is_meta=True,
            ),
        ]

    async def compact_messages(self, messages: list[Message]) -> tuple[list[Message], list[Message]]:
        summary_messages = await self.summarize(messages)
        # 保留尾部最近消息，作为 compact 后可继续执行的 retained tail。
        return summary_messages, _select_pair_safe_tail(messages, limit=8)


def _select_pair_safe_tail(messages: list[Message], limit: int) -> list[Message]:
    """选择 retained tail，避免旧 boundary 和明显孤儿 tool_result 进入 compact 后上下文。"""

    tail = [
        message
        for message in messages[-limit:]
        if not (
            message.type == "system"
            and message.payload.get("subtype") == "compact_boundary"
        )
    ]
    while tail and _is_tool_result_only_user(tail[0]):
        tail = tail[1:]
    return tail


def _is_tool_result_only_user(message: Message) -> bool:
    if message.type != "user":
        return False
    content = message.payload.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)
    )
