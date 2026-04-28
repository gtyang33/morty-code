from __future__ import annotations

import re

from morty_code.types.messages import Attachment, Message
from morty_code.types.runtime_state import QueuedCommand, ToolUseContext


AT_MENTION_RE = re.compile(r"@([A-Za-z0-9_./-]+)")


class AttachmentManager:
    """负责首轮同步附件与轮尾增量附件。"""

    async def collect_initial(
        self,
        input_text: str,
        context: ToolUseContext,
        messages: list[Message],
    ) -> list[Attachment]:
        attachments: list[Attachment] = []
        for match in AT_MENTION_RE.finditer(input_text):
            attachments.append(
                Attachment(
                    type="at_mentioned_file",
                    payload={"path": match.group(1)},
                )
            )
        return attachments

    async def collect_post_iteration(
        self,
        input_text: str,
        context: ToolUseContext,
        messages: list[Message],
        queued_commands: list[QueuedCommand],
    ) -> list[Attachment]:
        attachments: list[Attachment] = []
        for command in queued_commands:
            if command.mode not in {"prompt", "task-notification"}:
                continue
            attachments.append(
                Attachment(
                    type="queued_command",
                    payload={
                        "prompt": command.value,
                        "mode": command.mode,
                    },
                    source_uuid=command.uuid,
                    is_meta=command.is_meta,
                )
            )
        return attachments
