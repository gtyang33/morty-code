from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from morty_code.types.messages import Message

MemoryTarget = Literal["session", "durable"]
DurableMemoryType = Literal["user", "feedback", "project", "reference"]


@dataclass(frozen=True)
class MemoryCandidate:
    text: str
    target: MemoryTarget
    topic: str
    confidence: float
    reason: str
    memory_type: DurableMemoryType | None = None


class MemoryExtractor:
    """Classify new facts into session or durable memory candidates."""

    def __init__(self, max_summary_chars: int = 500) -> None:
        self.max_summary_chars = max_summary_chars

    def extract(self, messages: list[Message]) -> list[MemoryCandidate]:
        candidates: list[MemoryCandidate] = []
        seen: set[str] = set()
        for message in messages:
            if message.type != "assistant" or message.payload.get("is_api_error"):
                continue
            for text in self._text_blocks(message):
                candidate = self._classify(text)
                if candidate is None:
                    continue
                key = " ".join(candidate.text.lower().split())
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)
        return candidates

    def _text_blocks(self, message: Message) -> list[str]:
        content = message.payload.get("content")
        if not isinstance(content, list):
            return []
        blocks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = " ".join(str(block.get("text", "")).strip().split())
                if text:
                    blocks.append(text)
        return blocks

    def _classify(self, text: str) -> MemoryCandidate | None:
        if self._should_skip(text):
            return None
        lowered = text.lower()
        clipped = text[: self.max_summary_chars]
        if (
            lowered.startswith("remember:")
            or "user prefers" in lowered
            or "the user prefers" in lowered
        ):
            return MemoryCandidate(
                clipped,
                "durable",
                "preference",
                0.9,
                "explicit durable preference",
            )
        if "project constraint" in lowered or "project convention" in lowered:
            return MemoryCandidate(
                clipped,
                "durable",
                "constraint",
                0.85,
                "stable project constraint",
            )
        if "environment fact" in lowered:
            return MemoryCandidate(
                clipped,
                "durable",
                "environment",
                0.8,
                "stable environment fact",
            )
        if lowered.startswith("current task") or "current task discovery" in lowered:
            return MemoryCandidate(
                clipped,
                "session",
                "task",
                0.75,
                "current task context",
            )
        if "decision:" in lowered or lowered.startswith("decided "):
            return MemoryCandidate(
                clipped,
                "session",
                "decision",
                0.7,
                "current task decision",
            )
        return None

    def _should_skip(self, text: str) -> bool:
        lowered = text.lower()
        if lowered.startswith("echo:") or lowered.startswith("runtime error:"):
            return True
        if len(text) < 20 or len(text) > 4000:
            return True
        noisy_prefixes = ("traceback ", 'file "', "$ ", "```")
        return lowered.startswith(noisy_prefixes)
