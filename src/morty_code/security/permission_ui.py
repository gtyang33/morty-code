from __future__ import annotations

import json

from morty_code.ui import Choice, InteractionRequest


PERMISSION_CHOICES = (
    Choice(
        value="allow",
        label="Allow",
        description="允许本次工具调用",
        aliases=("1", "a", "y", "yes", "ok", "允许", "批准"),
    ),
    Choice(
        value="deny",
        label="Deny",
        description="拒绝本次工具调用",
        aliases=("2", "d", "n", "no", "拒绝", "取消"),
    ),
)


def build_permission_request(request: dict[str, object]) -> InteractionRequest:
    """把 ToolRunner 权限请求转换成通用交互请求。"""

    tool_name = str(request.get("tool_name") or "unknown")
    message = str(request.get("message") or "Tool permission is required.")
    tool_input = request.get("input")
    details = [
        f"Tool: {tool_name}",
        f"Reason: {request.get('decision_reason') or 'policy'}",
        "",
        message,
        "",
        "Input:",
        _format_input(tool_input),
    ]
    return InteractionRequest(
        title="Tool permission required",
        message="Review this tool call before it runs.",
        details="\n".join(details).strip(),
        choices=PERMISSION_CHOICES,
        default="deny",
        prompt="Permission [1 allow, 2 deny, Enter deny] › ",
    )


def _format_input(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)
