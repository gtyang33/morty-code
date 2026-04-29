from __future__ import annotations

from morty_code.types.messages import Message


class MemoryExtractor:
    """把本轮新事实蒸馏成可写入 durable/session memory 的摘要。"""

    def __init__(self, max_summary_chars: int = 500) -> None:
        self.max_summary_chars = max_summary_chars

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
                text = " ".join(str(content[0].get("text", "")).strip().split())
                if text and not text.startswith("Echo:"):
                    summaries.append(text[: self.max_summary_chars])
        return list(dict.fromkeys(summary for summary in summaries if summary))
