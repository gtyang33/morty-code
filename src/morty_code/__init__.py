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
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from morty_code.api.model_client import EchoModelClient, OpenAICompatibleModelClient
from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.compact.auto_compact import AutoCompactDecider
from morty_code.compact.compact_agent import CompactAgent
from morty_code.input.commands import CommandRegistry
from morty_code.input.clipboard_image import read_clipboard_image
from morty_code.input.clipboard_text import ClipboardTextError, read_clipboard_text
from morty_code.input.handle_input import InputDispatcher
from morty_code.input.image_input import convert_inline_images
from morty_code.input.process_user_input import (
    UserInputProcessor,
    _disable_mcp_server,
    _enable_mcp_server,
    _format_mcp_server_detail,
    _format_mcp_server_tools,
    _mcp_servers,
    _mcp_statuses,
    _reconnect_mcp_server,
    _tool_count,
)
from morty_code.harness import run_stream_json_harness
from morty_code.mcp.config import add_mcp_server, load_mcp_server_entries, parse_env_assignments
from morty_code.mcp.manager import create_mcp_tool_registry, merge_tool_registries
from morty_code.memory.memory_extractor import MemoryExtractor
from morty_code.memory.model_memory_extractor import ModelMemoryExtractor
from morty_code.plan.approval_ui import build_plan_approval_request, pending_plan_approval
from morty_code.agents.task_notifications import has_task_notifications
from morty_code.agents.task_registry import get_subagent_task_registry
from morty_code.prompt.prompt_builder import PromptBuilder
from morty_code.prompt.prompt_sections import PromptSectionRegistry
from morty_code.runtime.query_engine import QueryEngine
from morty_code.runtime.query_loop import QueryLoop
from morty_code.security import load_permission_settings
from morty_code.security.permission_ui import build_permission_request
from morty_code.skills import load_skill_registry
from morty_code.tools import ToolRunner, create_local_tool_registry
from morty_code.tools.tool_registry import ToolRegistry
from morty_code.tools.tool_result_formatter import format_tool_result_summary
from morty_code.transcript.transcript_store import TranscriptStore
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ContentReplacementState, ToolUseContext
from morty_code.ui import TerminalInteraction

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
    decision_gate_mode: str | None = None,
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
        "mcp_servers": tool_registry_mcp_servers(tool_registry),
        "decision_gate": _decision_gate_mode(decision_gate_mode),
        "enable_prompt_caching": os.environ.get("DISABLE_PROMPT_CACHING") != "1",
        "send_cache_control": os.environ.get("MORTY_SEND_CACHE_CONTROL") == "1",
        "prompt_cache_ttl": os.environ.get("MORTY_PROMPT_CACHE_TTL"),
    }


def _decision_gate_mode(raw: str | None = None) -> str:
    """读取复杂任务方案选择 gate 的开关。"""

    value = (raw if raw is not None else os.environ.get("MORTY_DECISION_GATE", "auto")).strip().lower()
    if value in {"off", "auto", "always"}:
        return value
    return "auto"


def tool_registry_mcp_servers(tool_registry) -> dict[str, object]:
    """占位字段由 main 里覆盖；保持测试和旧调用方的 app_state 结构稳定。"""

    return {}


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


def _create_repl_key_bindings(
    workspace_root: Path,
    pasted_contents: dict[int, dict[str, object]],
    next_paste_id: list[int],
) -> KeyBindings:
    """创建 REPL 粘贴快捷键。

    这里只处理终端实际传进来的文本粘贴事件：图片路径、Markdown 图片、
    data URL 会被转换成 `[Image #n]`。不尝试读取 GUI 剪贴板里的二进制图片。
    """

    key_bindings = KeyBindings()

    @key_bindings.add(Keys.BracketedPaste)
    def _paste_text_or_image_refs(event) -> None:
        pasted_text = str(event.data or "")
        _insert_pasted_text(
            event,
            pasted_text,
            workspace_root=workspace_root,
            pasted_contents=pasted_contents,
            next_paste_id=next_paste_id,
        )

    @key_bindings.add("c-v", eager=True)
    def _paste_from_system_clipboard(event) -> None:
        image = read_clipboard_image()
        if image is not None:
            paste_id = next_paste_id[0]
            next_paste_id[0] += 1
            image["id"] = paste_id
            pasted_contents[paste_id] = image
            event.app.current_buffer.insert_text(f"[Image #{paste_id}]")
            return
        try:
            pasted_text = read_clipboard_text()
        except ClipboardTextError as exc:
            message = str(exc)
            run_in_terminal(lambda: print(f"[system] {message}"))
            return
        if not pasted_text:
            return
        _insert_pasted_text(
            event,
            pasted_text,
            workspace_root=workspace_root,
            pasted_contents=pasted_contents,
            next_paste_id=next_paste_id,
        )

    return key_bindings


