from __future__ import annotations

from morty_code.memory.memory_extractor import MemoryExtractor
from morty_code.types.messages import Message


def assistant_message(text: str, *, is_api_error: bool = False) -> Message:
    payload: dict[str, object] = {"content": [{"type": "text", "text": text}]}
    if is_api_error:
        payload["is_api_error"] = True
    return Message(
        uuid="m1",
        timestamp="2026-05-15T00:00:00",
        type="assistant",
        payload=payload,
    )


def test_skips_ordinary_assistant_reply() -> None:
    extractor = MemoryExtractor()

    candidates = extractor.extract([assistant_message("Sure, I can help with that.")])

    assert candidates == []


def test_classifies_explicit_preference_as_durable() -> None:
    extractor = MemoryExtractor()

    candidates = extractor.extract(
        [
            assistant_message(
                "Remember: the user prefers concise Chinese responses for coding work."
            )
        ]
    )

    assert len(candidates) == 1
    assert candidates[0].target == "durable"
    assert candidates[0].topic == "preference"
    assert (
        candidates[0].text
        == "Remember: the user prefers concise Chinese responses for coding work."
    )


def test_classifies_current_task_discovery_as_session() -> None:
    extractor = MemoryExtractor()

    candidates = extractor.extract(
        [
            assistant_message(
                "Current task discovery: memory writes currently duplicate entries into both stores."
            )
        ]
    )

    assert len(candidates) == 1
    assert candidates[0].target == "session"
    assert candidates[0].topic == "task"


def test_skips_echo_and_runtime_errors() -> None:
    extractor = MemoryExtractor()

    candidates = extractor.extract(
        [
            assistant_message("Echo: hello"),
            assistant_message("Runtime error: model failed", is_api_error=True),
        ]
    )

    assert candidates == []


def test_deduplicates_normalized_candidates() -> None:
    extractor = MemoryExtractor()

    candidates = extractor.extract(
        [
            assistant_message("Remember: user prefers concise responses."),
            assistant_message("Remember:   user prefers concise responses."),
        ]
    )

    assert len(candidates) == 1
