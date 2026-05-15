from __future__ import annotations

import asyncio
import json

from morty_code.api.errors import ModelProviderError
from morty_code.memory.memory_extractor import MemoryCandidate
from morty_code.memory.model_memory_extractor import ModelMemoryExtractor
from morty_code.types.messages import Message


def assistant_message(text: str) -> Message:
    return Message(
        uuid="assistant-1",
        timestamp="2026-05-15T00:00:00",
        type="assistant",
        payload={"content": [{"type": "text", "text": text}]},
    )


class JsonMemoryModel:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls = 0
        self.last_messages: list[dict[str, object]] = []

    async def respond(
        self,
        messages,
        system_prompt,
        user_context,
        system_context,
    ) -> Message:
        self.calls += 1
        self.last_messages = messages
        return assistant_message(json.dumps(self.payload, ensure_ascii=False))


class TextMemoryModel:
    async def respond(
        self,
        messages,
        system_prompt,
        user_context,
        system_context,
    ) -> Message:
        return assistant_message("I would save a memory, but this is not JSON.")


class FailingMemoryModel:
    async def respond(
        self,
        messages,
        system_prompt,
        user_context,
        system_context,
    ) -> Message:
        raise ModelProviderError("timeout", detail="request timed out after 120s")


class FallbackExtractor:
    def extract(self, messages: list[Message]) -> list[MemoryCandidate]:
        return [
            MemoryCandidate(
                text="Current task discovery: fallback kept this.",
                target="session",
                topic="task",
                confidence=0.5,
                reason="fallback",
            )
        ]


def test_model_memory_extractor_parses_structured_candidates() -> None:
    model = JsonMemoryModel(
        {
            "memories": [
                {
                    "text": "The user prefers concise Chinese answers.",
                    "target": "durable",
                    "type": "user",
                    "topic": "preference",
                    "reason": "explicit user preference",
                },
                {
                    "text": "Current task discovery: durable memory now uses frontmatter.",
                    "target": "session",
                    "topic": "task",
                    "reason": "current implementation context",
                },
            ]
        }
    )
    extractor = ModelMemoryExtractor(model)

    candidates = asyncio.run(extractor.extract([assistant_message("done")]))

    assert model.calls == 1
    assert candidates[0].target == "durable"
    assert candidates[0].memory_type == "user"
    assert candidates[0].topic == "preference"
    assert candidates[1].target == "session"
    assert "done" in str(model.last_messages)


def test_model_memory_extractor_filters_invalid_candidates() -> None:
    model = JsonMemoryModel(
        {
            "memories": [
                {"text": "x", "target": "durable", "type": "user"},
                {"text": "Valid durable memory from model.", "target": "forever", "type": "user"},
                {"text": "Valid durable memory from model.", "target": "durable", "type": "unknown"},
                {"text": "Valid durable memory from model.", "target": "durable", "type": "project"},
            ]
        }
    )
    extractor = ModelMemoryExtractor(model)

    candidates = asyncio.run(extractor.extract([assistant_message("done")]))

    assert len(candidates) == 1
    assert candidates[0].memory_type == "project"


def test_model_memory_extractor_falls_back_when_model_fails() -> None:
    extractor = ModelMemoryExtractor(FailingMemoryModel(), fallback=FallbackExtractor())

    candidates = asyncio.run(extractor.extract([assistant_message("done")]))

    assert candidates == [
        MemoryCandidate(
            text="Current task discovery: fallback kept this.",
            target="session",
            topic="task",
            confidence=0.5,
            reason="fallback",
        )
    ]


def test_model_memory_extractor_falls_back_on_invalid_json() -> None:
    extractor = ModelMemoryExtractor(TextMemoryModel(), fallback=FallbackExtractor())

    candidates = asyncio.run(extractor.extract([assistant_message("done")]))

    assert candidates == [
        MemoryCandidate(
            text="Current task discovery: fallback kept this.",
            target="session",
            topic="task",
            confidence=0.5,
            reason="fallback",
        )
    ]