def _insert_pasted_text(
    event,
    pasted_text: str,
    *,
    workspace_root: Path,
    pasted_contents: dict[int, dict[str, object]],
    next_paste_id: list[int],
) -> None:
    """把粘贴文本写入当前输入框，并转换其中的图片引用。"""

    converted = convert_inline_images(
        pasted_text,
        cwd=workspace_root,
        start_id=next_paste_id[0],
    )
    if converted is None:
        event.app.current_buffer.insert_text(pasted_text)
        return
    pasted_contents.update(converted.pasted_contents)
    next_paste_id[0] = converted.next_id
    event.app.current_buffer.insert_text(converted.text)


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

    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        raise SystemExit(_handle_mcp_cli(sys.argv[2:], workspace_root=Path.cwd()))

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
    parser.add_argument(
        "--decision-gate",
        choices=["off", "auto", "always"],
        help="复杂任务是否先生成多个方案供用户选择，优先级高于 MORTY_DECISION_GATE",
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
            debug_workspace=workspace_root,
        )
        if args.provider == "openai-compatible"
        else EchoModelClient()
    )
    local_tool_registry = create_local_tool_registry(workspace_root) if args.enable_local_tools else None
    mcp_configs = load_mcp_server_entries(workspace_root)
    mcp_statuses: dict[str, dict[str, object]] = {
        name: {"status": "pending"} for name in mcp_configs
    }
    # Claude Code 的 MCP 连接不阻塞 REPL：启动时只放 pending，后台连接
    # 成功后再把 MCP tools 注入工具池。这里保留同一个 ToolRegistry 对象，
    # ToolRunner 后续能看到后台注册的新工具。
    tool_registry = merge_tool_registries(local_tool_registry) or ToolRegistry()
    permission_settings = load_permission_settings(
        workspace_root,
        env_allow=_env_list("MORTY_ALLOW_TOOLS"),
        env_deny=_env_list("MORTY_DENY_TOOLS"),
        env_ask=_env_list("MORTY_ASK_TOOLS"),
        env_default_mode=args.permission_mode or os.environ.get("MORTY_PERMISSION_MODE"),
    )
    permission_mode = permission_settings.default_mode or "default"
    tool_runner = ToolRunner(tool_registry)
    input_processor = UserInputProcessor(AttachmentManager())
    engine = QueryEngine(
        prompt_builder=PromptBuilder(PromptSectionRegistry()),
        input_dispatcher=InputDispatcher(workspace_root),
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
        decision_gate_mode=args.decision_gate,
    )
    app_state["mcp_servers"] = mcp_configs
    app_state["mcp_statuses"] = mcp_statuses
    app_state["tool_registry"] = tool_registry
    app_state["skill_registry"] = load_skill_registry(workspace_root)
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
    if mcp_configs and tool_registry is not None:
        _start_mcp_background_loader(
            mcp_configs=mcp_configs,
            workspace_root=workspace_root,
            registry=tool_registry,
            tool_context=tool_context,
            statuses=mcp_statuses,
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

    repl_pasted_contents: dict[int, dict[str, object]] = {}
    repl_next_paste_id = [1]
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_file)),
        lexer=_ReplLexer(),
        completer=_SlashCommandCompleter(input_processor.command_registry),
        key_bindings=_create_repl_key_bindings(
            workspace_root,
            repl_pasted_contents,
            repl_next_paste_id,
        ),
        style=_MORTY_STYLE,
    )
    spinner = _Spinner()
    tool_context.app_state["permission_request_handler"] = _make_cli_permission_request_handler(
        session,
        spinner,
    )
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
                if raw == "/mcp":
                    _run_mcp_interactive_menu(engine, tool_context, session)
                    continue
                turn_pasted_contents = dict(repl_pasted_contents)
                repl_pasted_contents.clear()
                spinner.start("thinking")
                try:
                    live_print, printed_ids = _make_live_printer(spinner=spinner)
                    with turn_lock:
                        messages = engine.submit_message_sync(
                            raw,
                            tool_context,
                            pasted_contents=turn_pasted_contents or None,
                            on_new_messages=live_print,
                        )
                finally:
                    spinner.stop()
                for message in messages:
                    if message.uuid in printed_ids:
                        continue
                    _print_cli_message(message)
                _run_plan_approval_interactive(
                    engine,
                    tool_context,
                    session,
                    spinner=spinner,
                    turn_lock=turn_lock,
                )

    finally:
        stop_notification_pump.set()
        notification_thread.join(timeout=1)


