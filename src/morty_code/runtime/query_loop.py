from __future__ import annotations

from dataclasses import dataclass

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.tools.tool_result_budget import apply_tool_result_budget
from morty_code.transcript.message_normalizer import MessageNormalizer
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


@dataclass
class QueryLoopResult:
    new_messages: list[Message]
    metadata_events: list[dict[str, object]]


class QueryLoop:
    """模型采样、工具执行和 post-iteration attachment 的主循环。

    当前职责：
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
        metadata_events: list[dict[str, object]] = []
        working_messages = list(messages)
        assistant_message: Message | None = None
        for _ in range(self.max_iterations):
            working_messages, replacement_records = apply_tool_result_budget(
                working_messages,
                tool_context.content_replacement_state,
                tool_results_dir=tool_context.app_state.get(
                    "tool_results_dir",
                    ".morty/tool-results",
                ),
                limit=int(tool_context.app_state.get("tool_result_message_budget_chars", 50000)),
                skip_tool_names=set(tool_context.app_state.get("tool_result_budget_skip_tools", [])),
            )
            if replacement_records:
                metadata_events.append(
                    {
                        "type": "content-replacement",
                        "replacements": [
                            record.as_event_payload() for record in replacement_records
                        ],
                    }
                )
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
            return QueryLoopResult(new_messages=[], metadata_events=metadata_events)
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
            new_messages=[*new_messages, *attachment_messages],
            metadata_events=metadata_events,
        )
