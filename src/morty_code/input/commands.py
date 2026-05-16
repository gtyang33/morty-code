from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable


CommandHandler = Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]


@dataclass
class CommandSpec:
    """统一 slash command 与 skill/prompt capability 的注册结构。"""

    name: str
    description: str
    kind: str
    handler: CommandHandler
    aliases: list[str] = field(default_factory=list)
    user_invocable: bool = True
    model_invocable: bool = True
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None


class CommandRegistry:
    """轻量命令注册表。"""

    def __init__(self, commands: list[CommandSpec] | None = None) -> None:
        """初始化对象状态。"""
        self._commands = commands or []

    def register(self, command: CommandSpec) -> None:
        """注册可供后续使用的条目。"""
        self._commands.append(command)

    def find(self, name: str) -> CommandSpec | None:
        """查找匹配的注册项或数据。"""
        for command in self._commands:
            if command.name == name or name in command.aliases:
                return command
        return None

    def list_user_invocable(self) -> list[CommandSpec]:
        """列出可用条目。"""
        return [command for command in self._commands if command.user_invocable]
