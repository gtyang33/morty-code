from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

from morty_code.agents.agent_definitions import load_project_agents
from morty_code.agents.subagent_runner import SubagentRunner
from morty_code.agents.task_notifications import enqueue_task_notification
from morty_code.agents.task_registry import get_subagent_task_registry
from morty_code.tools.tool_registry import ToolRegistry, ToolSpec
from morty_code.types.runtime_state import (
    CacheSafeParams,
    ToolUseContext,
    clone_tool_use_context_for_fork,
)


def register_subagent_tool(query_loop, registry: ToolRegistry) -> None:
    """把同步子代理注册成模型可调用工具。"""

    if registry.find("spawn_agent") is not None:
        return

    def make_runner(context: ToolUseContext) -> SubagentRunner:
        # 项目自定义 agent 必须跟随父 agent 的 workspace，而不是 morty-code 源码目录。
        """创建流程需要的辅助对象。"""
        agent_dir = str(context.app_state.get("agents_dir") or ".morty/agents")
        return SubagentRunner(
            query_loop=query_loop,
            tool_registry=registry,
            agent_registry=load_project_agents(agent_dir),
        )

    async def spawn_agent(
        args: dict[str, object],
        context: ToolUseContext,
        cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        """处理该方法负责的业务逻辑。"""
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        agent_type = str(args.get("subagent_type") or "general-purpose")
        run_in_background = bool(args.get("run_in_background") is True)
        description = str(args.get("description") or agent_type)
        runner = make_runner(context)
        if run_in_background:
            return _launch_background_agent(
                runner=runner,
                agent_type=agent_type,
                prompt=prompt,
                description=description,
                context=context,
                cache_safe=cache_safe,
                max_turns=_optional_int(args.get("max_turns")),
            )
        result = await runner.run(
            agent_type=agent_type,
            prompt=prompt,
            parent_context=context,
            parent_cache_safe=cache_safe,
            max_turns=_optional_int(args.get("max_turns")),
        )
        return {
            "status": result.status,
            "agent_id": result.agent_id,
            "agent_type": result.agent_type,
            "output": result.output,
            "output_file": result.output_file,
            "message_count": result.message_count,
        }

    registry.register(
        ToolSpec(
            name="spawn_agent",
            description="Delegate a bounded task to an isolated subagent and return its concise result.",
            prompt=(
                "Delegate a bounded task to an isolated subagent and return its concise result.\n\n"
                "Use the returned output or output_file as the authoritative subagent result. "
                "If the result is long, read output_file with read_file.\n\n"
                "Do not read .morty/subagents for normal task results. "
                "Do not parse transcript JSONL with bash, cat, sed, or Python; transcripts are diagnostic-only."
            ),
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
                    "description": {
                        "type": "string",
                        "description": "Short task description for background task displays.",
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": "Launch the subagent in the background and return a clean output file path.",
                        "default": False,
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


def _launch_background_agent(
    *,
    runner: SubagentRunner,
    agent_type: str,
    prompt: str,
    description: str,
    context: ToolUseContext,
    cache_safe: CacheSafeParams,
    max_turns: int | None,
) -> dict[str, object]:
    """内部处理该方法负责的业务逻辑。"""
    task_id = str(uuid4())
    agent_id = str(uuid4())
    task_root = str(context.app_state.get("subagent_tasks_dir") or ".morty/tasks")
    registry = get_subagent_task_registry(task_root)
    task = registry.create(
        task_id=task_id,
        agent_id=agent_id,
        agent_type=agent_type,
        description=description,
        prompt=prompt,
    )
    background_context = clone_tool_use_context_for_fork(
        context,
        fork_label=f"background_subagent:{agent_type}",
        skip_cache_write=True,
    )
    background_cache_safe = CacheSafeParams(
        system_prompt=list(cache_safe.system_prompt),
        user_context=dict(cache_safe.user_context),
        system_context=dict(cache_safe.system_context),
        messages=list(cache_safe.messages),
    )

    # 后台线程使用独立 event loop；当前 CLI 的 submit_message_sync 会关闭本轮 loop，
    # 不能依赖 asyncio.create_task 存活。
    def _worker() -> None:
        """内部处理该方法负责的业务逻辑。"""
        try:
            result = asyncio.run(
                runner.run(
                    agent_type=agent_type,
                    prompt=prompt,
                    parent_context=background_context,
                    parent_cache_safe=background_cache_safe,
                    max_turns=max_turns,
                    agent_id=agent_id,
                    output_file=task.output_file,
                    record_transcript=True,
                )
            )
            task.status = "completed"
            task.output = result.output
            task.transcript_path = result.transcript_path
            registry.update(task)
            enqueue_task_notification(
                context.app_state,
                task_id=task.task_id,
                output_file=task.output_file,
                description=task.description,
                status="completed",
                final_message=result.output,
            )
        except Exception as exc:  # noqa: BLE001 - 后台任务失败要落盘给父 agent 查询。
            task.status = "failed"
            task.error = str(exc)
            registry.update(task)
            enqueue_task_notification(
                context.app_state,
                task_id=task.task_id,
                output_file=task.output_file,
                description=task.description,
                status="failed",
                error=task.error,
            )
        finally:
            registry.update(task)

    thread = threading.Thread(target=_worker, name=f"morty-subagent-{task_id[:8]}", daemon=True)
    thread.start()
    return {
        "status": "async_launched",
        "task_id": task.task_id,
        "agent_id": task.agent_id,
        "agent_type": task.agent_type,
        "description": task.description,
        "output_file": task.output_file,
    }


def _optional_int(value: object) -> int | None:
    """内部处理该方法负责的业务逻辑。"""
    if value is None or value == "":
        return None
    try:
        return max(1, min(8, int(value)))
    except (TypeError, ValueError):
        return None
