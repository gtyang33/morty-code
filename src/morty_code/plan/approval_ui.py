from __future__ import annotations

from morty_code.types.runtime_state import ToolUseContext
from morty_code.ui import Choice, InteractionRequest


PLAN_APPROVAL_CHOICES = (
    Choice(
        value="approve",
        label="Approve and implement",
        description="恢复执行权限并立即按计划实现",
        aliases=("1", "a", "ok", "yes", "批准", "直接实现"),
    ),
    Choice(
        value="changes",
        label="Request changes",
        description="保持 plan mode，把修改意见交给模型重写计划",
        aliases=("2", "c", "change", "changes", "修改", "调整"),
    ),
    Choice(
        value="reject",
        label="Reject plan",
        description="清除当前待批准计划",
        aliases=("3", "r", "no", "reject", "取消", "拒绝"),
    ),
)


def pending_plan_approval(tool_context: ToolUseContext) -> dict[str, object] | None:
    """返回当前等待用户批准的计划状态。"""

    pending = tool_context.app_state.get("pending_plan_approval")
    return pending if isinstance(pending, dict) else None


def build_plan_approval_request(tool_context: ToolUseContext) -> InteractionRequest:
    """构建计划审批交互请求。"""

    pending = pending_plan_approval(tool_context) or {}
    plan = str(pending.get("plan") or "").strip() or "No plan content available."
    return InteractionRequest(
        title="Implementation plan ready",
        message=(
            "Review the full plan below. Choose an action with ↑/↓ and press Enter. "
            "No implementation will run until you approve."
        ),
        details=plan,
        choices=PLAN_APPROVAL_CHOICES,
        default="approve",
        prompt="Plan action [1 approve, 2 changes, 3 reject, Enter later] › ",
    )


def format_pending_plan(tool_context: ToolUseContext) -> str:
    """渲染完整待批准计划，供日志或非交互场景展示。"""

    return build_plan_approval_request(tool_context).details
