from __future__ import annotations

import json
from typing import Protocol

from morty_code.memory.memory_extractor import (
    DurableMemoryType,
    MemoryCandidate,
    MemoryExtractor,
    MemoryTarget,
)
from morty_code.types.messages import Message

VALID_TARGETS: set[str] = {"session", "durable"}
VALID_MEMORY_TYPES: set[str] = {"user", "feedback", "project", "reference"}


class MemoryModelClient(Protocol):
    async def respond(
        self,
        messages: list[dict[str, object]],
        system_prompt: list[str],
        user_context: dict[str, str],
        system_context: dict[str, str],
    ) -> Message: ...


class FallbackMemoryExtractor(Protocol):
    def extract(self, messages: list[Message]) -> list[MemoryCandidate]: ...


class ModelMemoryExtractor:
    """Use the active model to summarize recent messages into memory candidates."""

    def __init__(
        self,
        model_client: MemoryModelClient,
        fallback: FallbackMemoryExtractor | None = None,
        max_candidate_chars: int = 800,
    ) -> None:
        self.model_client = model_client
        self.fallback = fallback or MemoryExtractor()
        self.max_candidate_chars = max_candidate_chars

    async def extract(self, messages: list[Message]) -> list[MemoryCandidate]:
        try:
            response = await self.model_client.respond(
                messages=[
                    {
                        "role": "user",
                        "content": self._build_prompt(messages),
                    }
                ],
                system_prompt=[_SYSTEM_PROMPT],
                user_context={},
                system_context={},
            )
            candidates = self._parse_response(response)
            if candidates is None:
                return self.fallback.extract(messages)
            return candidates
        except Exception:  # noqa: BLE001 - memory extraction must not break the main turn.
            return self.fallback.extract(messages)

    def _build_prompt(self, messages: list[Message]) -> str:
        visible = []
        for message in messages:
            if message.type not in {"user", "assistant"}:
                continue
            visible.append(
                {
                    "type": message.type,
                    "payload": message.payload,
                }
            )
        return (
            "Analyze the recent conversation messages below and extract only useful memories.\n"
            "Return strict JSON with shape: {\"memories\":[{\"text\":\"...\","
            "\"target\":\"session|durable\",\"type\":\"user|feedback|project|reference\","
            "\"topic\":\"short topic\",\"reason\":\"short reason\"}]}.\n"
            "Use durable only for cross-session user preferences, feedback, project context, "
            "or external references. Use session for current-task continuity. "
            "Skip code facts, file paths, git history, command output, generic summaries, "
            "and anything already derivable from the repository.\n\n"
            f"Messages:\n{json.dumps(visible, ensure_ascii=False, default=str)}"
        )

    def _parse_response(self, response: Message) -> list[MemoryCandidate] | None:
        text = self._response_text(response)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        raw_memories = payload.get("memories") if isinstance(payload, dict) else None
        if not isinstance(raw_memories, list):
            return None
        candidates: list[MemoryCandidate] = []
        seen: set[str] = set()
        for item in raw_memories:
            candidate = self._candidate_from_item(item)
            if candidate is None:
                continue
            key = " ".join(candidate.text.lower().split())
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
        return candidates

    def _candidate_from_item(self, item: object) -> MemoryCandidate | None:
        if not isinstance(item, dict):
            return None
        text = " ".join(str(item.get("text", "")).strip().split())
        if len(text) < 20:
            return None
        target = str(item.get("target", "")).strip()
        if target not in VALID_TARGETS:
            return None
        raw_type = str(item.get("type", "")).strip()
        memory_type = raw_type if raw_type in VALID_MEMORY_TYPES else None
        if target == "durable" and memory_type is None:
            return None
        topic = " ".join(str(item.get("topic", "")).strip().split()) or memory_type or "task"
        reason = " ".join(str(item.get("reason", "")).strip().split()) or "model extracted memory"
        return MemoryCandidate(
            text=text[: self.max_candidate_chars],
            target=target,  # type: ignore[arg-type]
            topic=topic,
            confidence=0.8,
            reason=reason,
            memory_type=memory_type,  # type: ignore[arg-type]
        )

    def _response_text(self, response: Message) -> str:
        content = response.payload.get("content")
        if not isinstance(content, list):
            return ""
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
        return "\n".join(texts).strip()


_SYSTEM_PROMPT = (
    "You are a memory extraction subagent. Extract durable and session memories "
    "as strict JSON only. Do not include markdown or explanatory prose."
)
