from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

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


def test_openai_compatible_debug_model_io_writes_request_and_response(
    monkeypatch,
    tmp_path: Path,
) -> None:
    debug_dir = tmp_path / "model-io"
    monkeypatch.setenv("MORTY_DEBUG_MODEL_IO", "1")
    monkeypatch.setenv("MORTY_DEBUG_MODEL_IO_DIR", str(debug_dir))

    def fake_urlopen(_request, timeout):
        return _StreamingResponse(
            [
                'data: {"choices":[{"delta":{"content":"debug ok"}}],"usage":{"total_tokens":7}}\n',
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

    message = asyncio.run(
        client.respond(
            [{"role": "user", "content": "hello"}],
            ["base system"],
            {},
            {"available_skills": "- reviewer: Review code changes"},
        )
    )

    log_files = list(debug_dir.glob("*.jsonl"))
    assert len(log_files) == 1
    events = [
        json.loads(line)
        for line in log_files[0].read_text(encoding="utf-8").splitlines()
    ]
    assert [event["type"] for event in events] == ["request", "response"]
    assert events[0]["body"]["messages"][0]["role"] == "system"
    assert "available_skills" in events[0]["body"]["messages"][0]["content"]
    assert events[1]["payload"]["choices"][0]["message"]["content"] == "debug ok"
    assert events[1]["message"]["payload"] == message.payload


def test_openai_compatible_debug_model_io_defaults_to_workspace_morty_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MORTY_DEBUG_MODEL_IO", "1")
    monkeypatch.delenv("MORTY_DEBUG_MODEL_IO_DIR", raising=False)
    workspace = tmp_path / "workspace"

    def fake_urlopen(_request, timeout):
        return _StreamingResponse(
            [
                'data: {"choices":[{"delta":{"content":"workspace debug"}}]}\n',
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

    asyncio.run(client.respond([], [], {}, {"cwd": str(workspace)}))

    assert len(list((workspace / ".morty" / "model-io").glob("*.jsonl"))) == 1


def test_openai_compatible_debug_model_io_uses_constructor_workspace_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MORTY_DEBUG_MODEL_IO", "1")
    monkeypatch.delenv("MORTY_DEBUG_MODEL_IO_DIR", raising=False)
    workspace = tmp_path / "workspace"

    def fake_urlopen(_request, timeout):
        return _StreamingResponse(
            [
                'data: {"choices":[{"delta":{"content":"fallback debug"}}]}\n',
                "data: [DONE]\n",
            ]
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleModelClient(
        model="test",
        base_url="https://example.test/v1",
        api_key="key",
        timeout=12,
        debug_workspace=workspace,
    )

    asyncio.run(client.respond([], [], {}, {}))

    assert len(list((workspace / ".morty" / "model-io").glob("*.jsonl"))) == 1
