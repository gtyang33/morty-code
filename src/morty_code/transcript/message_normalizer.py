from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from morty_code.types.messages import Message


SYNTHETIC_TOOL_RESULT_PLACEHOLDER = "[Tool result missing due to conversation recovery]"
TOOL_REFERENCE_REMOVED_PLACEHOLDER = "[Tool references removed - tool search not enabled]"
NO_CONTENT_MESSAGE = "[No message content]"


@dataclass
class NormalizationReport:
    """记录 normalizer 的自愈动作，避免坏 transcript 被静默吞掉。"""

    repairs: dict[str, int] = field(default_factory=dict)

    def record(self, repair_type: str, count: int = 1) -> None:
        if count <= 0:
            return
        self.repairs[repair_type] = self.repairs.get(repair_type, 0) + count

    def to_event(self) -> dict[str, object] | None:
        if not self.repairs:
            return None
        return {
            "type": "message-normalization-repaired",
            "repairs": dict(sorted(self.repairs.items())),
        }


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
        self.last_report = NormalizationReport()

    def normalize_for_api(
        self,
        messages: list[Message],
        available_tools: list[str],
    ) -> list[dict[str, object]]:
        self.last_report = NormalizationReport()
        reordered = self._reorder_attachments(messages)
        materialized = [self._materialize_attachment(message) for message in reordered]
        filtered = [message for message in materialized if not message.is_virtual]
        normalized = [self._normalize_message_content(message) for message in filtered]
        assistant_merged = self._merge_assistant_stream_chunks(normalized)
        without_orphan_thinking = self._filter_orphaned_thinking_only_assistants(assistant_merged)
        without_trailing_thinking = self._filter_trailing_thinking_from_last_assistant(without_orphan_thinking)
        without_thinking_blocks = self._strip_thinking_blocks(without_trailing_thinking)
        without_whitespace_assistant = self._filter_whitespace_only_assistants(without_thinking_blocks)
        non_empty_assistant = self._ensure_non_final_assistants_have_content(without_whitespace_assistant)
        merged_users = self._merge_adjacent_users(non_empty_assistant)
        smooshed = self._smoosh_system_reminder_siblings(merged_users)
        paired = self._ensure_tool_pairing(smooshed)
        api_messages = [
            self._to_api_message(message)
            for message in paired
            if message.type in {"user", "assistant"}
        ]
        return self._final_validate_api_messages(api_messages)

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
                self.last_report.record("adjacent-user-merged")
            else:
                merged.append(message)
        return merged

    def _merge_user_content(self, left: object, right: object) -> object:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n{right}".strip()
        if isinstance(left, list) and isinstance(right, list):
            return [*left, *right]
        if isinstance(left, list) and isinstance(right, str):
            return [*left, *self._text_blocks_from_string(right)]
        if isinstance(left, str) and isinstance(right, list):
            return [*self._text_blocks_from_string(left), *right]
        if left is None:
            return right
        if right is None:
            return left
        return f"{left}\n{right}".strip()

    def _text_blocks_from_string(self, value: str) -> list[dict[str, object]]:
        stripped = value.strip()
        if not stripped:
            return []
        return [{"type": "text", "text": stripped}]

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
            normalized.payload = {**normalized.payload, "content": [{"type": "text", "text": content}]}
            return normalized
        if isinstance(content, list):
            blocks: list[object] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                blocks.append(block)
            normalized.payload = {**normalized.payload, "content": blocks}
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
                self.last_report.record("tool-reference-stripped", len(content) - len(filtered))
                content = filtered or [{"type": "text", "text": TOOL_REFERENCE_REMOVED_PLACEHOLDER}]
            if updated.get("is_error"):
                text_only = [
                    item
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                if len(text_only) != len(content):
                    self.last_report.record("error-tool-result-non-text-stripped", len(content) - len(text_only))
                content = text_only
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
            "approved_plan",
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
            self.last_report.record("system-reminder-smooshed", len(system_texts))
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
                            self.last_report.record("duplicate-tool-use-stripped")
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
                        self.last_report.record("missing-tool-result-synthesized", len(missing_ids))
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
                            filtered_next = self._filter_user_tool_results(
                                next_content,
                                allowed_ids=set(assistant_tool_use_ids),
                            )
                            output.append(self._with_content(next_message, [*synthetic_blocks, *filtered_next]))
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
                    stripped_count = 0
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tool_use_id = str(block.get("tool_use_id", ""))
                            if tool_use_id not in seen_tool_use_ids or tool_use_id in seen_result_ids:
                                stripped_count += 1
                                continue
                            seen_result_ids.add(tool_use_id)
                        filtered_content.append(block)
                    if stripped_count:
                        self.last_report.record("orphan-or-duplicate-tool-result-stripped", stripped_count)
                    if not filtered_content:
                        if not output:
                            output.append(
                                self._with_content(
                                    message,
                                    [
                                        {
                                            "type": "text",
                                            "text": "[Orphaned tool result removed due to conversation resume]",
                                        }
                                    ],
                                )
                            )
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

    def _filter_user_tool_results(
        self,
        content: list[object],
        allowed_ids: set[str],
    ) -> list[object]:
        filtered: list[object] = []
        seen_result_ids: set[str] = set()
        stripped_count = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_use_id = str(block.get("tool_use_id", ""))
                if tool_use_id not in allowed_ids or tool_use_id in seen_result_ids:
                    stripped_count += 1
                    continue
                seen_result_ids.add(tool_use_id)
            filtered.append(block)
        if stripped_count:
            self.last_report.record("orphan-or-duplicate-tool-result-stripped", stripped_count)
        return filtered

    def _merge_assistant_stream_chunks(self, messages: list[Message]) -> list[Message]:
        """合并同一 API response 在 streaming 中拆出的 assistant block。

        Claude Code 会按 message.id 回看合并。这里只跨过 tool_result user
        message，避免把不同用户 turn 的 assistant 错合并。
        """

        output: list[Message] = []
        for message in messages:
            if message.type != "assistant" or not message.payload.get("id"):
                output.append(message)
                continue
            message_id = str(message.payload["id"])
            merged = False
            for index in range(len(output) - 1, -1, -1):
                previous = output[index]
                if previous.type != "assistant" and not self._is_tool_result_user(previous):
                    break
                if previous.type == "assistant" and str(previous.payload.get("id", "")) == message_id:
                    output[index] = self._merge_assistant_messages(previous, message)
                    self.last_report.record("assistant-stream-chunk-merged")
                    merged = True
                    break
            if not merged:
                output.append(message)
        return output

    def _merge_assistant_messages(self, left: Message, right: Message) -> Message:
        left_content = left.payload.get("content")
        right_content = right.payload.get("content")
        if not isinstance(left_content, list):
            left_content = [{"type": "text", "text": str(left_content or "")}]
        if not isinstance(right_content, list):
            right_content = [{"type": "text", "text": str(right_content or "")}]
        return self._with_content(left, [*left_content, *right_content])

    def _filter_orphaned_thinking_only_assistants(self, messages: list[Message]) -> list[Message]:
        ids_with_non_thinking: set[str] = set()
        for message in messages:
            if message.type != "assistant":
                continue
            content = message.payload.get("content")
            if not isinstance(content, list):
                continue
            if any(
                isinstance(block, dict)
                and block.get("type") not in {"thinking", "redacted_thinking"}
                for block in content
            ) and message.payload.get("id"):
                ids_with_non_thinking.add(str(message.payload["id"]))

        output: list[Message] = []
        for message in messages:
            if message.type != "assistant":
                output.append(message)
                continue
            content = message.payload.get("content")
            if not isinstance(content, list) or not content:
                output.append(message)
                continue
            all_thinking = all(
                isinstance(block, dict)
                and block.get("type") in {"thinking", "redacted_thinking"}
                for block in content
            )
            if all_thinking and str(message.payload.get("id", "")) not in ids_with_non_thinking:
                self.last_report.record("orphan-thinking-assistant-stripped")
                continue
            output.append(message)
        return output

    def _filter_trailing_thinking_from_last_assistant(self, messages: list[Message]) -> list[Message]:
        if not messages or messages[-1].type != "assistant":
            return messages
        content = messages[-1].payload.get("content")
        if not isinstance(content, list) or not content:
            return messages
        last_valid_index = len(content) - 1
        while last_valid_index >= 0:
            block = content[last_valid_index]
            if not isinstance(block, dict) or block.get("type") not in {"thinking", "redacted_thinking"}:
                break
            last_valid_index -= 1
        removed = len(content) - last_valid_index - 1
        if not removed:
            return messages
        replacement = (
            [{"type": "text", "text": NO_CONTENT_MESSAGE}]
            if last_valid_index < 0
            else content[: last_valid_index + 1]
        )
        result = list(messages)
        result[-1] = self._with_content(messages[-1], replacement)
        self.last_report.record("trailing-thinking-stripped", removed)
        return result

    def _filter_whitespace_only_assistants(self, messages: list[Message]) -> list[Message]:
        output: list[Message] = []
        removed = 0
        for message in messages:
            if message.type != "assistant":
                output.append(message)
                continue
            content = message.payload.get("content")
            if not isinstance(content, list) or not content:
                output.append(message)
                continue
            if all(
                isinstance(block, dict)
                and block.get("type") == "text"
                and not str(block.get("text", "")).strip()
                for block in content
            ):
                removed += 1
                continue
            output.append(message)
        if removed:
            self.last_report.record("whitespace-only-assistant-stripped", removed)
            return self._merge_adjacent_users(output)
        return messages

    def _strip_thinking_blocks(self, messages: list[Message]) -> list[Message]:
        output: list[Message] = []
        for message in messages:
            if message.type != "assistant":
                output.append(message)
                continue
            content = message.payload.get("content")
            if not isinstance(content, list):
                output.append(message)
                continue
            filtered = [
                block
                for block in content
                if not (
                    isinstance(block, dict)
                    and block.get("type") in {"thinking", "redacted_thinking"}
                )
            ]
            if len(filtered) != len(content):
                self.last_report.record("thinking-block-stripped", len(content) - len(filtered))
                output.append(self._with_content(message, filtered))
                continue
            output.append(message)
        return output

    def _ensure_non_final_assistants_have_content(self, messages: list[Message]) -> list[Message]:
        output: list[Message] = []
        for index, message in enumerate(messages):
            if message.type != "assistant" or index == len(messages) - 1:
                output.append(message)
                continue
            content = message.payload.get("content")
            if isinstance(content, list) and not content:
                output.append(self._with_content(message, [{"type": "text", "text": NO_CONTENT_MESSAGE}]))
                self.last_report.record("empty-non-final-assistant-filled")
                continue
            output.append(message)
        return output

    def _final_validate_api_messages(
        self,
        messages: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """最后一道 API shape 防线：只保留可见消息并消除相邻同 role。"""

        output: list[dict[str, object]] = []
        for message in messages:
            role = message.get("role")
            if role not in {"user", "assistant"}:
                self.last_report.record("non-api-role-stripped")
                continue
            if not self._api_message_has_content(message):
                self.last_report.record("empty-api-message-stripped")
                continue
            if output and output[-1].get("role") == role:
                output[-1] = self._merge_api_messages(output[-1], message)
                self.last_report.record("final-adjacent-role-merged")
                continue
            output.append(message)
        return output

    def _api_message_has_content(self, message: dict[str, object]) -> bool:
        content = message.get("content")
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return bool(content)
        return content is not None

    def _merge_api_messages(
        self,
        left: dict[str, object],
        right: dict[str, object],
    ) -> dict[str, object]:
        if left.get("role") == "assistant":
            left_content = left.get("content")
            right_content = right.get("content")
            if not isinstance(left_content, list):
                left_content = [{"type": "text", "text": str(left_content or "")}]
            if not isinstance(right_content, list):
                right_content = [{"type": "text", "text": str(right_content or "")}]
            return {**left, "content": [*left_content, *right_content]}
        return {**left, "content": self._merge_user_content(left.get("content"), right.get("content"))}

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