def _handle_mcp_cli(argv: list[str], *, workspace_root: Path) -> int:
    """处理 `morty-code mcp ...` 子命令，目前支持 Claude 风格的 stdio add。"""

    if not argv or argv[0] != "add":
        parser = argparse.ArgumentParser(prog="morty-code mcp")
        parser.error("only `morty-code mcp add` is currently supported")
    if len(argv) < 2:
        raise SystemExit("usage: morty-code mcp add <name> [options] -- <command> [args...]")
    name = argv[1]
    scope = "project"
    env_values: list[str] = []
    server_command: list[str] = []
    index = 2
    while index < len(argv):
        token = argv[index]
        if token == "--":
            server_command = argv[index + 1 :]
            break
        if token in {"-s", "--scope"}:
            index += 1
            if index >= len(argv):
                raise SystemExit(f"{token} requires a value")
            scope = argv[index]
        elif token in {"-e", "--env"}:
            index += 1
            if index >= len(argv):
                raise SystemExit(f"{token} requires a value")
            env_values.append(argv[index])
        else:
            server_command = argv[index:]
            break
        index += 1
    if not server_command:
        raise SystemExit("server command is required; use: morty-code mcp add <name> -- <command> [args...]")
    env = parse_env_assignments(env_values)
    config_path = add_mcp_server(
        name=name,
        scope=scope,
        command=server_command[0],
        args=server_command[1:],
        env=env,
        workspace_root=workspace_root,
    )
    print(
        "Added stdio MCP server "
        f"{name} with command: {' '.join(server_command)} "
        f"to {scope} config"
    )
    print(f"File modified: {config_path}")
    return 0


def _run_mcp_interactive_menu(
    engine: QueryEngine,
    tool_context: ToolUseContext,
    session: PromptSession[str],
) -> None:
    """Claude 风格的 `/mcp` 交互菜单：列表 -> 详情 -> 动作。"""

    del engine
    servers = _mcp_servers(tool_context)
    if not servers:
        print(_mcp_box("Manage MCP servers", ["No MCP servers configured."]))
        return
    server_names = sorted(str(name) for name in servers)
    print(_format_mcp_interactive_list(tool_context, selected=None))
    choice = session.prompt("Select server › ").strip()
    if not choice:
        return
    server_name = _resolve_mcp_menu_choice(choice, server_names)
    if server_name not in server_names:
        print(f"MCP server not found: {server_name}")
        return

    print(_format_mcp_interactive_detail(tool_context, server_name))
    while True:
        action = session.prompt("Action › ").strip()
        if not action:
            return
        action_name = {
            "1": "tools",
            "2": "reconnect",
            "3": "disable",
            "4": "enable",
            "tools": "tools",
            "reconnect": "reconnect",
            "disable": "disable",
            "enable": "enable",
        }.get(action)
        if action_name is None:
            print(f"Unknown action: {action}")
            continue
        if action_name == "tools":
            print(_format_mcp_interactive_tools(tool_context, server_name))
            continue
        if action_name == "reconnect":
            message = asyncio.run(_reconnect_mcp_server(tool_context, server_name))
        elif action_name == "disable":
            message = _disable_mcp_server(tool_context, server_name)
        else:
            message = asyncio.run(_enable_mcp_server(tool_context, server_name))
        print(_mcp_box("MCP action complete", [message]))
        print(_format_mcp_interactive_detail(tool_context, server_name))


def _resolve_mcp_menu_choice(choice: str, server_names: list[str]) -> str:
    if not choice.isdigit():
        return choice
    index = int(choice) - 1
    if index < 0 or index >= len(server_names):
        return choice
    return server_names[index]


