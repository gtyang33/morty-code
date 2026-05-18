"""REPL UI 组件测试：_ReplLexer、_Spinner、_MORTY_STYLE。"""

from __future__ import annotations

import sys
import time

from prompt_toolkit.document import Document
from prompt_toolkit.completion import CompleteEvent

from morty_code import (
    _MORTY_STYLE,
    _ReplLexer,
    _SlashCommandCompleter,
    _Spinner,
    _SPINNER_FRAMES,
    _render_restored_cli_message,
    _render_cli_message,
)
from morty_code.input.commands import CommandRegistry, CommandSpec
from morty_code.types.messages import Message


# ---------------------------------------------------------------------------
# _ReplLexer
# ---------------------------------------------------------------------------


class TestReplLexer:
    """测试斜杠命令高亮与普通文本回退。"""

    def _lex(self, text: str) -> list[list[tuple[str, str]]]:
        doc = Document(text)
        lexer = _ReplLexer()
        get_line = lexer.lex_document(doc)
        return [get_line(i) for i in range(len(doc.lines))]

    def test_empty_line(self):
        tokens = self._lex("")[0]
        assert tokens == [("", "")]

    def test_plain_text(self):
        tokens = self._lex("hello world")[0]
        assert tokens == [("", "hello world")]

    def test_slash_command_no_args(self):
        tokens = self._lex("/help")[0]
        assert tokens == [("class:slash", "/"), ("class:command", "help")]

    def test_slash_command_with_args(self):
        tokens = self._lex("/plan save my plan")[0]
        assert tokens == [
            ("class:slash", "/"),
            ("class:command", "plan"),
            ("class:argument", " save my plan"),
        ]

    def test_slash_only(self):
        """单独一个 / 也应该被解析为 slash + 空 command。"""
        tokens = self._lex("/")[0]
        assert tokens == [("class:slash", "/"), ("class:command", "")]

    def test_multiline(self):
        tokens_list = self._lex("hello\n/help\n/world arg")
        assert tokens_list[0] == [("", "hello")]
        assert tokens_list[1] == [("class:slash", "/"), ("class:command", "help")]
        assert tokens_list[2] == [
            ("class:slash", "/"),
            ("class:command", "world"),
            ("class:argument", " arg"),
        ]


# ---------------------------------------------------------------------------
# _Spinner
# ---------------------------------------------------------------------------


class TestSpinner:
    """测试 _Spinner 的启动、停止和线程安全。"""

    def test_start_stop(self):
        spinner = _Spinner(interval=0.05)
        spinner.start("test")
        assert spinner._thread is not None
        assert spinner._thread.is_alive()
        spinner.stop()
        assert spinner._thread is None

    def test_stop_idempotent(self):
        """重复 stop 不应报错。"""
        spinner = _Spinner(interval=0.05)
        spinner.stop()  # never started
        spinner.start("test")
        spinner.stop()
        spinner.stop()  # already stopped

    def test_start_while_running(self):
        """已经 running 时再次 start 应该忽略。"""
        spinner = _Spinner(interval=0.05)
        spinner.start("first")
        thread1 = spinner._thread
        spinner.start("second")  # should be no-op
        assert spinner._thread is thread1
        spinner.stop()

    def test_custom_frames(self):
        custom = ["1", "2", "3"]
        spinner = _Spinner(frames=custom, interval=0.05)
        assert spinner.frames == custom
        spinner.start("test")
        spinner.stop()


# ---------------------------------------------------------------------------
# _MORTY_STYLE
# ---------------------------------------------------------------------------


class TestMortyStyle:
    """验证样式字典包含所有 lexer 引用的 class。"""

    def test_all_lexer_classes_in_style(self):
        style_classes = {name for name, _style in _MORTY_STYLE.style_rules}
        for cls in ("slash", "command", "argument"):
            assert cls in style_classes


# ---------------------------------------------------------------------------
# Slash command completion
# ---------------------------------------------------------------------------


