# Memory Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace noisy automatic memory summaries with classified memory candidates that route to either session or durable memory.

**Architecture:** `MemoryExtractor` will return structured `MemoryCandidate` objects with target, topic, confidence, and reason. `QueryEngine._write_memories()` will route candidates to exactly one store. Tests will cover extractor classification and end-to-end routing through the query engine helper.

**Tech Stack:** Python 3.12, dataclasses, pytest, existing Morty Code stores and message types.

---

## File Structure

- Modify `src/morty_code/memory/memory_extractor.py`: define `MemoryCandidate`, `MemoryTarget`, and rule-based classification.
- Modify `src/morty_code/runtime/query_engine.py`: route candidates by `target`.
- Create `tests/test_memory_extractor.py`: extractor unit tests.
- Create `tests/test_query_engine_memory.py`: routing test for `_write_memories()`.

---

### Task 1: Memory Extractor Classification

**Files:**
- Modify: `src/morty_code/memory/memory_extractor.py`
- Test: `tests/test_memory_extractor.py`

- [ ] **Step 1: Write failing extractor tests**

Create `tests/test_memory_extractor.py` with:

```python
from __future__ import annotations

from morty_code.memory.memory_extractor import MemoryExtractor
from morty_code.types.messages import Message


def assistant_message(text: str, *, is_api_error: bool = False) -> Message:
    payload: dict[str, object] = {"content": [{"type": "text", "text": text}]}
    if is_api_error:
        payload["is_api_error"] = True
    return Message(uuid="m1", timestamp="2026-05-15T00:00:00", type="assistant", payload=payload)


def test_skips_ordinary_assistant_reply() -> None:
    extractor = MemoryExtractor()

    candidates = extractor.extract([assistant_message("Sure, I can help with that.")])

    assert candidates == []


def test_classifies_explicit_preference_as_durable() -> None:
    extractor = MemoryExtractor()

    candidates = extractor.extract(
        [assistant_message("Remember: the user prefers concise Chinese responses for coding work.")]
    )

    assert len(candidates) == 1
    assert candidates[0].target == "durable"
    assert candidates[0].topic == "preference"
    assert candidates[0].text == "Remember: the user prefers concise Chinese responses for coding work."


def test_classifies_current_task_discovery_as_session() -> None:
    extractor = MemoryExtractor()

    candidates = extractor.extract(
        [assistant_message("Current task discovery: memory writes currently duplicate entries into both stores.")]
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
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_memory_extractor.py -q
```

Expected: FAIL because `MemoryCandidate` fields and classification behavior do not exist yet.

- [ ] **Step 3: Implement minimal extractor**

Update `src/morty_code/memory/memory_extractor.py` to:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from morty_code.types.messages import Message

MemoryTarget = Literal["session", "durable"]


@dataclass(frozen=True)
class MemoryCandidate:
    text: str
    target: MemoryTarget
    topic: str
    confidence: float
    reason: str


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
        if lowered.startswith("remember:") or "user prefers" in lowered or "the user prefers" in lowered:
            return MemoryCandidate(clipped, "durable", "preference", 0.9, "explicit durable preference")
        if "project constraint" in lowered or "project convention" in lowered:
            return MemoryCandidate(clipped, "durable", "constraint", 0.85, "stable project constraint")
        if "environment fact" in lowered:
            return MemoryCandidate(clipped, "durable", "environment", 0.8, "stable environment fact")
        if lowered.startswith("current task") or "current task discovery" in lowered:
            return MemoryCandidate(clipped, "session", "task", 0.75, "current task context")
        if "decision:" in lowered or lowered.startswith("decided "):
            return MemoryCandidate(clipped, "session", "decision", 0.7, "current task decision")
        return None

    def _should_skip(self, text: str) -> bool:
        lowered = text.lower()
        if lowered.startswith("echo:") or lowered.startswith("runtime error:"):
            return True
        if len(text) < 20 or len(text) > 4000:
            return True
        noisy_prefixes = ("traceback ", "file \"", "$ ", "```")
        return lowered.startswith(noisy_prefixes)
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
uv run pytest tests/test_memory_extractor.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit extractor change**

Run:

```bash
git add src/morty_code/memory/memory_extractor.py tests/test_memory_extractor.py
git commit -m "feat: classify memory candidates"
```

---

### Task 2: Query Engine Memory Routing

**Files:**
- Modify: `src/morty_code/runtime/query_engine.py`
- Test: `tests/test_query_engine_memory.py`

- [ ] **Step 1: Write failing routing test**

Create `tests/test_query_engine_memory.py` with:

```python
from __future__ import annotations

from pathlib import Path

from morty_code.memory.memory_extractor import MemoryCandidate
from morty_code.runtime.query_engine import QueryEngine
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ToolUseContext


class StubExtractor:
    def extract(self, messages: list[Message]) -> list[MemoryCandidate]:
        return [
            MemoryCandidate("Current task discovery: keep this in session.", "session", "task", 0.8, "test"),
            MemoryCandidate("Remember: keep this durable.", "durable", "preference", 0.9, "test"),
        ]


def test_write_memories_routes_candidates_to_one_store(tmp_path: Path) -> None:
    session_path = tmp_path / "session_memory.md"
    durable_dir = tmp_path / "memory"
    engine = QueryEngine(memory_extractor=StubExtractor())  # type: ignore[arg-type]
    context = ToolUseContext(
        cwd=str(tmp_path),
        tools=[],
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
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run pytest tests/test_query_engine_memory.py -q
```

Expected: FAIL because `_write_memories()` still treats extracted values as strings and writes all values to both stores.

- [ ] **Step 3: Implement routing**

Update `src/morty_code/runtime/query_engine.py` imports and `_write_memories()`:

```python
from morty_code.memory.memory_extractor import MemoryCandidate, MemoryExtractor
```

```python
    def _write_memories(self, tool_context: ToolUseContext, new_messages: list[Message]) -> None:
        candidates = self.memory_extractor.extract(new_messages)
        if not candidates:
            return
        if tool_context.session_memory_path:
            session_store = SessionMemoryStore(tool_context.session_memory_path)
            for candidate in candidates:
                if candidate.target == "session":
                    session_store.append_note(candidate.text)
        if tool_context.durable_memory_dir:
            durable_store = DurableMemoryStore(tool_context.durable_memory_dir)
            for candidate in candidates:
                if candidate.target == "durable":
                    durable_store.append_summary(candidate.text)
```

If `MemoryCandidate` is unused by the module after typing settles, remove that import and keep only `MemoryExtractor`.

- [ ] **Step 4: Run focused routing test**

Run:

```bash
uv run pytest tests/test_query_engine_memory.py -q
```

Expected: PASS.

- [ ] **Step 5: Run memory tests together**

Run:

```bash
uv run pytest tests/test_memory_extractor.py tests/test_query_engine_memory.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit routing change**

Run:

```bash
git add src/morty_code/runtime/query_engine.py tests/test_query_engine_memory.py
git commit -m "feat: route memory candidates by target"
```

---

### Task 3: Regression Sweep

**Files:**
- Verify all changed files and relevant tests.

- [ ] **Step 1: Run existing test suite**

Run:

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 2: Inspect final diff**

Run:

```bash
git status --short
git diff --stat HEAD~2..HEAD
```

Expected: only memory extractor, query engine, tests, and docs plan/spec changes are present.

- [ ] **Step 3: Commit plan if it is still uncommitted**

Run:

```bash
git add docs/superpowers/plans/2026-05-15-memory-management.md
git commit -m "docs: plan memory candidate routing"
```

Expected: plan is committed, unless it was already included in an earlier commit.