def _run_plan_approval_interactive(
    engine: QueryEngine,
    tool_context: ToolUseContext,
    session: PromptSession[str],
    *,
    spinner: _Spinner,
    turn_lock: threading.Lock,
) -> None:
    """展示计划审批菜单，并把用户选择回灌到主会话。"""

    interaction = TerminalInteraction(session)
    while pending_plan_approval(tool_context) is not None:
        action = interaction.ask(build_plan_approval_request(tool_context)).value
        if not action:
            return
        if action == "reject":
            tool_context.app_state.pop("pending_plan_approval", None)
            print(_mcp_box("Plan rejected", ["Pending plan cleared. Ask for a new plan when needed."]))
            return
        if action == "changes":
            feedback = session.prompt("Change request › ").strip()
            if not feedback:
                continue
            # 用户要求改计划时，旧计划不再处于“待批准”状态；继续保留
            # plan mode，把修改意见交给模型生成下一版计划。
            tool_context.app_state.pop("pending_plan_approval", None)
            _submit_plan_approval_followup(
                engine,
                tool_context,
                feedback,
                spinner=spinner,
                turn_lock=turn_lock,
            )
            continue
        if action == "approve":
            _submit_plan_approval_followup(
                engine,
                tool_context,
                "批准，直接实现",
                spinner=spinner,
                turn_lock=turn_lock,
            )
            continue


def _submit_plan_approval_followup(
    engine: QueryEngine,
    tool_context: ToolUseContext,
    text: str,
    *,
    spinner: _Spinner,
    turn_lock: threading.Lock,
) -> None:
    """把审批动作作为下一条用户输入提交给模型。"""

    spinner.start("thinking")
    try:
        live_print, printed_ids = _make_live_printer(spinner=spinner)
        with turn_lock:
            messages = engine.submit_message_sync(
                text,
                tool_context,
                on_new_messages=live_print,
            )
    finally:
        spinner.stop()
    for message in messages:
        if message.uuid in printed_ids:
            continue
        _print_cli_message(message)


def _make_cli_permission_request_handler(
    session: PromptSession[str],
    spinner: _Spinner,
) -> Callable[[dict[str, object]], dict[str, object]]:
    """创建 CLI 权限审批 handler，复用通用交互组件。"""

    interaction = TerminalInteraction(session)

    def _handler(request: dict[str, object]) -> dict[str, object]:
        spinner.stop()
        try:
            result = interaction.ask(build_permission_request(request))
        finally:
            spinner.start("thinking")
        if result.value == "allow":
            return {"behavior": "allow"}
        return {
            "behavior": "deny",
            "message": "Tool use denied by user.",
        }

    return _handler


def _format_mcp_interactive_list(
    tool_context: ToolUseContext,
    *,
    selected: str | None,
) -> str:
    servers = _mcp_servers(tool_context)
    statuses = _mcp_statuses(tool_context)
    lines = [f"{len(servers)} {_count_word(len(servers), 'server')}"]
    current_scope = None
    for index, name in enumerate(sorted(servers), start=1):
        config = servers[name]
        scope = str(config.get("_scope") or "other")
        if scope != current_scope:
            current_scope = scope
            config_path = config.get("_config_path")
            lines.append("")
            lines.append(f"{_scope_title(scope)} ({config_path})" if config_path else _scope_title(scope))
        status = statuses.get(name, {})
        marker = "❯" if selected == name else " "
        lines.append(
            f"{marker} {index}. {name} · {_status_badge(status)}{_tool_suffix(status)}"
        )
    lines.append("")
    lines.append("Select a server by number/name. Press Enter to close.")
    return _mcp_box("Manage MCP servers", lines)


def _format_mcp_interactive_detail(tool_context: ToolUseContext, server_name: str) -> str:
    servers = _mcp_servers(tool_context)
    statuses = _mcp_statuses(tool_context)
    config = servers[server_name]
    status = statuses.get(server_name, {})
    args = config.get("args") if isinstance(config.get("args"), list) else []
    capabilities = status.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        capabilities = ["tools"] if _tool_count(status) else []
    lines = [
        f"Status: {_status_badge(status)}",
        f"Command: {config.get('command') or ''}",
        f"Args: {' '.join(str(arg) for arg in args)}",
        f"Config location: {config.get('_config_path') or 'unknown'}",
        f"Capabilities: {' · '.join(str(item) for item in capabilities) if capabilities else 'none'}",
        f"Tools: {_tool_count(status)} {_count_word(_tool_count(status), 'tool')}",
    ]
    if status.get("error"):
        lines.append(f"Error: {status.get('error')}")
    lines.append("")
    lines.append("❯ 1. View tools")
    lines.append("  2. Reconnect")
    lines.append("  3. Disable")
    if servers[server_name].get("disabled") or statuses.get(server_name, {}).get("status") == "disabled":
        lines.append("  4. Enable")
    lines.append("")
    lines.append("Choose an action by number/name. Press Enter to go back.")
    return _mcp_box(f"{_mcp_title(server_name)} MCP Server", lines)


