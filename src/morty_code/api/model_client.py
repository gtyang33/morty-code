from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from morty_code.api.errors import ModelProviderError
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
    """本地 echo provider，用于无网络验证 runtime 主链路。"""

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
            timestamp=datetime.now(UTC).isoformat(),
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
        timeout: float | None = None,
    ) -> None:
        self.model = model
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.timeout = timeout if timeout is not None else _env_float(
            "MORTY_API_TIMEOUT",
            "OPENAI_TIMEOUT",
            "LLM_TIMEOUT",
            default=120.0,
        )
        # 普通 OpenAI Chat 网关可能拒绝 cache_control；默认只在 runtime 内部规划。
        self.send_cache_control = os.environ.get("MORTY_SEND_CACHE_CONTROL") == "1"

    async def respond(
        self,
        messages: list[dict[str, object]],
        system_prompt: list[str],
        user_context: dict[str, str],
        system_context: dict[str, str],
    ) -> Message:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for openai-compatible provider")
        wire_messages = self._normalize_wire_messages([
            {
                "role": "system",
                "content": self._render_system_message(system_prompt, user_context, system_context),
            },
            *messages,
        ])
        body = json.dumps(
            self._build_request_body(
                model=self.model,
                wire_messages=wire_messages,
                system_context=system_context,
            ),
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
            retry_after = _parse_retry_after(exc.headers.get("retry-after"))
            raise ModelProviderError(
                f"model provider returned HTTP {exc.code}",
                status=exc.code,
                detail=detail,
                retry_after=retry_after,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ModelProviderError(
                f"model provider request timed out after {self.timeout:g}s",
                detail=f"request timed out after {self.timeout:g}s",
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            detail = str(exc) or exc.__class__.__name__
            if "timed out" in detail.lower():
                detail = f"request timed out after {self.timeout:g}s"
            raise ModelProviderError(
                f"model provider connection failed: {detail}",
                detail=detail,
            ) from exc
        choice = payload.get("choices", [{}])[0].get("message", {})
        message = self._message_from_choice(choice)
        if isinstance(payload.get("usage"), dict):
            message.payload["usage"] = payload["usage"]
        return message

    def _build_request_body(
        self,
        model: str,
        wire_messages: list[dict[str, object]],
        system_context: dict[str, str],
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "model": model,
            "messages": wire_messages,
        }
        tool_schemas_json = system_context.get("tool_schemas_json")
        if tool_schemas_json:
            tools = json.loads(tool_schemas_json)
            body["tools"] = tools if self.send_cache_control else self._strip_cache_fields(tools)
        return body

    def _message_from_choice(self, choice: dict[str, object]) -> Message:
        content = choice.get("content") or ""
        tool_calls = choice.get("tool_calls") or []
        blocks: list[dict[str, object]] = []
        if content:
            blocks.append({"type": "text", "text": str(content)})
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") or {}
                if not isinstance(function, dict):
                    continue
                raw_arguments = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(str(raw_arguments))
                except json.JSONDecodeError:
                    arguments = {"raw_arguments": raw_arguments}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tool_call.get("id", uuid4())),
                        "name": str(function.get("name", "")),
                        "input": arguments,
                    }
                )
        if not blocks:
            blocks.append({"type": "text", "text": ""})
        return Message(
            uuid=str(uuid4()),
            timestamp=datetime.now(UTC).isoformat(),
            type="assistant",
            payload={"content": blocks},
        )

    def _normalize_wire_messages(
        self,
        messages: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """把内部 Anthropic-like content blocks 转成 OpenAI Chat Completions 格式。"""

        normalized: list[dict[str, object]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "assistant" and isinstance(content, list):
                text_parts: list[str] = []
                tool_calls: list[dict[str, object]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(str(block.get("text", "")))
                    elif block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "id": str(block.get("id", uuid4())),
                                "type": "function",
                                "function": {
                                    "name": str(block.get("name", "")),
                                    "arguments": json.dumps(
                                        block.get("input", {}),
                                        ensure_ascii=False,
                                    ),
                                },
                            }
                        )
                assistant_message: dict[str, object] = {
                    "role": "assistant",
                    "content": "\n".join(part for part in text_parts if part).strip() or None,
                }
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls
                normalized.append(assistant_message)
                continue
            if role == "user" and isinstance(content, list) and self._is_tool_result_blocks(content):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    normalized.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(block.get("tool_use_id", "")),
                            "content": self._stringify_tool_result(block.get("content", "")),
                        }
                    )
                continue
            if isinstance(content, list):
                if role == "user":
                    normalized.append({**message, "content": self._to_openai_user_parts(content)})
                else:
                    normalized.append({**message, "content": self._stringify_content_blocks(content)})
                continue
            normalized.append(message)
        return normalized

    def _is_tool_result_blocks(self, content: list[object]) -> bool:
        return bool(content) and all(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )

    def _stringify_tool_result(self, content: object) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False)

    def _stringify_content_blocks(self, content: list[object]) -> str:
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(part for part in parts if part).strip()

    def _to_openai_user_parts(self, content: list[object]) -> list[dict[str, object]]:
        parts: list[dict[str, object]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text", ""))
                if text:
                    text_part: dict[str, object] = {"type": "text", "text": text}
                    if self.send_cache_control and isinstance(block.get("cache_control"), dict):
                        text_part["cache_control"] = block["cache_control"]
                    parts.append(text_part)
            elif block.get("type") == "image":
                source = block.get("source")
                if isinstance(source, str) and source:
                    image_part: dict[str, object] = {"type": "image_url", "image_url": {"url": source}}
                    if self.send_cache_control and isinstance(block.get("cache_control"), dict):
                        image_part["cache_control"] = block["cache_control"]
                    parts.append(image_part)
        if not parts:
            return [{"type": "text", "text": ""}]
        return parts

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
            visible_system_context = {
                key: value
                for key, value in system_context.items()
                if key not in {"tool_schemas_json", "prompt_cache_plan_json"}
            }
            if visible_system_context:
                parts.append("System context:\n" + json.dumps(visible_system_context, ensure_ascii=False, indent=2))
        return "\n\n".join(part for part in parts if part.strip())

    def _strip_cache_fields(self, value: object) -> object:
        if isinstance(value, list):
            return [self._strip_cache_fields(item) for item in value]
        if isinstance(value, dict):
            return {
                key: self._strip_cache_fields(item)
                for key, item in value.items()
                if key not in {"cache_control", "cache_reference"}
            }
        return value


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _env_float(*names: str, default: float) -> float:
    for name in names:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return default
