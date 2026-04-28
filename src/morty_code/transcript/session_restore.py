from __future__ import annotations

from morty_code.types.messages import Message
from morty_code.types.runtime_state import ContentReplacementState, ToolUseContext


class SessionRestore:
    """第一阶段只保留 runtime 恢复接口。"""

    def restore(
        self,
        messages: list[Message],
        metadata: dict[str, object],
    ) -> dict[str, object]:
        read_file_state: dict[str, object] = {}
        for message in messages:
            if message.type != "attachment":
                continue
            if message.payload.get("attachment_type") != "at_mentioned_file":
                continue
            path = str(message.payload.get("path", ""))
            if path:
                read_file_state[path] = {
                    "path": path,
                    "content": "",
                    "is_partial_view": False,
                }
        return {
            "messages": messages,
            "metadata": metadata,
            "tool_context": ToolUseContext(
                tools=[],
                model=str(metadata.get("model", "echo-model")),
                permission_mode=str(metadata.get("permission_mode", "default")),
                app_state={"cwd": metadata.get("cwd", ".")},
                read_file_state=read_file_state,
                content_replacement_state=ContentReplacementState(),
            ),
        }
