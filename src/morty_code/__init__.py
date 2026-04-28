from __future__ import annotations

from dataclasses import asdict

from morty_code.api.model_client import EchoModelClient
from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.input.handle_input import InputDispatcher
from morty_code.input.process_user_input import UserInputProcessor
from morty_code.prompt.prompt_builder import PromptBuilder
from morty_code.prompt.prompt_sections import PromptSectionRegistry
from morty_code.runtime.query_engine import QueryEngine
from morty_code.runtime.query_loop import QueryLoop
from morty_code.tools.tool_runner import NullToolRunner
from morty_code.transcript.transcript_store import TranscriptStore
from morty_code.types.runtime_state import ContentReplacementState, ToolUseContext


def main() -> None:
    """最小 CLI 入口，用于手动验证 runtime 主链路是否能跑通。"""
    engine = QueryEngine(
        prompt_builder=PromptBuilder(PromptSectionRegistry()),
        input_dispatcher=InputDispatcher(),
        input_processor=UserInputProcessor(AttachmentManager()),
        query_loop=QueryLoop(EchoModelClient(), NullToolRunner()),
        transcript_store=TranscriptStore.for_session_dir(".morty/sessions"),
    )
    tool_context = ToolUseContext(
        tools=[],
        model="echo-model",
        permission_mode="default",
        app_state={"cwd": "."},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )
    raw = input("morty-code> ").strip()
    if not raw:
        return
    messages = engine.submit_message_sync(raw, tool_context)
    for message in messages:
        print(asdict(message))
