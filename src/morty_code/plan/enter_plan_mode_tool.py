from __future__ import annotations

from morty_code.plan.plan_store import PlanStore
from morty_code.tools.tool_registry import ToolRegistry, ToolSpec
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


ENTER_PLAN_MODE_TOOL_NAME = "enter_plan_mode"
EXIT_PLAN_MODE_TOOL_NAME = "exit_plan_mode"


def register_enter_plan_mode_tool(registry: ToolRegistry) -> None:
    """注册模型可主动调用的计划模式工具。"""

    if registry.find(ENTER_PLAN_MODE_TOOL_NAME) is not None:
        _register_exit_plan_mode_tool(registry)
        return

    async def enter_plan_mode(
        args: dict[str, object],
        context: ToolUseContext,
        _cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        """把当前会话切换到 plan mode，并向模型返回下一步约束。"""

        reason = str(args.get("reason") or "").strip()
        _activate_plan_mode(context)
        plan_path = str(context.app_state["plan_file_path"])
        guidance = [
            "Entered plan mode.",
            "Explore the request and compare viable approaches before implementation.",
            "Ask the user to choose or approve the plan when meaningful tradeoffs exist.",
            "Do not write or edit implementation files until the user approves the plan.",
            f"If the user wants the plan saved, write only this plan file: {plan_path}.",
        ]
        if reason:
            guidance.append(f"Reason: {reason}")
        return {
            "status": "entered_plan_mode",
            "plan_file_path": plan_path,
            "instructions": "\n".join(guidance),
        }

    registry.register(
        ToolSpec(
            name=ENTER_PLAN_MODE_TOOL_NAME,
            description="Enter plan mode before implementing a complex or ambiguous request.",
            prompt=(
                "Use this tool proactively when you are about to start a non-trivial implementation task, "
                "especially for new features, refactors, architecture decisions, multi-file changes, unclear "
                "requirements, or work where the user may need to choose between approaches.\n\n"
                "After calling this tool, research and present a concrete plan or 2-3 options with tradeoffs. "
                "Do not edit files, run mutating commands, spawn agents, or implement code until the user approves "
                "the plan. In short: do not edit files before approval. You may use read-only exploration tools "
                "while in plan mode."
            ),
            handler=enter_plan_mode,
            needs_context=True,
            input_schema={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Short reason why this request needs planning before implementation.",
                    }
                },
            },
        )
    )
    _register_exit_plan_mode_tool(registry)


def _register_exit_plan_mode_tool(registry: ToolRegistry) -> None:
    """注册计划提交工具；它只请求批准，不直接恢复执行权限。"""

    if registry.find(EXIT_PLAN_MODE_TOOL_NAME) is not None:
        return

    async def exit_plan_mode(
        args: dict[str, object],
        context: ToolUseContext,
        _cache_safe: CacheSafeParams,
    ) -> dict[str, object]:
        """提交计划并等待用户批准，模拟 Claude 的 ExitPlanMode 语义。"""

        if str(context.permission_mode or context.app_state.get("permission_mode") or "") != "plan":
            return {
                "status": "not_in_plan_mode",
                "message": "exit_plan_mode can only be used while plan mode is active.",
            }
        plan = str(args.get("plan") or "").strip()
        if not plan:
            raise ValueError("plan is required")
        # 这里不直接退出 plan mode。真正的权限恢复必须来自用户批准，
        # 否则模型可以绕过 plan mode 的写入保护。
        context.app_state["pending_plan_approval"] = {
            "status": "awaiting_user_approval",
            "plan": plan,
        }
        return {
            "status": "awaiting_user_approval",
            "message": (
                "Awaiting user approval for the implementation plan. "
                "Do not implement yet. Ask the user to approve, modify, or reject the plan."
            ),
            "plan": plan,
        }

    registry.register(
        ToolSpec(
            name=EXIT_PLAN_MODE_TOOL_NAME,
            description="Request user approval for a completed plan before leaving plan mode.",
            prompt=(
                "Use this tool when the implementation plan is ready and you need the user to approve it before "
                "making changes. Provide the concrete plan in the `plan` field. This tool does not implement the "
                "plan and does not grant write permission by itself; after calling it, wait for the user to approve "
                "or request changes."
            ),
            handler=exit_plan_mode,
            needs_context=True,
            input_schema={
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "string",
                        "description": "Concrete implementation plan to present for user approval.",
                    }
                },
                "required": ["plan"],
            },
        )
    )


def _activate_plan_mode(context: ToolUseContext) -> None:
    """复用 /plan 的状态协议，让工具进入和命令进入保持一致。"""

    current_mode = str(context.permission_mode or context.app_state.get("permission_mode") or "default")
    plan_store = PlanStore.from_app_state(context.app_state)
    plan_path = plan_store.ensure()
    if current_mode != "plan":
        context.app_state["pre_plan_mode"] = current_mode
    elif "pre_plan_mode" not in context.app_state:
        context.app_state["pre_plan_mode"] = "default"
    context.permission_mode = "plan"
    context.app_state["permission_mode"] = "plan"
    context.app_state["plan_mode"] = True
    context.app_state["plan_file_path"] = str(plan_path)
    context.app_state["approved_plan"] = None
    context.app_state["last_plan_mode_attachment_turn"] = 0
