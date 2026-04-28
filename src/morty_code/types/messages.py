from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


Role = Literal["user", "assistant", "system", "attachment", "progress"]


@dataclass
class Message:
    """内部统一消息结构。

    这里故意不直接复用 API payload 结构。
    transcript/runtime/API 各自的语义不同，必须先统一到内部消息，
    再在发送前做 normalize。
    """

    uuid: str
    timestamp: str
    type: Role
    payload: dict[str, Any]
    is_meta: bool = False
    is_virtual: bool = False
    origin: dict[str, Any] | None = None


@dataclass
class Attachment:
    """附件只是动态上下文载体，不是 transcript 主链协议消息。"""

    type: str
    payload: dict[str, Any]
    source_uuid: str | None = None
    is_meta: bool = False
