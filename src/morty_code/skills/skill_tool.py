from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from morty_code.agents.subagent_runner import SubagentRunner
from morty_code.skills.registry import SkillRegistry, SkillSpec, load_skill_registry
from morty_code.tools.tool_registry import ToolRegistry, ToolSpec
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


SKILL_TOOL_NAME = "skill"


def register_skill_tool(
    registry: ToolRegistry,
    *,
    skill_registry: SkillRegistry | None = None,
    query_loop=None,
) -> None:
    """注册 Claude 风格 SkillTool。

    skill tool 自身只负责按需加载 SKILL.md，并把完整内容作为 meta
    user message 回灌；具体执行仍由下一轮模型完成。
    """

    async def _handler(
        args: dict[str, object],
        context: ToolUseContext,
        cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        name = str(args.get("skill") or "").strip().removeprefix("/")
        if not name:
            raise ValueError("skill is required")
        call_args = str(args.get("args") or "")
        active_registry = skill_registry or _registry_from_context(context)
        skill = active_registry.find(name)
        if skill is None or not skill.model_invocable:
            raise ValueError(f"Unknown or unavailable skill: {name}")
        if skill.context == "fork":
            if query_loop is None:
                raise RuntimeError("forked skill execution requires query_loop")
            return await _run_forked_skill(skill, call_args, context, cache_safe, query_loop)
        return _load_inline_skill(skill, call_args, context)

    registry.register(
        ToolSpec(
            name=SKILL_TOOL_NAME,
            description="Load and execute a Morty skill.",
            prompt=(
                "Execute a skill within the main conversation. When a user task "
                "matches an available skill, call this tool before answering. "
                "Use the skill name and optional args; do not use this for built-in slash commands."
            ),
            handler=_handler,
            needs_context=True,
            input_schema={
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": "Skill name, with or without leading slash.",
                    },
                    "args": {
                        "type": "string",
                        "description": "Optional arguments passed to the skill.",
                    },
                },
                "required": ["skill"],
            },
        )
    )


def _registry_from_context(context: ToolUseContext) -> SkillRegistry:
    registry = context.app_state.get("skill_registry")
    if isinstance(registry, SkillRegistry):
        return registry
    cwd = Path(str(context.app_state.get("cwd") or "."))
    registry = load_skill_registry(cwd)
    context.app_state["skill_registry"] = registry
    return registry


def _load_inline_skill(
    skill: SkillSpec,
    args: str,
    context: ToolUseContext,
) -> dict[str, object]:
    content = skill.render_prompt(args, context)
    _record_invoked_skill(skill, content, context)
    _apply_skill_context(skill, context)
    return {
        "__morty_tool_result_content__": f"Skill loaded: {skill.name}",
        "__morty_new_messages__": [
            Message(
                uuid=str(uuid4()),
                timestamp=datetime.now(UTC).isoformat(),
                type="user",
                payload={"content": content},
                is_meta=True,
                origin={"source": "skill", "skill": skill.name},
            )
        ],
    }


async def _run_forked_skill(
    skill: SkillSpec,
    args: str,
    context: ToolUseContext,
    cache_safe: CacheSafeParams,
    query_loop,
) -> dict[str, object]:
    content = skill.render_prompt(args, context)
    _record_invoked_skill(skill, content, context)
    _apply_skill_context(skill, context)
    tool_registry = context.app_state.get("tool_registry")
    if not isinstance(tool_registry, ToolRegistry):
        raise RuntimeError("tool_registry is required for forked skill")
    runner = SubagentRunner(query_loop, tool_registry)
    result = await runner.run(
        agent_type=skill.agent or "general-purpose",
        prompt=content,
        parent_context=context,
        parent_cache_safe=cache_safe,
        record_transcript=True,
    )
    return {
        "__morty_tool_result_content__": (
            f"Skill completed: {skill.name}\n\n{result.output}"
        ).strip()
    }


def _record_invoked_skill(
    skill: SkillSpec,
    content: str,
    context: ToolUseContext,
) -> None:
    invoked = context.app_state.setdefault("invoked_skills", {})
    if not isinstance(invoked, dict):
        invoked = {}
        context.app_state["invoked_skills"] = invoked
    invoked[skill.name] = {
        "name": skill.name,
        "path": str(skill.source_path),
        "content": content,
        "invoked_at": datetime.now(UTC).isoformat(),
    }
    context.discovered_skill_names.add(skill.name)


def _apply_skill_context(skill: SkillSpec, context: ToolUseContext) -> None:
    if skill.allowed_tools:
        for tool in skill.allowed_tools:
            if tool not in context.tools:
                context.tools.append(tool)
        tool_registry = context.app_state.get("tool_registry")
        if isinstance(tool_registry, ToolRegistry):
            context.app_state["tool_schemas"] = tool_registry.api_tool_schemas(set(context.tools))
    if skill.model:
        context.model = skill.model
    if skill.effort:
        context.app_state["effort"] = skill.effort
