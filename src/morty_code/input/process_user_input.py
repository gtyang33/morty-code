from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.input.commands import CommandRegistry, CommandSpec
from morty_code.input.slash_commands import SlashCommandProcessor, parse_slash_command
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ProcessedUserInput, QueuedCommand, ToolUseContext


class UserInputProcessor:
    """进入 query 前的消息改写层。"""

    def __init__(
        self,
        attachment_manager: AttachmentManager,
        command_registry: CommandRegistry | None = None,
    ) -> None:
        self.attachment_manager = attachment_manager
        self.command_registry = command_registry or self._build_default_commands()
        self.slash_processor = SlashCommandProcessor(self.command_registry)

    async def process(
        self,
        command: QueuedCommand,
        context: ToolUseContext,
        messages: list[Message],
        skip_attachments: bool = False,
    ) -> ProcessedUserInput:
        self.attachment_manager.bind_context(context)
        text = command.value if isinstance(command.value, str) else ""
        if text.startswith("/") and not command.skip_slash_commands:
            processed = await self.slash_processor.process(
                text,
                {
                    "tool_context": context,
                    "messages": messages,
                },
            )
            if processed.allowed_tools is not None:
                processed.messages.append(
                    Message(
                        uuid=str(uuid4()),
                        timestamp=datetime.utcnow().isoformat(),
                        type="attachment",
                        payload={
                            "attachment_type": "command_permissions",
                            "command": text.split(" ", 1)[0],
                            "allowed_tools": processed.allowed_tools,
                        },
                        is_meta=True,
                    )
                )
            return processed

        attachments = []
        if text and not skip_attachments:
            attachments = await self.attachment_manager.collect_initial(
                input_text=text,
                context=context,
                messages=messages,
            )

        user_content: object = text
        if command.pasted_contents:
            user_content = self._build_multimodal_content(text, command.pasted_contents)

        user_message = Message(
            uuid=command.uuid or str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="user",
            payload={"content": user_content, "mode": command.mode},
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

    def _build_multimodal_content(
        self,
        text: str,
        pasted_contents: dict[int, dict[str, object]],
    ) -> list[dict[str, object]]:
        """把 pasted image 保留为结构化块，文本 paste 已在 InputDispatcher 展开。"""

        blocks: list[dict[str, object]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for item in pasted_contents.values():
            if item.get("type") != "image":
                continue
            blocks.append(
                {
                    "type": "image",
                    "source": item.get("source") or item.get("content") or item.get("data"),
                    "media_type": item.get("media_type", "image/png"),
                }
            )
        return blocks

    def _build_default_commands(self) -> CommandRegistry:
        registry = CommandRegistry()
        registry.register(
            CommandSpec(
                name="help",
                description="显示可用命令",
                kind="local",
                handler=self._handle_help,
            )
        )
        registry.register(
            CommandSpec(
                name="compact",
                description="请求对话压缩",
                kind="prompt",
                handler=self._handle_compact,
                allowed_tools=[],
            )
        )
        registry.register(
            CommandSpec(
                name="memory",
                description="请求刷新会话记忆",
                kind="prompt",
                handler=self._handle_memory,
                allowed_tools=[],
            )
        )
        return registry

    async def _handle_help(self, args: str, context: dict[str, object]) -> dict[str, object]:
        names = [command.name for command in self.command_registry.list_user_invocable()]
        return {
            "mode": "local",
            "content": "Available commands: " + ", ".join(sorted(names)),
        }

    async def _handle_compact(self, args: str, context: dict[str, object]) -> dict[str, object]:
        return {
            "mode": "prompt",
            "content": "Please compact the current conversation and preserve active constraints.",
        }

    async def _handle_memory(self, args: str, context: dict[str, object]) -> dict[str, object]:
        return {
            "mode": "prompt",
            "content": "Please refresh session memory and surface relevant durable memories for the current task.",
        }
