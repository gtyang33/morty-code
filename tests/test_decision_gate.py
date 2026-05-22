from __future__ import annotations

from pathlib import Path

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.compact.auto_compact import AutoCompactDecider
from morty_code.input.handle_input import InputDispatcher
from morty_code.input.process_user_input import UserInputProcessor
from morty_code import _decision_gate_mode, _runtime_app_state
from morty_code.runtime.query_engine import QueryEngine
from morty_code.runtime.query_loop import QueryLoopResult
from morty_code.transcript.transcript_store import TranscriptStore
from morty_code.types.runtime_state import CacheSafeParams, ContentReplacementState, ToolUseContext


class StubPromptBuilder:
    async def build_for_context(self, context: ToolUseContext) -> tuple[list[str], dict[str, str], dict[str, str]]:
        return ["system"], {}, {}


class RecordingQueryLoop:
    def __init__(self) -> None:
        self.seen_tools: list[list[str]] = []
        self.seen_message_texts: list[str] = []

    async def run(
        self,
        *,
        messages,
        cache_safe: CacheSafeParams,
        tool_context: ToolUseContext,
        on_new_messages=None,
    ) -> QueryLoopResult:
        self.seen_tools.append(list(tool_context.tools))
        self.seen_message_texts.append("\n".join(str(message.payload.get("content", "")) for message in messages))
        return QueryLoopResult(new_messages=[], metadata_events=[])


class StubPermissionSettings:
    allow: list[str] = []
    deny: list[str] = []
    ask: list[str] = []
    sources: list[str] = []


class StubToolRegistry:
    def api_tool_schemas(self) -> list[dict[str, object]]:
        return []


def _engine(tmp_path: Path, query_loop: RecordingQueryLoop) -> QueryEngine:
    return QueryEngine(
        prompt_builder=StubPromptBuilder(),
        input_dispatcher=InputDispatcher(),
        input_processor=UserInputProcessor(AttachmentManager()),
        query_loop=query_loop,
        transcript_store=TranscriptStore(tmp_path / "session.jsonl", "session"),
        auto_compact_decider=AutoCompactDecider(token_threshold=999999),
    )


def _context() -> ToolUseContext:
    return ToolUseContext(
        tools=["read_file", "write_file", "bash"],
        model="test-model",
        permission_mode="default",
        app_state={"decision_gate": "auto"},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


def test_complex_request_first_turn_asks_for_options_without_tools(tmp_path: Path) -> None:
    query_loop = RecordingQueryLoop()
    context = _context()

    _engine(tmp_path, query_loop).submit_message_sync(
        "完善 MCP 管理界面，做成更优雅的交互体验",
        context,
    )

    assert query_loop.seen_tools == [[]]
    assert "给出 2-3 个可选方案" in query_loop.seen_message_texts[0]
    assert "不要修改文件" in query_loop.seen_message_texts[0]
    assert "不要调用任何工具" in query_loop.seen_message_texts[0]
    assert context.tools == ["read_file", "write_file", "bash"]
    assert context.app_state["decision_gate_pending"]["status"] == "awaiting_choice"


def test_complex_request_keeps_tools_when_enter_plan_mode_is_available(tmp_path: Path) -> None:
    query_loop = RecordingQueryLoop()
    context = _context()
    context.tools.append("enter_plan_mode")

    _engine(tmp_path, query_loop).submit_message_sync(
        "完善 MCP 管理界面，做成更优雅的交互体验",
        context,
    )

    assert query_loop.seen_tools == [["read_file", "write_file", "bash", "enter_plan_mode"]]
    assert "decision_gate_pending" not in context.app_state


def test_user_choice_clears_gate_and_restores_tools(tmp_path: Path) -> None:
    query_loop = RecordingQueryLoop()
    context = _context()
    engine = _engine(tmp_path, query_loop)

    engine.submit_message_sync("重构 MCP 管理和工具展示", context)
    engine.submit_message_sync("选推荐方案，直接实现", context)

    assert query_loop.seen_tools[0] == []
    assert query_loop.seen_tools[1] == ["read_file", "write_file", "bash"]
    assert "decision_gate_pending" not in context.app_state


def test_simple_request_does_not_trigger_decision_gate(tmp_path: Path) -> None:
    query_loop = RecordingQueryLoop()
    context = _context()

    _engine(tmp_path, query_loop).submit_message_sync("现在有哪些工具", context)

    assert query_loop.seen_tools == [["read_file", "write_file", "bash"]]
    assert "decision_gate_pending" not in context.app_state


def test_runtime_app_state_reads_decision_gate_mode_from_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MORTY_DECISION_GATE", "always")

    app_state = _runtime_app_state(
        workspace_root=tmp_path,
        transcript_store=TranscriptStore(tmp_path / "session.jsonl", "session"),
        permission_mode="default",
        permission_settings=StubPermissionSettings(),
        tool_registry=StubToolRegistry(),
    )

    assert app_state["decision_gate"] == "always"


def test_runtime_app_state_prefers_cli_decision_gate_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MORTY_DECISION_GATE", "always")

    app_state = _runtime_app_state(
        workspace_root=tmp_path,
        transcript_store=TranscriptStore(tmp_path / "session.jsonl", "session"),
        permission_mode="default",
        permission_settings=StubPermissionSettings(),
        tool_registry=StubToolRegistry(),
        decision_gate_mode="off",
    )

    assert app_state["decision_gate"] == "off"


def test_decision_gate_mode_rejects_invalid_values(monkeypatch) -> None:
    monkeypatch.setenv("MORTY_DECISION_GATE", "maybe")

    assert _decision_gate_mode(None) == "auto"
    assert _decision_gate_mode("always") == "always"


def test_status_reports_decision_gate_mode_and_pending_state(tmp_path: Path) -> None:
    context = _context()
    context.app_state["decision_gate_pending"] = {"status": "awaiting_choice"}
    processor = UserInputProcessor(AttachmentManager())

    processed = __import__("asyncio").run(
        processor.process(
            command=__import__("morty_code.types.runtime_state", fromlist=["QueuedCommand"]).QueuedCommand(
                value="/status",
                mode="prompt",
            ),
            context=context,
            messages=[],
        )
    )

    content = str(processed.messages[0].payload["content"])
    assert "decision_gate: auto" in content
    assert "decision_gate_pending: awaiting_choice" in content
