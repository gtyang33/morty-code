from __future__ import annotations

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
