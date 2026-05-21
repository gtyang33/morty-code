from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
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
    "append_file",
    "multi_edit",
    "move_path",
    "bash",
    "spawn_agent",
}

_PLAN_FILE_WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "append_file",
    "multi_edit",
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
        # Claude Code 的 plan mode 是“只读 + 只能写 plan file”。这允许模型
        # 把最终计划写入专用文件，但仍阻止实现代码、命令和子代理改动系统。
        if tool_name in _MUTATING_TOOLS:
            if tool_name in _PLAN_FILE_WRITE_TOOLS and _is_plan_file_write(tool_input, context):
                return PermissionDecision("allow", "mode", "Plan mode allows writing the active plan file.")
            return PermissionDecision(
                behavior="deny",
                reason="mode",
                message=(
                    f"Tool '{tool_name}' is blocked while plan mode is active; "
                    "only the plan file may be written."
                ),
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
    if tool_name == "bash" and command:
        file_edit_intent = detect_bash_file_edit_intent(command)
        if file_edit_intent:
            return PermissionDecision(
                behavior="ask",
                reason="bash_file_edit_intent",
                message=(
                    "This bash command looks like a file edit. Prefer "
                    f"edit_file/multi_edit/write_file instead ({file_edit_intent})."
                ),
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


def detect_bash_file_edit_intent(command: str) -> str | None:
    """识别 Bash 中容易和结构化编辑工具混淆的文件写入意图。"""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        executable = Path(token).name
        args = tokens[index + 1 :]
        next_token = tokens[index + 1] if index + 1 < len(tokens) else ""
        if executable == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in args):
            return "sed in-place edit"
        if executable == "perl" and any("i" in arg.lstrip("-") for arg in args if arg.startswith("-")):
            return "perl in-place edit"
        if executable in {"python", "python3"} and next_token and not next_token.startswith("-"):
            if Path(next_token).name.endswith(".py"):
                return f"{executable} script"
        if executable == "tee" and any(not arg.startswith("-") for arg in args):
            return "tee file write"
    if _has_unquoted_output_redirection(command):
        return "shell output redirection"
    return None


def _has_unquoted_output_redirection(command: str) -> bool:
    """只在 shell 语法层面识别 > / >>，忽略引号里的文本和 process substitution。"""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char != ">":
            continue
        previous_char = command[index - 1] if index > 0 else ""
        next_char = command[index + 1] if index + 1 < len(command) else ""
        if previous_char.isdigit():
            continue
        if next_char == "(":
            continue
        if previous_char == "&":
            continue
        return True
    return False


def _is_plan_file_write(tool_input: dict[str, Any], context: ToolUseContext) -> bool:
    """判断写文件工具是否只指向当前 plan 文件。"""
    raw_plan_path = context.app_state.get("plan_file_path")
    raw_input_path = tool_input.get("path")
    if not raw_plan_path or not raw_input_path:
        return False
    try:
        plan_path = Path(str(raw_plan_path)).expanduser()
        cwd = Path(str(context.app_state.get("cwd") or ".")).expanduser()
        if not plan_path.is_absolute():
            plan_path = cwd / plan_path
        input_path = Path(str(raw_input_path)).expanduser()
        if not input_path.is_absolute():
            input_path = cwd / input_path
        return input_path.resolve() == plan_path.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _rule_suffix(rule: str | None) -> str:
    """内部处理该方法负责的业务逻辑。"""
    return f" ({rule})" if rule else ""
