from __future__ import annotations

from morty_code.types.messages import Message
from morty_code.types.runtime_state import ContentReplacementState, FileViewState, ToolUseContext


class SessionRestore:
    """从清洗后的 transcript 重建可继续执行的 runtime state。"""

    def restore(
        self,
        messages: list[Message],
        metadata: dict[str, object],
    ) -> dict[str, object]:
        read_file_state: dict[str, FileViewState] = {}
        content_replacement_state = ContentReplacementState()
        for message in messages:
            if message.type != "attachment":
                if message.type == "user":
                    self._restore_replacements(message, content_replacement_state)
                continue
            if message.payload.get("attachment_type") == "at_mentioned_file":
                path = str(message.payload.get("path", ""))
                if path:
                    read_file_state[path] = FileViewState(
                        path=path,
                        content=str(message.payload.get("content", "")),
                        is_partial_view=bool(message.payload.get("truncated", False)),
                    )
        return {
            "messages": messages,
            "metadata": metadata,
            "tool_context": ToolUseContext(
                tools=[],
                model=str(metadata.get("model", "echo-model")),
                permission_mode=str(metadata.get("permission_mode", "default")),
                app_state={"cwd": metadata.get("cwd", ".")},
                read_file_state=read_file_state,
                content_replacement_state=content_replacement_state,
            ),
        }

    def _restore_replacements(
        self,
        message: Message,
        state: ContentReplacementState,
    ) -> None:
        content = message.payload.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = str(block.get("tool_use_id", ""))
            result_content = block.get("content")
            if tool_use_id and isinstance(result_content, str) and result_content.startswith("[Tool result "):
                state.seen_ids.add(tool_use_id)
                state.replacements[tool_use_id] = result_content

    def restore_content_replacement_events(
        self,
        events: list[dict[str, object]],
        state: ContentReplacementState,
    ) -> None:
        for event in events:
            if event.get("type") != "content-replacement":
                continue
            replacements = event.get("replacements")
            if not isinstance(replacements, list):
                continue
            for record in replacements:
                if not isinstance(record, dict) or record.get("kind") != "tool-result":
                    continue
                tool_use_id = str(record.get("tool_use_id", ""))
                replacement = record.get("replacement")
                if tool_use_id and isinstance(replacement, str):
                    state.seen_ids.add(tool_use_id)
                    state.replacements[tool_use_id] = replacement
