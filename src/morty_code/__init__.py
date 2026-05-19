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
from typing import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from morty_code.api.model_client import EchoModelClient, OpenAICompatibleModelClient
from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.compact.auto_compact import AutoCompactDecider
from morty_code.compact.compact_agent import CompactAgent
from morty_code.input.commands import CommandRegistry
from morty_code.input.handle_input import InputDispatcher
from morty_code.input.process_user_input import UserInputProcessor
from morty_code.harness import run_stream_json_harness
from morty_code.memory.memory_extractor import MemoryExtractor
from morty_code.memory.model_memory_extractor import ModelMemoryExtractor
from morty_code.agents.task_notifications import has_task_notifications
from morty_code.agents.task_registry import get_subagent_task_registry
from morty_code.prompt.prompt_builder import PromptBuilder
from morty_code.prompt.prompt_sections import PromptSectionRegistry
from morty_code.runtime.query_engine import QueryEngine
from morty_code.runtime.query_loop import QueryLoop
from morty_code.security import load_permission_settings
from morty_code.tools import NullToolRunner, ToolRunner, create_local_tool_registry
from morty_code.tools.tool_result_formatter import format_tool_result_summary
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


def _env_list(name: str) -> list[str]:
    """内部处理该方法负责的业务逻辑。"""
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _resolve_workspace_root(raw_cwd: str | None) -> Path:
    """解析 morty-code 的目标工作区，CLI 进程目录和项目目录可以分离。"""

    root = Path(raw_cwd).expanduser() if raw_cwd else Path.cwd()
    root = root.resolve()
    if not root.exists():
        raise FileNotFoundError(f"workspace cwd does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"workspace cwd is not a directory: {root}")
    return root


def _resolve_cli_path(raw_path: str, workspace_root: Path) -> Path:
    """CLI 传入的相对路径按 workspace root 解析，避免受启动目录影响。"""

    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (workspace_root / path).resolve()


def _runtime_app_state(
    *,
    workspace_root: Path,
    transcript_store: TranscriptStore,
    permission_mode: str,
    permission_settings,
    tool_registry,
) -> dict[str, object]:
    """内部执行核心流程。"""
    morty_dir = workspace_root / ".morty"
    return {
        "cwd": str(workspace_root),
        "morty_dir": str(morty_dir),
        "permission_mode": permission_mode,
        "session_id": transcript_store.session_id,
        "transcript_path": str(transcript_store.path),
        "plans_dir": str(morty_dir / "plans"),
        "subagent_transcripts_dir": str(morty_dir / "subagents"),
        "subagent_tasks_dir": str(morty_dir / "tasks"),
        "agents_dir": str(morty_dir / "agents"),
        "tool_results_dir": str(morty_dir / "tool-results"),
        "allow_dangerous_bash": os.environ.get("MORTY_ALLOW_DANGEROUS_BASH") == "1",
        "always_allowed_tools": permission_settings.allow,
        "denied_tools": permission_settings.deny,
        "always_ask_tools": permission_settings.ask,
        "permission_settings_sources": permission_settings.sources,
        "tool_schemas": tool_registry.api_tool_schemas() if tool_registry is not None else [],
        "enable_prompt_caching": os.environ.get("DISABLE_PROMPT_CACHING") != "1",
        "send_cache_control": os.environ.get("MORTY_SEND_CACHE_CONTROL") == "1",
        "prompt_cache_ttl": os.environ.get("MORTY_PROMPT_CACHE_TTL"),
    }


class _ReplLexer(Lexer):
    """简单 lexer：/command 高亮，其余为普通文本。"""

    def lex_document(self, document):
        """处理该方法负责的业务逻辑。"""
        lines = document.lines

        def get_line(lineno):
            """获取运行所需数据。"""
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


class _SlashCommandCompleter(Completer):
    """为 REPL 提供 slash command 补全。"""

    def __init__(self, command_registry: CommandRegistry) -> None:
        """初始化对象状态。"""
        self.command_registry = command_registry

    def get_completions(self, document, complete_event):
        """输入行以 / 开头时提示用户可调用命令。"""
        text_before_cursor = document.text_before_cursor
        if not text_before_cursor.startswith("/") or " " in text_before_cursor:
            return
        prefix = text_before_cursor[1:]
        commands = sorted(
            self.command_registry.list_user_invocable(),
            key=lambda command: command.name,
        )
        for command in commands:
            if not command.name.startswith(prefix):
                continue
            yield Completion(
                f"/{command.name}",
                start_position=-len(text_before_cursor),
                display=f"/{command.name}",
                display_meta=command.description,
            )


class _Spinner:
    """在 stderr 上显示旋转动画，用于模型响应等待期间。"""

    def __init__(self, frames=None, interval=0.12):
        """初始化对象状态。"""
        self.frames = frames or _SPINNER_FRAMES
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, text: str = "thinking"):
        """启动后台或交互流程。"""
        if self._thread is not None:
            return
        self._stop.clear()

        def _spin():
            """内部处理该方法负责的业务逻辑。"""
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
        """停止后台或交互流程。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None


def main() -> None:
    """最小 CLI 入口，用于手动验证 runtime 主链路是否能跑通。"""

    parser = argparse.ArgumentParser(prog="morty-code")
    parser.add_argument("--cwd", help="目标工作区目录，默认使用当前 shell 所在目录")
    parser.add_argument("--session", help="恢复指定 JSONL transcript 文件")
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="恢复当前 workspace 最近一次 .morty/sessions 会话",
    )
    parser.add_argument("--once", help="只提交一条输入后退出")
    parser.add_argument("--input-format", choices=["text", "stream-json"], default="text")
    parser.add_argument("--provider", choices=["echo", "openai-compatible"], default="echo")
    parser.add_argument("--model", default="echo-model")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL，默认读取 OPENAI_BASE_URL")
    parser.add_argument(
        "--api-timeout",
        type=float,
        help="OpenAI-compatible 单次请求超时时间秒数，默认读取 MORTY_API_TIMEOUT/OPENAI_TIMEOUT/LLM_TIMEOUT 或 120",
    )
    parser.add_argument("--enable-local-tools", action="store_true", help="启用 cwd 内本地文件和命令工具")
    parser.add_argument(
        "--permission-mode",
        choices=["acceptEdits", "bypassPermissions", "default", "dontAsk", "plan"],
        help="覆盖项目权限配置里的默认 permission mode",
    )
    args = parser.parse_args()
    # `--session` 是精确恢复某个 transcript；`-c` 是“恢复最近一次”。
    # 两者同时出现时无法判断用户真实意图，直接报错比猜测更安全。
    if args.session and args.continue_session:
        parser.error("--session and -c/--continue cannot be used together")

    try:
        workspace_root = _resolve_workspace_root(args.cwd)
    except (FileNotFoundError, NotADirectoryError) as exc:
        parser.error(str(exc))
    morty_dir = workspace_root / ".morty"

    # 会话文件始终绑定 workspace，而不是绑定 morty-code 源码目录。
    # 这样从任意目录用 `uv run --project ... --cwd /some/project -c`
    # 时，恢复的是 `/some/project/.morty/sessions` 下的历史对话。
    restoring_session = bool(args.session or args.continue_session)
    if args.session:
        # 手动指定 session 时，相对路径按 workspace_root 解析，保持和 @file、
        # 本地工具、权限配置同一个路径语义。
        transcript_path = _resolve_cli_path(args.session, workspace_root)
        transcript_store = TranscriptStore(transcript_path, transcript_path.stem)
    elif args.continue_session:
        # Claude Code 风格的 `-c`：不要求用户记住 session id，直接选当前
        # workspace 最近写入的 transcript。找不到历史时不自动创建新会话，
        # 避免用户以为已经恢复上下文但实际上是空会话。
        transcript_store = TranscriptStore.latest_in_session_dir(morty_dir / "sessions")
        if transcript_store is None:
            parser.error(
                f"no previous session found in {morty_dir / 'sessions'}; "
                "start a new session without -c"
            )
    else:
        transcript_store = TranscriptStore.for_session_dir(morty_dir / "sessions")
    model_client = (
        OpenAICompatibleModelClient(
            model=args.model,
            base_url=args.base_url,
            timeout=args.api_timeout,
        )
        if args.provider == "openai-compatible"
        else EchoModelClient()
    )
    tool_registry = create_local_tool_registry(workspace_root) if args.enable_local_tools else None
    permission_settings = load_permission_settings(
        workspace_root,
        env_allow=_env_list("MORTY_ALLOW_TOOLS"),
        env_deny=_env_list("MORTY_DENY_TOOLS"),
        env_ask=_env_list("MORTY_ASK_TOOLS"),
        env_default_mode=args.permission_mode or os.environ.get("MORTY_PERMISSION_MODE"),
    )
    permission_mode = permission_settings.default_mode or "default"
    tool_runner = ToolRunner(tool_registry) if tool_registry is not None else NullToolRunner()
    input_processor = UserInputProcessor(AttachmentManager())
    engine = QueryEngine(
        prompt_builder=PromptBuilder(PromptSectionRegistry()),
        input_dispatcher=InputDispatcher(),
        input_processor=input_processor,
        query_loop=QueryLoop(model_client, tool_runner),
        transcript_store=transcript_store,
        auto_compact_decider=AutoCompactDecider(token_threshold=4000),
        compact_agent=CompactAgent(model_client=model_client),
        memory_extractor=ModelMemoryExtractor(model_client, fallback=MemoryExtractor()),
    )
    app_state = _runtime_app_state(
        workspace_root=workspace_root,
        transcript_store=transcript_store,
        permission_mode=permission_mode,
        permission_settings=permission_settings,
        tool_registry=tool_registry,
    )
    tool_context = ToolUseContext(
        tools=tool_registry.list_names() if tool_registry is not None else [],
        model=args.model,
        permission_mode=permission_mode,
        app_state=app_state,
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
        session_memory_path=str(morty_dir / "session_memory.md"),
        durable_memory_dir=str(morty_dir / "memory"),
    )
    atexit.register(
        _mark_running_subagents_interrupted,
        str(tool_context.app_state["subagent_tasks_dir"]),
        os.getpid(),
    )
    get_subagent_task_registry(
        str(tool_context.app_state["subagent_tasks_dir"])
    ).interrupt_orphaned_running()
    if restoring_session:
        restored = asyncio.run(
            engine.restore_from_transcript(
                {
                    **app_state,
                    "model": args.model,
                }
            )
        )
        tool_context = restored["tool_context"]
        tool_context.tools = tool_registry.list_names() if tool_registry is not None else []
        tool_context.model = args.model
        tool_context.permission_mode = permission_mode
        tool_context.session_memory_path = str(morty_dir / "session_memory.md")
        tool_context.durable_memory_dir = str(morty_dir / "memory")
        tool_context.app_state.update(app_state)
        print(f"restored {len(restored['messages'])} messages from {transcript_store.path}")
        for message in restored["messages"]:
            _print_restored_cli_message(message)

    if args.once is not None:
        live_print, printed_ids = _make_live_printer()
        for message in engine.submit_message_sync(
            args.once,
            tool_context,
            on_new_messages=live_print,
        ):
            if message.uuid in printed_ids:
                continue
            _print_cli_message(message)
        return

    if args.input_format == "stream-json":
        run_stream_json_harness(engine, tool_context)
        return

    history_file = morty_dir / "repl_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_file)),
        lexer=_ReplLexer(),
        completer=_SlashCommandCompleter(input_processor.command_registry),
        style=_MORTY_STYLE,
    )
    spinner = _Spinner()
    turn_lock = threading.Lock()
    stop_notification_pump = threading.Event()
    notification_thread = _start_task_notification_pump(
        engine,
        tool_context,
        turn_lock=turn_lock,
        stop_event=stop_notification_pump,
    )

    try:
        with patch_stdout():
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
                    live_print, printed_ids = _make_live_printer(spinner=spinner)
                    with turn_lock:
                        messages = engine.submit_message_sync(
                            raw,
                            tool_context,
                            on_new_messages=live_print,
                        )
                finally:
                    spinner.stop()
                for message in messages:
                    if message.uuid in printed_ids:
                        continue
                    _print_cli_message(message)
    finally:
        stop_notification_pump.set()
        notification_thread.join(timeout=1)


def _start_task_notification_pump(
    engine: QueryEngine,
    tool_context: ToolUseContext,
    *,
    turn_lock: threading.Lock,
    stop_event: threading.Event,
    interval: float = 0.2,
) -> threading.Thread:
    """启动后台任务通知 pump，让异步 agent 完成后能自动回灌主会话。"""

    def _pump() -> None:
        while not stop_event.wait(interval):
            if not has_task_notifications(tool_context.app_state):
                continue
            if not turn_lock.acquire(blocking=False):
                continue
            try:
                live_print, printed_ids = _make_live_printer()
                messages = engine.submit_pending_notifications_sync(
                    tool_context,
                    on_new_messages=live_print,
                )
                for message in messages:
                    if message.uuid in printed_ids:
                        continue
                    _print_cli_message(message)
            except Exception as exc:  # noqa: BLE001 - pump 不能杀死 REPL 主循环。
                print(f"[task-notification:error] {exc}")
            finally:
                turn_lock.release()

    thread = threading.Thread(target=_pump, name="morty-task-notification-pump", daemon=True)
    thread.start()
    return thread


def _make_live_printer(
    spinner: _Spinner | None = None,
) -> tuple[Callable[[list[Message]], None], set[str]]:
    """内部创建流程需要的辅助对象。"""
    printed_ids: set[str] = set()

    def _print(messages: list[Message]) -> None:
        """内部处理该方法负责的业务逻辑。"""
        if spinner is not None:
            spinner.stop()
        for message in messages:
            printed_ids.add(message.uuid)
            _print_cli_message(message)
        if spinner is not None:
            spinner.start("thinking")

    return _print, printed_ids


def _print_cli_message(message: Message) -> None:
    """内部处理该方法负责的业务逻辑。"""
    rendered = _render_cli_message(message)
    if rendered:
        print(rendered)


def _print_restored_cli_message(message: Message) -> None:
    """内部处理该方法负责的业务逻辑。"""
    rendered = _render_restored_cli_message(message)
    if rendered:
        print(rendered)


def _mark_running_subagents_interrupted(task_dir: str, process_id: int) -> None:
    """CLI 正常退出时标记未完成后台子代理。

    这里只处理正常解释器退出；SIGKILL、机器断电等场景需要后续 resume/reaper。
    """

    get_subagent_task_registry(task_dir).interrupt_running(process_id=process_id)


def _render_cli_message(message: Message) -> str:
    """把内部消息渲染成面向人的 CLI 文本，完整结构仍保存在 transcript。"""

    verbose = os.environ.get("MORTY_VERBOSE_TOOL_OUTPUT") == "1"
    content = message.payload.get("content")
    if message.type == "assistant":
        return _render_content(content, verbose=verbose)
    if message.type == "system":
        rendered = _render_content(content, verbose=verbose)
        subtype = message.payload.get("subtype")
        prefix = f"[system:{subtype}]" if subtype else "[system]"
        return f"{prefix}\n{rendered}".strip()
    if message.type == "user":
        if _is_tool_result_content(content):
            return _render_tool_results(content, verbose=verbose)
        if verbose:
            rendered = _render_content(content, verbose=verbose)
            return f"[user]\n{rendered}".strip()
        return ""
    if message.type == "attachment":
        if message.is_meta:
            return ""
        rendered = _render_content(content, verbose=verbose)
        attachment_type = message.payload.get("attachment_type", "unknown")
        return f"[attachment:{attachment_type}]\n{rendered}".strip()
    rendered = _render_content(content, verbose=verbose)
    return f"[{message.type}]\n{rendered}".strip()


def _render_restored_cli_message(message: Message) -> str:
    """内部渲染面向用户或模型的文本。"""
    if message.type != "user":
        return _render_cli_message(message)
    content = message.payload.get("content")
    if _is_tool_result_content(content):
        return _render_tool_results(content, verbose=os.environ.get("MORTY_VERBOSE_TOOL_OUTPUT") == "1")
    rendered = _render_content(content, verbose=True)
    return f"[user]\n{rendered}".strip() if rendered else ""


def _render_content(content: object, *, verbose: bool = False) -> str:
    """内部渲染面向用户或模型的文本。"""
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
            if verbose:
                parts.append(_json_fallback(block))
            else:
                parts.append(_render_tool_use(block))
            continue
        if isinstance(block, dict) and block.get("type") == "tool_result":
            parts.append(_render_single_tool_result(block, verbose=verbose))
            continue
        parts.append(_json_fallback(block))
    return "\n".join(part for part in parts if part).strip()


def _is_tool_result_content(content: object) -> bool:
    """内部判断当前对象是否满足条件。"""
    if not isinstance(content, list) or not content:
        return False
    return all(isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content)


def _render_tool_use(block: dict[str, object]) -> str:
    """内部渲染面向用户或模型的文本。"""
    name = str(block.get("name") or "unknown")
    tool_input = block.get("input")
    summary = _summarize_tool_input(name, tool_input)
    return f"[tool] {name}{(': ' + summary) if summary else ''}"


def _summarize_tool_input(name: str, tool_input: object) -> str:
    """内部压缩并总结上下文内容。"""
    if not isinstance(tool_input, dict):
        return ""
    if name in {"read_file", "list_dir", "file_info"}:
        return str(tool_input.get("path") or ".")
    if name == "grep_text":
        pattern = str(tool_input.get("pattern") or tool_input.get("query") or "")
        path = str(tool_input.get("path") or ".")
        return _truncate_text(f"{pattern} in {path}", 120)
    if name == "glob_files":
        pattern = str(tool_input.get("pattern") or "")
        path = str(tool_input.get("path") or ".")
        return _truncate_text(f"{pattern} in {path}", 120)
    if name == "bash":
        return _truncate_text(str(tool_input.get("command") or ""), 160)
    items = []
    for key, value in list(tool_input.items())[:3]:
        items.append(f"{key}={_truncate_text(str(value), 40)}")
    return ", ".join(items)


def _render_tool_results(content: object, *, verbose: bool) -> str:
    """内部渲染面向用户或模型的文本。"""
    if not isinstance(content, list):
        return ""
    return "\n".join(
        rendered for rendered in (
            _render_single_tool_result(block, verbose=verbose)
            for block in content
            if isinstance(block, dict)
        )
        if rendered
    )


def _render_single_tool_result(block: dict[str, object], *, verbose: bool) -> str:
    """内部渲染面向用户或模型的文本。"""
    status = "error" if block.get("is_error") else "ok"
    if verbose:
        return (
            f"[tool_result:{status} id={block.get('tool_use_id', '')}]\n"
            f"{_render_content(block.get('content'), verbose=verbose)}"
        ).strip()
    return f"[tool:{status}] {_summarize_tool_result(block.get('content'))}".strip()


def _summarize_tool_result(content: object) -> str:
    """内部压缩并总结上下文内容。"""
    return format_tool_result_summary(content, max_chars=240)


def _parse_tool_result_payload(content: object) -> object:
    """内部解析输入文本或结构化数据。"""
    if isinstance(content, list):
        text_parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        content = "\n".join(part for part in text_parts if part)
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return content
        return content
    return content


def _truncate_text(text: str, limit: int) -> str:
    """内部处理该方法负责的业务逻辑。"""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 3)] + "..."


def _json_fallback(value: object) -> str:
    """内部处理该方法负责的业务逻辑。"""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
