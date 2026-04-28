from __future__ import annotations

from morty_code.types.messages import Message


class ConversationRecovery:
    """第一阶段只保留恢复入口。

    后续再补：
    - unresolved tool_use 清洗
    - orphaned thinking 过滤
    - interruption detection
    """

    def recover(self, messages: list[Message]) -> list[Message]:
        return list(messages)
