from __future__ import annotations

from datetime import datetime
from pathlib import Path

from morty_code.memory.durable_memory import DurableMemoryStore
from morty_code.prompt.prompt_sections import PromptSectionRegistry, SystemPromptSection
from morty_code.skills import SkillRegistry
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
                cache_break=True,
            ),
            SystemPromptSection(
                name="skills",
                compute=lambda: (
                    "Skills: 当用户任务匹配 available_skills 中的能力时，必须先调用 "
                    "skill 工具加载对应 skill，再继续回答。不要只提到 skill 而不调用；"
                    "看到已加载的 skill 指令后直接遵循，不要重复调用。"
                    if "skill" in tools
                    else ""
                ),
                cache_break=True,
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
        skill_registry = context.app_state.get("skill_registry")
        if isinstance(skill_registry, SkillRegistry):
            listing = skill_registry.render_listing()
            if listing.strip():
                system_context["available_skills"] = listing
        tool_schemas = context.app_state.get("tool_schemas")
        if tool_schemas:
            import json

            allowed_tools = set(context.tools)
            filtered_tool_schemas = [
                schema
                for schema in tool_schemas
                if _schema_tool_name(schema) in allowed_tools
            ]
            if filtered_tool_schemas:
                system_context["tool_schemas_json"] = json.dumps(
                    filtered_tool_schemas,
                    ensure_ascii=False,
                )
        return system_prompt, user_context, system_context


def _schema_tool_name(schema: object) -> str | None:
    """从 OpenAI-compatible tool schema 中取函数名，用于当前工具集过滤。"""

    if not isinstance(schema, dict):
        return None
    function = schema.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return str(name) if name is not None else None
