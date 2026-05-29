from __future__ import annotations

import base64
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
DATA_URL_RE = re.compile(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)")
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*]\(([^)]+)\)")


@dataclass(frozen=True)
class ImageInputConversion:
    """图片输入转换结果。"""

    text: str
    pasted_contents: dict[int, dict[str, object]]
    next_id: int


def convert_inline_images(
    raw_input: str,
    *,
    cwd: str | Path | None = None,
    start_id: int = 1,
) -> ImageInputConversion | None:
    """把无需宿主捕获的图片输入转换成 Morty 内部图片引用。

    支持：
    - 整条输入是图片文件路径；
    - Markdown 图片语法 `![alt](path/to/image.png)`；
    - `data:image/png;base64,...` 形式的 data URL。
    """

    workspace = Path(cwd).expanduser().resolve() if cwd is not None else Path.cwd()
    pasted_contents: dict[int, dict[str, object]] = {}
    next_id = start_id

    direct_image = load_image_from_path(raw_input.strip(), cwd=workspace)
    if direct_image is not None:
        direct_image["id"] = next_id
        return ImageInputConversion(
            text=_image_ref(next_id),
            pasted_contents={next_id: direct_image},
            next_id=next_id + 1,
        )

    text = raw_input

    def replace_markdown(match: re.Match[str]) -> str:
        nonlocal next_id
        path_text = match.group(1).strip()
        image = load_image_from_path(path_text, cwd=workspace)
        if image is None:
            return match.group(0)
        image_id = next_id
        next_id += 1
        image["id"] = image_id
        pasted_contents[image_id] = image
        return _image_ref(image_id)

    text = MARKDOWN_IMAGE_RE.sub(replace_markdown, text)

    def replace_data_url(match: re.Match[str]) -> str:
        nonlocal next_id
        image_id = next_id
        next_id += 1
        media_type = match.group(1)
        data = "".join(match.group(2).split())
        pasted_contents[image_id] = {
            "id": image_id,
            "type": "image",
            "content": data,
            "media_type": media_type,
        }
        return _image_ref(image_id)

    text = DATA_URL_RE.sub(replace_data_url, text)
    if not pasted_contents:
        return None
    return ImageInputConversion(text=text, pasted_contents=pasted_contents, next_id=next_id)


def load_image_from_path(path_text: str, *, cwd: str | Path | None = None) -> dict[str, object] | None:
    """从图片路径读取文件并返回 base64 payload。"""

    stripped = path_text.strip().strip("\"'")
    if not stripped:
        return None
    path = Path(stripped).expanduser()
    if not path.is_absolute():
        path = (Path(cwd) if cwd is not None else Path.cwd()) / path
    path = path.resolve()
    if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    media_type = mimetypes.guess_type(path.name)[0] or _media_type_from_suffix(path.suffix)
    return {
        "type": "image",
        "content": base64.b64encode(path.read_bytes()).decode("ascii"),
        "media_type": media_type,
        "filename": path.name,
        "path": str(path),
    }


def _media_type_from_suffix(suffix: str) -> str:
    """根据扩展名兜底推断媒体类型。"""

    normalized = suffix.lower().lstrip(".")
    if normalized == "jpg":
        normalized = "jpeg"
    return f"image/{normalized or 'png'}"


def _image_ref(image_id: int) -> str:
    """构造 Morty/Claude 兼容的图片引用占位符。"""

    return f"[Image #{image_id}]"
