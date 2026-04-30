from __future__ import annotations

import argparse
import asyncio
import builtins
import json
import os
from pathlib import Path

from morty_code.api.model_client import EchoModelClient, OpenAICompatibleModelClient
from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.compact.auto_compact import AutoCompactDecider
from morty_code.compact.compact_agent import CompactAgent
from morty_code.input.handle_input import InputDispatcher
from morty_code.input.process_user_input import UserInputProcessor
from morty_code.memory.memory_extractor import MemoryExtractor
from morty_code.prompt.prompt_builder import PromptBuilder
from morty_code.prompt.prompt_sections import PromptSectionRegistry
from morty_code.runtime.query_engine import QueryEngine
from morty_code.runtime.query_loop import QueryLoop
from morty_code.tools import NullToolRunner, ToolRunner, create_local_tool_registry
from morty_code.transcript.transcript_store import TranscriptStore
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ContentReplacementState, ToolUseContext


def main() -> None:
    """最小 CLI 入口，用于手动验证 runtime 主链路是否能跑通。"""

    parser = argparse.ArgumentParser(prog="morty-code")
    parser.add_argument("--session", help="恢复指定 JSONL transcript 文件")
    parser.add_argument("--once", help="只提交一条输入后退出")
    parser.add_argument("--provider", choices=["echo", "openai-compatible"], default="echo")
    parser.add_argument("--model", default="echo-model")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL，默认读取 OPENAI_BASE_URL")
    parser.add_argument("--enable-local-tools", action="store_true", help="启用 cwd 内只读本地工具")
    args = parser.parse_args()

    if args.session:
        transcript_path = Path(args.session)
        transcript_store = TranscriptStore(transcript_path, transcript_path.stem)
    else:
        transcript_store = TranscriptStore.for_session_dir(".morty/sessions")
    model_client = (
        OpenAICompatibleModelClient(model=args.model, base_url=args.base_url)
        if args.provider == "openai-compatible"
        else EchoModelClient()
    )
    tool_registry = create_local_tool_registry(".") if args.enable_local_tools else None
    tool_runner = ToolRunner(tool_registry) if tool_registry is not None else NullToolRunner()
    engine = QueryEngine(
        prompt_builder=PromptBuilder(PromptSectionRegistry()),
        input_dispatcher=InputDispatcher(),
        input_processor=UserInputProcessor(AttachmentManager()),
        query_loop=QueryLoop(model_client, tool_runner),
        transcript_store=transcript_store,
        auto_compact_decider=AutoCompactDecider(token_threshold=4000),
        compact_agent=CompactAgent(),
        memory_extractor=MemoryExtractor(),
    )
    tool_context = ToolUseContext(
        tools=tool_registry.list_names() if tool_registry is not None else [],
        model=args.model,
        permission_mode="default",
        app_state={
            "cwd": ".",
            "transcript_path": str(transcript_store.path),
            "tool_schemas": tool_registry.api_tool_schemas() if tool_registry is not None else [],
            "enable_prompt_caching": os.environ.get("DISABLE_PROMPT_CACHING") != "1",
            "send_cache_control": os.environ.get("MORTY_SEND_CACHE_CONTROL") == "1",
            "prompt_cache_ttl": os.environ.get("MORTY_PROMPT_CACHE_TTL"),
        },
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
        session_memory_path=".morty/session_memory.md",
        durable_memory_dir=".morty/memory",
    )
    if args.session:
        restored = asyncio.run(engine.restore_from_transcript({"cwd": ".", "model": args.model}))
        tool_context = restored["tool_context"]
        print(f"restored {len(restored['messages'])} messages from {args.session}")

    if args.once is not None:
        for message in engine.submit_message_sync(args.once, tool_context):
            print(_render_cli_message(message))
        return

    while True:
        raw = builtins.input("morty-code> ").strip()
        if raw in {"/exit", "/quit"}:
            return
        if not raw:
            continue
        messages = engine.submit_message_sync(raw, tool_context)
        for message in messages:
            print(_render_cli_message(message))


def _render_cli_message(message: Message) -> str:
    """把内部消息渲染成面向人的 CLI 文本，完整结构仍保存在 transcript。"""

    content = message.payload.get("content")
    rendered = _render_content(content)
    if message.type == "assistant":
        return rendered
    if message.type == "system":
        subtype = message.payload.get("subtype")
        prefix = f"[system:{subtype}]" if subtype else "[system]"
        return f"{prefix}\n{rendered}".strip()
    if message.type == "user":
        return f"[user]\n{rendered}".strip()
    if message.type == "attachment":
        attachment_type = message.payload.get("attachment_type", "unknown")
        return f"[attachment:{attachment_type}]\n{rendered}".strip()
    return f"[{message.type}]\n{rendered}".strip()


def _render_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _json_fallback(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
            continue
        if isinstance(block, dict) and block.get("type") == "tool_use":
            parts.append(
                f"[tool_use:{block.get('name', 'unknown')} id={block.get('id', '')}]"
            )
            continue
        if isinstance(block, dict) and block.get("type") == "tool_result":
            status = "error" if block.get("is_error") else "ok"
            parts.append(
                f"[tool_result:{status} id={block.get('tool_use_id', '')}]\n"
                f"{_render_content(block.get('content'))}"
            )
            continue
        parts.append(_json_fallback(block))
    return "\n".join(part for part in parts if part).strip()


def _json_fallback(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
