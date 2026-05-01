from __future__ import annotations

from typing import Any

from morty_code.types.messages import Message


def message_to_sdk_event(message: Message, session_id: str) -> dict[str, Any]:
    """把 morty 内部消息映射成 SDK-like JSON event。

    这里保持字段简单稳定，方便 harness 客户端流式消费；完整内部结构仍由
    transcript 保存。
    """

    if message.type == "assistant":
        return {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": message.payload.get("content", []),
            },
            "session_id": session_id,
            "uuid": message.uuid,
        }
    if message.type == "user":
        return {
            "type": "user",
            "message": {
                "role": "user",
                "content": message.payload.get("content", ""),
            },
            "session_id": session_id,
            "uuid": message.uuid,
            "is_meta": message.is_meta,
        }
    if message.type == "attachment":
        return {
            "type": "system",
            "subtype": "attachment",
            "attachment_type": message.payload.get("attachment_type", "unknown"),
            "content": message.payload,
            "session_id": session_id,
            "uuid": message.uuid,
            "is_meta": message.is_meta,
        }
    return {
        "type": "system",
        "subtype": message.type,
        "content": message.payload,
        "session_id": session_id,
        "uuid": message.uuid,
        "is_meta": message.is_meta,
    }


def result_event(
    *,
    session_id: str,
    success: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "result",
        "subtype": "success" if success else "error",
        "session_id": session_id,
    }
    if error:
        payload["error"] = error
    return payload
