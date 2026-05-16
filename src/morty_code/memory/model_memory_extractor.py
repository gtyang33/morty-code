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
    ) -> Message:
        """处理该方法负责的业务逻辑。"""
        ...


class FallbackMemoryExtractor(Protocol):
    def extract(self, messages: list[Message]) -> list[MemoryCandidate]:
        """提取后续流程需要的信息。"""
        ...


class ModelMemoryExtractor:
    """Use the active model to summarize recent messages into memory candidates."""

    def __init__(
        self,
        model_client: MemoryModelClient,
        fallback: FallbackMemoryExtractor | None = None,
        max_candidate_chars: int = 800,
        max_prompt_chars: int = 12000,
        max_message_chars: int = 1000,
    ) -> None:
        """初始化对象状态。"""
        self.model_client = model_client
        self.fallback = fallback or MemoryExtractor()
        self.max_candidate_chars = max_candidate_chars
        self.max_prompt_chars = max_prompt_chars
        self.max_message_chars = max_message_chars

    async def extract(self, messages: list[Message]) -> list[MemoryCandidate]:
        """提取后续流程需要的信息。"""
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
        """内部构建后续流程需要的数据。"""
        visible: list[str] = []
        for message in messages:
            if message.type not in {"user", "assistant"}:
                continue
            text = self._message_memory_text(message)
            if not text:
                continue
            visible.append(f"- {message.type}: {text[: self.max_message_chars]}")
        header = (
            "Extract only useful memories. Return strict JSON with memories: "
            "text, target(session|durable), type(user|feedback|project|reference), "
            "topic, reason. Durable is cross-session; session is current-task. "
            "Skip code facts, file paths, command output, and generic summaries.\n\n"
            "Messages:\n"
        )
        return self._fit_prompt(header, visible)

    def _message_memory_text(self, message: Message) -> str:
        """内部处理该方法负责的业务逻辑。"""
        content = message.payload.get("content")
        if isinstance(content, str):
            return " ".join(content.strip().split())
        if not isinstance(content, list):
            return ""
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = " ".join(str(block.get("text", "")).strip().split())
                if text:
                    texts.append(text)
        return "\n".join(texts)

    def _fit_prompt(self, header: str, entries: list[str]) -> str:
        """内部处理该方法负责的业务逻辑。"""
        if len(header) >= self.max_prompt_chars:
            return header[: self.max_prompt_chars]
        kept: list[str] = []
        for entry in reversed(entries):
            candidate_entries = [entry, *kept]
            candidate = header + "\n".join(candidate_entries)
            if len(candidate) <= self.max_prompt_chars:
                kept = candidate_entries
                continue
            remaining = self.max_prompt_chars - len(header)
            if not kept and remaining > 0:
                kept = [entry[:remaining]]
            break
        return (header + "\n".join(kept))[: self.max_prompt_chars]

    def _parse_response(self, response: Message) -> list[MemoryCandidate] | None:
        """内部解析输入文本或结构化数据。"""
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
        """内部处理该方法负责的业务逻辑。"""
        if not isinstance(item, dict):
            return None
        text = " ".join(str(item.get("text", "")).strip().split())
        if len(text) < 20:
            return None
        if self._looks_like_process_noise(text):
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
        """内部处理该方法负责的业务逻辑。"""
        content = response.payload.get("content")
        if not isinstance(content, list):
            return ""
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
        return "\n".join(texts).strip()

    def _looks_like_process_noise(self, text: str) -> bool:
        """内部处理该方法负责的业务逻辑。"""
        lowered = text.lower()
        if lowered.startswith("model provider error") or "request timed out" in lowered:
            return True
        noisy_prefixes = (
            "让我",
            "现在让我",
            "好的",
            "我来",
            "文件还没有写入成功",
            "i will",
            "let me",
        )
        return text.startswith(noisy_prefixes)


_SYSTEM_PROMPT = (
    "You are a memory extraction subagent. Extract durable and session memories "
    "as strict JSON only. Do not include markdown or explanatory prose."
)
