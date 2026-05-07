from __future__ import annotations

import asyncio
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
