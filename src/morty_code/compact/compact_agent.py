from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from morty_code.tools.tool_result_formatter import format_tool_result_summary
from morty_code.types.messages import Message


ERROR_MESSAGE_NOT_ENOUGH_MESSAGES = "Not enough messages to compact."

_NO_TOOLS_COMPACT_SYSTEM_PROMPT = (
    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools. "
    "Tool calls will be rejected and will waste the compact turn. "
    "Write an <analysis> block followed by a <summary> block."
)


class CompactModelClient(Protocol):
    async def respond(
        self,
        messages: list[dict[str, object]],
        system_prompt: list[str],
        user_context: dict[str, str],
        system_context: dict[str, str],
    ) -> Message:
        """使用模型生成 compact 摘要。"""
        ...


class CompactAgent:
    """规则版 compact agent。

    真实 Claude Code 会用 no-tools 子 agent 做总结；这里先用确定性摘要保持
    Python MVP 无外部依赖，同时保留 compact boundary 的状态迁移语义。
    """

    def __init__(
        self,
        max_summary_chars: int = 12000,
        model_client: CompactModelClient | None = None,
        max_compact_prompt_chars: int = 60000,
    ) -> None:
        """初始化 compact 摘要的总字符预算。"""
        self.max_summary_chars = max_summary_chars
        self.model_client = model_client
        self.max_compact_prompt_chars = max_compact_prompt_chars

    async def summarize(self, messages: list[Message], trigger: str = "auto") -> list[Message]:
        """压缩并总结上下文内容。"""
        summary = await self._summarize_text(messages)
        now = datetime.now(UTC).isoformat()
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

    async def _summarize_text(self, messages: list[Message]) -> str:
        """优先使用 no-tools 模型总结，失败时回退到规则摘要。"""
        if self.model_client is None:
            return _build_structured_summary(messages, max_summary_chars=self.max_summary_chars)
        try:
            response = await self.model_client.respond(
                messages=[
                    {
                        "role": "user",
                        "content": _build_compact_prompt(
                            messages,
                            max_chars=self.max_compact_prompt_chars,
                        ),
                    }
                ],
                system_prompt=[_NO_TOOLS_COMPACT_SYSTEM_PROMPT],
                user_context={},
                system_context={},
            )
            summary = _extract_model_summary(response)
            if summary:
                return _truncate(summary, self.max_summary_chars)
        except Exception:
            # compact 是上下文维护动作，模型总结失败不能打断主对话。
            pass
        return _build_structured_summary(messages, max_summary_chars=self.max_summary_chars)

    async def compact_messages(
        self,
        messages: list[Message],
        trigger: str = "auto",
    ) -> tuple[list[Message], list[Message]]:
        """处理该方法负责的业务逻辑。"""
        if not messages:
            raise ValueError(ERROR_MESSAGE_NOT_ENOUGH_MESSAGES)
        summary_messages = await self.summarize(messages, trigger=trigger)
        # 保留尾部最近消息，作为 compact 后可继续执行的 retained tail。
        return summary_messages, _select_pair_safe_tail(messages, limit=8)


def _build_structured_summary(messages: list[Message], max_summary_chars: int = 12000) -> str:
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
            if message.payload.get("source") == "post_compact_reinject":
                # compact 后会重新注入的附件不再进入摘要，避免重复放大上下文。
                continue
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
    return _fit_summary_budget(rendered, max_summary_chars)


def _build_compact_prompt(messages: list[Message], max_chars: int) -> str:
    """构建 Claude Code 风格的 compact 请求，并按预算保留最新消息。"""
    header = (
        "Your task is to create a detailed summary of the conversation so far. "
        "Capture the user's explicit requests, important technical decisions, "
        "files changed or inspected, errors and fixes, pending tasks, current work, "
        "and the next step if it is directly implied by the latest request. "
        "Return an <analysis> block followed by a <summary> block.\n\n"
        "Sanitized recent conversation:\n"
    )
    entries = _render_sanitized_messages(messages[-80:])
    return _fit_prompt_budget(header, entries, max_chars)


def _render_sanitized_messages(messages: list[Message]) -> list[str]:
    """把消息转成 compact 模型可读的净化文本，避免媒体和大结果进入请求。"""
    rendered: list[str] = []
    for message in messages:
        if message.type == "attachment" and message.payload.get("source") == "post_compact_reinject":
            continue
        content = message.payload.get("content")
        if message.type == "user":
            parts = [*_extract_user_visible_text(content), *_extract_tool_results(content)]
        elif message.type == "assistant":
            parts = [*_extract_assistant_text(content), *_extract_tool_uses(content)]
        elif message.type == "attachment":
            attachment_type = str(message.payload.get("attachment_type", "unknown"))
            source = str(message.payload.get("path") or message.payload.get("source") or "")
            parts = [f"attachment {attachment_type}: {source}".rstrip()]
        else:
            parts = []
        if parts:
            rendered.append(f"{message.type} {message.uuid}: " + "\n".join(_truncate(part, 1000) for part in parts))
    return rendered


def _fit_prompt_budget(header: str, entries: list[str], max_chars: int) -> str:
    """按 compact prompt 预算从后往前保留消息。"""
    if len(header) >= max_chars:
        return header[:max_chars]
    kept: list[str] = []
    for entry in reversed(entries):
        candidate = header + "\n\n".join([entry, *kept])
        if len(candidate) <= max_chars:
            kept = [entry, *kept]
            continue
        if not kept:
            remaining = max_chars - len(header)
            kept = [entry[:remaining]]
        break
    return (header + "\n\n".join(kept))[:max_chars]


def _extract_model_summary(response: Message) -> str:
    """从模型 compact 响应中剥离 analysis，只保留 summary 正文。"""
    text = "\n".join(_extract_assistant_text(response.payload.get("content")))
    if not text.strip():
        return ""
    summary_match = re.search(r"<summary>\s*(.*?)\s*</summary>", text, flags=re.DOTALL | re.IGNORECASE)
    if summary_match:
        return summary_match.group(1).strip()
    return re.sub(r"<analysis>.*?</analysis>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


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
            f"{block.get('tool_use_id', '')} {status}: {format_tool_result_summary(result_content)}"
        )
    return results


def _stringify_content(content: object) -> str:
    """内部处理该方法负责的业务逻辑。"""
    if isinstance(content, str):
        if content.lstrip().startswith("<persisted-output>"):
            return "[persisted tool result]"
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict) and item.get("type") == "image":
                parts.append("[image]")
            elif isinstance(item, dict) and item.get("type") == "document":
                parts.append("[document]")
            elif isinstance(item, dict) and item.get("type") == "tool_reference":
                parts.append("[tool reference removed]")
            elif isinstance(item, dict):
                parts.append("[structured tool content]")
        return "\n".join(parts)
    return str(content)


def _fit_summary_budget(lines: list[str], max_chars: int) -> str:
    """按总字符预算裁剪摘要，优先丢弃较早的条目并保留章节骨架。"""
    kept = list(lines)
    rendered = "\n".join(kept)
    if len(rendered) <= max_chars:
        return rendered
    while len(rendered) > max_chars:
        removable_index = next(
            (
                index
                for index, line in enumerate(kept)
                if line.startswith("- ") and line != "- 无"
            ),
            None,
        )
        if removable_index is None:
            break
        del kept[removable_index]
        rendered = "\n".join(kept)
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max(0, max_chars - 3)] + "..."


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
