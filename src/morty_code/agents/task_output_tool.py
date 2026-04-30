from __future__ import annotations

import asyncio
from dataclasses import asdict

from morty_code.agents.task_registry import SubagentTask, get_subagent_task_registry
from morty_code.tools.tool_registry import ToolRegistry, ToolSpec
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


def register_task_output_tool(registry: ToolRegistry) -> None:
    """注册后台任务输出查询工具。

    Claude Code 的 TaskOutput 已经提示模型优先 Read output_file；这里保留
    block/non-block 查询能力，方便 OpenAI-compatible provider 直接函数调用。
    """

    if registry.find("task_output") is not None:
        return

    async def task_output(
        args: dict[str, object],
        context: ToolUseContext,
        _cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        block = args.get("block")
        should_block = True if block is None else bool(block)
        timeout_ms = _bounded_timeout(args.get("timeout_ms"))
        registry_root = str(context.app_state.get("subagent_tasks_dir") or ".morty/tasks")
        task_registry = get_subagent_task_registry(registry_root)
        task_registry.interrupt_orphaned_running()
        task = task_registry.get(task_id)
        if task is None:
            return {"retrieval_status": "not_found", "task": None}
        if not should_block:
            return _format_task(task, retrieval_status=_status_for(task))
        completed = await _wait_for_task(task_registry, task_id, timeout_ms)
        if completed is None:
            return {"retrieval_status": "not_found", "task": None}
        if completed.status == "running":
            return _format_task(completed, retrieval_status="timeout")
        if completed.status == "interrupted":
            return _format_task(completed, retrieval_status="interrupted")
        return _format_task(completed, retrieval_status="success")

    registry.register(
        ToolSpec(
            name="task_output",
            description="Read status and output for a background subagent task.",
            handler=task_output,
            needs_context=True,
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID returned by spawn_agent when run_in_background is true.",
                    },
                    "block": {
                        "type": "boolean",
                        "description": "Wait until the task reaches completed or failed.",
                        "default": True,
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Maximum wait time in milliseconds.",
                        "minimum": 0,
                        "maximum": 600000,
                        "default": 30000,
                    },
                },
                "required": ["task_id"],
            },
        )
    )


async def _wait_for_task(task_registry, task_id: str, timeout_ms: int) -> SubagentTask | None:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while True:
        task = task_registry.get(task_id)
        if task is None or task.status != "running":
            return task
        if asyncio.get_running_loop().time() >= deadline:
            return task
        await asyncio.sleep(0.1)


def _format_task(task: SubagentTask, retrieval_status: str) -> dict[str, object]:
    payload = asdict(task)
    return {
        "retrieval_status": retrieval_status,
        "task": payload,
    }


def _status_for(task: SubagentTask) -> str:
    if task.status == "running":
        return "not_ready"
    if task.status == "interrupted":
        return "interrupted"
    return "success"


def _bounded_timeout(value: object) -> int:
    if value is None or value == "":
        return 30000
    try:
        return max(0, min(600000, int(value)))
    except (TypeError, ValueError):
        return 30000
