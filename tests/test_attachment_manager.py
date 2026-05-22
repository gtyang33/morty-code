from __future__ import annotations

import asyncio

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.types.runtime_state import ContentReplacementState, FileViewState, ToolUseContext


def test_reinjection_summarizes_large_file_views(tmp_path) -> None:
    path = tmp_path / "large.java"
    context = ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="default",
        app_state={},
        read_file_state={
            str(path): FileViewState(
                path=str(path),
                content="class X {}\n" * 1000,
                is_partial_view=True,
                offset=0,
                limit=20000,
            )
        },
        content_replacement_state=ContentReplacementState(),
    )

    attachments = AttachmentManager().collect_reinjection(context, messages=[])

    assert len(attachments) == 1
    content = str(attachments[0].payload["content"])
    assert len(content) < 500
    assert "previously read file view" in content
    assert "class X {}" not in content


def test_plan_mode_attachment_includes_pending_approval(tmp_path) -> None:
    context = ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="plan",
        app_state={
            "plan_mode": True,
            "plans_dir": str(tmp_path / ".morty" / "plans"),
            "session_id": "session-1",
            "pending_plan_approval": {
                "status": "awaiting_user_approval",
                "plan": "1. 修改输入层\n2. 增加测试",
            },
        },
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )

    attachments = asyncio.run(
        AttachmentManager().collect_post_iteration(
            input_text="",
            context=context,
            messages=[],
            queued_commands=[],
        )
    )

    assert len(attachments) == 1
    assert attachments[0].type == "plan_mode"
    assert attachments[0].payload["pending_plan_status"] == "awaiting_user_approval"
    assert attachments[0].payload["pending_plan"] == "1. 修改输入层\n2. 增加测试"
