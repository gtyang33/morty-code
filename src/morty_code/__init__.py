from __future__ import annotations

import argparse
import asyncio
import atexit
import builtins
import json
import os
import sys
import threading
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style

from morty_code.api.model_client import EchoModelClient, OpenAICompatibleModelClient
from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.compact.auto_compact import AutoCompactDecider
from morty_code.compact.compact_agent import CompactAgent
from morty_code.input.handle_input import InputDispatcher
from morty_code.input.process_user_input import UserInputProcessor
from morty_code.memory.memory_extractor import MemoryExtractor
from morty_code.agents.task_registry import get_subagent_task_registry
from morty_code.prompt.prompt_builder import PromptBuilder
from morty_code.prompt.prompt_sections import PromptSectionRegistry
from morty_code.runtime.query_engine import QueryEngine
from morty_code.runtime.query_loop import QueryLoop
from morty_code.tools import NullToolRunner, ToolRunner, create_local_tool_registry
from morty_code.transcript.transcript_store import TranscriptStore
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ContentReplacementState, ToolUseContext

# ---------------------------------------------------------------------------
# REPL 语法高亮 & 空闲动画
# ---------------------------------------------------------------------------

_MORTY_STYLE = Style.from_dict(
    {
        "slash": "#ansicyan bold",
        "command": "#ansigreen bold",
        "argument": "#ansiwhite",
        "text": "",
    }
)

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class _ReplLexer(Lexer):
    """简单 lexer：/command 高亮，其余为普通文本。"""

    def lex_document(self, document):
        lines = document.lines

        def get_line(lineno):
            line = lines[lineno]
            if not line:
                return [("", "")]
            if line.startswith("/"):
                parts = line.split(None, 1)
                slash_cmd = parts[0]
                rest = parts[1] if len(parts) > 1 else ""
                tokens: list[tuple[str, str]] = [("class:slash", "/")]
                tokens.append(("class:command", slash_cmd[1:]))
                if rest:
                    tokens.append(("class:argument", " " + rest))
                return tokens
            return [("", line)]

        return get_line


class _Spinner:
    """在 stderr 上显示旋转动画，用于模型响应等待期间。"""

    def __init__(self, frames=None, interval=0.12):
        self.frames = frames or _SPINNER_FRAMES
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, text: str = "thinking"):
        if self._thread is not None:
            return
        self._stop.clear()

        def _spin():
            idx = 0
            while not self._stop.is_set():
                frame = self.frames[idx % len(self.frames)]
                sys.stderr.write(f"\r  {frame} {text}...")
                sys.stderr.flush()
                idx += 1
                self._stop.wait(self.interval)
            sys.stderr.write("\r" + " " * (len(text) + 12) + "\r")
            sys.stderr.flush()

        self._thread = threading.Thread(target=_spin, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None


def main() -> None:
    """最小 CLI 入口，用于手动验证 runtime 主链路是否能跑通。"""

    parser = argparse.ArgumentParser(prog="morty-code")
    parser.add_argument("--session", help="恢复指定 JSONL transcript 文件")
    parser.add_argument("--once", help="只提交一条输入后退出")
    parser.add_argument("--provider", choices=["echo", "openai-compatible"], default="echo")
    parser.add_argument("--model", default="echo-model")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL，默认读取 OPENAI_BASE_URL")
    parser.add_argument("--enable-local-tools", action="store_true", help="启用 cwd 内本地文件和命令工具")
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
            "session_id": transcript_store.session_id,
            "transcript_path": str(transcript_store.path),
            "plans_dir": ".morty/plans",
            "subagent_transcripts_dir": ".morty/subagents",
            "subagent_tasks_dir": ".morty/tasks",
            "allow_dangerous_bash": os.environ.get("MORTY_ALLOW_DANGEROUS_BASH") == "1",
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
    atexit.register(
        _mark_running_subagents_interrupted,
        str(tool_context.app_state["subagent_tasks_dir"]),
        os.getpid(),
    )
    get_subagent_task_registry(
        str(tool_context.app_state["subagent_tasks_dir"])
    ).interrupt_orphaned_running()
    if args.session:
        restored = asyncio.run(
            engine.restore_from_transcript(
                {
                    "cwd": ".",
                    "model": args.model,
                    "session_id": transcript_store.session_id,
                    "transcript_path": str(transcript_store.path),
                    "plans_dir": ".morty/plans",
                    "subagent_transcripts_dir": ".morty/subagents",
                    "subagent_tasks_dir": ".morty/tasks",
                    "allow_dangerous_bash": os.environ.get("MORTY_ALLOW_DANGEROUS_BASH") == "1",
                }
            )
        )
        tool_context = restored["tool_context"]
        print(f"restored {len(restored['messages'])} messages from {args.session}")

    if args.once is not None:
        for message in engine.submit_message_sync(args.once, tool_context):
            _print_cli_message(message)
        return

    history_file = Path(".morty/repl_history")
    history_file.parent.mkdir(parents=True, exist_ok=True)

    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_file)),
        lexer=_ReplLexer(),
        style=_MORTY_STYLE,
    )
    spinner = _Spinner()

    while True:
        try:
            raw = session.prompt(
                FormattedText([("class:slash", "morty-code"), ("", "> ")]),
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if raw in {"/exit", "/quit"}:
            return
        if not raw:
            continue
        spinner.start("thinking")
        try:
            messages = engine.submit_message_sync(raw, tool_context)
        finally:
            spinner.stop()
        for message in messages:
            _print_cli_message(message)


def _print_cli_message(message: Message) -> None:
    rendered = _render_cli_message(message)
    if rendered:
        print(rendered)


def _mark_running_subagents_interrupted(task_dir: str, process_id: int) -> None:
    """CLI 正常退出时标记未完成后台子代理。

    这里只处理正常解释器退出；SIGKILL、机器断电等场景需要后续 resume/reaper。
    """

    get_subagent_task_registry(task_dir).interrupt_running(process_id=process_id)


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
        if message.is_meta:
            return ""
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
