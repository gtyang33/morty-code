from __future__ import annotations

import asyncio
from pathlib import Path

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.input.process_user_input import UserInputProcessor
from morty_code.runtime.query_loop import QueryLoop
from morty_code.tools.tool_registry import ToolRegistry
from morty_code.tools.tool_runner import ToolRunner
from morty_code.types.messages import Message
from morty_code.types.runtime_state import (
    CacheSafeParams,
    ContentReplacementState,
    QueuedCommand,
    ToolUseContext,
)


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


def test_direct_implementation_request_exits_plan_mode_and_queries_model(tmp_path: Path) -> None:
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
    plan_path.write_text("## Plan\n- implement files\n", encoding="utf-8")

    processed = asyncio.run(
        processor.process(
            QueuedCommand(value="直接实现", mode="prompt"),
            context,
            [],
        )
    )

    assert processed.should_query is True
    assert context.permission_mode == "acceptEdits"
    assert context.app_state["permission_mode"] == "acceptEdits"
    assert context.app_state["plan_mode"] is False
    assert context.app_state["approved_plan"] == "## Plan\n- implement files"
    assert context.app_state["needs_plan_mode_exit_attachment"] is True
    prompt = str(processed.messages[-1].payload["content"])
    assert "Plan approved" in prompt
    assert "直接实现" in prompt


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


def make_cache() -> CacheSafeParams:
    return CacheSafeParams(system_prompt=[], user_context={}, system_context={}, messages=[])


def make_tool_message(name: str, payload: dict[str, object] | None = None) -> Message:
    return Message(
        uuid="assistant-1",
        timestamp="2026-05-21T00:00:00",
        type="assistant",
        payload={
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": name,
                    "input": payload or {},
                }
            ]
        },
    )


def test_query_loop_registers_enter_plan_mode_tool() -> None:
    registry = ToolRegistry([])

    QueryLoop(model_client=object(), tool_runner=ToolRunner(registry))

    schema = registry.api_tool_schemas({"enter_plan_mode"})[0]["function"]
    assert schema["name"] == "enter_plan_mode"
    assert "non-trivial implementation" in str(schema["description"])
    assert "do not edit files" in str(schema["description"])
    exit_schema = registry.api_tool_schemas({"exit_plan_mode"})[0]["function"]
    assert exit_schema["name"] == "exit_plan_mode"
    assert "Use this tool when the implementation plan is ready" in str(exit_schema["description"])


def test_enter_plan_mode_tool_switches_to_plan_mode(tmp_path: Path) -> None:
    registry = ToolRegistry([])
    QueryLoop(model_client=object(), tool_runner=ToolRunner(registry))
    context = make_context(tmp_path, mode="acceptEdits")
    context.tools = registry.list_names()

    result_messages = asyncio.run(
        ToolRunner(registry).run(
            make_tool_message(
                "enter_plan_mode",
                {"reason": "需要先比较两种 MCP 管理界面方案"},
            ),
            context,
            make_cache(),
        )
    )

    tool_result = result_messages[0].payload["content"][0]
    assert tool_result["is_error"] is False
    assert context.permission_mode == "plan"
    assert context.app_state["permission_mode"] == "plan"
    assert context.app_state["pre_plan_mode"] == "acceptEdits"
    assert context.app_state["plan_mode"] is True
    assert Path(str(context.app_state["plan_file_path"])).parent.exists()
    assert "Entered plan mode" in str(tool_result["content"])
    assert "Do not write or edit implementation files" in str(tool_result["content"])


def test_exit_plan_mode_tool_requests_user_approval_without_leaving_plan_mode(tmp_path: Path) -> None:
    registry = ToolRegistry([])
    QueryLoop(model_client=object(), tool_runner=ToolRunner(registry))
    context = make_context(tmp_path, mode="acceptEdits")
    context.tools = registry.list_names()
    asyncio.run(
        ToolRunner(registry).run(
            make_tool_message("enter_plan_mode", {"reason": "复杂改造"}),
            context,
            make_cache(),
        )
    )

    result_messages = asyncio.run(
        ToolRunner(registry).run(
            make_tool_message(
                "exit_plan_mode",
                {"plan": "1. 修改输入层\n2. 增加测试\n3. 验证全量测试"},
            ),
            context,
            make_cache(),
        )
    )

    tool_result = result_messages[0].payload["content"][0]
    assert tool_result["is_error"] is False
    assert context.permission_mode == "plan"
    assert context.app_state["plan_mode"] is True
    assert context.app_state["pending_plan_approval"]["status"] == "awaiting_user_approval"
    assert context.app_state["pending_plan_approval"]["plan"] == "1. 修改输入层\n2. 增加测试\n3. 验证全量测试"
    assert "Awaiting user approval" in str(tool_result["content"])
    assert "Do not implement yet" in str(tool_result["content"])


def test_approval_after_exit_plan_mode_uses_pending_plan(tmp_path: Path) -> None:
    registry = ToolRegistry([])
    QueryLoop(model_client=object(), tool_runner=ToolRunner(registry))
    context = make_context(tmp_path, mode="acceptEdits")
    context.tools = registry.list_names()
    processor = UserInputProcessor(AttachmentManager())
    plan = "1. 修改输入层\n2. 增加测试\n3. 验证全量测试"
    asyncio.run(
        ToolRunner(registry).run(
            make_tool_message("enter_plan_mode", {"reason": "复杂改造"}),
            context,
            make_cache(),
        )
    )
    asyncio.run(
        ToolRunner(registry).run(
            make_tool_message("exit_plan_mode", {"plan": plan}),
            context,
            make_cache(),
        )
    )

    processed = asyncio.run(
        processor.process(
            QueuedCommand(value="批准，直接实现", mode="prompt"),
            context,
            [],
        )
    )

    assert processed.should_query is True
    assert context.permission_mode == "acceptEdits"
    assert context.app_state["plan_mode"] is False
    assert context.app_state["approved_plan"] == plan
    assert "pending_plan_approval" not in context.app_state


def test_status_reports_pending_plan_approval(tmp_path: Path) -> None:
    context = make_context(tmp_path, mode="plan")
    context.app_state["plan_mode"] = True
    context.app_state["pending_plan_approval"] = {
        "status": "awaiting_user_approval",
        "plan": "1. 修改输入层",
    }
    processor = UserInputProcessor(AttachmentManager())

    processed = asyncio.run(
        processor.process(
            QueuedCommand(value="/status", mode="prompt"),
            context,
            [],
        )
    )

    content = str(processed.messages[0].payload["content"])
    assert "pending_plan_approval: awaiting_user_approval" in content
