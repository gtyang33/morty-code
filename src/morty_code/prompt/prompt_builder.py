from __future__ import annotations

from datetime import datetime
from pathlib import Path

from morty_code.memory.durable_memory import DurableMemoryStore
from morty_code.prompt.prompt_sections import PromptSectionRegistry, SystemPromptSection
from morty_code.types.runtime_state import ToolUseContext


SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


class PromptBuilder:
    """负责构建 cache-safe 三段上下文。"""

    def __init__(self, registry: PromptSectionRegistry) -> None:
        """初始化对象状态。"""
        self.registry = registry

    async def build(
        self,
        tools: list[str],
        model: str,
        runtime_state: dict[str, object],
    ) -> tuple[list[str], dict[str, str], dict[str, str]]:
        """构建后续流程需要的数据。"""
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
                name="file-editing",
                compute=lambda: (
                    "文件修改策略: 修改已有文件时先用 read_file 获取相关内容，再优先使用 "
                    "edit_file 或 multi_edit 做精确替换；新建文件或整文件重写才使用 "
                    "write_file。不要用 bash 修改源码或文档，不要用 sed -i、python3 -c、"
                    "perl -pi、awk、eho 重定向或 hceredoc 写文件。bash 只用于搜索、查看、"
                    "测试、构建、git 等命令执行。"
                ),
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
        """构建后续流程需要的数据。"""
        system_prompt, user_context, system_context = await self.build(
            context.tools,
            context.model,
            context.app_state,
        )
        # durable/session memory 放在 user_context，不改写 cache-safe system prompt 主体。
        if context.durable_memory_dir:
            store = DurableMemoryStore(context.durable_memory_dir)
            index = store.read_index()[:12000]
            if index.strip():
                user_context["durable_memory_index"] = index
        if context.session_memory_path:
            session_path = Path(context.session_memory_path)
            if session_path.exists():
                user_context["session_memory"] = session_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )[:12000]
        if context.discovered_skill_names:
            system_context["discovered_skills"] = ", ".join(
                sorted(context.discovered_skill_names)
            )
        tool_schemas = context.app_state.get("tool_schemas")
        if tool_schemas:
            import json

            system_context["tool_schemas_json"] = json.dumps(
                tool_schemas,
                ensure_ascii=False,
            )
        return system_prompt, user_context, system_context
