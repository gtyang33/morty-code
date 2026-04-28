from __future__ import annotations

from dataclasses import dataclass

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.transcript.message_normalizer import MessageNormalizer
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


@dataclass
class QueryLoopResult:
    new_messages: list[Message]


class QueryLoop:
    """最小 query while-loop。

    第一阶段只实现：
    - normalize
    - model sampling
    - tool runner 回灌
    """

    def __init__(self, model_client, tool_runner, attachment_manager: AttachmentManager | None = None) -> None:
        self.model_client = model_client
        self.tool_runner = tool_runner
        self.normalizer = MessageNormalizer()
        self.attachment_manager = attachment_manager or AttachmentManager()

    async def run(
        self,
        messages: list[Message],
        cache_safe: CacheSafeParams,
        tool_context: ToolUseContext,
    ) -> QueryLoopResult:
        api_messages = self.normalizer.normalize_for_api(messages, tool_context.tools)
        assistant_message = await self.model_client.respond(
            messages=api_messages,
            system_prompt=cache_safe.system_prompt,
            user_context=cache_safe.user_context,
            system_context=cache_safe.system_context,
        )
        tool_messages = await self.tool_runner.run(assistant_message, tool_context)
        post_attachments = await self.attachment_manager.collect_post_iteration(
            input_text="",
            context=tool_context,
            messages=[*messages, assistant_message, *tool_messages],
            queued_commands=[],
        )
        attachment_messages = [
            Message(
                uuid=f"attachment-{index}",
                timestamp=assistant_message.timestamp,
                type="attachment",
                payload={"attachment_type": attachment.type, **attachment.payload},
                is_meta=attachment.is_meta,
            )
            for index, attachment in enumerate(post_attachments)
        ]
        return QueryLoopResult(
            new_messages=[assistant_message, *tool_messages, *attachment_messages]
        )