def _format_mcp_interactive_tools(tool_context: ToolUseContext, server_name: str) -> str:
    statuses = _mcp_statuses(tool_context)
    raw = _format_mcp_server_tools(server_name, statuses.get(server_name, {}))
    lines = raw.splitlines()[2:] if "\n\n" in raw else raw.splitlines()
    return _mcp_box(f"Tools · {server_name}", lines or ["No tools are currently registered."])


def _mcp_box(title: str, lines: list[str]) -> str:
    width = min(118, max([len(_display_safe(title)) + 4, *(len(_display_safe(line)) + 4 for line in lines)]))
    top = f"╭─ {title} " + "─" * max(0, width - len(_display_safe(title)) - 5) + "╮"
    bottom = "╰" + "─" * (width - 2) + "╯"
    body = [f"│ {line}{' ' * max(0, width - len(_display_safe(line)) - 3)}│" for line in lines]
    return "\n".join([top, *body, bottom])


def _display_safe(value: object) -> str:
    return str(value).replace("\t", "    ")


def _scope_title(scope: str) -> str:
    return {
        "user": "User MCPs",
        "project": "Project MCPs",
    }.get(scope, "Other MCPs")


def _status_badge(status: dict[str, object]) -> str:
    state = str(status.get("status") or "pending")
    icon = {
        "connected": "✓",
        "pending": "◌",
        "connecting": "…",
        "failed": "✗",
        "disabled": "○",
    }.get(state, "•")
    return f"{icon} {state}"


def _tool_suffix(status: dict[str, object]) -> str:
    count = _tool_count(status)
    return f" · {count} {_count_word(count, 'tool')}" if count else ""


def _count_word(count: int, singular: str) -> str:
    return singular if count == 1 else f"{singular}s"


def _mcp_title(name: str) -> str:
    return " ".join(part.capitalize() for part in name.replace("-", "_").split("_") if part)


def _submit_and_print_local_command(
    engine: QueryEngine,
    tool_context: ToolUseContext,
    raw: str,
) -> None:
    """执行本地 slash command 并复用 CLI 消息渲染。"""

    for message in engine.submit_message_sync(raw, tool_context):
        _print_cli_message(message)


def _start_mcp_background_loader(
    *,
    mcp_configs: dict[str, dict[str, object]],
    workspace_root: Path,
    registry: ToolRegistry,
    tool_context: ToolUseContext,
    statuses: dict[str, dict[str, object]],
) -> threading.Thread:
    """后台加载 MCP tools，避免 npx/数据库连接阻塞 Morty 启动。"""

    def _load() -> None:
        try:
            mcp_registry = asyncio.run(
                create_mcp_tool_registry(
                    mcp_configs,
                    workspace_root=workspace_root,
                    statuses=statuses,
                )
            )
            tools = [
                tool
                for name in mcp_registry.list_names()
                if (tool := mcp_registry.find(name)) is not None
            ]
            registry.extend(tools)
            tool_context.tools = registry.list_names()
            tool_context.app_state["tool_schemas"] = registry.api_tool_schemas()
            tool_context.app_state["mcp_statuses"] = statuses
        except Exception as exc:  # noqa: BLE001 - 后台 MCP 加载不能杀死主会话。
            tool_context.app_state["mcp_loader_error"] = str(exc)

    thread = threading.Thread(target=_load, name="morty-mcp-loader", daemon=True)
    thread.start()
    return thread


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
    """恢复历史时显式标出角色，避免用户和助手内容混在一起。"""

    content = message.payload.get("content")
    if message.type == "assistant":
        rendered = _render_content(content, verbose=False)
        return f"[assistant]\n{rendered}".strip() if rendered else ""
    if message.type == "system":
        rendered = _render_content(content, verbose=False)
        subtype = message.payload.get("subtype")
        prefix = "[local]" if subtype == "local_command" else f"[system:{subtype}]" if subtype else "[system]"
        return f"{prefix}\n{rendered}".strip() if rendered else prefix
    if message.type != "user":
        rendered = _render_cli_message(message)
        return rendered
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
    display_name = _display_tool_name(name)
    return f"[tool] {display_name}{(': ' + summary) if summary else ''}"


def _summarize_tool_input(name: str, tool_input: object) -> str:
    """内部压缩并总结上下文内容。"""
    if not isinstance(tool_input, dict):
        return ""
    if name.startswith("mcp__") and "sql" in tool_input:
        return _truncate_text(str(tool_input.get("sql") or ""), 160)
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


def _display_tool_name(name: str) -> str:
    """把 MCP 内部工具名压成用户更容易扫读的短名称。"""

    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return parts[-1] or name
    return name


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
