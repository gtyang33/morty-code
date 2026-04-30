from __future__ import annotations

from copy import deepcopy

from morty_code.types.messages import Message


SYNTHETIC_TOOL_RESULT_PLACEHOLDER = "[Tool result missing due to conversation recovery]"
TOOL_REFERENCE_REMOVED_PLACEHOLDER = "[Tool references removed - tool search not enabled]"


class MessageNormalizer:
    """API 发送前的消息规范化流水线。

    当前保留最关键的几步：
    1. attachment 冒泡
    2. strip virtual
    3. merge adjacent user
    4. transcript message -> API payload
    """

    def __init__(self, max_images: int = 20) -> None:
        self.max_images = max_images

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
        merged = self._merge_adjacent_users(non_empty)
        smooshed = self._smoosh_system_reminder_siblings(merged)
        paired = self._ensure_tool_pairing(smooshed)
        return [
            self._to_api_message(message)
            for message in paired
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
        if message.type == "user":
            return self._normalize_user_content(message)
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

    def _normalize_user_content(self, message: Message) -> Message:
        content = message.payload.get("content")
        if not isinstance(content, list):
            return message
        image_count = 0
        blocks: list[dict[str, object]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and str(block.get("text", "")).strip():
                blocks.append(block)
            elif block_type == "image":
                source = block.get("source")
                if not source or image_count >= self.max_images:
                    continue
                image_count += 1
                blocks.append(block)
            elif block_type == "tool_result":
                blocks.append(self._normalize_tool_result_block(block))
        blocks = self._hoist_tool_results(blocks)
        normalized = deepcopy(message)
        normalized.payload = {"content": blocks}
        return normalized

    def _normalize_tool_result_block(self, block: dict[str, object]) -> dict[str, object]:
        updated = dict(block)
        content = updated.get("content")
        if isinstance(content, list):
            filtered = [
                item
                for item in content
                if not (isinstance(item, dict) and item.get("type") == "tool_reference")
            ]
            if len(filtered) != len(content):
                content = filtered or [{"type": "text", "text": TOOL_REFERENCE_REMOVED_PLACEHOLDER}]
            if updated.get("is_error"):
                content = [
                    item
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
            updated["content"] = content
        return updated

    def _hoist_tool_results(self, blocks: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            *[block for block in blocks if block.get("type") == "tool_result"],
            *[block for block in blocks if block.get("type") != "tool_result"],
        ]

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
                f"allowed_tools: {payload.get('allowed_tools', [])}\n"
                f"tool_schema_count: {payload.get('tool_schema_count', '')}"
            ).strip()
        if attachment_type == "session_memory":
            return f"path: {payload.get('path', '')}\n{payload.get('content', '')}".strip()
        if attachment_type == "skill_discovery":
            skills = payload.get("skills", [])
            if isinstance(skills, list):
                return "discovered_skills:\n" + "\n".join(f"- {skill}" for skill in skills)
            return f"discovered_skills: {skills}"
        if attachment_type == "content_replacement_state":
            replacement_ids = payload.get("replacement_ids", [])
            if isinstance(replacement_ids, list):
                return (
                    "large tool results already replaced:\n"
                    + "\n".join(f"- {tool_use_id}" for tool_use_id in replacement_ids)
                )
            return f"large tool results already replaced: {replacement_ids}"
        if attachment_type in {
            "date_change",
            "plan_mode",
            "plan_mode_exit",
            "hook_additional_context",
        }:
            return "\n".join(f"{key}: {value}" for key, value in payload.items())
        return "\n".join(f"{key}: {value}" for key, value in payload.items())

    def _smoosh_system_reminder_siblings(self, messages: list[Message]) -> list[Message]:
        output: list[Message] = []
        for message in messages:
            content = message.payload.get("content")
            if message.type != "user" or not isinstance(content, list):
                output.append(message)
                continue
            system_texts = [
                block
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and str(block.get("text", "")).startswith("<system-reminder>")
            ]
            if not system_texts:
                output.append(message)
                continue
            kept = [
                block
                for block in content
                if not (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and str(block.get("text", "")).startswith("<system-reminder>")
                )
            ]
            tool_result_indexes = [
                index
                for index, block in enumerate(kept)
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            if not tool_result_indexes:
                output.append(message)
                continue
            target_index = tool_result_indexes[-1]
            target = kept[target_index]
            if not isinstance(target, dict) or self._tool_result_has_reference(target):
                output.append(message)
                continue
            kept[target_index] = self._append_to_tool_result(target, system_texts)
            output.append(
                Message(
                    uuid=message.uuid,
                    timestamp=message.timestamp,
                    type=message.type,
                    payload={**message.payload, "content": kept},
                    is_meta=message.is_meta,
                    is_virtual=message.is_virtual,
                    origin=message.origin,
                )
            )
        return output

    def _tool_result_has_reference(self, block: dict[str, object]) -> bool:
        content = block.get("content")
        return isinstance(content, list) and any(
            isinstance(item, dict) and item.get("type") == "tool_reference"
            for item in content
        )

    def _append_to_tool_result(
        self,
        tool_result: dict[str, object],
        blocks: list[dict[str, object]],
    ) -> dict[str, object]:
        incoming_text = "\n\n".join(
            str(block.get("text", "")).strip()
            for block in blocks
            if str(block.get("text", "")).strip()
        )
        if not incoming_text:
            return tool_result
        updated = dict(tool_result)
        content = updated.get("content")
        if updated.get("is_error"):
            updated["content"] = self._join_text_content(content, incoming_text)
            return updated
        if isinstance(content, list):
            updated["content"] = [*content, {"type": "text", "text": incoming_text}]
            return updated
        updated["content"] = self._join_text_content(content, incoming_text)
        return updated

    def _join_text_content(self, content: object, extra: str) -> str:
        base = ""
        if isinstance(content, str):
            base = content.strip()
        elif isinstance(content, list):
            base = "\n\n".join(
                str(item.get("text", "")).strip()
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
        return "\n\n".join(part for part in [base, extra] if part)

    def _ensure_tool_pairing(self, messages: list[Message]) -> list[Message]:
        """修复 tool_use/tool_result 配对，避免 API 结构错误。"""

        output: list[Message] = []
        seen_tool_use_ids: set[str] = set()
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.type == "assistant":
                content = message.payload.get("content")
                if not isinstance(content, list):
                    output.append(message)
                    index += 1
                    continue
                assistant_tool_use_ids: list[str] = []
                deduped_content: list[dict[str, object]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_use_id = str(block.get("id", ""))
                        if not tool_use_id or tool_use_id in seen_tool_use_ids:
                            continue
                        seen_tool_use_ids.add(tool_use_id)
                        assistant_tool_use_ids.append(tool_use_id)
                    deduped_content.append(block)
                assistant_message = self._with_content(
                    message,
                    deduped_content or [{"type": "text", "text": "[Tool use interrupted]"}],
                )
                output.append(assistant_message)
                next_message = messages[index + 1] if index + 1 < len(messages) else None
                if assistant_tool_use_ids:
                    existing_ids = self._tool_result_ids(
                        next_message.payload.get("content")
                        if next_message and next_message.type == "user"
                        else None
                    )
                    missing_ids = [
                        tool_use_id
                        for tool_use_id in assistant_tool_use_ids
                        if tool_use_id not in existing_ids
                    ]
                    if missing_ids:
                        synthetic_blocks = [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": SYNTHETIC_TOOL_RESULT_PLACEHOLDER,
                                "is_error": True,
                            }
                            for tool_use_id in missing_ids
                        ]
                        if next_message and next_message.type == "user":
                            next_content = next_message.payload.get("content")
                            if not isinstance(next_content, list):
                                next_content = [{"type": "text", "text": str(next_content or "")}]
                            output.append(self._with_content(next_message, [*synthetic_blocks, *next_content]))
                            index += 2
                            continue
                        output.append(
                            Message(
                                uuid=f"{message.uuid}-synthetic-tool-results",
                                timestamp=message.timestamp,
                                type="user",
                                payload={"content": synthetic_blocks},
                                is_meta=True,
                            )
                        )
                index += 1
                continue
            if message.type == "user":
                content = message.payload.get("content")
                if isinstance(content, list):
                    filtered_content: list[object] = []
                    seen_result_ids: set[str] = set()
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tool_use_id = str(block.get("tool_use_id", ""))
                            if tool_use_id not in seen_tool_use_ids or tool_use_id in seen_result_ids:
                                continue
                            seen_result_ids.add(tool_use_id)
                        filtered_content.append(block)
                    if not filtered_content:
                        index += 1
                        continue
                    if len(filtered_content) != len(content):
                        message = self._with_content(message, filtered_content)
                output.append(message)
                index += 1
                continue
            output.append(message)
            index += 1
        return output

    def _with_content(self, message: Message, content: object) -> Message:
        return Message(
            uuid=message.uuid,
            timestamp=message.timestamp,
            type=message.type,
            payload={**message.payload, "content": content},
            is_meta=message.is_meta,
            is_virtual=message.is_virtual,
            origin=message.origin,
        )

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
