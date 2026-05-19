from __future__ import annotations

import html
import threading
from uuid import uuid4

from morty_code.types.runtime_state import QueuedCommand


_QUEUE_KEY = "task_notification_queue"
_LOCK = threading.Lock()


def enqueue_task_notification(
    app_state: dict[str, object],
    *,
    task_id: str,
    output_file: str,
    description: str,
    status: str,
    final_message: str | None = None,
    error: str | None = None,
    tool_use_id: str | None = None,
) -> None:
    """把后台 agent 完成事件放进父会话队列，后续作为模型可见输入处理。"""

    command = QueuedCommand(
        value=_format_task_notification(
            task_id=task_id,
            output_file=output_file,
            description=description,
            status=status,
            final_message=final_message,
            error=error,
            tool_use_id=tool_use_id,
        ),
        mode="task-notification",
        skip_slash_commands=True,
        is_meta=True,
        uuid=str(uuid4()),
        origin={"source": "task_notification", "task_id": task_id},
    )
    with _LOCK:
        queue = app_state.setdefault(_QUEUE_KEY, [])
        if isinstance(queue, list):
            queue.append(command)
        else:
            app_state[_QUEUE_KEY] = [command]


def drain_task_notifications(app_state: dict[str, object]) -> list[QueuedCommand]:
    """取出待注入模型上下文的后台任务通知。"""

    with _LOCK:
        raw_queue = app_state.get(_QUEUE_KEY)
        app_state[_QUEUE_KEY] = []
    if not isinstance(raw_queue, list):
        return []
    commands: list[QueuedCommand] = []
    for item in raw_queue:
        if isinstance(item, QueuedCommand):
            commands.append(item)
        elif isinstance(item, dict):
            commands.append(
                QueuedCommand(
                    value=str(item.get("value") or ""),
                    mode=str(item.get("mode") or "task-notification"),
                    skip_slash_commands=True,
                    is_meta=True,
                    origin={"source": "task_notification"},
                )
            )
    return commands


def has_task_notifications(app_state: dict[str, object]) -> bool:
    """判断当前会话是否有待处理的后台任务通知。"""

    queue = app_state.get(_QUEUE_KEY)
    return isinstance(queue, list) and bool(queue)


def _format_task_notification(
    *,
    task_id: str,
    output_file: str,
    description: str,
    status: str,
    final_message: str | None,
    error: str | None,
    tool_use_id: str | None,
) -> str:
    """生成 Claude 风格 task-notification XML。"""

    summary = _summary(description=description, status=status, error=error)
    lines = [
        "<task-notification>",
        f"<task_id>{_escape(task_id)}</task_id>",
    ]
    if tool_use_id:
        lines.append(f"<tool_use_id>{_escape(tool_use_id)}</tool_use_id>")
    lines.extend(
        [
            f"<output_file>{_escape(output_file)}</output_file>",
            f"<status>{_escape(status)}</status>",
            f"<summary>{_escape(summary)}</summary>",
        ]
    )
    if final_message:
        lines.append(f"<result>{_escape(final_message)}</result>")
    elif error:
        lines.append(f"<error>{_escape(error)}</error>")
    lines.append("</task-notification>")
    return "\n".join(lines)


def _summary(*, description: str, status: str, error: str | None) -> str:
    """格式化通知摘要，和 Claude 的 completed/failed/killed 语义保持一致。"""

    if status == "completed":
        return f'Agent "{description}" completed'
    if status == "failed":
        return f'Agent "{description}" failed: {error or "Unknown error"}'
    if status in {"killed", "interrupted"}:
        return f'Agent "{description}" was stopped'
    return f'Agent "{description}" is {status}'


def _escape(value: str) -> str:
    """XML 文本转义，避免 agent 输出破坏通知结构。"""

    return html.escape(value, quote=True)
