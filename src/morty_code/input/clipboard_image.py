from __future__ import annotations

import base64
import shutil
import subprocess
import sys


IMAGE_TARGETS = [
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/bmp",
]


def read_clipboard_image(*, platform: str | None = None) -> dict[str, object] | None:
    """读取系统剪贴板里的图片。

    不启动外部宿主，仅调用本机剪贴板命令读取当前剪贴板 target。当前 Linux
    主要覆盖 X11 的 xclip 和 Wayland 的 wl-paste。
    """

    current_platform = platform or sys.platform
    if current_platform == "darwin":
        return None
    if current_platform.startswith("win"):
        return None
    return _read_linux_clipboard_image()


def _read_linux_clipboard_image() -> dict[str, object] | None:
    """读取 Linux 剪贴板图片。"""

    if shutil.which("wl-paste") is not None:
        image = _read_wl_paste_image()
        if image is not None:
            return image
    if shutil.which("xclip") is not None:
        return _read_xclip_image()
    return None


def _read_wl_paste_image() -> dict[str, object] | None:
    """通过 wl-paste 读取 Wayland 剪贴板图片。"""

    targets = _run_bytes(["wl-paste", "--list-types"])
    if targets is None:
        return None
    return _read_first_available_target(
        targets.decode(errors="replace").splitlines(),
        lambda target: _run_bytes(["wl-paste", "--no-newline", "--type", target]),
    )


def _read_xclip_image() -> dict[str, object] | None:
    """通过 xclip 读取 X11 剪贴板图片。"""

    targets = _run_bytes(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-out"])
    if targets is None:
        return None
    return _read_first_available_target(
        targets.decode(errors="replace").splitlines(),
        lambda target: _run_bytes(["xclip", "-selection", "clipboard", "-t", target, "-out"]),
    )


def _read_first_available_target(
    targets: list[str],
    reader,
) -> dict[str, object] | None:
    """从剪贴板 targets 中读取第一种支持的图片格式。"""

    available = {target.strip() for target in targets}
    for target in IMAGE_TARGETS:
        if target not in available:
            continue
        payload = reader(target)
        if not payload:
            continue
        extension = "jpg" if target in {"image/jpeg", "image/jpg"} else target.split("/", 1)[1]
        return {
            "type": "image",
            "content": base64.b64encode(payload).decode("ascii"),
            "media_type": "image/jpeg" if target == "image/jpg" else target,
            "filename": f"clipboard.{extension}",
        }
    return None


def _run_bytes(command: list[str]) -> bytes | None:
    """执行剪贴板命令并返回二进制 stdout。"""

    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout
