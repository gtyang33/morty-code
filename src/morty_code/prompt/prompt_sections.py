from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable


SectionFn = Callable[[], str | None | Awaitable[str | None]]


@dataclass
class SystemPromptSection:
    """system prompt section 的最小抽象。"""

    name: str
    compute: SectionFn
    cache_break: bool = False


class PromptSectionRegistry:
    """管理可缓存 section，与动态 section 的边界。"""

    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}

    async def resolve_sections(
        self,
        sections: list[SystemPromptSection],
    ) -> list[str]:
        resolved: list[str] = []
        for section in sections:
            if not section.cache_break and section.name in self._cache:
                value = self._cache[section.name]
            else:
                value = section.compute()
                if inspect.isawaitable(value):
                    value = await value
                if not section.cache_break:
                    self._cache[section.name] = value
            if value:
                resolved.append(value)
        return resolved
