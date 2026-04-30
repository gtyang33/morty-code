from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from morty_code.types.messages import Message
from morty_code.types.runtime_state import ProcessedUserInput


@dataclass
class ParsedSlashCommand:
    command_name: str
    args: str


def parse_slash_command(input_text: str) -> ParsedSlashCommand | None:
    if not input_text.startswith("/"):
        return None
    body = input_text[1:].strip()
    if not body:
        return None
    parts = body.split(" ", 1)
    command_name = parts[0]
    args = parts[1].strip() if len(parts) > 1 else ""
    return ParsedSlashCommand(command_name=command_name, args=args)


class SlashCommandProcessor:
    """把 slash command 编译成模型可见消息或本地结果。"""

    def __init__(self, registry) -> None:
        self.registry = registry

    async def process(
        self,
        input_text: str,
        context: dict[str, object],
    ) -> ProcessedUserInput:
        parsed = parse_slash_command(input_text)
        if parsed is None:
            raise ValueError("input is not a slash command")
        command = self.registry.find(parsed.command_name)
        if command is None:
            message = Message(
                uuid=str(uuid4()),
                timestamp=datetime.utcnow().isoformat(),
                type="user",
                payload={"content": f"Unknown command: /{parsed.command_name}"},
            )
            return ProcessedUserInput(messages=[message], should_query=False)

        result = await command.handler(parsed.args, context)
        mode = str(result.get("mode", "prompt"))
        if mode == "compact":
            message = Message(
                uuid=str(uuid4()),
                timestamp=datetime.utcnow().isoformat(),
                type="system",
                payload={
                    "subtype": "local_command",
                    "content": str(result.get("content", "Compaction requested.")),
                },
            )
            return ProcessedUserInput(
                messages=[message],
                should_query=False,
                trigger_compact=True,
            )
        if mode == "local":
            message = Message(
                uuid=str(uuid4()),
                timestamp=datetime.utcnow().isoformat(),
                type="system",
                payload={
                    "subtype": "local_command",
                    "content": str(result.get("content", "")),
                },
            )
            return ProcessedUserInput(messages=[message], should_query=False)

        metadata_message = Message(
            uuid=str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="user",
            payload={"content": f"Loading command /{command.name}"},
        )
        prompt_message = Message(
            uuid=str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="user",
            payload={"content": result.get("content", "")},
            is_meta=True,
        )
        return ProcessedUserInput(
            messages=[metadata_message, prompt_message],
            should_query=True,
            # 空列表表示“显式禁用所有工具”，不能用 `or None` 吞掉。
            allowed_tools=command.allowed_tools,
            model=command.model,
            effort=command.effort,
        )
