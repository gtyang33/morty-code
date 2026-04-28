from __future__ import annotations

from morty_code.types.messages import Message


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
