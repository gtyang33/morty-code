from __future__ import annotations

import asyncio
from pathlib import Path

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.input.process_user_input import UserInputProcessor
from morty_code.types.runtime_state import ContentReplacementState, QueuedCommand, ToolUseContext


def make_context(tmp_path: Path, *, mode: str = "default") -> ToolUseContext:
    return ToolUseContext(
        tools=["read_file", "write_file", "edit_file", "bash"],
        model="test-model",
        permission_mode=mode,
        app_state={
            "cwd": str(tmp_path),
            "plans_dir": str(tmp_path / ".morty" / "plans"),
            "session_id": "session-1",
            "permission_mode": mode,
        },
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


def test_plan_with_prompt_enters_plan_mode_and_queries_model(tmp_path: Path) -> None:
    context = make_context(tmp_path, mode="acceptEdits")
    processor = UserInputProcessor(AttachmentManager())

    processed = asyncio.run(
        processor.process(
            QueuedCommand(value="/plan 调研 plan mode 并实现", mode="prompt"),
            context,
            [],
        )
    )

    assert processed.should_query is True
    assert context.permission_mode == "plan"
    assert context.app_state["permission_mode"] == "plan"
    assert context.app_state["pre_plan_mode"] == "acceptEdits"
    assert context.app_state["plan_mode"] is True
    prompt = str(processed.messages[-1].payload["content"])
    assert "调研 plan mode 并实现" in prompt
    assert "ask the user whether to save it" in prompt
    assert "/plan-save" not in prompt
    assert Path(str(context.app_state["plan_file_path"])).parent.exists()
    assert not Path(str(context.app_state["plan_file_path"])).exists()


def test_auto_exits_plan_mode_and_restores_previous_mode(tmp_path: Path) -> None:
    context = make_context(tmp_path, mode="acceptEdits")
    processor = UserInputProcessor(AttachmentManager())

    asyncio.run(
        processor.process(
            QueuedCommand(value="/plan 实现功能", mode="prompt"),
            context,
            [],
        )
    )
    plan_path = Path(str(context.app_state["plan_file_path"]))
    plan_path.write_text("## Plan\n- change files\n", encoding="utf-8")

    processed = asyncio.run(
        processor.process(
            QueuedCommand(value="/auto", mode="prompt"),
            context,
            [],
        )
    )

    assert processed.should_query is False
    assert context.permission_mode == "acceptEdits"
    assert context.app_state["permission_mode"] == "acceptEdits"
    assert context.app_state["plan_mode"] is False
    assert context.app_state["pre_plan_mode"] is None
    assert context.app_state["approved_plan"] == "## Plan\n- change files"
    assert context.app_state["needs_plan_mode_exit_attachment"] is True


def test_auto_can_exit_plan_mode_without_saved_plan_file(tmp_path: Path) -> None:
    context = make_context(tmp_path, mode="acceptEdits")
    processor = UserInputProcessor(AttachmentManager())

    asyncio.run(
        processor.process(
            QueuedCommand(value="/plan 实现功能", mode="prompt"),
            context,
            [],
        )
    )

    processed = asyncio.run(
        processor.process(
            QueuedCommand(value="/auto", mode="prompt"),
            context,
            [],
        )
    )

    assert processed.should_query is False
    assert context.permission_mode == "acceptEdits"
    assert context.app_state["plan_mode"] is False
    assert context.app_state["approved_plan"] == ""
    assert "No plan file was saved" in str(processed.messages[0].payload["content"])


def test_plan_save_command_is_not_registered(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    processor = UserInputProcessor(AttachmentManager())

    processed = asyncio.run(
        processor.process(
            QueuedCommand(value="/help", mode="prompt"),
            context,
            [],
        )
    )

    assert "/plan-save" not in str(processed.messages[0].payload["content"])
