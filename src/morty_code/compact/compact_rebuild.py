from __future__ import annotations

from morty_code.attachments.attachment_manager import AttachmentManager
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

    manager = AttachmentManager.from_context(context)
    return [
        manager.to_message(attachment)
        for attachment in manager.collect_reinjection(context, messages=[])
    ]
