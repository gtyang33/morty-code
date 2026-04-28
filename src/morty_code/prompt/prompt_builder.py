from __future__ import annotations

from datetime import datetime
from pathlib import Path

from morty_code.prompt.prompt_sections import PromptSectionRegistry, SystemPromptSection
from morty_code.types.runtime_state import ToolUseContext


SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


class PromptBuilder:
    """负责构建 cache-safe 三段上下文。"""

    def __init__(self, registry: PromptSectionRegistry) -> None:
        self.registry = registry

    async def build(
        self,
        tools: list[str],
        model: str,
        runtime_state: dict[str, object],
    ) -> tuple[list[str], dict[str, str], dict[str, str]]:
        sections = [
            SystemPromptSection(
                name="identity",
                compute=lambda: "你是一个可恢复的长会话编码运行时。",
            ),
            SystemPromptSection(
                name="behavior",
                compute=lambda: "优先保持上下文连续性、稳定的工具轨迹和可恢复性。",
            ),
            SystemPromptSection(
                name="tools",
                compute=lambda: f"可用工具: {', '.join(tools) if tools else '无'}",
            ),
            SystemPromptSection(
                name="dynamic-boundary",
                compute=lambda: SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
                cache_break=True,
            ),
            SystemPromptSection(
                name="model",
                compute=lambda: f"当前模型: {model}",
                cache_break=True,
            ),
        ]
        system_prompt = await self.registry.resolve_sections(sections)
        cwd = str(runtime_state.get("cwd", Path.cwd()))
        user_context = {
            "current_date": datetime.now().date().isoformat(),
        }
        system_context = {
            "cwd": cwd,
        }
        return system_prompt, user_context, system_context

    async def build_for_context(
        self,
        context: ToolUseContext,
    ) -> tuple[list[str], dict[str, str], dict[str, str]]:
        return await self.build(context.tools, context.model, context.app_state)
