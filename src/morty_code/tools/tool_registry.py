from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


ToolHandler = Callable[..., Awaitable[dict[str, object]]]


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler
    prompt: str | None = None
    input_schema: dict[str, Any] | None = None
    needs_context: bool = False


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec] | None = None) -> None:
        """初始化对象状态。"""
        self._tools = tools or []

    def register(self, tool: ToolSpec) -> None:
        """注册可供后续使用的条目。"""
        self._tools.append(tool)

    def find(self, name: str) -> ToolSpec | None:
        """查找匹配的注册项或数据。"""
        return next((tool for tool in self._tools if tool.name == name), None)

    def list_names(self) -> list[str]:
        """列出可用条目。"""
        return [tool.name for tool in self._tools]

    def api_tool_schemas(self, allowed_names: set[str] | None = None) -> list[dict[str, object]]:
        """渲染 OpenAI-compatible tools schema。"""

        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.prompt or tool.description,
                    "parameters": tool.input_schema
                    or {
                        "type": "object",
                        "properties": {},
                    },
                },
            }
            for tool in self._tools
            if allowed_names is None or tool.name in allowed_names
        ]
