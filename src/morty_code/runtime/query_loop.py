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

    def __init__(
        self,
        model_client,
        tool_runner,
        attachment_manager: AttachmentManager | None = None,
        max_iterations: int = 6,
    ) -> None:
        self.model_client = model_client
        self.tool_runner = tool_runner
        self.normalizer = MessageNormalizer()
        self.attachment_manager = attachment_manager or AttachmentManager()
        self.max_iterations = max_iterations

    async def run(
        self,
        messages: list[Message],
        cache_safe: CacheSafeParams,
        tool_context: ToolUseContext,
    ) -> QueryLoopResult:
        new_messages: list[Message] = []
        working_messages = list(messages)
        assistant_message: Message | None = None
        for _ in range(self.max_iterations):
            api_messages = self.normalizer.normalize_for_api(working_messages, tool_context.tools)
            assistant_message = await self.model_client.respond(
                messages=api_messages,
                system_prompt=cache_safe.system_prompt,
                user_context=cache_safe.user_context,
                system_context=cache_safe.system_context,
            )
            new_messages.append(assistant_message)
            working_messages.append(assistant_message)

            tool_messages = await self.tool_runner.run(assistant_message, tool_context)
            if not tool_messages:
                break
            new_messages.extend(tool_messages)
            working_messages.extend(tool_messages)
        if assistant_message is None:
            return QueryLoopResult(new_messages=[])
        post_attachments = await self.attachment_manager.collect_post_iteration(
            input_text="",
            context=tool_context,
            messages=working_messages,
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
            new_messages=[*new_messages, *attachment_messages]
        )
