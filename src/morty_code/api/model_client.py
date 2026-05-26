from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
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
    ) -> Message:
        """处理该方法负责的业务逻辑。"""
        ...


class EchoModelClient:
    """本地 echo provider，用于无网络验证 runtime 主链路。"""

    async def respond(
        self,
        messages: list[dict[str, object]],
        system_prompt: list[str],
        user_context: dict[str, str],
        system_context: dict[str, str],
    ) -> Message:
        """处理该方法负责的业务逻辑。"""
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
        debug_workspace: str | Path | None = None,
    ) -> None:
        """初始化对象状态。"""
        self.model = model
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.debug_workspace = Path(debug_workspace) if debug_workspace is not None else None
        self.timeout = timeout if timeout is not None else _env_float(
            "MORTY_API_TIMEOUT",
            "OPENAI_TIMEOUT",
            "LLM_TIMEOUT",
            default=120.0,
        )
        # 普通 OpenAI Chat 网关可能拒绝 cache_control；默认只在 runtime 内部规划。
        self.send_cache_control = os.environ.get("MORTY_SEND_CACHE_CONTROL") == "1"
        # 默认启用 OpenAI-compatible SSE streaming；外部接口仍返回完整 Message。
        self.streaming = os.environ.get("MORTY_STREAMING", "1") != "0"
        self.debug_model_io = os.environ.get("MORTY_DEBUG_MODEL_IO") == "1"
        self.debug_model_io_path: Path | None = None

    async def respond(
        self,
        messages: list[dict[str, object]],
        system_prompt: list[str],
        user_context: dict[str, str],
        system_context: dict[str, str],
    ) -> Message:
        """处理该方法负责的业务逻辑。"""
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for openai-compatible provider")
        # runtime 内部用 Anthropic-like content block；这里统一转换成
        # OpenAI Chat Completions wire format，保证上层不需要关心具体 provider。
        wire_messages = self._normalize_wire_messages([
            {
                "role": "system",
                "content": self._render_system_message(system_prompt, user_context, system_context),
            },
            *messages,
        ])
        request_body = self._build_request_body(
            model=self.model,
            wire_messages=wire_messages,
            system_context=system_context,
            stream=self.streaming,
        )
        self._write_debug_model_io_event(
            system_context,
            {
                "type": "request",
                "timestamp": datetime.now(UTC).isoformat(),
                "model": self.model,
                "base_url": self.base_url,
                "body": request_body,
            }
        )
        body = json.dumps(
            request_body,
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
                if self.streaming:
                    payload = self._read_streaming_payload(response)
                else:
                    payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTP 错误保留 status/detail/retry-after，QueryLoop 会基于这些字段
            # 决定是否重试、是否降级 cache_control。
            detail = exc.read().decode("utf-8", errors="replace")
            retry_after = _parse_retry_after(exc.headers.get("retry-after"))
            raise ModelProviderError(
                f"model provider returned HTTP {exc.code}",
                status=exc.code,
                detail=detail,
                retry_after=retry_after,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            # socket.timeout 在不同 Python/平台上可能表现为 socket.timeout、
            # TimeoutError 或 URLError.reason；统一成可诊断的秒数信息。
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
        # usage 不参与模型下一轮语义，但保留在 payload 里供 prompt cache 统计、
        # transcript 诊断和用户排查 token 膨胀问题。
        if isinstance(payload.get("usage"), dict):
            message.payload["usage"] = payload["usage"]
        self._write_debug_model_io_event(
            system_context,
            {
                "type": "response",
                "timestamp": datetime.now(UTC).isoformat(),
                "model": self.model,
                "payload": payload,
                "message": {
                    "uuid": message.uuid,
                    "timestamp": message.timestamp,
                    "type": message.type,
                    "payload": message.payload,
                    "is_meta": message.is_meta,
                    "origin": message.origin,
                },
            }
        )
        return message

    def _build_request_body(
        self,
        model: str,
        wire_messages: list[dict[str, object]],
        system_context: dict[str, str],
        stream: bool | None = None,
    ) -> dict[str, object]:
        """内部构建后续流程需要的数据。"""
        body: dict[str, object] = {
            "model": model,
            "messages": wire_messages,
        }
        if stream:
            body["stream"] = True
        tool_schemas_json = system_context.get("tool_schemas_json")
        if tool_schemas_json:
            tools = json.loads(tool_schemas_json)
            body["tools"] = tools if self.send_cache_control else self._strip_cache_fields(tools)
        return body

    def _read_streaming_payload(self, response) -> dict[str, object]:
        """读取 OpenAI-compatible SSE 流，并组装成非流式 payload 形态。"""
        content_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, object]] = {}
        usage: dict[str, object] | None = None
        while True:
            raw_line = response.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            choices = event.get("choices") or []
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            if delta.get("content"):
                content_parts.append(str(delta["content"]))
            tool_calls = delta.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for tool_call_delta in tool_calls:
                    if isinstance(tool_call_delta, dict):
                        self._accumulate_tool_call_delta(tool_calls_by_index, tool_call_delta)
        message: dict[str, object] = {}
        content = "".join(content_parts)
        if content:
            message["content"] = content
        if tool_calls_by_index:
            message["tool_calls"] = [
                tool_calls_by_index[index]
                for index in sorted(tool_calls_by_index)
            ]
        payload: dict[str, object] = {"choices": [{"message": message}]}
        if usage is not None:
            payload["usage"] = usage
        return payload

    def _accumulate_tool_call_delta(
        self,
        calls_by_index: dict[int, dict[str, object]],
        delta: dict[str, object],
    ) -> None:
        """合并 streaming tool_calls 分片，尤其是增量 arguments 字符串。"""
        try:
            index = int(delta.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        current = calls_by_index.setdefault(
            index,
            {
                "id": str(delta.get("id") or uuid4()),
                "type": str(delta.get("type") or "function"),
                "function": {"name": "", "arguments": ""},
            },
        )
        if delta.get("id"):
            current["id"] = str(delta["id"])
        if delta.get("type"):
            current["type"] = str(delta["type"])
        function_delta = delta.get("function") or {}
        if not isinstance(function_delta, dict):
            return
        function = current.setdefault("function", {"name": "", "arguments": ""})
        if not isinstance(function, dict):
            function = {"name": "", "arguments": ""}
            current["function"] = function
        if function_delta.get("name"):
            function["name"] = str(function_delta["name"])
        if function_delta.get("arguments"):
            function["arguments"] = str(function.get("arguments") or "") + str(function_delta["arguments"])

    def _message_from_choice(self, choice: dict[str, object]) -> Message:
        """内部处理该方法负责的业务逻辑。"""
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
        """内部判断当前对象是否满足条件。"""
        return bool(content) and all(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )

    def _stringify_tool_result(self, content: object) -> str:
        """内部处理该方法负责的业务逻辑。"""
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False)

    def _stringify_content_blocks(self, content: list[object]) -> str:
        """内部处理该方法负责的业务逻辑。"""
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(part for part in parts if part).strip()

    def _to_openai_user_parts(self, content: list[object]) -> list[dict[str, object]]:
        """内部转换为目标数据结构。"""
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
        """内部渲染面向用户或模型的文本。"""
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
        """内部处理该方法负责的业务逻辑。"""
        if isinstance(value, list):
            return [self._strip_cache_fields(item) for item in value]
        if isinstance(value, dict):
            return {
                key: self._strip_cache_fields(item)
                for key, item in value.items()
                if key not in {"cache_control", "cache_reference"}
            }
        return value

    def _make_debug_model_io_path(self, system_context: dict[str, str]) -> Path:
        """为一次进程内模型 IO 调试创建稳定 JSONL 路径。"""

        configured_dir = os.environ.get("MORTY_DEBUG_MODEL_IO_DIR")
        if configured_dir:
            debug_dir = Path(configured_dir)
        else:
            cwd = Path(str(system_context.get("cwd") or self.debug_workspace or "."))
            debug_dir = cwd / ".morty" / "model-io"
        debug_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return debug_dir / f"model-io-{timestamp}-{os.getpid()}.jsonl"

    def _write_debug_model_io_event(
        self,
        system_context: dict[str, str],
        event: dict[str, object],
    ) -> None:
        """开启 debug 模式时追加记录真实发给模型和收到的内容。"""

        if not self.debug_model_io:
            return
        if self.debug_model_io_path is None:
            self.debug_model_io_path = self._make_debug_model_io_path(system_context)
        # 调试文件可能包含完整 prompt 和敏感上下文，因此仅在显式环境变量开启时写入。
        with self.debug_model_io_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str))
            handle.write("\n")


def _parse_retry_after(value: str | None) -> float | None:
    """内部解析输入文本或结构化数据。"""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _env_float(*names: str, default: float) -> float:
    """内部处理该方法负责的业务逻辑。"""
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
