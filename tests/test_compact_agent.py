from __future__ import annotations

import asyncio

from morty_code.api.errors import ModelProviderError
from morty_code.compact.compact_agent import (
    ERROR_MESSAGE_NOT_ENOUGH_MESSAGES,
    CompactAgent,
)
from morty_code.compact.compact_rebuild import (
    clone_retained_messages_for_compact,
    rebuild_post_compact_messages,
)
from morty_code.types.messages import Message


class CompactModel:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.last_messages: list[dict[str, object]] = []
        self.last_system_prompt: list[str] = []

    async def respond(
        self,
        messages,
        system_prompt,
        user_context,
        system_context,
    ) -> Message:
        self.calls += 1
        self.last_messages = messages
        self.last_system_prompt = system_prompt
        return _message("assistant", [{"type": "text", "text": self.text}])


class FailingCompactModel:
    async def respond(
        self,
        messages,
        system_prompt,
        user_context,
        system_context,
    ) -> Message:
        raise ModelProviderError("compact failed")


def _message(message_type: str, content: object, *, uuid: str = "m1") -> Message:
    return Message(
        uuid=uuid,
        timestamp="2026-05-16T00:00:00",
        type=message_type,
        payload={"content": content},
    )


def test_compact_summary_sanitizes_large_tool_result_payloads() -> None:
    """验证 compact 摘要不会重新塞入大工具结果或媒体块原文。"""

    messages = [
        _message(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "<persisted-output>\nSECRET_OUTPUT\n</persisted-output>",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-2",
                    "content": [
                        {"type": "text", "text": "small useful text"},
                        {"type": "image", "source": {"data": "BASE64_IMAGE"}},
                        {"type": "document", "source": {"data": "BASE64_DOC"}},
                        {"type": "tool_reference", "name": "demo"},
                    ],
                },
            ],
        )
    ]

    summary_messages = asyncio.run(CompactAgent().summarize(messages))
    summary = summary_messages[0].payload["summary"]

    assert "SECRET_OUTPUT" not in summary
    assert "BASE64_IMAGE" not in summary
    assert "BASE64_DOC" not in summary
    assert "tool_reference" not in summary
    assert "[persisted tool result]" in summary
    assert "[image]" in summary
    assert "[document]" in summary
    assert "small useful text" in summary


def test_compact_summary_formats_tool_results_for_readability() -> None:
    """验证 compact 摘要里的工具结果不会直接暴露原始 dict。"""

    messages = [
        _message(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-grep",
                    "content": {
                        "command": "grep -rn TOK_INSERT SemanticAnalyzer.java | head -20",
                        "exit_code": 0,
                        "timed_out": False,
                        "stdout": "2394: if (...) TOK_INSERT\n2604: && !(...)",
                        "stderr": "",
                    },
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call-file",
                    "content": {
                        "path": "/repo/src/VMUtil.java",
                        "content": "public class VMUtil {\n  // many lines\n}",
                        "line_count": 437,
                        "truncated": True,
                    },
                },
            ],
        )
    ]

    summary_messages = asyncio.run(CompactAgent().summarize(messages))
    summary = summary_messages[0].payload["summary"]

    assert "call-grep ok: command=`grep -rn TOK_INSERT SemanticAnalyzer.java | head -20` exit=0" in summary
    assert "stdout: 2394: if (...) TOK_INSERT / 2604: && !(...)" in summary
    assert "call-file ok: file=/repo/src/VMUtil.java lines=437 truncated" in summary
    assert "{'command':" not in summary
    assert "'content': 'public class VMUtil" not in summary


def test_compact_summary_skips_reinjected_attachments() -> None:
    """验证 compact 不总结 compact 后本来会重新注入的附件。"""

    messages = [
        Message(
            uuid="attachment-1",
            timestamp="2026-05-16T00:00:00",
            type="attachment",
            payload={
                "attachment_type": "at_mentioned_file",
                "source": "post_compact_reinject",
                "path": "/repo/large.py",
                "content": "SHOULD_NOT_APPEAR",
            },
        )
    ]

    summary_messages = asyncio.run(CompactAgent().summarize(messages))
    summary = summary_messages[0].payload["summary"]

    assert "SHOULD_NOT_APPEAR" not in summary
    assert "post_compact_reinject" not in summary


