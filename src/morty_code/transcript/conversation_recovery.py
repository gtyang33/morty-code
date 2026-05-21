from __future__ import annotations

from morty_code.types.messages import Message


class ConversationRecovery:
    """恢复 transcript 时做防御性清洗。"""

    def recover(self, messages: list[Message]) -> list[Message]:
        """修复历史消息中的可恢复问题。"""
        visible_messages = [message for message in messages if not message.is_virtual]
        visible_messages = self._messages_after_latest_compact_boundary(visible_messages)
        recovered: list[Message] = []
        open_tool_use_ids: set[str] = set()
        for message in visible_messages:
            if message.type == "assistant":
                content = message.payload.get("content")
                if not self._assistant_has_visible_content(content):
                    continue
                open_tool_use_ids.update(self._tool_use_ids(content))
            if message.type == "user":
                content = message.payload.get("content")
                tool_result_ids = self._tool_result_ids(content)
                if tool_result_ids and not tool_result_ids.intersection(open_tool_use_ids):
                    # 孤儿 tool_result 会导致多数 API 拒绝请求，恢复时直接丢弃。
                    continue
                open_tool_use_ids.difference_update(tool_result_ids)
            recovered.append(message)
        return recovered

    def _messages_after_latest_compact_boundary(self, messages: list[Message]) -> list[Message]:
        """只恢复最后一次 compact boundary 之后的主链上下文。"""

        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if (
                message.type == "system"
                and message.payload.get("subtype") == "compact_boundary"
            ):
                return messages[index:]
        return messages

    def _assistant_has_visible_content(self, content: object) -> bool:
        """内部处理该方法负责的业务逻辑。"""
        if content is None or content == []:
            return False
        if isinstance(content, str):
            return bool(content.strip())
        if not isinstance(content, list):
            return True
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                return True
            if block.get("type") == "tool_use":
                return True
        return False

    def _tool_use_ids(self, content: object) -> set[str]:
        """内部处理该方法负责的业务逻辑。"""
        if not isinstance(content, list):
            return set()
        return {
            str(block.get("id"))
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
        }

    def _tool_result_ids(self, content: object) -> set[str]:
        """内部处理该方法负责的业务逻辑。"""
        if not isinstance(content, list):
            return set()
        return {
            str(block.get("tool_use_id"))
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id")
        }
