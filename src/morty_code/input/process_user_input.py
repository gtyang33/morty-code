from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ProcessedUserInput, QueuedCommand, ToolUseContext


class UserInputProcessor:
    """进入 query 前的消息改写层。"""

    def __init__(self, attachment_manager: AttachmentManager) -> None:
        self.attachment_manager = attachment_manager

    async def process(
        self,
        command: QueuedCommand,
        context: ToolUseContext,
        messages: list[Message],
        skip_attachments: bool = False,
    ) -> ProcessedUserInput:
        # 这里先只实现常规 prompt 路径。
        # slash/bridge/keyword rewrite 第二阶段再扩展。
        text = command.value if isinstance(command.value, str) else ""
        attachments = []
        if text and not skip_attachments:
            attachments = await self.attachment_manager.collect_initial(
                input_text=text,
                context=context,
                messages=messages,
            )

        user_message = Message(
            uuid=command.uuid or str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="user",
            payload={"content": text, "mode": command.mode},
            is_meta=command.is_meta,
            origin=command.origin,
        )
        attachment_messages = [
            Message(
                uuid=str(uuid4()),
                timestamp=datetime.utcnow().isoformat(),
                type="attachment",
                payload={"attachment_type": attachment.type, **attachment.payload},
                is_meta=attachment.is_meta,
                origin=command.origin,
            )
            for attachment in attachments
        ]
        return ProcessedUserInput(
            messages=[user_message, *attachment_messages],
            should_query=True,
        )
