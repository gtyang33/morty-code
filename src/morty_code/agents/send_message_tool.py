from __future__ import annotations

from morty_code.agents.task_registry import SubagentTask, get_subagent_task_registry
from morty_code.tools.tool_registry import ToolRegistry, ToolSpec
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


def register_send_message_tool(registry: ToolRegistry) -> None:
    """注册主代理向后台子代理发送消息的工具。"""

    if registry.find("send_message") is not None:
        return

    async def send_message(
        args: dict[str, object],
        context: ToolUseContext,
        _cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        """把消息排队到目标后台子代理。"""

        target = str(args.get("to") or "").strip()
        message = str(args.get("message") or "").strip()
        if not target:
            raise ValueError("to is required")
        if not message:
            raise ValueError("message is required")
        registry_root = str(context.app_state.get("subagent_tasks_dir") or ".morty/tasks")
        task_registry = get_subagent_task_registry(registry_root)
        task = _resolve_task(task_registry, context.app_state, target)
        if task is None:
            return {
                "success": False,
                "message": f"No subagent found for target '{target}'. Use a running agent_id, task_id, or registered name.",
            }
        if task.status != "running":
            return {
                "success": False,
                "message": (
                    f"Agent '{target}' is {task.status}; resuming stopped subagents "
                    "from transcript is not supported yet."
                ),
                "task_id": task.task_id,
                "agent_id": task.agent_id,
            }
        task_registry.queue_pending_message(task.task_id, message)
        return {
            "success": True,
            "message": f"Message queued for delivery to {target} at its next model/tool round.",
            "task_id": task.task_id,
            "agent_id": task.agent_id,
        }

    registry.register(
        ToolSpec(
            name="send_message",
            description="Send a message to a running background subagent.",
            prompt=(
                "Send a message to a running background subagent.\n\n"
                "Use this only to continue, clarify, or redirect an agent that was launched in the background. "
                "The `to` field may be the agent_id, task_id, or the optional name used when spawning the agent. "
                "Messages are delivered at the target agent's next model/tool round; they do not interrupt an in-flight model call. "
                "Do not use this for ordinary user-facing replies."
            ),
            handler=send_message,
            needs_context=True,
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Target subagent name, agent_id, or task_id.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message to deliver to the subagent.",
                    },
                },
                "required": ["to", "message"],
            },
        )
    )


def _resolve_task(task_registry, app_state: dict[str, object], target: str) -> SubagentTask | None:
    """按 name/task_id/agent_id 解析目标后台子代理。"""

    names = app_state.get("agent_name_registry")
    if isinstance(names, dict):
        named = names.get(target)
        if named:
            target = str(named)
    return task_registry.get(target) or task_registry.get_by_agent_id(target)