def test_compact_summary_respects_total_budget_and_keeps_recent_context() -> None:
    """验证 compact 摘要受总预算约束，并优先保留最新上下文。"""

    old_message = _message("assistant", "OLD_CONTEXT " * 200, uuid="old")
    recent_message = _message(
        "assistant",
        "Current work: compact module keeps the newest task details.",
        uuid="recent",
    )

    summary_messages = asyncio.run(
        CompactAgent(max_summary_chars=450).summarize([old_message, recent_message])
    )
    summary = summary_messages[0].payload["summary"]

    assert len(summary) <= 450
    assert "newest task details" in summary
    assert "OLD_CONTEXT" not in summary


def test_rebuild_places_reinjected_attachments_before_retained_tail() -> None:
    """验证 compact 后恢复附件先于 retained tail 注入。"""

    summary = _message("system", "summary", uuid="summary")
    reminder = _message("user", "summary reminder", uuid="reminder")
    attachment = _message("attachment", "file state", uuid="attachment")
    retained = _message("assistant", "recent answer", uuid="retained")

    rebuilt = rebuild_post_compact_messages(
        [summary, reminder],
        [retained],
        [attachment],
    )

    assert [message.uuid for message in rebuilt] == [
        "summary",
        "reminder",
        "attachment",
        "retained",
    ]


def test_clone_retained_messages_for_compact_preserves_tail_after_boundary() -> None:
    """验证 retained tail 写入 transcript 时会变成 boundary 后的新消息。"""

    retained = _message("assistant", [{"type": "text", "text": "recent answer"}], uuid="old-retained")

    [cloned] = clone_retained_messages_for_compact([retained])

    assert cloned.uuid != retained.uuid
    assert cloned.type == retained.type
    assert cloned.payload == retained.payload
    assert cloned.origin == {
        "source": "post_compact_retained",
        "original_uuid": "old-retained",
    }


def test_compact_rejects_empty_message_list() -> None:
    """验证没有可总结消息时 compact 显式失败。"""

    try:
        asyncio.run(CompactAgent().compact_messages([]))
    except ValueError as exc:
        assert str(exc) == ERROR_MESSAGE_NOT_ENOUGH_MESSAGES
    else:
        raise AssertionError("expected compact to reject empty messages")


def test_compact_uses_no_tools_model_summary_and_strips_analysis() -> None:
    """验证 compact 优先使用 no-tools 模型总结并剥离 analysis 草稿。"""

    model = CompactModel(
        "<analysis>draft reasoning</analysis>\n"
        "<summary>\n"
        "1. Primary Request and Intent: 用户要求完善 compact。\n"
        "9. Optional Next Step: 继续实现重试。\n"
        "</summary>"
    )

    summary_messages = asyncio.run(
        CompactAgent(model_client=model).summarize(
            [_message("user", [{"type": "text", "text": "请完善 compact"}])]
        )
    )
    summary = summary_messages[0].payload["summary"]
    prompt = str(model.last_messages[0]["content"])

    assert model.calls == 1
    assert "Do NOT call any tools" in model.last_system_prompt[0]
    assert "draft reasoning" not in summary
    assert "Primary Request and Intent" in summary
    assert "请完善 compact" in prompt


def test_compact_model_summary_falls_back_to_rule_summary_on_failure() -> None:
    """验证模型 compact 失败时回退到确定性规则摘要。"""

    summary_messages = asyncio.run(
        CompactAgent(model_client=FailingCompactModel()).summarize(
            [_message("user", [{"type": "text", "text": "保留这个用户目标"}])]
        )
    )

    assert "保留这个用户目标" in summary_messages[0].payload["summary"]
