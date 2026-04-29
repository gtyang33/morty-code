from __future__ import annotations

from pathlib import Path

from morty_code.types.messages import Attachment


class RelevantMemoryFinder:
    """第一阶段使用轻量规则而不是复杂检索。"""

    def __init__(
        self,
        root_dir: str | Path,
        max_files: int = 5,
        max_file_chars: int = 6000,
        max_total_chars: int = 18000,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.max_files = max_files
        self.max_file_chars = max_file_chars
        self.max_total_chars = max_total_chars

    def find(self, input_text: str) -> list[Attachment]:
        if not self.root_dir.exists():
            return []
        matches: list[Attachment] = []
        lowered = input_text.lower()
        used_chars = 0
        for path in sorted(self.root_dir.glob("*.md")):
            if path.name == "MEMORY.md":
                # MEMORY.md 是轻量索引，不作为 topic 正文重复注入。
                continue
            name = path.stem.lower()
            content = path.read_text(encoding="utf-8", errors="replace")
            haystack = f"{name}\n{content}".lower()
            if not self._looks_relevant(lowered, haystack):
                continue
            remaining = self.max_total_chars - used_chars
            if remaining <= 0 or len(matches) >= self.max_files:
                break
            visible = content[: min(self.max_file_chars, remaining)]
            used_chars += len(visible)
            matches.append(
                Attachment(
                    type="relevant_memories",
                    payload={
                        "path": str(path),
                        "content": visible,
                        "truncated": len(content) > len(visible),
                    },
                    is_meta=True,
                )
            )
        return matches

    def _looks_relevant(self, lowered_input: str, lowered_memory: str) -> bool:
        tokens = {
            token
            for token in lowered_input.replace("/", " ").replace("_", " ").split()
            if len(token) >= 3
        }
        if not tokens:
            return False
        return any(token in lowered_memory for token in tokens)
