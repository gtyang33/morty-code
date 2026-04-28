from __future__ import annotations

from morty_code.types.messages import Message


class MemoryExtractor:
    """把本轮新事实蒸馏成可写入 durable/session memory 的摘要。"""

    def extract(self, messages: list[Message]) -> list[str]:
        summaries: list[str] = []
        for message in messages:
            if message.type != "assistant":
                continue
            content = message.payload.get("content")
            if (
                isinstance(content, list)
                and content
                and isinstance(content[0], dict)
                and content[0].get("type") == "text"
            ):
                summaries.append(str(content[0].get("text", "")).strip())
        return [summary for summary in summaries if summary]
