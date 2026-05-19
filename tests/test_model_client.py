from __future__ import annotations

import asyncio
import json
import socket

import pytest

from morty_code.api.errors import ModelProviderError
from morty_code.api.model_client import OpenAICompatibleModelClient


def test_openai_compatible_timeout_can_come_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MORTY_API_TIMEOUT", "300")

    client = OpenAICompatibleModelClient(model="test", api_key="key")

    assert client.timeout == 300.0


def test_openai_compatible_timeout_error_includes_seconds(monkeypatch) -> None:
    def raise_timeout(*_args, **_kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr("urllib.request.urlopen", raise_timeout)
    client = OpenAICompatibleModelClient(
        model="test",
        base_url="https://example.test/v1",
        api_key="key",
        timeout=12,
    )

    with pytest.raises(ModelProviderError) as exc_info:
        asyncio.run(client.respond([], [], {}, {}))

    assert exc_info.value.detail == "request timed out after 12s"


class _StreamingResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode("utf-8") for line in lines]
        self._index = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def readline(self) -> bytes:
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line


def test_openai_compatible_streaming_accumulates_text(monkeypatch) -> None:
    captured_body: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured_body.update(json.loads(request.data.decode("utf-8")))
        return _StreamingResponse(
            [
                'data: {"choices":[{"delta":{"content":"hello "}}]}\n',
                'data: {"choices":[{"delta":{"content":"world"}}]}\n',
                "data: [DONE]\n",
            ]
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleModelClient(
        model="test",
        base_url="https://example.test/v1",
        api_key="key",
        timeout=12,
    )

    message = asyncio.run(client.respond([], [], {}, {}))

    assert captured_body["stream"] is True
    assert message.payload["content"] == [{"type": "text", "text": "hello world"}]


def test_openai_compatible_streaming_accumulates_tool_call_arguments(monkeypatch) -> None:
    def fake_urlopen(_request, timeout):
        return _StreamingResponse(
            [
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"bash","arguments":"{\\"command\\":"}}]}}]}\n',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"git status\\"}"}}]}}]}\n',
                "data: [DONE]\n",
            ]
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleModelClient(
        model="test",
        base_url="https://example.test/v1",
        api_key="key",
        timeout=12,
    )

    message = asyncio.run(client.respond([], [], {}, {}))

    assert message.payload["content"] == [
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "bash",
            "input": {"command": "git status"},
        }
    ]
