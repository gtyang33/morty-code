from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ToolUseContext


def rebuild_post_compact_messages(
    summary_messages: list[Message],
    messages_to_keep: list[Message],
    attachments: list[Message] | None = None,
) -> list[Message]:
    """compact 后重建新的可继续执行消息序列。"""

    # Claude Code 的 compact 后上下文会先恢复弱持久化状态，再继续保留尾部消息；
    # 这样模型读取 retained tail 前，已经能看到文件视图、计划模式和 session memory。
    rebuilt = [*summary_messages]
    if attachments:
        rebuilt.extend(attachments)
    rebuilt.extend(messages_to_keep)
    return rebuilt


def clone_retained_messages_for_compact(messages_to_keep: list[Message]) -> list[Message]:
    """为 transcript 复制 retained tail，让它们稳定落在 compact boundary 后面。"""

    now = datetime.now(UTC).isoformat()
    cloned: list[Message] = []
    for message in messages_to_keep:
        # retained tail 原本已经在 transcript 的旧位置出现过；这里必须换新 uuid，
        # 否则 append-only parent 链会出现重复节点，恢复和 UI 展示都会变得含混。
        cloned.append(
            Message(
                uuid=str(uuid4()),
                timestamp=now,
                type=message.type,
                payload=deepcopy(message.payload),
                is_meta=message.is_meta,
                is_virtual=message.is_virtual,
                origin={
                    **(message.origin or {}),
                    "source": "post_compact_retained",
                    "original_uuid": message.uuid,
                },
            )
        )
    return cloned


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
