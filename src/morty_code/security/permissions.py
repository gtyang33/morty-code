from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from morty_code.security.shell_rules import rule_matches_bash_command, rule_matches_tool
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
    updated_input: dict[str, Any] | None = None


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

    denied = _as_string_set(context.app_state.get("denied_tools"))
    allowed = _as_string_set(context.app_state.get("always_allowed_tools"))
    ask = _as_string_set(context.app_state.get("always_ask_tools"))
    mode = str(context.permission_mode or context.app_state.get("permission_mode") or "default")

    command = str(tool_input.get("command", "")).strip() if tool_name == "bash" else ""
    # Bash 支持内容级规则，例如 Bash(git push:*)。先看内容规则，再看工具名
    # 规则，才能表达“允许 Bash，但某些命令仍 deny/ask”。
    content_deny = _matches_content_rule(denied, tool_name, command)
    if content_deny or _matches_tool_rule(denied, tool_name):
        return PermissionDecision(
            behavior="deny",
            reason="rule",
            message=f"Tool '{tool_name}' denied by configured permission rule{_rule_suffix(content_deny)}.",
        )
    content_ask = _matches_content_rule(ask, tool_name, command)
    if content_ask:
        return PermissionDecision(
            behavior="ask",
            reason="rule",
            message=f"Tool '{tool_name}' requires approval by configured permission rule ({content_ask}).",
        )
    if _matches_tool_rule(ask, tool_name):
        return PermissionDecision(
            behavior="ask",
            reason="rule",
            message=f"Tool '{tool_name}' requires approval by configured permission rule.",
        )
    if mode == "bypassPermissions":
        # bypass 只跳过权限弹窗，不跳过工具自身安全 guard；例如 write_file 仍会
        # 拒绝写 .env，bash 仍会经过危险命令检查。
        return PermissionDecision("allow", "mode", "bypassPermissions allows tool execution.")
    content_allow = _matches_content_rule(allowed, tool_name, command)
    if content_allow or _matches_tool_rule(allowed, tool_name):
        return PermissionDecision("allow", "rule", f"Tool '{tool_name}' explicitly allowed{_rule_suffix(content_allow)}.")

    if mode == "plan" or bool(context.app_state.get("plan_mode", False)):
        # plan mode 是只读规划状态，禁止写文件/执行命令/启动可能改动代码的子代理。
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
    """内部转换为目标表示。"""
    if value is None:
        return set()
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, list | tuple | set):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _matches_tool_rule(rules: set[str], tool_name: str) -> bool:
    """内部判断规则是否匹配输入。"""
    return "*" in rules or tool_name in rules or any(rule_matches_tool(rule, tool_name) for rule in rules)


def _matches_content_rule(rules: set[str], tool_name: str, command: str) -> str | None:
    """内部判断规则是否匹配输入。"""
    if tool_name != "bash" or not command:
        return None
    for rule in sorted(rules):
        if rule_matches_bash_command(rule, command):
            return rule
    return None


def _rule_suffix(rule: str | None) -> str:
    """内部处理该方法负责的业务逻辑。"""
    return f" ({rule})" if rule else ""
