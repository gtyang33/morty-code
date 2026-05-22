from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Sequence

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Box, Frame, Label, RadioList, TextArea


@dataclass(frozen=True)
class Choice:
    """通用交互选项；plan approval 和权限审批都可以复用。"""

    value: str
    label: str
    description: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InteractionRequest:
    """一次终端交互请求。"""

    title: str
    message: str
    details: str = ""
    choices: tuple[Choice, ...] = ()
    default: str | None = None
    prompt: str = "Action › "


@dataclass(frozen=True)
class InteractionResult:
    """终端交互结果。"""

    value: str | None
    raw_input: str = ""


class TerminalInteraction:
    """可复用的终端选择组件。

    TTY 场景使用 prompt-toolkit application：左侧展示完整详情，底部用
    ↑/↓ 切换、Enter 直接确认。非 TTY 或控件渲染失败时退回普通文本菜单。
    """

    def __init__(self, session: PromptSession[str] | None = None) -> None:
        self.session = session or PromptSession()

    def ask(self, request: InteractionRequest) -> InteractionResult:
        """展示交互并返回用户选择。"""

        if _is_interactive_tty():
            try:
                value = self._ask_application(request)
                return InteractionResult(value=value)
            except Exception:  # noqa: BLE001 - 终端能力复杂，失败时必须能退回文本模式。
                pass
        print(format_interaction_request(request))
        raw = self.session.prompt(request.prompt).strip()
        return InteractionResult(value=resolve_choice(raw, request.choices), raw_input=raw)

    def _ask_application(self, request: InteractionRequest) -> str | None:
        """用 prompt-toolkit Application 渲染选择界面。"""

        return self._build_application(request).run()

    def _build_application(self, request: InteractionRequest) -> Application:
        """构建 prompt-toolkit Application，便于测试键绑定。"""

        radio = RadioList(
            values=[(choice.value, _choice_text(choice)) for choice in request.choices],
            default=request.default,
            show_numbers=True,
            show_cursor=True,
        )
        details_text = request.details.strip() or "No details available."
        detail = self._build_detail_area(details_text)
        key_bindings = KeyBindings()

        @key_bindings.add("enter", eager=True)
        def _(event) -> None:
            # Enter 应该直接选择当前项，而不是先跳到 OK 按钮。
            event.app.exit(result=radio.current_value)

        @key_bindings.add("escape")
        @key_bindings.add("c-c")
        def _(event) -> None:
            event.app.exit(result=None)

        body = HSplit(
            [
                Label(text=request.message),
                Frame(detail, title="Details"),
                Label(text=""),
                Label(text="Actions"),
                radio,
                Label(text=""),
                Label(text="Use ↑/↓ to move, Enter to select, mouse wheel to scroll details, Esc to cancel."),
            ],
            padding=1,
        )
        app = Application(
            layout=Layout(
                Frame(Box(body, padding=1), title=request.title),
                focused_element=radio,
            ),
            key_bindings=key_bindings,
            mouse_support=True,
            full_screen=False,
            style=Style.from_dict(
                {
                    "frame.label": "ansired bold",
                    "radio-selected": "ansicyan bold",
                    "radio-checked": "ansigreen bold",
                }
            ),
        )
        return app

    def _build_detail_area(self, details_text: str) -> TextArea:
        """构建可滚动详情区。"""

        return TextArea(
            text=details_text,
            read_only=True,
            focusable=True,
            focus_on_click=True,
            scrollbar=True,
            wrap_lines=True,
            height=Dimension(preferred=min(18, max(6, len(details_text.splitlines()) + 2)), max=22),
        )


def format_interaction_request(request: InteractionRequest) -> str:
    """渲染文本兜底菜单，完整展示 details。"""

    lines = [_box_line(request.title), request.message]
    if request.details.strip():
        lines.extend(["", request.details.strip()])
    if request.choices:
        lines.append("")
        for index, choice in enumerate(request.choices, start=1):
            suffix = f" - {choice.description}" if choice.description else ""
            lines.append(f"{index}. {choice.label}{suffix}")
    lines.append(_box_line(""))
    return "\n".join(lines)


def resolve_choice(raw: str, choices: Sequence[Choice]) -> str | None:
    """解析数字、value、alias 等输入。"""

    normalized = raw.strip().lower()
    if not normalized:
        return None
    if normalized.isdigit():
        index = int(normalized) - 1
        if 0 <= index < len(choices):
            return choices[index].value
    for choice in choices:
        tokens = {choice.value.lower(), choice.label.lower(), *(alias.lower() for alias in choice.aliases)}
        if normalized in tokens:
            return choice.value
    return None


def _choice_text(choice: Choice) -> str:
    if not choice.description:
        return choice.label
    return f"{choice.label} - {choice.description}"


def _box_line(title: str) -> str:
    title = title.strip()
    if not title:
        return "─" * 72
    return f"─ {title} " + "─" * max(0, 72 - len(title) - 3)


def _is_interactive_tty() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())
