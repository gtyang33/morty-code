from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.input.commands import CommandRegistry, CommandSpec
from morty_code.input.slash_commands import SlashCommandProcessor, parse_slash_command
from morty_code.memory.durable_memory import DurableMemoryStore
from morty_code.types.messages import Attachment, Message
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
                    self.attachment_manager.to_message(
                        Attachment(
                            type="command_permissions",
                            payload={
                                "command": text.split(" ", 1)[0],
                                "allowed_tools": processed.allowed_tools,
                            },
                            is_meta=True,
                            phase="input",
                            stable_key=f"input:command_permissions:{text.split(' ', 1)[0]}",
                        ),
                        timestamp=datetime.utcnow().isoformat(),
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
            self.attachment_manager.to_message(
                attachment,
                timestamp=datetime.utcnow().isoformat(),
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
                name="status",
                description="显示当前 runtime 状态",
                kind="local",
                handler=self._handle_status,
            )
        )
        registry.register(
            CommandSpec(
                name="tools",
                description="显示当前允许的工具",
                kind="local",
                handler=self._handle_tools,
            )
        )
        registry.register(
            CommandSpec(
                name="memory-index",
                description="显示 durable memory 索引",
                kind="local",
                handler=self._handle_memory_index,
            )
        )
        registry.register(
            CommandSpec(
                name="plan",
                description="进入 plan mode",
                kind="local",
                handler=self._handle_plan_mode,
            )
        )
        registry.register(
            CommandSpec(
                name="auto",
                description="退出 plan mode，回到自动执行",
                kind="local",
                handler=self._handle_auto_mode,
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
        commands = sorted(
            self.command_registry.list_user_invocable(),
            key=lambda command: command.name,
        )
        return {
            "mode": "local",
            "content": "\n".join(
                f"/{command.name} - {command.description}" for command in commands
            ),
        }

    async def _handle_status(self, args: str, context: dict[str, object]) -> dict[str, object]:
        tool_context = context["tool_context"]
        messages = context["messages"]
        if not isinstance(tool_context, ToolUseContext) or not isinstance(messages, list):
            return {"mode": "local", "content": "Runtime status unavailable."}
        approximate_tokens = sum(len(str(message.payload)) for message in messages)
        return {
            "mode": "local",
            "content": "\n".join(
                [
                    f"model: {tool_context.model}",
                    f"permission_mode: {tool_context.permission_mode}",
                    f"messages: {len(messages)}",
                    f"approximate_prompt_chars: {approximate_tokens}",
                    f"tools: {', '.join(tool_context.tools) if tool_context.tools else 'none'}",
                    f"read_file_state: {len(tool_context.read_file_state)}",
                    f"prompt_cache_calls: {tool_context.prompt_cache_state.call_count}",
                    f"prompt_cache_read_tokens: {tool_context.prompt_cache_state.cache_read_input_tokens}",
                    f"prompt_cache_creation_tokens: {tool_context.prompt_cache_state.cache_creation_input_tokens}",
                    f"session_memory_path: {tool_context.session_memory_path or 'none'}",
                    f"durable_memory_dir: {tool_context.durable_memory_dir or 'none'}",
                    f"transcript_path: {tool_context.app_state.get('transcript_path', 'unknown')}",
                ]
            ),
        }

    async def _handle_tools(self, args: str, context: dict[str, object]) -> dict[str, object]:
        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext):
            return {"mode": "local", "content": "Tool context unavailable."}
        if not tool_context.tools:
            return {"mode": "local", "content": "No tools are currently allowed."}
        schemas = tool_context.app_state.get("tool_schemas") or []
        descriptions: dict[str, str] = {}
        if isinstance(schemas, list):
            for schema in schemas:
                if not isinstance(schema, dict):
                    continue
                function = schema.get("function")
                if isinstance(function, dict):
                    descriptions[str(function.get("name", ""))] = str(function.get("description", ""))
        return {
            "mode": "local",
            "content": "\n".join(
                f"- {tool}: {descriptions.get(tool, '')}".rstrip()
                for tool in tool_context.tools
            ),
        }

    async def _handle_memory_index(self, args: str, context: dict[str, object]) -> dict[str, object]:
        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext) or not tool_context.durable_memory_dir:
            return {"mode": "local", "content": "No durable memory directory configured."}
        index = DurableMemoryStore(tool_context.durable_memory_dir).read_index()
        return {"mode": "local", "content": index.strip() or "Memory index is empty."}

    async def _handle_plan_mode(self, args: str, context: dict[str, object]) -> dict[str, object]:
        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext):
            return {"mode": "local", "content": "Tool context unavailable."}
        tool_context.app_state["plan_mode"] = True
        tool_context.app_state["last_plan_mode_attachment_turn"] = 0
        return {"mode": "local", "content": "Plan mode enabled."}

    async def _handle_auto_mode(self, args: str, context: dict[str, object]) -> dict[str, object]:
        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext):
            return {"mode": "local", "content": "Tool context unavailable."}
        was_plan_mode = bool(tool_context.app_state.get("plan_mode", False))
        tool_context.app_state["plan_mode"] = False
        if was_plan_mode:
            tool_context.app_state["needs_plan_mode_exit_attachment"] = True
        return {"mode": "local", "content": "Auto mode enabled."}

    async def _handle_compact(self, args: str, context: dict[str, object]) -> dict[str, object]:
        return {
            "mode": "compact",
            "content": "Conversation compaction requested.",
        }

    async def _handle_memory(self, args: str, context: dict[str, object]) -> dict[str, object]:
        return {
            "mode": "prompt",
            "content": "Please refresh session memory and surface relevant durable memories for the current task.",
        }
