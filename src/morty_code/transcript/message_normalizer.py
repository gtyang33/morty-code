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
        materialized = [self._materialize_attachment(message) for message in reordered]
        filtered = [message for message in materialized if not message.is_virtual]
        normalized = [self._normalize_message_content(message) for message in filtered]
        non_empty = [message for message in normalized if self._has_api_visible_content(message)]
        paired = self._ensure_tool_pairing(non_empty)
        merged = self._merge_adjacent_users(paired)
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

    def _normalize_message_content(self, message: Message) -> Message:
        if message.type != "assistant":
            return message
        content = message.payload.get("content")
        normalized = deepcopy(message)
        if isinstance(content, str):
            normalized.payload = {"content": [{"type": "text", "text": content}]}
            return normalized
        if isinstance(content, list):
            blocks: list[object] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                # thinking block 不能孤立发送给普通 API，恢复/normalize 时过滤掉。
                if block.get("type") == "thinking":
                    continue
                if block.get("type") == "text" and not str(block.get("text", "")).strip():
                    continue
                blocks.append(block)
            normalized.payload = {"content": blocks}
        return normalized

    def _has_api_visible_content(self, message: Message) -> bool:
        if message.type == "assistant":
            content = message.payload.get("content")
            if isinstance(content, str):
                return bool(content.strip())
            if isinstance(content, list):
                return any(
                    isinstance(block, dict)
                    and (
                        block.get("type") == "tool_use"
                        or (block.get("type") == "text" and str(block.get("text", "")).strip())
                    )
                    for block in content
                )
            return False
        if message.type == "user":
            content = message.payload.get("content")
            if isinstance(content, str):
                return bool(content.strip())
            if isinstance(content, list):
                return bool(content)
            return content is not None
        return True

    def _materialize_attachment(self, message: Message) -> Message:
        if message.type != "attachment":
            return message
        attachment_type = str(message.payload.get("attachment_type", "unknown"))
        content = self._format_attachment_content(message.payload)
        return Message(
            uuid=message.uuid,
            timestamp=message.timestamp,
            type="user",
            payload={"content": f"[Attachment: {attachment_type}]\n{content}".strip()},
            is_meta=message.is_meta,
            is_virtual=message.is_virtual,
            origin=message.origin,
        )

    def _format_attachment_content(self, payload: dict[str, object]) -> str:
        attachment_type = str(payload.get("attachment_type", "unknown"))
        if attachment_type == "at_mentioned_file":
            path = payload.get("path", "")
            kind = payload.get("kind", "file")
            content = payload.get("content", "")
            truncated = "\n[内容已截断]" if payload.get("truncated") else ""
            return f"path: {path}\nkind: {kind}\n{content}{truncated}".strip()
        if attachment_type == "relevant_memories":
            return f"path: {payload.get('path', '')}\n{payload.get('content', '')}".strip()
        if attachment_type == "queued_command":
            return f"mode: {payload.get('mode', '')}\nprompt: {payload.get('prompt', '')}".strip()
        if attachment_type == "command_permissions":
            return (
                f"command: {payload.get('command', '')}\n"
                f"allowed_tools: {payload.get('allowed_tools', [])}"
            ).strip()
        if attachment_type == "session_memory":
            return f"path: {payload.get('path', '')}\n{payload.get('content', '')}".strip()
        return "\n".join(f"{key}: {value}" for key, value in payload.items())

    def _ensure_tool_pairing(self, messages: list[Message]) -> list[Message]:
        """去掉没有对应 tool_use 的 tool_result，避免 API 结构错误。"""

        open_tool_use_ids: set[str] = set()
        output: list[Message] = []
        for message in messages:
            if message.type == "assistant":
                open_tool_use_ids.update(self._tool_use_ids(message.payload.get("content")))
                output.append(message)
                continue
            if message.type == "user":
                content = message.payload.get("content")
                result_ids = self._tool_result_ids(content)
                if result_ids:
                    valid_ids = result_ids.intersection(open_tool_use_ids)
                    if not valid_ids:
                        continue
                    open_tool_use_ids.difference_update(valid_ids)
                output.append(message)
                continue
            output.append(message)
        return output

    def _tool_use_ids(self, content: object) -> set[str]:
        if not isinstance(content, list):
            return set()
        return {
            str(block.get("id"))
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
        }

    def _tool_result_ids(self, content: object) -> set[str]:
        if not isinstance(content, list):
            return set()
        return {
            str(block.get("tool_use_id"))
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id")
        }
