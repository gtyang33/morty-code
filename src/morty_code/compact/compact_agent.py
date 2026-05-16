from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from morty_code.types.messages import Message


class CompactAgent:
    """规则版 compact agent。

    真实 Claude Code 会用 no-tools 子 agent 做总结；这里先用确定性摘要保持
    Python MVP 无外部依赖，同时保留 compact boundary 的状态迁移语义。
    """

    async def summarize(self, messages: list[Message], trigger: str = "auto") -> list[Message]:
        """压缩并总结上下文内容。"""
        summary = _build_structured_summary(messages)
        now = datetime.utcnow().isoformat()
        return [
            Message(
                uuid=str(uuid4()),
                timestamp=now,
                type="system",
                payload={
                    "subtype": "compact_boundary",
                    "content": "Conversation compacted",
                    "trigger": trigger,
                    "summary": summary,
                    "source_message_count": len(messages),
                    "logical_parent_uuid": messages[-1].uuid if messages else None,
                },
            )
            ,
            Message(
                uuid=str(uuid4()),
                timestamp=now,
                type="user",
                payload={
                    "content": (
                        "<system-reminder>\n"
                        "Conversation compacted. Continue from this summary:\n"
                        f"{summary}\n"
                        "</system-reminder>"
                    ),
                    "is_compact_summary": True,
                },
                is_meta=True,
            ),
        ]

    async def compact_messages(
        self,
        messages: list[Message],
        trigger: str = "auto",
    ) -> tuple[list[Message], list[Message]]:
        """处理该方法负责的业务逻辑。"""
        summary_messages = await self.summarize(messages, trigger=trigger)
        # 保留尾部最近消息，作为 compact 后可继续执行的 retained tail。
        return summary_messages, _select_pair_safe_tail(messages, limit=8)


def _build_structured_summary(messages: list[Message]) -> str:
    """生成可续跑摘要，避免 compact 后只剩模糊聊天片段。"""

    user_intents: list[str] = []
    assistant_notes: list[str] = []
    tool_uses: list[str] = []
    tool_results: list[str] = []
    attachments: list[str] = []
    for message in messages[-40:]:
        content = message.payload.get("content")
        if message.type == "user":
            user_intents.extend(_extract_user_visible_text(content))
            tool_results.extend(_extract_tool_results(content))
        elif message.type == "assistant":
            assistant_notes.extend(_extract_assistant_text(content))
            tool_uses.extend(_extract_tool_uses(content))
        elif message.type == "attachment":
            attachment_type = str(message.payload.get("attachment_type", "unknown"))
            source = str(message.payload.get("path") or message.payload.get("source") or "")
            attachments.append(f"- {attachment_type}: {source}".rstrip())

    sections = [
        ("用户目标和最新输入", user_intents[-8:]),
        ("助手已完成的关键动作", assistant_notes[-8:]),
        ("最近工具调用", tool_uses[-12:]),
        ("最近工具结果", tool_results[-12:]),
        ("仍需保留的附件状态", attachments[-12:]),
    ]
    rendered: list[str] = []
    for title, items in sections:
        rendered.append(f"## {title}")
        if items:
            rendered.extend(f"- {_truncate(item, 500)}" for item in items if item.strip())
        else:
            rendered.append("- 无")
    return "\n".join(rendered)


def _extract_user_visible_text(content: object) -> list[str]:
    """内部提取后续流程需要的信息。"""
    if isinstance(content, str):
        return [content.strip()] if content.strip() else []
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                texts.append(text)
    return texts


def _extract_assistant_text(content: object) -> list[str]:
    """内部提取后续流程需要的信息。"""
    if isinstance(content, str):
        return [content.strip()] if content.strip() else []
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                texts.append(text)
    return texts


def _extract_tool_uses(content: object) -> list[str]:
    """内部提取后续流程需要的信息。"""
    if not isinstance(content, list):
        return []
    calls: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        calls.append(
            f"{block.get('name', 'tool')} id={block.get('id', '')} input={_truncate(str(block.get('input', '')), 300)}"
        )
    return calls


def _extract_tool_results(content: object) -> list[str]:
    """内部提取后续流程需要的信息。"""
    if not isinstance(content, list):
        return []
    results: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        result_content = block.get("content")
        status = "error" if block.get("is_error") else "ok"
        results.append(
            f"{block.get('tool_use_id', '')} {status}: {_truncate(_stringify_content(result_content), 400)}"
        )
    return results


def _stringify_content(content: object) -> str:
    """内部处理该方法负责的业务逻辑。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict):
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _truncate(text: str, max_chars: int) -> str:
    """内部处理该方法负责的业务逻辑。"""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def _select_pair_safe_tail(messages: list[Message], limit: int) -> list[Message]:
    """选择 retained tail，避免旧 boundary 和明显孤儿 tool_result 进入 compact 后上下文。"""

    tail = [
        message
        for message in messages[-limit:]
        if not (
            message.type == "system"
            and message.payload.get("subtype") == "compact_boundary"
        )
    ]
    while tail and _is_tool_result_only_user(tail[0]):
        tail = tail[1:]
    return tail


def _is_tool_result_only_user(message: Message) -> bool:
    """内部判断当前对象是否满足条件。"""
    if message.type != "user":
        return False
    content = message.payload.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)
    )
