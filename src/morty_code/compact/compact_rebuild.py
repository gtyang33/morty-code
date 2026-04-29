from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from morty_code.types.messages import Message
from morty_code.types.runtime_state import ToolUseContext


def rebuild_post_compact_messages(
    summary_messages: list[Message],
    messages_to_keep: list[Message],
    attachments: list[Message] | None = None,
) -> list[Message]:
    """compact 后重建新的可继续执行消息序列。"""

    rebuilt = [*summary_messages, *messages_to_keep]
    if attachments:
        rebuilt.extend(attachments)
    return rebuilt


def build_reinjection_attachments(context: ToolUseContext) -> list[Message]:
    """compact 后重新注入弱持久化状态，避免摘要丢掉执行现场。

    这些信息本来可以从 transcript 推导，但 compact 后历史会被替换成摘要，
    所以需要把当前模型已经见过的文件视图和 session memory 重新显式化。
    """

    now = datetime.utcnow().isoformat()
    attachments: list[Message] = []
    for file_state in context.read_file_state.values():
        attachments.append(
            Message(
                uuid=str(uuid4()),
                timestamp=now,
                type="attachment",
                payload={
                    "attachment_type": "at_mentioned_file",
                    "path": file_state.path,
                    "resolved_path": file_state.path,
                    "kind": "file",
                    "content": file_state.content,
                    "truncated": file_state.is_partial_view,
                    "source": "post_compact_reinject",
                },
                is_meta=True,
            )
        )
    if context.session_memory_path:
        from pathlib import Path

        session_path = Path(context.session_memory_path)
        if session_path.exists():
            attachments.append(
                Message(
                    uuid=str(uuid4()),
                    timestamp=now,
                    type="attachment",
                    payload={
                        "attachment_type": "session_memory",
                        "path": str(session_path),
                        "content": session_path.read_text(encoding="utf-8", errors="replace")[:12000],
                        "source": "post_compact_reinject",
                    },
                    is_meta=True,
                )
            )
    return attachments
