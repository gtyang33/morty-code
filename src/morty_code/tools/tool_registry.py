from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
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
        self._lock = RLock()

    def register(self, tool: ToolSpec) -> None:
        """注册可供后续使用的条目。"""
        with self._lock:
            self._tools = [existing for existing in self._tools if existing.name != tool.name]
            self._tools.append(tool)

    def extend(self, tools: list[ToolSpec]) -> None:
        """批量注册工具；同名工具以后注册的版本为准。"""

        with self._lock:
            for tool in tools:
                self._tools = [existing for existing in self._tools if existing.name != tool.name]
                self._tools.append(tool)

    def remove_matching(self, predicate: Callable[[ToolSpec], bool]) -> None:
        """移除满足条件的工具，用于 MCP server disable/reconnect 后刷新工具池。"""

        with self._lock:
            self._tools = [tool for tool in self._tools if not predicate(tool)]

    def find(self, name: str) -> ToolSpec | None:
        """查找匹配的注册项或数据。"""
        with self._lock:
            return next((tool for tool in self._tools if tool.name == name), None)

    def list_names(self) -> list[str]:
        """列出可用条目。"""
        with self._lock:
            return [tool.name for tool in self._tools]

    def api_tool_schemas(self, allowed_names: set[str] | None = None) -> list[dict[str, object]]:
        """渲染 OpenAI-compatible tools schema。"""

        with self._lock:
            tools = list(self._tools)
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
            for tool in tools
            if allowed_names is None or tool.name in allowed_names
        ]
