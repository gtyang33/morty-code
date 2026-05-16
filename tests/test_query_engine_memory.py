from __future__ import annotations

import asyncio
from pathlib import Path

from morty_code.memory.memory_extractor import MemoryCandidate
from morty_code.runtime.query_engine import QueryEngine
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ContentReplacementState, ToolUseContext


class StubExtractor:
    def extract(self, messages: list[Message]) -> list[MemoryCandidate]:
        return [
            MemoryCandidate(
                "Current task discovery: keep this in session.",
                "session",
                "task",
                0.8,
                "test",
            ),
            MemoryCandidate(
                "Remember: keep this durable.",
                "durable",
                "preference",
                0.9,
                "test",
            ),
        ]


class AsyncStubExtractor:
    async def extract(self, messages: list[Message]) -> list[MemoryCandidate]:
        return [
            MemoryCandidate(
                "The user prefers concise Chinese answers.",
                "durable",
                "preference",
                0.8,
                "model",
                memory_type="user",
            )
        ]


class TypedDurableExtractor:
    async def extract(self, messages: list[Message]) -> list[MemoryCandidate]:
        return [
            MemoryCandidate(
                "The user disliked the previous migration strategy.",
                "durable",
                "preference",
                0.9,
                "model",
                memory_type="feedback",
            )
        ]


def make_engine(*, memory_write_char_threshold: int = 12000) -> QueryEngine:
    return QueryEngine(
        prompt_builder=None,
        input_dispatcher=None,
        input_processor=None,
        query_loop=None,
        transcript_store=None,
        memory_extractor=StubExtractor(),  # type: ignore[arg-type]
        memory_write_char_threshold=memory_write_char_threshold,
    )


def make_context(tmp_path: Path) -> ToolUseContext:
    return ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="default",
        app_state={},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
        session_memory_path=str(tmp_path / "session_memory.md"),
        durable_memory_dir=str(tmp_path / "memory"),
    )


def assistant_message(text: str) -> Message:
    return Message(
        uuid="m1",
        timestamp="2026-05-15T00:00:00",
        type="assistant",
        payload={"content": [{"type": "text", "text": text}]},
    )


def test_write_memories_routes_candidates_to_one_store(tmp_path: Path) -> None:
    session_path = tmp_path / "session_memory.md"
    durable_dir = tmp_path / "memory"
    engine = make_engine()
    context = make_context(tmp_path)

    engine._write_memories(context, [])

    session_text = session_path.read_text(encoding="utf-8")
    durable_index = (durable_dir / "MEMORY.md").read_text(encoding="utf-8")

    assert "Current task discovery: keep this in session." in session_text
    assert "Remember: keep this durable." not in session_text
    assert "Remember: keep this durable." in durable_index
    assert "Current task discovery: keep this in session." not in durable_index
    durable_topic = next(path for path in durable_dir.glob("*.md") if path.name != "MEMORY.md")
    assert "type: user\n" in durable_topic.read_text(encoding="utf-8")


def test_maybe_write_memories_skips_short_ordinary_turn(tmp_path: Path) -> None:
    engine = make_engine(memory_write_char_threshold=1000)
    context = make_context(tmp_path)
    engine.messages = [assistant_message("short turn")]

    wrote = engine._maybe_write_memories(
        context,
        [assistant_message("Current task discovery: keep this in session.")],
        raw_input="continue",
    )

    assert wrote is False
    assert not Path(context.session_memory_path or "").exists()
    assert not Path(context.durable_memory_dir or "").exists()


def test_maybe_write_memories_writes_when_message_budget_is_high(tmp_path: Path) -> None:
    engine = make_engine(memory_write_char_threshold=100)
    context = make_context(tmp_path)
    engine.messages = [assistant_message("x" * 200)]

    wrote = engine._maybe_write_memories(
        context,
        [assistant_message("Current task discovery: keep this in session.")],
        raw_input="continue",
    )

    assert wrote is True
    assert Path(context.session_memory_path or "").exists()


def test_maybe_write_memories_writes_for_explicit_memory_command(tmp_path: Path) -> None:
    engine = make_engine(memory_write_char_threshold=999999)
    context = make_context(tmp_path)
    engine.messages = [assistant_message("short turn")]

    wrote = engine._maybe_write_memories(
        context,
        [assistant_message("Current task discovery: keep this in session.")],
        raw_input="/memory",
    )

    assert wrote is True
    assert Path(context.session_memory_path or "").exists()


def test_async_memory_extractor_writes_model_candidates(tmp_path: Path) -> None:
    engine = QueryEngine(
        prompt_builder=None,
        input_dispatcher=None,
        input_processor=None,
        query_loop=None,
        transcript_store=None,
        memory_extractor=AsyncStubExtractor(),  # type: ignore[arg-type]
    )
    context = make_context(tmp_path)

    wrote = asyncio.run(
        engine._maybe_write_memories_async(
            context,
            [assistant_message("final response")],
            raw_input="/memory",
        )
    )

    assert wrote is True
    durable_topic = next(
        path
        for path in Path(context.durable_memory_dir or "").glob("*.md")
        if path.name != "MEMORY.md"
    )
    topic_text = durable_topic.read_text(encoding="utf-8")
    assert "The user prefers concise Chinese answers." in topic_text
    assert "type: user\n" in topic_text


def test_memory_routing_uses_explicit_durable_memory_type(tmp_path: Path) -> None:
    engine = QueryEngine(
        prompt_builder=None,
        input_dispatcher=None,
        input_processor=None,
        query_loop=None,
        transcript_store=None,
        memory_extractor=TypedDurableExtractor(),  # type: ignore[arg-type]
    )
    context = make_context(tmp_path)

    wrote = asyncio.run(
        engine._maybe_write_memories_async(
            context,
            [assistant_message("final response")],
            raw_input="/memory",
        )
    )

    assert wrote is True
    durable_topic = next(
        path
        for path in Path(context.durable_memory_dir or "").glob("*.md")
        if path.name != "MEMORY.md"
    )
    assert "type: feedback\n" in durable_topic.read_text(encoding="utf-8")
