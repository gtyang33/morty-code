from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from morty_code.types.messages import Message


class ModelClient(Protocol):
    async def respond(
        self,
        messages: list[dict[str, object]],
        system_prompt: list[str],
        user_context: dict[str, str],
        system_context: dict[str, str],
    ) -> Message: ...


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


class OpenAICompatibleModelClient:
    """最小 OpenAI-compatible chat completions provider。

    只依赖 Python 标准库，避免重新引入 pip 依赖。默认读取：
    - `OPENAI_BASE_URL`，默认 `https://api.openai.com/v1`
    - `OPENAI_API_KEY`
    """

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.timeout = timeout

    async def respond(
        self,
        messages: list[dict[str, object]],
        system_prompt: list[str],
        user_context: dict[str, str],
        system_context: dict[str, str],
    ) -> Message:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for openai-compatible provider")
        wire_messages = [
            {
                "role": "system",
                "content": self._render_system_message(system_prompt, user_context, system_context),
            },
            *messages,
        ]
        body = json.dumps(
            {
                "model": self.model,
                "messages": wire_messages,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            url=f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"model provider returned HTTP {exc.code}: {detail}") from exc
        choice = payload.get("choices", [{}])[0].get("message", {})
        content = choice.get("content") or ""
        return Message(
            uuid=str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="assistant",
            payload={"content": [{"type": "text", "text": str(content)}]},
        )

    def _render_system_message(
        self,
        system_prompt: list[str],
        user_context: dict[str, str],
        system_context: dict[str, str],
    ) -> str:
        parts = ["\n\n".join(system_prompt)]
        if user_context:
            parts.append("User context:\n" + json.dumps(user_context, ensure_ascii=False, indent=2))
        if system_context:
            parts.append("System context:\n" + json.dumps(system_context, ensure_ascii=False, indent=2))
        return "\n\n".join(part for part in parts if part.strip())
