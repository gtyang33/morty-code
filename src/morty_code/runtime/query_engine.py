from __future__ import annotations

import asyncio

from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


class QueryEngine:
    """顶层入口，负责把输入层、prompt 层、query loop 串起来。"""

    def __init__(
        self,
        prompt_builder,
        input_dispatcher,
        input_processor,
        query_loop,
        transcript_store,
    ) -> None:
        self.prompt_builder = prompt_builder
        self.input_dispatcher = input_dispatcher
        self.input_processor = input_processor
        self.query_loop = query_loop
        self.transcript_store = transcript_store
        self.messages: list[Message] = []

    async def submit_message(
        self,
        raw_input: str,
        tool_context: ToolUseContext,
        pasted_contents: dict[int, dict[str, object]] | None = None,
    ) -> list[Message]:
        queued_commands = await self.input_dispatcher.submit(
            raw_input=raw_input,
            mode="prompt",
            pasted_contents=pasted_contents,
        )

        new_messages: list[Message] = []
        should_query = False
        for index, command in enumerate(queued_commands):
            processed = await self.input_processor.process(
                command=command,
                context=tool_context,
                messages=self.messages,
                skip_attachments=index > 0,
            )
            new_messages.extend(processed.messages)
            if index == 0:
                should_query = processed.should_query

        self.messages.extend(new_messages)
        if new_messages:
            await self.transcript_store.append_messages(new_messages)
        if not should_query:
            return new_messages

        system_prompt, user_context, system_context = await self.prompt_builder.build_for_context(
            tool_context
        )
        result = await self.query_loop.run(
            messages=self.messages,
            cache_safe=CacheSafeParams(
                system_prompt=system_prompt,
                user_context=user_context,
                system_context=system_context,
                messages=list(self.messages),
            ),
            tool_context=tool_context,
        )
        self.messages.extend(result.new_messages)
        if result.new_messages:
            await self.transcript_store.append_messages(result.new_messages)
        return result.new_messages

    def submit_message_sync(
        self,
        raw_input: str,
        tool_context: ToolUseContext,
        pasted_contents: dict[int, dict[str, object]] | None = None,
    ) -> list[Message]:
        return asyncio.run(self.submit_message(raw_input, tool_context, pasted_contents))
