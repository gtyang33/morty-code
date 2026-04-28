from __future__ import annotations

from morty_code.types.messages import Message


class SessionRestore:
    """第一阶段只保留 runtime 恢复接口。"""

    def restore(
        self,
        messages: list[Message],
        metadata: dict[str, object],
    ) -> dict[str, object]:
        return {
            "messages": messages,
            "metadata": metadata,
        }