class TestSlashCommandCompleter:
    """输入 / 时应提示可用 slash command。"""

    async def _noop(self, args, context):
        return {"mode": "local", "content": ""}

    def _completions(self, text: str):
        registry = CommandRegistry(
            [
                CommandSpec("help", "显示帮助", "local", self._noop),
                CommandSpec("memory", "刷新记忆", "prompt", self._noop),
                CommandSpec("hidden", "隐藏命令", "local", self._noop, user_invocable=False),
            ]
        )
        completer = _SlashCommandCompleter(registry)
        return list(
            completer.get_completions(
                Document(text, cursor_position=len(text)),
                CompleteEvent(completion_requested=True),
            )
        )

    def test_slash_lists_user_invocable_commands(self):
        completions = self._completions("/")

        assert [completion.text for completion in completions] == ["/help", "/memory"]
        assert [completion.start_position for completion in completions] == [-1, -1]

    def test_slash_prefix_filters_commands(self):
        completions = self._completions("/me")

        assert [completion.text for completion in completions] == ["/memory"]
        assert completions[0].start_position == -3

    def test_plain_text_does_not_complete_slash_commands(self):
        assert self._completions("hello /") == []


# ---------------------------------------------------------------------------
# CLI message rendering
# ---------------------------------------------------------------------------


class TestCliMessageRendering:
    """默认 CLI 输出应面向用户，而不是暴露内部 transcript 结构。"""

    def _message(self, role: str, content: object) -> Message:
        return Message(
            uuid="u1",
            timestamp="2026-05-07T00:00:00",
            type=role,  # type: ignore[arg-type]
            payload={"content": content},
        )

    def test_assistant_tool_use_is_compact(self):
        message = self._message(
            "assistant",
            [
                {"type": "text", "text": "我先搜索相关代码。"},
                {
                    "type": "tool_use",
                    "name": "grep_text",
                    "id": "tool-1",
                    "input": {"pattern": "MvRecommendation", "path": "core"},
                },
            ],
        )

        rendered = _render_cli_message(message)

        assert "我先搜索相关代码。" in rendered
        assert "[tool] grep_text: MvRecommendation in core" in rendered
        assert '"input"' not in rendered

    def test_user_tool_result_is_summarized(self):
        message = self._message(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": {
                        "path": "/tmp/project",
                        "entries": [{"name": "core", "kind": "directory"}],
                        "truncated": False,
                    },
                }
            ],
        )

        rendered = _render_cli_message(message)

        assert rendered == "[tool:ok] list_dir /tmp/project: 1 entries"

    def test_structured_tool_results_are_readable(self):
        message = self._message(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": {
                        "command": "grep -rn TOK_INSERT SemanticAnalyzer.java | head -20",
                        "exit_code": 0,
                        "timed_out": False,
                        "stdout": "2394: if (...) TOK_INSERT\n2604: && !(...)",
                        "stderr": "",
                    },
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-2",
                    "content": {
                        "path": "/repo/src/VMUtil.java",
                        "content": "public class VMUtil {\n  // many lines\n}",
                        "line_count": 437,
                        "truncated": True,
                    },
                },
            ],
        )

        rendered = _render_cli_message(message)

        assert "[tool:ok] command=`grep -rn TOK_INSERT SemanticAnalyzer.java | head -20` exit=0" in rendered
        assert "stdout: 2394: if (...) TOK_INSERT / 2604: && !(...)" in rendered
        assert "[tool:ok] file=/repo/src/VMUtil.java lines=437 truncated" in rendered
        assert "{'command':" not in rendered
        assert "public class VMUtil" not in rendered

    def test_large_tool_result_replacement_is_hidden(self):
        message = self._message(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "[Tool result tool-1 was 20000 chars and was replaced to keep prompt size stable.]",
                }
            ],
        )

        rendered = _render_cli_message(message)

        assert rendered == "[tool:ok] large result hidden; full content is kept in transcript/tool-results"

    def test_plain_user_message_is_not_echoed_by_default(self):
        message = self._message("user", "hello")

        assert _render_cli_message(message) == ""

    def test_restored_plain_user_message_is_shown_with_prefix(self):
        message = self._message(
            "user",
            [{"type": "text", "text": "之前的问题"}],
        )

        assert _render_restored_cli_message(message) == "[user]\n之前的问题"

    def test_restored_assistant_message_uses_normal_rendering(self):
        message = self._message(
            "assistant",
            [{"type": "text", "text": "之前的回答"}],
        )

        assert _render_restored_cli_message(message) == "之前的回答"


# ---------------------------------------------------------------------------
# _SPINNER_FRAMES
# ---------------------------------------------------------------------------


class TestSpinnerFrames:
    """验证 spinner 帧数据。"""

    def test_frames_not_empty(self):
        assert len(_SPINNER_FRAMES) > 0

    def test_frames_are_strings(self):
        assert all(isinstance(f, str) for f in _SPINNER_FRAMES)
