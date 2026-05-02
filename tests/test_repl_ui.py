"""REPL UI 组件测试：_ReplLexer、_Spinner、_MORTY_STYLE。"""

from __future__ import annotations

import sys
import time

from prompt_toolkit.document import Document

from morty_code import _MORTY_STYLE, _ReplLexer, _Spinner, _SPINNER_FRAMES


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
# _SPINNER_FRAMES
# ---------------------------------------------------------------------------


class TestSpinnerFrames:
    """验证 spinner 帧数据。"""

    def test_frames_not_empty(self):
        assert len(_SPINNER_FRAMES) > 0

    def test_frames_are_strings(self):
        assert all(isinstance(f, str) for f in _SPINNER_FRAMES)
