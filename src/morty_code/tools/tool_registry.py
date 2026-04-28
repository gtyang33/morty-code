from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


ToolHandler = Callable[[dict[str, object]], Awaitable[dict[str, object]]]


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec] | None = None) -> None:
        self._tools = tools or []

    def register(self, tool: ToolSpec) -> None:
        self._tools.append(tool)

    def find(self, name: str) -> ToolSpec | None:
        return next((tool for tool in self._tools if tool.name == name), None)

    def list_names(self) -> list[str]:
        return [tool.name for tool in self._tools]
