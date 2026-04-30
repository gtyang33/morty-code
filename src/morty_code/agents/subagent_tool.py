from __future__ import annotations

from morty_code.agents.agent_definitions import load_project_agents
from morty_code.agents.subagent_runner import SubagentRunner
from morty_code.tools.tool_registry import ToolRegistry, ToolSpec
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


def register_subagent_tool(query_loop, registry: ToolRegistry) -> None:
    """把同步子代理注册成模型可调用工具。"""

    if registry.find("spawn_agent") is not None:
        return
    runner = SubagentRunner(
        query_loop=query_loop,
        tool_registry=registry,
        agent_registry=load_project_agents(),
    )

    async def spawn_agent(
        args: dict[str, object],
        context: ToolUseContext,
        cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        result = await runner.run(
            agent_type=str(args.get("subagent_type") or "general-purpose"),
            prompt=prompt,
            parent_context=context,
            parent_cache_safe=cache_safe,
            max_turns=_optional_int(args.get("max_turns")),
        )
        return {
            "status": result.status,
            "agent_type": result.agent_type,
            "output": result.output,
            "message_count": result.message_count,
            "metadata_events": result.metadata_events,
        }

    registry.register(
        ToolSpec(
            name="spawn_agent",
            description="Delegate a bounded task to an isolated subagent and return its concise result.",
            handler=spawn_agent,
            needs_context=True,
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The task for the subagent to perform.",
                    },
                    "subagent_type": {
                        "type": "string",
                        "description": "Subagent type: general-purpose, Explore, Plan, or verification.",
                        "default": "general-purpose",
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Optional max model/tool iterations for this subagent.",
                        "minimum": 1,
                        "maximum": 8,
                    },
                },
                "required": ["prompt"],
            },
        )
    )


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(1, min(8, int(value)))
    except (TypeError, ValueError):
        return None
