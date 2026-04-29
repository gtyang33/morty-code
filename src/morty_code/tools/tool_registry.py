from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Any


ToolHandler = Callable[[dict[str, object]], Awaitable[dict[str, object]]]


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler
    input_schema: dict[str, Any] | None = None


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec] | None = None) -> None:
        self._tools = tools or []

    def register(self, tool: ToolSpec) -> None:
        self._tools.append(tool)

    def find(self, name: str) -> ToolSpec | None:
        return next((tool for tool in self._tools if tool.name == name), None)

    def list_names(self) -> list[str]:
        return [tool.name for tool in self._tools]

    def api_tool_schemas(self) -> list[dict[str, object]]:
        """渲染 OpenAI-compatible tools schema。"""

        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema
                    or {
                        "type": "object",
                        "properties": {},
                    },
                },
            }
            for tool in self._tools
        ]
