from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from morty_code.types.messages import Message


class EchoModelClient:
    """最小模型客户端。

    第一阶段先用 echo 语义把主链路跑通，后面再替换真实 provider。
    """

    async def respond(
        self,
        messages: list[dict[str, object]],
        system_prompt: list[str],
        user_context: dict[str, str],
        system_context: dict[str, str],
    ) -> Message:
        last_user = next(
            (message for message in reversed(messages) if message["role"] == "user"),
            None,
        )
        content = "收到输入。"
        if last_user is not None:
            content = f"Echo: {last_user['content']}"
        return Message(
            uuid=str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="assistant",
            payload={
                "content": [
                    {
                        "type": "text",
                        "text": content,
                    }
                ]
            },
        )
