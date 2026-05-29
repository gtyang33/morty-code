from __future__ import annotations

import shutil
import subprocess
import sys


class ClipboardTextError(RuntimeError):
    """读取系统剪贴板文本失败。"""


def read_clipboard_text(*, platform: str | None = None) -> str:
    """读取系统剪贴板里的文本。

    这是纯 CLI 内的 Ctrl+V 兜底能力：只读取文本，不尝试捕获 GUI 剪贴板
    中的图片二进制。图片应以路径、Markdown 图片或 data URL 文本进入。
    """

    current_platform = platform or sys.platform
    for command in _candidate_commands(current_platform):
        executable = command[0]
        if shutil.which(executable) is None:
            continue
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClipboardTextError(f"Clipboard command failed: {executable}: {exc}") from exc
        if result.returncode == 0:
            return result.stdout
        raise ClipboardTextError(
            f"Clipboard command failed: {executable}: {result.stderr.strip()}"
        )
    raise ClipboardTextError(_missing_tools_message(current_platform))


def _candidate_commands(platform: str) -> list[list[str]]:
    """按平台返回可用剪贴板文本读取命令。"""

    if platform == "darwin":
        return [["pbpaste"]]
    if platform.startswith("win"):
        return [
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Clipboard -Raw",
            ]
        ]
    return [
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-out"],
        ["xsel", "--clipboard", "--output"],
    ]


def _missing_tools_message(platform: str) -> str:
    """生成缺少剪贴板命令时的可读提示。"""

    if platform == "darwin":
        return "Clipboard paste requires pbpaste on macOS."
    if platform.startswith("win"):
        return "Clipboard paste requires PowerShell Get-Clipboard on Windows."
    return "Clipboard paste requires wl-paste, xclip, or xsel on Linux."
