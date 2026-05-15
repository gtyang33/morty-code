from __future__ import annotations

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


def test_write_memories_routes_candidates_to_one_store(tmp_path: Path) -> None:
    session_path = tmp_path / "session_memory.md"
    durable_dir = tmp_path / "memory"
    engine = QueryEngine(
        prompt_builder=None,
        input_dispatcher=None,
        input_processor=None,
        query_loop=None,
        transcript_store=None,
        memory_extractor=StubExtractor(),  # type: ignore[arg-type]
    )
    context = ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="default",
        app_state={},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
        session_memory_path=str(session_path),
        durable_memory_dir=str(durable_dir),
    )

    engine._write_memories(context, [])

    session_text = session_path.read_text(encoding="utf-8")
    durable_index = (durable_dir / "MEMORY.md").read_text(encoding="utf-8")

    assert "Current task discovery: keep this in session." in session_text
    assert "Remember: keep this durable." not in session_text
    assert "Remember: keep this durable." in durable_index
    assert "Current task discovery: keep this in session." not in durable_index
