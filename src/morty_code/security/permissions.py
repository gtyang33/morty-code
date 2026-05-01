from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from morty_code.types.runtime_state import ToolUseContext


@dataclass(frozen=True)
class PermissionDecision:
    """工具权限决策结果。

    Claude Code 会返回 allow / ask / deny 三态。morty-code 目前没有交互式
    permission prompt，因此 ask 在执行层会被当作拒绝处理。
    """

    behavior: str
    reason: str
    message: str


_MUTATING_TOOLS = {
    "write_file",
    "edit_file",
    "bash",
    "spawn_agent",
}

_SENSITIVE_TOOLS = {
    "bash",
    "spawn_agent",
}


def evaluate_tool_permission(
    tool_name: str,
    tool_input: dict[str, Any],
    context: ToolUseContext,
) -> PermissionDecision:
    """按统一顺序计算工具权限。

    顺序刻意保持 deny 优先：
    1. 显式 deny rule。
    2. bypassPermissions 直接放行到工具自身安全检查。
    3. 显式 allow rule。
    4. mode 约束。
    5. 默认放行。
    """

    del tool_input  # 预留给后续 Bash prefix / path scoped rule。
    denied = _as_string_set(context.app_state.get("denied_tools"))
    allowed = _as_string_set(context.app_state.get("always_allowed_tools"))
    ask = _as_string_set(context.app_state.get("always_ask_tools"))
    mode = str(context.permission_mode or context.app_state.get("permission_mode") or "default")

    if tool_name in denied or "*" in denied:
        return PermissionDecision(
            behavior="deny",
            reason="rule",
            message=f"Tool '{tool_name}' denied by configured permission rule.",
        )
    if mode == "bypassPermissions":
        return PermissionDecision("allow", "mode", "bypassPermissions allows tool execution.")
    if tool_name in allowed or "*" in allowed:
        return PermissionDecision("allow", "rule", f"Tool '{tool_name}' explicitly allowed.")
    if tool_name in ask or "*" in ask:
        return PermissionDecision(
            behavior="ask",
            reason="rule",
            message=f"Tool '{tool_name}' requires approval by configured permission rule.",
        )

    if mode == "plan" or bool(context.app_state.get("plan_mode", False)):
        if tool_name in _MUTATING_TOOLS:
            return PermissionDecision(
                behavior="deny",
                reason="mode",
                message=f"Tool '{tool_name}' is blocked while plan mode is active.",
            )
    if mode == "acceptEdits" and tool_name in _SENSITIVE_TOOLS:
        return PermissionDecision(
            behavior="ask",
            reason="mode",
            message=f"Tool '{tool_name}' requires approval in acceptEdits mode.",
        )
    if mode == "dontAsk" and tool_name not in allowed:
        return PermissionDecision(
            behavior="deny",
            reason="mode",
            message=f"Tool '{tool_name}' is blocked by dontAsk mode.",
        )

    return PermissionDecision("allow", "default", "Tool allowed by default policy.")


def _as_string_set(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, list | tuple | set):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()
