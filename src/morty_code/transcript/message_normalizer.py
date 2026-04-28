from __future__ import annotations

from copy import deepcopy

from morty_code.types.messages import Message


class MessageNormalizer:
    """API 发送前的消息规范化流水线。

    第一阶段先保留最关键的几步：
    1. attachment 冒泡
    2. strip virtual
    3. merge adjacent user
    4. transcript message -> API payload
    """

    def normalize_for_api(
        self,
        messages: list[Message],
        available_tools: list[str],
    ) -> list[dict[str, object]]:
        reordered = self._reorder_attachments(messages)
        filtered = [message for message in reordered if not message.is_virtual]
        merged = self._merge_adjacent_users(filtered)
        return [
            self._to_api_message(message)
            for message in merged
            if message.type in {"user", "assistant"}
        ]

    def _reorder_attachments(self, messages: list[Message]) -> list[Message]:
        result: list[Message] = []
        pending_attachments: list[Message] = []
        for message in reversed(messages):
            if message.type == "attachment":
                pending_attachments.append(message)
                continue
            is_stop = message.type == "assistant" or self._is_tool_result_user(message)
            if is_stop and pending_attachments:
                result.extend(pending_attachments)
                pending_attachments.clear()
            result.append(message)
        result.extend(pending_attachments)
        result.reverse()
        return result

    def _is_tool_result_user(self, message: Message) -> bool:
        if message.type != "user":
            return False
        content = message.payload.get("content")
        return (
            isinstance(content, list)
            and bool(content)
            and isinstance(content[0], dict)
            and content[0].get("type") == "tool_result"
        )

    def _merge_adjacent_users(self, messages: list[Message]) -> list[Message]:
        merged: list[Message] = []
        for message in messages:
            if merged and message.type == "user" and merged[-1].type == "user":
                previous = deepcopy(merged[-1])
                previous.payload = {
                    "content": self._merge_user_content(
                        previous.payload.get("content"),
                        message.payload.get("content"),
                    )
                }
                merged[-1] = previous
            else:
                merged.append(message)
        return merged

    def _merge_user_content(self, left: object, right: object) -> object:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n{right}".strip()
        if isinstance(left, list) and isinstance(right, list):
            return [*left, *right]
        if left is None:
            return right
        if right is None:
            return left
        return f"{left}\n{right}".strip()

    def _to_api_message(self, message: Message) -> dict[str, object]:
        if message.type == "user":
            return {"role": "user", "content": message.payload.get("content", "")}
        return {"role": "assistant", "content": message.payload.get("content", [])}
